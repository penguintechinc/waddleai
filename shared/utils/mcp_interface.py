"""
MCP (Model Context Protocol) Interface for WaddleAI
Provides standard MCP server implementation with authentication
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Union

import websockets
from websockets.server import WebSocketServerProtocol

from shared.auth.penguin_auth import verify_token as _aaa_verify_token
from shared.auth.rbac import RBACManager, UserContext
from shared.utils.request_router import LLMRequestRouter

logger = logging.getLogger(__name__)


@dataclass
class MCPMessage:
    """MCP protocol message structure"""

    jsonrpc: str = "2.0"
    id: Optional[Union[str, int]] = None
    method: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None


@dataclass
class MCPResource:
    """MCP resource definition"""

    uri: str
    name: str
    description: str
    mimeType: str


@dataclass
class MCPTool:
    """MCP tool definition"""

    name: str
    description: str
    inputSchema: Dict[str, Any]


class MCPServer:
    """MCP server implementation with WaddleAI integration"""

    def __init__(self, rbac_manager: RBACManager, request_router: LLMRequestRouter, db, oidc_provider=None):
        self.rbac = rbac_manager
        self.router = request_router
        self.db = db
        self.oidc_provider = oidc_provider
        self.clients: Dict[WebSocketServerProtocol, UserContext] = {}

        # Define available tools
        self.tools = {
            "chat_completion": MCPTool(
                name="chat_completion",
                description="Generate AI chat completion using WaddleAI routing",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "messages": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "role": {"type": "string", "enum": ["system", "user", "assistant"]},
                                    "content": {"type": "string"},
                                },
                                "required": ["role", "content"],
                            },
                        },
                        "model": {"type": "string"},
                        "max_tokens": {"type": "integer", "minimum": 1},
                        "temperature": {"type": "number", "minimum": 0, "maximum": 2},
                    },
                    "required": ["messages", "model"],
                },
            ),
            "list_models": MCPTool(
                name="list_models",
                description="List available AI models",
                inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
            "get_usage": MCPTool(
                name="get_usage",
                description="Get token usage statistics",
                inputSchema={
                    "type": "object",
                    "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30}},
                    "additionalProperties": False,
                },
            ),
            "get_routing_stats": MCPTool(
                name="get_routing_stats",
                description="Get LLM routing statistics",
                inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
        }

        # Define available resources
        self.resources = {
            "usage_analytics": MCPResource(
                uri="waddleai://usage/analytics",
                name="Usage Analytics",
                description="Token usage analytics and statistics",
                mimeType="application/json",
            ),
            "system_health": MCPResource(
                uri="waddleai://system/health",
                name="System Health",
                description="System health status and metrics",
                mimeType="application/json",
            ),
        }

    async def handle_client(self, websocket: WebSocketServerProtocol, path: str):
        """Handle MCP client connection"""
        logger.info(f"MCP client connected: {websocket.remote_address}")

        try:
            # Wait for authentication
            auth_message = await websocket.recv()

            try:
                auth_data = json.loads(auth_message)
                user_context = await self._authenticate_client(auth_data)

                if not user_context:
                    await websocket.send(
                        json.dumps({"jsonrpc": "2.0", "error": {"code": -32600, "message": "Authentication failed"}})
                    )
                    await websocket.close()
                    return

                # Store authenticated client
                self.clients[websocket] = user_context

                # Send authentication success
                await websocket.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "result": {
                                "authenticated": True,
                                "user": {
                                    "id": user_context.user_id,
                                    "username": user_context.username,
                                    "role": user_context.role.value,
                                },
                            },
                        }
                    )
                )

                # Handle MCP messages
                async for message in websocket:
                    await self._handle_message(websocket, message, user_context)

            except json.JSONDecodeError:
                await websocket.send(
                    json.dumps({"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}})
                )
                await websocket.close()
                return

        except Exception as e:
            logger.error(f"MCP client error: {e}")
        finally:
            # Clean up client
            if websocket in self.clients:
                del self.clients[websocket]
            logger.info(f"MCP client disconnected: {websocket.remote_address}")

    async def _authenticate_client(self, auth_data: Dict[str, Any]) -> Optional[UserContext]:
        """Authenticate MCP client"""
        try:
            if "api_key" in auth_data:
                # API key authentication
                api_key = auth_data["api_key"]
                return self.rbac.verify_api_key(api_key)
            elif "jwt_token" in auth_data:
                # JWT token authentication
                jwt_token = auth_data["jwt_token"]
                if self.oidc_provider is not None:
                    return _aaa_verify_token(jwt_token, self.oidc_provider)
                return None
            else:
                return None

        except Exception as e:
            logger.error(f"MCP authentication error: {e}")
            return None

    async def _handle_message(self, websocket: WebSocketServerProtocol, message: str, user_context: UserContext):
        """Handle incoming MCP message"""
        try:
            data = json.loads(message)
            mcp_message = MCPMessage(**data)

            if mcp_message.method:
                # Handle method calls
                response = await self._handle_method(mcp_message, user_context)
            else:
                response = MCPMessage(id=mcp_message.id, error={"code": -32601, "message": "Method not found"})

            # Send response
            response_data = asdict(response)
            # Remove None values
            response_data = {k: v for k, v in response_data.items() if v is not None}
            await websocket.send(json.dumps(response_data))

        except json.JSONDecodeError:
            await websocket.send(
                json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            )
        except Exception as e:
            logger.error(f"Message handling error: {e}")
            await websocket.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": data.get("id") if "data" in locals() else None,
                        "error": {"code": -32603, "message": "Internal error"},
                    }
                )
            )

    async def _handle_method(self, message: MCPMessage, user_context: UserContext) -> MCPMessage:
        """Handle MCP method calls"""
        method = message.method

        try:
            if method == "initialize":
                return await self._handle_initialize(message, user_context)
            elif method == "tools/list":
                return await self._handle_tools_list(message, user_context)
            elif method == "tools/call":
                return await self._handle_tools_call(message, user_context)
            elif method == "resources/list":
                return await self._handle_resources_list(message, user_context)
            elif method == "resources/read":
                return await self._handle_resources_read(message, user_context)
            else:
                return MCPMessage(id=message.id, error={"code": -32601, "message": f"Method '{method}' not found"})

        except Exception as e:
            logger.error(f"Method '{method}' error: {e}")
            return MCPMessage(id=message.id, error={"code": -32603, "message": f"Method error: {str(e)}"})

    async def _handle_initialize(self, message: MCPMessage, user_context: UserContext) -> MCPMessage:
        """Handle MCP initialize request"""
        return MCPMessage(
            id=message.id,
            result={
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": True},
                    "resources": {"subscribe": False, "listChanged": True},
                },
                "serverInfo": {
                    "name": "WaddleAI MCP Server",
                    "version": "1.0.0",
                    "description": "WaddleAI Model Context Protocol Server",
                },
            },
        )

    async def _handle_tools_list(self, message: MCPMessage, user_context: UserContext) -> MCPMessage:
        """List available tools"""
        tools_list = []

        for tool_name, tool in self.tools.items():
            # Basic access control - all authenticated users can see tools
            tools_list.append({"name": tool.name, "description": tool.description, "inputSchema": tool.inputSchema})

        return MCPMessage(id=message.id, result={"tools": tools_list})

    async def _handle_tools_call(self, message: MCPMessage, user_context: UserContext) -> MCPMessage:
        """Execute tool calls"""
        params = message.params or {}
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if tool_name not in self.tools:
            return MCPMessage(id=message.id, error={"code": -32602, "message": f"Tool '{tool_name}' not found"})

        try:
            if tool_name == "chat_completion":
                result = await self._tool_chat_completion(arguments, user_context)
            elif tool_name == "list_models":
                result = await self._tool_list_models(arguments, user_context)
            elif tool_name == "get_usage":
                result = await self._tool_get_usage(arguments, user_context)
            elif tool_name == "get_routing_stats":
                result = await self._tool_get_routing_stats(arguments, user_context)
            else:
                raise ValueError(f"Tool '{tool_name}' not implemented")

            return MCPMessage(id=message.id, result={"content": [{"type": "text", "text": json.dumps(result)}]})

        except Exception as e:
            return MCPMessage(id=message.id, error={"code": -32603, "message": f"Tool execution error: {str(e)}"})

    async def _handle_resources_list(self, message: MCPMessage, user_context: UserContext) -> MCPMessage:
        """List available resources"""
        resources_list = []

        for resource_uri, resource in self.resources.items():
            # Access control based on user role
            if resource_uri == "system_health" and user_context.role.value != "admin":
                continue

            resources_list.append(
                {
                    "uri": resource.uri,
                    "name": resource.name,
                    "description": resource.description,
                    "mimeType": resource.mimeType,
                }
            )

        return MCPMessage(id=message.id, result={"resources": resources_list})

    async def _handle_resources_read(self, message: MCPMessage, user_context: UserContext) -> MCPMessage:
        """Read resource content"""
        params = message.params or {}
        uri = params.get("uri")

        if not uri:
            return MCPMessage(id=message.id, error={"code": -32602, "message": "URI parameter required"})

        try:
            if uri == "waddleai://usage/analytics":
                content = await self._get_usage_analytics(user_context)
            elif uri == "waddleai://system/health":
                if user_context.role.value != "admin":
                    return MCPMessage(id=message.id, error={"code": -32603, "message": "Admin access required"})
                content = await self._get_system_health(user_context)
            else:
                return MCPMessage(id=message.id, error={"code": -32602, "message": f"Resource '{uri}' not found"})

            return MCPMessage(
                id=message.id,
                result={
                    "contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(content, indent=2)}]
                },
            )

        except Exception as e:
            return MCPMessage(id=message.id, error={"code": -32603, "message": f"Resource read error: {str(e)}"})

    # Tool implementations
    async def _tool_chat_completion(self, arguments: Dict[str, Any], user_context: UserContext) -> Dict[str, Any]:
        """Chat completion tool"""
        messages = arguments.get("messages", [])
        model = arguments.get("model", "gpt-3.5-turbo")
        max_tokens = arguments.get("max_tokens")
        temperature = arguments.get("temperature")

        kwargs = {}
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature

        # Route request through WaddleAI system
        response_text, usage_info = await self.router.route_request(model=model, messages=messages, **kwargs)

        return {"response": response_text, "usage": usage_info, "model": model, "provider": usage_info.get("provider")}

    async def _tool_list_models(self, arguments: Dict[str, Any], user_context: UserContext) -> Dict[str, Any]:
        """List models tool"""
        models = await self.router.llm_manager.list_all_models()
        return {"models": models}

    async def _tool_get_usage(self, arguments: Dict[str, Any], user_context: UserContext) -> Dict[str, Any]:
        """Get usage statistics tool"""
        from datetime import timedelta

        days = arguments.get("days", 30)

        # Query usage based on user role
        query = self.db.token_usage.created_at > datetime.utcnow() - timedelta(days=days)

        if user_context.role.value == "admin":
            usage_records = self.db(query).select()
        elif user_context.role.value in ["resource_manager", "reporter"]:
            usage_records = self.db(
                query & (self.db.token_usage.organization_id == user_context.organization_id)
            ).select()
        else:
            usage_records = self.db(query & (self.db.token_usage.user_id == user_context.user_id)).select()

        total_tokens = sum(record.waddleai_tokens for record in usage_records)
        total_requests = len(usage_records)

        return {
            "period_days": days,
            "total_tokens": total_tokens,
            "total_requests": total_requests,
            "average_tokens_per_request": total_tokens / max(total_requests, 1),
        }

    async def _tool_get_routing_stats(self, arguments: Dict[str, Any], user_context: UserContext) -> Dict[str, Any]:
        """Get routing statistics tool"""
        stats = self.router.get_provider_stats()
        return {"routing_strategy": self.router.default_strategy.value, "provider_stats": stats}

    # Resource implementations
    async def _get_usage_analytics(self, user_context: UserContext) -> Dict[str, Any]:
        """Get usage analytics resource"""
        from datetime import timedelta

        # Get last 30 days of usage
        query = self.db.token_usage.created_at > datetime.utcnow() - timedelta(days=30)

        if user_context.role.value == "admin":
            usage_records = self.db(query).select()
        elif user_context.role.value in ["resource_manager", "reporter"]:
            usage_records = self.db(
                query & (self.db.token_usage.organization_id == user_context.organization_id)
            ).select()
        else:
            usage_records = self.db(query & (self.db.token_usage.user_id == user_context.user_id)).select()

        # Aggregate by day
        daily_usage = {}
        for record in usage_records:
            day = record.created_at.date().isoformat()
            if day not in daily_usage:
                daily_usage[day] = {"tokens": 0, "requests": 0}
            daily_usage[day]["tokens"] += record.waddleai_tokens
            daily_usage[day]["requests"] += 1

        # Aggregate by provider
        provider_usage = {}
        for record in usage_records:
            provider = record.provider
            if provider not in provider_usage:
                provider_usage[provider] = {"tokens": 0, "requests": 0}
            provider_usage[provider]["tokens"] += record.waddleai_tokens
            provider_usage[provider]["requests"] += 1

        return {
            "period": "30_days",
            "total_tokens": sum(record.waddleai_tokens for record in usage_records),
            "total_requests": len(usage_records),
            "daily_breakdown": daily_usage,
            "provider_breakdown": provider_usage,
        }

    async def _get_system_health(self, user_context: UserContext) -> Dict[str, Any]:
        """Get system health resource (Admin only)"""
        # This would integrate with the health monitoring system
        return {
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "services": {"database": "healthy", "redis": "healthy", "llm_providers": "healthy"},
            "metrics": {"active_connections": len(self.clients), "uptime_seconds": 3600},  # Placeholder
        }

    async def start_server(self, host: str = "localhost", port: int = 8765):
        """Start MCP WebSocket server"""
        logger.info(f"Starting MCP server on {host}:{port}")

        server = await websockets.serve(self.handle_client, host, port, ping_interval=30, ping_timeout=10)

        logger.info(f"MCP server running on ws://{host}:{port}")
        return server


def create_mcp_server(rbac_manager: RBACManager, request_router: LLMRequestRouter, db, oidc_provider=None) -> MCPServer:
    """Factory function to create MCP server"""
    return MCPServer(rbac_manager, request_router, db, oidc_provider)
