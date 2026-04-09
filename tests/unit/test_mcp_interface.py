"""
Unit tests for MCP (Model Context Protocol) interface
"""

from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest

from shared.auth.rbac import Role, UserContext
from shared.utils.mcp_interface import MCPMessage, MCPResource, MCPServer, MCPTool, create_mcp_server


class TestMCPMessage:
    """Test MCPMessage dataclass"""

    def test_mcp_message_creation(self):
        """Test MCPMessage creation"""
        message = MCPMessage(jsonrpc="2.0", id=1, method="test_method", params={"arg1": "value1"})

        assert message.jsonrpc == "2.0"
        assert message.id == 1
        assert message.method == "test_method"
        assert message.params == {"arg1": "value1"}
        assert message.result is None
        assert message.error is None

    def test_mcp_message_defaults(self):
        """Test MCPMessage default values"""
        message = MCPMessage()

        assert message.jsonrpc == "2.0"
        assert message.id is None
        assert message.method is None
        assert message.params is None
        assert message.result is None
        assert message.error is None


class TestMCPResource:
    """Test MCPResource dataclass"""

    def test_mcp_resource_creation(self):
        """Test MCPResource creation"""
        resource = MCPResource(
            uri="test://resource", name="Test Resource", description="A test resource", mimeType="application/json"
        )

        assert resource.uri == "test://resource"
        assert resource.name == "Test Resource"
        assert resource.description == "A test resource"
        assert resource.mimeType == "application/json"


class TestMCPTool:
    """Test MCPTool dataclass"""

    def test_mcp_tool_creation(self):
        """Test MCPTool creation"""
        schema = {"type": "object", "properties": {"arg1": {"type": "string"}}}

        tool = MCPTool(name="test_tool", description="A test tool", inputSchema=schema)

        assert tool.name == "test_tool"
        assert tool.description == "A test tool"
        assert tool.inputSchema == schema


class TestMCPServer:
    """Test MCPServer class"""

    def test_mcp_server_init(self, mock_db):
        """Test MCP server initialization"""
        rbac_manager = Mock()
        request_router = Mock()

        server = MCPServer(rbac_manager, request_router, mock_db)

        assert server.rbac == rbac_manager
        assert server.router == request_router
        assert server.db == mock_db
        assert len(server.tools) > 0
        assert len(server.resources) > 0
        assert isinstance(server.clients, dict)

    def test_tools_registration(self, mock_db):
        """Test that all expected tools are registered"""
        rbac_manager = Mock()
        request_router = Mock()

        server = MCPServer(rbac_manager, request_router, mock_db)

        expected_tools = [
            "chat_completion",
            "list_models",
            "get_usage",
            "get_routing_stats",
        ]

        for tool_name in expected_tools:
            assert tool_name in server.tools
            assert isinstance(server.tools[tool_name], MCPTool)

    def test_resources_registration(self, mock_db):
        """Test that all expected resources are registered"""
        rbac_manager = Mock()
        request_router = Mock()

        server = MCPServer(rbac_manager, request_router, mock_db)

        expected_resources = ["usage_analytics", "system_health"]

        for resource_name in expected_resources:
            assert resource_name in server.resources
            assert isinstance(server.resources[resource_name], MCPResource)

    @pytest.mark.asyncio
    async def test_authenticate_client_api_key(self, mock_db):
        """Test client authentication with API key"""
        rbac_manager = Mock()
        rbac_manager.verify_api_key.return_value = UserContext(
            user_id=1, username="test", role=Role.USER, organization_id=1, managed_orgs=[], permissions=set()
        )
        request_router = Mock()

        server = MCPServer(rbac_manager, request_router, mock_db)

        auth_data = {"api_key": "test-api-key"}
        user_context = await server._authenticate_client(auth_data)

        assert user_context is not None
        assert user_context.user_id == 1
        assert user_context.username == "test"
        rbac_manager.verify_api_key.assert_called_once_with("test-api-key")

    @pytest.mark.asyncio
    async def test_authenticate_client_jwt(self, mock_db):
        """Test client authentication with JWT token"""
        from unittest.mock import patch as _patch

        from shared.auth.penguin_auth import create_oidc_provider

        rbac_manager = Mock()
        rbac_manager.verify_api_key.side_effect = Exception("Not an API key")
        expected_context = UserContext(
            user_id=2, username="admin", role=Role.ADMIN, organization_id=1, managed_orgs=[], permissions=set()
        )
        request_router = Mock()
        oidc_provider = create_oidc_provider()

        with _patch("shared.utils.mcp_interface._aaa_verify_token", return_value=expected_context) as mock_verify:
            server = MCPServer(rbac_manager, request_router, mock_db, oidc_provider)
            auth_data = {"jwt_token": "jwt.token.here"}
            user_context = await server._authenticate_client(auth_data)

        assert user_context is not None
        assert user_context.user_id == 2
        assert user_context.username == "admin"
        mock_verify.assert_called_once_with("jwt.token.here", oidc_provider)

    @pytest.mark.asyncio
    async def test_authenticate_client_invalid(self, mock_db):
        """Test client authentication with invalid credentials"""
        rbac_manager = Mock()
        rbac_manager.verify_api_key.side_effect = Exception("Invalid API key")
        rbac_manager.verify_api_key.side_effect = Exception("Invalid API key")
        request_router = Mock()

        server = MCPServer(rbac_manager, request_router, mock_db)

        auth_data = {"api_key": "invalid-key"}
        user_context = await server._authenticate_client(auth_data)

        assert user_context is None

    @pytest.mark.asyncio
    async def test_handle_initialize(self, mock_db, sample_user_context):
        """Test initialize method handling"""
        rbac_manager = Mock()
        request_router = Mock()

        server = MCPServer(rbac_manager, request_router, mock_db)

        message = MCPMessage(id=1, method="initialize")
        response = await server._handle_initialize(message, sample_user_context)

        assert response.id == 1
        assert response.result is not None
        assert "protocolVersion" in response.result
        assert "capabilities" in response.result
        assert "serverInfo" in response.result

    @pytest.mark.asyncio
    async def test_handle_tools_list(self, mock_db, sample_user_context):
        """Test tools list method handling"""
        rbac_manager = Mock()
        request_router = Mock()

        server = MCPServer(rbac_manager, request_router, mock_db)

        message = MCPMessage(id=1, method="tools/list")
        response = await server._handle_tools_list(message, sample_user_context)

        assert response.id == 1
        assert response.result is not None
        assert "tools" in response.result
        assert len(response.result["tools"]) > 0

    @pytest.mark.asyncio
    async def test_handle_resources_list(self, mock_db, sample_user_context):
        """Test resources list method handling"""
        rbac_manager = Mock()
        request_router = Mock()

        server = MCPServer(rbac_manager, request_router, mock_db)

        message = MCPMessage(id=1, method="resources/list")
        response = await server._handle_resources_list(message, sample_user_context)

        assert response.id == 1
        assert response.result is not None
        assert "resources" in response.result
        assert len(response.result["resources"]) > 0

    @pytest.mark.asyncio
    async def test_handle_resources_list_admin_only(self, mock_db, admin_user_context):
        """Test resources list with admin user (sees all resources)"""
        rbac_manager = Mock()
        request_router = Mock()

        server = MCPServer(rbac_manager, request_router, mock_db)

        message = MCPMessage(id=1, method="resources/list")
        response = await server._handle_resources_list(message, admin_user_context)

        # Admin should see all resources including system_health
        resource_uris = [r["uri"] for r in response.result["resources"]]
        assert "waddleai://system/health" in resource_uris

    @pytest.mark.asyncio
    async def test_handle_resources_list_non_admin(self, mock_db, sample_user_context):
        """Test resources list with non-admin user (filtered resources)"""
        rbac_manager = Mock()
        request_router = Mock()

        server = MCPServer(rbac_manager, request_router, mock_db)

        message = MCPMessage(id=1, method="resources/list")
        response = await server._handle_resources_list(message, sample_user_context)

        # Non-admin should not see system_health
        resource_uris = [r["uri"] for r in response.result["resources"]]
        assert "waddleai://system/health" not in resource_uris
        assert "waddleai://usage/analytics" in resource_uris

    @pytest.mark.asyncio
    async def test_tool_chat_completion(self, mock_db, sample_user_context):
        """Test chat completion tool"""
        rbac_manager = Mock()
        request_router = Mock()
        request_router.route_request = AsyncMock(
            return_value=("Test response", {"provider": "openai", "input_tokens": 10, "output_tokens": 5})
        )

        server = MCPServer(rbac_manager, request_router, mock_db)

        arguments = {"messages": [{"role": "user", "content": "Hello"}], "model": "gpt-3.5-turbo"}

        result = await server._tool_chat_completion(arguments, sample_user_context)

        assert "response" in result
        assert "usage" in result
        assert result["response"] == "Test response"
        assert result["model"] == "gpt-3.5-turbo"
        request_router.route_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_list_models(self, mock_db, sample_user_context):
        """Test list models tool"""
        rbac_manager = Mock()
        request_router = Mock()
        request_router.llm_manager = Mock()
        request_router.llm_manager.list_all_models = AsyncMock(
            return_value=[{"id": "gpt-3.5-turbo", "provider": "openai"}, {"id": "claude-3", "provider": "anthropic"}]
        )

        server = MCPServer(rbac_manager, request_router, mock_db)

        result = await server._tool_list_models({}, sample_user_context)

        assert "models" in result
        assert len(result["models"]) == 2
        request_router.llm_manager.list_all_models.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_get_usage(self, mock_db, sample_user_context):
        """Test get usage tool"""
        rbac_manager = Mock()
        request_router = Mock()

        server = MCPServer(rbac_manager, request_router, mock_db)

        # Mock database query
        from unittest.mock import PropertyMock

        mock_usage_records = [Mock(waddleai_tokens=100), Mock(waddleai_tokens=50)]
        mock_db.token_usage = Mock()
        type(mock_db.token_usage).created_at = PropertyMock(return_value=datetime.utcnow())
        mock_db.return_value = Mock()
        mock_db.return_value.select = Mock(return_value=mock_usage_records)

        arguments = {"days": 7}
        result = await server._tool_get_usage(arguments, sample_user_context)

        assert "period_days" in result
        assert "total_tokens" in result
        assert "total_requests" in result
        assert result["period_days"] == 7
        assert result["total_tokens"] == 150
        assert result["total_requests"] == 2

    @pytest.mark.asyncio
    async def test_tool_get_routing_stats(self, mock_db, sample_user_context):
        """Test get routing stats tool"""
        rbac_manager = Mock()
        request_router = Mock()
        request_router.get_provider_stats.return_value = {"openai": {"success_rate": 0.95, "avg_latency": 1200}}
        request_router.default_strategy = Mock()
        request_router.default_strategy.value = "load_balanced"

        server = MCPServer(rbac_manager, request_router, mock_db)

        result = await server._tool_get_routing_stats({}, sample_user_context)

        assert "routing_strategy" in result
        assert "provider_stats" in result
        assert result["routing_strategy"] == "load_balanced"
        request_router.get_provider_stats.assert_called_once()

    @pytest.mark.asyncio
    async def test_resource_usage_analytics(self, mock_db, sample_user_context):
        """Test usage analytics resource"""
        rbac_manager = Mock()
        request_router = Mock()

        server = MCPServer(rbac_manager, request_router, mock_db)

        # Mock database query
        mock_usage_records = [
            Mock(waddleai_tokens=100, provider="openai", created_at=datetime.utcnow()),
            Mock(waddleai_tokens=50, provider="anthropic", created_at=datetime.utcnow()),
        ]
        from unittest.mock import PropertyMock

        mock_db.token_usage = Mock()
        type(mock_db.token_usage).created_at = PropertyMock(return_value=datetime.utcnow())
        mock_db.return_value = Mock()
        mock_db.return_value.select = Mock(return_value=mock_usage_records)

        result = await server._get_usage_analytics(sample_user_context)

        assert "period" in result
        assert "total_tokens" in result
        assert "total_requests" in result
        assert "daily_breakdown" in result
        assert "provider_breakdown" in result

    @pytest.mark.asyncio
    async def test_resource_system_health_admin(self, mock_db, admin_user_context):
        """Test system health resource (admin only)"""
        rbac_manager = Mock()
        request_router = Mock()

        server = MCPServer(rbac_manager, request_router, mock_db)

        result = await server._get_system_health(admin_user_context)

        assert "status" in result
        assert "timestamp" in result
        assert "services" in result
        assert "metrics" in result
        assert result["status"] == "healthy"


class TestMCPServerFactory:
    """Test MCP server factory function"""

    def test_create_mcp_server(self, mock_db):
        """Test creating MCP server"""
        rbac_manager = Mock()
        request_router = Mock()

        server = create_mcp_server(rbac_manager, request_router, mock_db)

        assert isinstance(server, MCPServer)
        assert server.rbac == rbac_manager
        assert server.router == request_router
        assert server.db == mock_db
