"""
WaddleAI Proxy Server
OpenAI-compatible API proxy with routing, security, and token management
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, Response
import uvicorn
import asyncio
import aiohttp
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List
import json
import time
from datetime import datetime
import logging
import structlog

from penguin_dal import get_dal
from shared.auth.rbac import RBACManager, AuthenticationError, AuthorizationError, Permission
from shared.security.prompt_security import create_security_scanner, Action
from shared.utils.token_manager import create_token_manager
from shared.utils.llm_connectors import create_llm_connection_manager
from shared.utils.request_router import create_request_router, RoutingStrategy
from shared.utils.memory_integration import create_memory_manager
from proxy.apps.proxy_server.mem0_api import mem0_router, set_memory_manager
from shared.utils.metrics import get_proxy_metrics, MetricsMiddleware
from shared.utils.health_checks import WaddleAIHealthMonitor
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

# Configure structured logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="ISO"),
        structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)

# Initialize metrics
proxy_metrics = get_proxy_metrics()


class ProxyServer:
    """WaddleAI Proxy Server"""
    
    def __init__(self):
        self.db = None
        self.rbac = None
        self.security_scanner = None
        self.token_manager = None
        self.llm_manager = None
        self.request_router = None
        self.memory_manager = None
        self.health_monitor = None
        self.http_session = None
        self.metrics = proxy_metrics
        
        # Configuration
        self.config = {
            'jwt_secret': os.getenv('JWT_SECRET', 'your-secret-key-change-in-production'),
            'management_server_url': os.getenv('MANAGEMENT_SERVER_URL', 'http://localhost:8001'),
            'security_policy': os.getenv('SECURITY_POLICY', 'balanced'),
            'max_concurrent_requests': int(os.getenv('MAX_CONCURRENT_REQUESTS', '100')),
        }
    
    async def startup(self):
        """Initialize server components"""
        logger.info("Starting WaddleAI Proxy Server")
        
        # Initialize database (pgvector-enabled PostgreSQL primary)
        database_url = os.getenv('DATABASE_URL', 'postgresql://waddleai:password@localhost:5432/waddleai')
        self.db = get_dal(database_url)
        
        # Initialize components
        self.rbac = RBACManager(self.db, self.config['jwt_secret'])
        self.security_scanner = create_security_scanner(self.db, self.config['security_policy'])
        self.token_manager = create_token_manager(self.db)
        self.llm_manager = create_llm_connection_manager(self.db)
        self.request_router = create_request_router(self.llm_manager, self.db)
        from shared.utils.memory_integration import ReadReplicaPool
        replica_pool = ReadReplicaPool.from_env("DATABASE_REPLICA_URL")
        self.memory_manager = create_memory_manager(
            backend="pgvector",
            write_db=self.db,
            replica_pool=replica_pool,
        )
        
        # Initialize HTTP session for external requests
        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=300),
            connector=aiohttp.TCPConnector(limit=100)
        )
        
        # Initialize health monitoring
        self.health_monitor = WaddleAIHealthMonitor('proxy')
        self.health_monitor.add_database_check('database', self.db)
        
        redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
        self.health_monitor.add_redis_check('redis', redis_url)
        
        self.health_monitor.add_system_resources_check()
        self.health_monitor.add_llm_providers_check('llm_providers', self.llm_manager)
        
        # Add management server check
        mgmt_url = f"{self.config['management_server_url']}/healthz"
        self.health_monitor.add_http_service_check('management_server', mgmt_url)
        
        # Initialize memory manager
        await self.memory_manager.initialize()
        
        # Wire memory manager into mem0-compatible API
        set_memory_manager(self.memory_manager)
        logger.info("Proxy server initialized successfully")
    
    async def shutdown(self):
        """Cleanup server components"""
        if self.http_session:
            await self.http_session.close()
        
        if self.llm_manager:
            await self.llm_manager.close_all()
        
        logger.info("Proxy server shutdown complete")


# Global server instance
proxy_server = ProxyServer()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager"""
    await proxy_server.startup()
    yield
    await proxy_server.shutdown()


# FastAPI app
app = FastAPI(
    title="WaddleAI Proxy Server",
    description="OpenAI-compatible API proxy with routing, security, and token management",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# mem0-compatible API for MarchProxy AILB memory injection
app.include_router(mem0_router)

# Request metrics middleware
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start_time = time.time()
    
    response = await call_next(request)
    
    # Record metrics
    duration = time.time() - start_time
    proxy_server.metrics.record_request(
        endpoint=request.url.path,
        method=request.method,
        status_code=response.status_code,
        duration=duration
    )
    
    return response


# Authentication dependency
async def get_current_user(
    request: Request,
    authorization: Optional[str] = Header(None)
):
    """Extract and validate user authentication"""
    try:
        if not authorization:
            raise HTTPException(status_code=401, detail="Authorization header required")

        if authorization.startswith("Bearer "):
            # JWT token
            token = authorization[7:]
            user_context = proxy_server.rbac.verify_jwt_token(token)
        elif authorization.startswith("sk-") or authorization.startswith("wa-"):
            # API key
            api_key = authorization
            user_context = proxy_server.rbac.authenticate_api_key(api_key)
        else:
            raise HTTPException(status_code=401, detail="Invalid authorization format")

        return user_context
    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error("Authentication failed", error=str(e))
        raise HTTPException(status_code=401, detail="Authentication failed")


def determine_target_model(
    request_model: Optional[str],
    user_context,
    x_preferred_model: Optional[str] = None
) -> str:
    """
    Determine target model using hierarchy:
    1. Request model parameter (if provided)
    2. X-Preferred-Model header (if provided)
    3. API key default_model
    4. User default_model
    5. Organization default_model
    6. Routing LLM decision (delegated to router)
    7. System default

    Args:
        request_model: Model from request body
        user_context: Authenticated user context
        x_preferred_model: X-Preferred-Model header value

    Returns:
        Target model name
    """
    # Priority 1: Request model parameter
    if request_model:
        logger.debug(f"Using request model: {request_model}")
        return request_model

    # Priority 2: X-Preferred-Model header
    if x_preferred_model:
        logger.debug(f"Using X-Preferred-Model header: {x_preferred_model}")
        return x_preferred_model

    # Priority 3: API key default_model
    if user_context.api_key_id:
        try:
            api_key = proxy_server.db(
                proxy_server.db.api_keys.id == user_context.api_key_id
            ).select().first()

            if api_key and api_key.default_model:
                logger.debug(f"Using API key default model: {api_key.default_model}")
                return api_key.default_model
        except Exception as e:
            logger.warning(f"Failed to get API key default model: {e}")

    # Priority 4: User default_model
    try:
        user = proxy_server.db(
            proxy_server.db.users.id == user_context.user_id
        ).select().first()

        if user and user.default_model:
            logger.debug(f"Using user default model: {user.default_model}")
            return user.default_model
    except Exception as e:
        logger.warning(f"Failed to get user default model: {e}")

    # Priority 5: Organization default_model
    try:
        org = proxy_server.db(
            proxy_server.db.organizations.id == user_context.organization_id
        ).select().first()

        if org and org.default_model:
            logger.debug(f"Using organization default model: {org.default_model}")
            return org.default_model
    except Exception as e:
        logger.warning(f"Failed to get organization default model: {e}")

    # Priority 6: Routing LLM will decide (return None to let router decide)
    # Priority 7: System default (fallback in router)
    logger.debug("No model specified, will use routing LLM decision")
    return None


# Health check endpoints
@app.get("/healthz")
async def health_check():
    """Kubernetes-style health check"""
    return "healthy"


@app.get("/api/status")
async def detailed_status():
    """Detailed health status"""
    try:
        # Check database connectivity
        proxy_server.db(proxy_server.db.users.id > 0).count()
        
        # Check external dependencies
        dependencies = {
            "database": {"status": "healthy"},
            "management_server": {"status": "unknown"},  # TODO: Check management server
            "security_scanner": {"status": "healthy" if proxy_server.security_scanner.policy.enabled else "disabled"},
            "token_manager": {"status": "healthy"}
        }
        
        return {
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "version": "1.0.0",
            "dependencies": dependencies,
            "performance": {
                "requests_per_minute": 0,  # TODO: Calculate from metrics
                "avg_response_time": "0ms",  # TODO: Calculate from metrics
                "error_rate": "0%"  # TODO: Calculate from metrics
            }
        }
    except Exception as e:
        logger.error("Health check failed", error=str(e))
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "error": str(e)}
        )


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus metrics endpoint"""
    try:
        metrics_data = proxy_server.metrics.get_metrics()
        return Response(
            content=metrics_data,
            media_type=CONTENT_TYPE_LATEST
        )
    except Exception as e:
        logger.error(f"Metrics endpoint failed: {e}")
        raise HTTPException(status_code=500, detail="Metrics unavailable")


# OpenAI Compatible API Endpoints
@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    user_context = Depends(get_current_user),
    x_preferred_model: Optional[str] = Header(None, alias="X-Preferred-Model")
):
    """OpenAI-compatible chat completions endpoint"""
    start_time = time.time()
    proxy_server.metrics.set_active_connections('requests_in_flight', 1)  # This would need proper tracking

    try:
        # Parse request
        body = await request.json()
        messages = body.get("messages", [])
        request_model = body.get("model")

        # Determine target model using hierarchy
        model = determine_target_model(
            request_model=request_model,
            user_context=user_context,
            x_preferred_model=x_preferred_model
        ) or "gpt-3.5-turbo"  # Final fallback
        
        # Combine messages for security scanning
        prompt_text = "\n".join([msg.get("content", "") for msg in messages if msg.get("content")])
        
        # Security scanning
        threats, sanitized_prompt = proxy_server.security_scanner.scan_prompt(
            prompt_text,
            user_id=user_context.user_id,
            api_key_id=user_context.api_key_id,
            ip_address=request.client.host
        )
        
        # Handle security threats
        for threat in threats:
            proxy_server.metrics.record_security_event(
                event_type=threat.threat_type.value,
                severity=threat.severity.value,
                action=threat.suggested_action.value
            )
            
            if threat.suggested_action == Action.BLOCK:
                raise HTTPException(
                    status_code=400,
                    detail=f"Request blocked due to security threat: {threat.description}"
                )
        
        # Check quota
        quota_ok, quota_info = proxy_server.token_manager.check_quota(user_context.api_key_id)
        if not quota_ok:
            raise HTTPException(
                status_code=429,
                detail=f"Quota exceeded. Daily: {quota_info['daily']['used']}/{quota_info['daily']['limit']}, Monthly: {quota_info['monthly']['used']}/{quota_info['monthly']['limit']}"
            )
        
        # Get session ID for memory (could be from headers or body)
        session_id = body.get('session_id') or request.headers.get('X-Session-ID')
        
        # Get conversation context from memory
        conversation_context = await proxy_server.memory_manager.get_conversation_context(
            user_id=user_context.user_id,
            organization_id=user_context.organization_id,
            current_messages=messages,
            session_id=session_id
        )
        
        # Enhance messages with memory context if available
        enhanced_messages = await proxy_server.memory_manager.enhance_messages_with_context(
            messages=messages,
            context=conversation_context
        )
        
        # Route request to appropriate LLM provider
        try:
            response_text, routing_usage_info = await proxy_server.request_router.route_request(
                model=model,
                messages=enhanced_messages,
                **{k: v for k, v in body.items() if k not in ['messages', 'model', 'session_id']}
            )
        except Exception as e:
            logger.error(f"LLM routing failed: {e}")
            raise HTTPException(
                status_code=503,
                detail=f"LLM routing failed: {str(e)}"
            )
        
        # Extract provider and usage info from routing
        actual_provider = routing_usage_info.get('provider', 'unknown')
        input_tokens = routing_usage_info.get('input_tokens', 0)
        output_tokens = routing_usage_info.get('output_tokens', 0)
        
        # Process token usage with actual provider
        usage = proxy_server.token_manager.process_usage(
            input_text=prompt_text,
            output_text=response_text,
            provider=actual_provider,
            model=model,
            api_key_id=user_context.api_key_id or 0,
            user_id=user_context.user_id,
            organization_id=user_context.organization_id,
            actual_input_tokens=input_tokens,
            actual_output_tokens=output_tokens
        )
        
        # Update metrics
        proxy_server.metrics.record_llm_request(
            provider=actual_provider,
            model=model,
            status="success",
            token_usage={
                'input_tokens': input_tokens,
                'output_tokens': output_tokens,
                'waddleai_tokens': usage.waddleai_tokens,
                'organization': user_context.organization_id,
                'user': user_context.user_id
            }
        )
        
        # Store conversation in memory (asynchronously, don't block response)
        asyncio.create_task(proxy_server.memory_manager.add_conversation_turn(
            user_id=user_context.user_id,
            organization_id=user_context.organization_id,
            messages=messages,  # Original messages without enhancement
            response=response_text,
            session_id=session_id,
            metadata={
                'model': model,
                'provider': actual_provider,
                'waddleai_tokens': usage.waddleai_tokens,
                'llm_tokens_input': input_tokens,
                'llm_tokens_output': output_tokens
            }
        ))
        
        # Return OpenAI-compatible response
        response = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response_text
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": usage.llm_tokens_input,
                "completion_tokens": usage.llm_tokens_output,
                "total_tokens": usage.llm_tokens_input + usage.llm_tokens_output,
                "waddleai_tokens": usage.waddleai_tokens
            }
        }
        
        REQUEST_COUNT.labels(method="POST", endpoint="chat_completions", status="200").inc()
        return response
        
    except HTTPException:
        REQUEST_COUNT.labels(method="POST", endpoint="chat_completions", status="400").inc()
        raise
    except Exception as e:
        logger.error("Chat completion failed", error=str(e), user=user_context.username)
        REQUEST_COUNT.labels(method="POST", endpoint="chat_completions", status="500").inc()
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        duration = time.time() - start_time
        proxy_server.metrics.record_request(
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=200,  # This should be dynamic based on response
            duration=duration
        )


@app.get("/v1/models")
async def list_models(user_context = Depends(get_current_user)):
    """List available models"""
    try:
        # Get actual available models from connection links
        models = await proxy_server.llm_manager.list_all_models()
        
        # If no models available, return empty list
        if not models:
            models = []
        
        return {
            "object": "list",
            "data": models
        }
    except Exception as e:
        logger.error("Failed to list models", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to list models")


@app.get("/api/routing/stats")
async def get_routing_stats(user_context = Depends(get_current_user)):
    """Get LLM provider routing statistics"""
    try:
        stats = proxy_server.request_router.get_provider_stats()
        return {
            "routing_strategy": proxy_server.request_router.default_strategy.value,
            "provider_stats": stats
        }
    except Exception as e:
        logger.error(f"Failed to get routing stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to get routing stats")


@app.post("/api/routing/strategy")
async def set_routing_strategy(
    request: Request,
    user_context = Depends(get_current_user)
):
    """Set routing strategy (Admin only)"""
    try:
        # Check admin permission
        if not proxy_server.rbac.check_permission(user_context, Permission.ADMIN_MANAGE):
            raise HTTPException(status_code=403, detail="Admin permission required")
        
        body = await request.json()
        strategy_name = body.get("strategy")
        
        try:
            strategy = RoutingStrategy(strategy_name)
            proxy_server.request_router.set_routing_strategy(strategy)
            return {"status": "success", "strategy": strategy.value}
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid strategy: {strategy_name}")
        
    except Exception as e:
        logger.error(f"Failed to set routing strategy: {e}")
        raise HTTPException(status_code=500, detail="Failed to set routing strategy")


@app.get("/api/memory/stats")
async def get_memory_stats(user_context = Depends(get_current_user)):
    """Get memory statistics for current user"""
    try:
        stats = await proxy_server.memory_manager.get_memory_stats(
            user_id=user_context.user_id,
            organization_id=user_context.organization_id
        )
        return stats
    except Exception as e:
        logger.error(f"Failed to get memory stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to get memory stats")


@app.delete("/api/memory/cleanup")
async def cleanup_old_memories(
    days: int = 90,
    user_context = Depends(get_current_user)
):
    """Cleanup old memories (Admin only or own memories)"""
    try:
        # Admin can cleanup all memories, users can only cleanup their own
        if proxy_server.rbac.check_permission(user_context, Permission.ADMIN_MANAGE):
            cleaned = await proxy_server.memory_manager.cleanup_old_memories(days)
            return {"cleaned_memories": cleaned, "scope": "system"}
        else:
            # For now, users cannot clean their own memories
            # This could be implemented as a user-specific cleanup
            raise HTTPException(status_code=403, detail="Admin permission required")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to cleanup memories: {e}")
        raise HTTPException(status_code=500, detail="Failed to cleanup memories")


@app.get("/api/usage")
async def get_usage(user_context = Depends(get_current_user)):
    """Get current API key usage stats"""
    try:
        stats = proxy_server.token_manager.get_usage_stats(
            api_key_id=user_context.api_key_id,
            days=30
        )
        return stats
    except Exception as e:
        logger.error("Failed to get usage stats", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to get usage stats")


@app.get("/api/quota")
async def get_quota(user_context = Depends(get_current_user)):
    """Get remaining quota for API key"""
    try:
        quota_ok, quota_info = proxy_server.token_manager.check_quota(user_context.api_key_id)
        return {
            "quota_ok": quota_ok,
            **quota_info
        }
    except Exception as e:
        logger.error("Failed to get quota info", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to get quota info")


# Claude Messages API endpoint (Anthropic-compatible)
@app.post("/v1/messages")
async def claude_messages(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    anthropic_version: Optional[str] = Header(None, alias="anthropic-version")
):
    """Anthropic Claude Messages API compatible endpoint"""
    start_time = time.time()

    try:
        # Authenticate using x-api-key header or Authorization header
        authorization = x_api_key or request.headers.get("Authorization")
        if not authorization:
            raise HTTPException(status_code=401, detail="x-api-key or Authorization header required")

        # Remove 'Bearer ' prefix if present
        if authorization.startswith("Bearer "):
            authorization = authorization[7:]

        # Authenticate user
        try:
            user_context = proxy_server.rbac.authenticate_api_key(authorization)
        except AuthenticationError as e:
            raise HTTPException(status_code=401, detail=str(e))

        # Parse request body
        body = await request.json()
        model = body.get("model", "claude-3-sonnet-20240229")
        messages = body.get("messages", [])
        max_tokens = body.get("max_tokens", 1024)
        system = body.get("system", "")
        temperature = body.get("temperature", 1.0)
        stream = body.get("stream", False)

        # Convert Claude Messages format to OpenAI format
        openai_messages = []
        if system:
            openai_messages.append({"role": "system", "content": system})

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            # Handle content array format (Claude uses array for multimodal)
            if isinstance(content, list):
                # Concatenate text content from array
                text_content = " ".join([
                    item.get("text", "") for item in content
                    if item.get("type") == "text"
                ])
                content = text_content

            openai_messages.append({"role": role, "content": content})

        # Combine messages for security scanning
        prompt_text = "\n".join([msg.get("content", "") for msg in openai_messages if msg.get("content")])

        # Security scanning
        threats, sanitized_prompt = proxy_server.security_scanner.scan_prompt(
            prompt_text,
            user_id=user_context.user_id,
            api_key_id=user_context.api_key_id,
            ip_address=request.client.host
        )

        # Handle security threats
        for threat in threats:
            proxy_server.metrics.record_security_event(
                event_type=threat.threat_type.value,
                severity=threat.severity.value,
                action=threat.suggested_action.value
            )

            if threat.suggested_action == Action.BLOCK:
                raise HTTPException(
                    status_code=400,
                    detail=f"Request blocked due to security threat: {threat.description}"
                )

        # Check quota
        quota_ok, quota_info = proxy_server.token_manager.check_quota(user_context.api_key_id)
        if not quota_ok:
            raise HTTPException(
                status_code=429,
                detail=f"Quota exceeded. Daily: {quota_info['daily']['used']}/{quota_info['daily']['limit']}, Monthly: {quota_info['monthly']['used']}/{quota_info['monthly']['limit']}"
            )

        # Route request to appropriate LLM provider
        try:
            response_text, routing_usage_info = await proxy_server.request_router.route_request(
                model=model,
                messages=openai_messages,
                max_tokens=max_tokens,
                temperature=temperature
            )
        except Exception as e:
            logger.error(f"LLM routing failed: {e}")
            raise HTTPException(
                status_code=503,
                detail=f"LLM routing failed: {str(e)}"
            )

        # Extract provider and usage info
        actual_provider = routing_usage_info.get('provider', 'unknown')
        input_tokens = routing_usage_info.get('input_tokens', 0)
        output_tokens = routing_usage_info.get('output_tokens', 0)

        # Process token usage
        usage = proxy_server.token_manager.process_usage(
            input_text=prompt_text,
            output_text=response_text,
            provider=actual_provider,
            model=model,
            api_key_id=user_context.api_key_id or 0,
            user_id=user_context.user_id,
            organization_id=user_context.organization_id,
            actual_input_tokens=input_tokens,
            actual_output_tokens=output_tokens
        )

        # Update metrics
        proxy_server.metrics.record_llm_request(
            provider=actual_provider,
            model=model,
            status="success",
            token_usage={
                'input_tokens': input_tokens,
                'output_tokens': output_tokens,
                'waddleai_tokens': usage.waddleai_tokens,
                'organization': user_context.organization_id,
                'user': user_context.user_id
            }
        )

        # Store conversation in memory (asynchronously)
        asyncio.create_task(proxy_server.memory_manager.add_conversation_turn(
            user_id=user_context.user_id,
            organization_id=user_context.organization_id,
            messages=openai_messages,
            response=response_text,
            session_id=None,
            metadata={
                'model': model,
                'provider': actual_provider,
                'waddleai_tokens': usage.waddleai_tokens,
                'llm_tokens_input': input_tokens,
                'llm_tokens_output': output_tokens,
                'api_format': 'claude_messages'
            }
        ))

        # Return Claude Messages API compatible response
        response = {
            "id": f"msg_{int(time.time() * 1000)}",
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": response_text
                }
            ],
            "model": model,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.llm_tokens_input,
                "output_tokens": usage.llm_tokens_output
            }
        }

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Claude messages API failed", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


if __name__ == "__main__":
    # Development server
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=True,
        log_level="info"
    )