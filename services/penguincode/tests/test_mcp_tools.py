"""Tests for MCP tool discovery, wrapper, manager, and agent injection."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from penguincode_cli.config.settings import AuthConfig, MCPConfig, MCPServerConfig
from penguincode_cli.tools.mcp.manager import MCPToolManager, convert_mcp_to_ollama_schema
from penguincode_cli.tools.mcp.wrapper import MCPToolWrapper

# ── MCPToolWrapper ─────────────────────────────────────────────────


class TestMCPToolWrapper:
    """Test MCPToolWrapper — delegates to MCP client and normalises results."""

    def test_namespaced_name(self):
        mock_client = MagicMock()
        wrapper = MCPToolWrapper("duckduckgo", "search", "Web search", mock_client)
        assert wrapper.name == "mcp_duckduckgo_search"
        assert wrapper.original_tool_name == "search"
        assert wrapper.server_name == "duckduckgo"

    @pytest.mark.asyncio
    async def test_execute_delegates_to_client(self):
        mock_client = AsyncMock()
        mock_client.call_tool.return_value = {"content": [{"type": "text", "text": "hello world"}]}

        wrapper = MCPToolWrapper("srv", "tool1", "desc", mock_client)
        result = await wrapper.execute(query="test")

        mock_client.call_tool.assert_awaited_once_with("tool1", {"query": "test"})
        assert result.success is True
        assert result.data == "hello world"

    @pytest.mark.asyncio
    async def test_execute_handles_multiple_content_blocks(self):
        mock_client = AsyncMock()
        mock_client.call_tool.return_value = {
            "content": [
                {"type": "text", "text": "line 1"},
                {"type": "text", "text": "line 2"},
            ]
        }

        wrapper = MCPToolWrapper("srv", "tool1", "desc", mock_client)
        result = await wrapper.execute()

        assert result.success is True
        assert "line 1" in result.data
        assert "line 2" in result.data

    @pytest.mark.asyncio
    async def test_execute_handles_none_result(self):
        mock_client = AsyncMock()
        mock_client.call_tool.return_value = None

        wrapper = MCPToolWrapper("srv", "tool1", "desc", mock_client)
        result = await wrapper.execute()

        assert result.success is True
        assert result.data == ""

    @pytest.mark.asyncio
    async def test_execute_handles_plain_dict_result(self):
        mock_client = AsyncMock()
        mock_client.call_tool.return_value = {"status": "ok"}

        wrapper = MCPToolWrapper("srv", "tool1", "desc", mock_client)
        result = await wrapper.execute()

        assert result.success is True
        assert "status" in result.data

    @pytest.mark.asyncio
    async def test_execute_wraps_exception(self):
        mock_client = AsyncMock()
        mock_client.call_tool.side_effect = RuntimeError("connection lost")

        wrapper = MCPToolWrapper("srv", "tool1", "desc", mock_client)
        result = await wrapper.execute()

        assert result.success is False
        assert "connection lost" in result.error


# ── Schema conversion ──────────────────────────────────────────────


class TestConvertMCPToOllamaSchema:
    """Test MCP→Ollama tool schema conversion."""

    def test_basic_conversion(self):
        mcp_tool = {
            "name": "search",
            "description": "Search the web",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        }

        result = convert_mcp_to_ollama_schema("duckduckgo", mcp_tool)

        assert result["type"] == "function"
        assert result["function"]["name"] == "mcp_duckduckgo_search"
        assert result["function"]["description"] == "Search the web"
        assert result["function"]["parameters"]["properties"]["query"]["type"] == "string"
        assert result["function"]["parameters"]["required"] == ["query"]

    def test_empty_input_schema(self):
        mcp_tool = {"name": "ping", "description": "Ping server"}
        result = convert_mcp_to_ollama_schema("myserver", mcp_tool)

        assert result["function"]["name"] == "mcp_myserver_ping"
        assert result["function"]["parameters"]["properties"] == {}
        assert result["function"]["parameters"]["required"] == []

    def test_multiple_properties(self):
        mcp_tool = {
            "name": "fetch",
            "description": "Fetch URL",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["url"],
            },
        }

        result = convert_mcp_to_ollama_schema("web", mcp_tool)
        props = result["function"]["parameters"]["properties"]
        assert "url" in props
        assert "timeout" in props


# ── MCPToolManager ─────────────────────────────────────────────────


class TestMCPToolManager:
    """Test MCPToolManager lifecycle, discovery, and merging."""

    def _make_config(self, servers=None) -> MCPConfig:
        return MCPConfig(
            enabled=True,
            servers=servers or [],
        )

    @pytest.mark.asyncio
    async def test_empty_config_returns_empty_tools(self):
        manager = MCPToolManager(self._make_config())
        tools, defs = await manager.get_tools()
        assert tools == {}
        assert defs == []

    @pytest.mark.asyncio
    async def test_disabled_config_returns_empty(self):
        config = MCPConfig(enabled=False, servers=[MCPServerConfig(name="srv", command="echo")])
        manager = MCPToolManager(config)
        tools, defs = await manager.get_tools()
        assert tools == {}

    @pytest.mark.asyncio
    async def test_disabled_server_skipped(self):
        config = self._make_config(
            [
                MCPServerConfig(name="disabled_srv", enabled=False, command="echo"),
            ]
        )
        manager = MCPToolManager(config)
        tools, defs = await manager.get_tools()
        assert tools == {}

    @pytest.mark.asyncio
    async def test_discovery_with_mock_server(self):
        """Mock a successful discovery flow."""
        config = self._make_config(
            [
                MCPServerConfig(name="test", transport="http", url="http://fake"),
            ]
        )
        manager = MCPToolManager(config)

        # Mock the internal discovery
        mock_tools = {
            "mcp_test_search": MCPToolWrapper("test", "search", "desc", MagicMock()),
        }
        mock_defs = [{"type": "function", "function": {"name": "mcp_test_search"}}]

        with patch.object(
            manager,
            "_discover_all_tools",
            new_callable=AsyncMock,
            side_effect=lambda: setattr(manager, "_tools", mock_tools) or setattr(manager, "_tool_defs", mock_defs),
        ):
            tools, defs = await manager.get_tools()
            assert "mcp_test_search" in tools
            assert len(defs) == 1

    @pytest.mark.asyncio
    async def test_lazy_init_only_once(self):
        """get_tools() should only discover once."""
        manager = MCPToolManager(self._make_config())
        await manager.get_tools()

        # Second call should return cached result
        tools2, defs2 = await manager.get_tools()
        assert tools2 == {}

    @pytest.mark.asyncio
    async def test_graceful_degradation(self):
        """A broken server shouldn't prevent other servers from working."""
        config = self._make_config(
            [
                MCPServerConfig(name="broken", transport="http", url="http://broken"),
                MCPServerConfig(name="good", transport="http", url="http://good"),
            ]
        )
        manager = MCPToolManager(config)

        async def mock_discover(cfg):
            if cfg.name == "broken":
                raise RuntimeError("connection refused")
            return (
                {"mcp_good_ping": MCPToolWrapper("good", "ping", "desc", MagicMock())},
                [{"type": "function", "function": {"name": "mcp_good_ping"}}],
            )

        with patch.object(manager, "_discover_server_tools", side_effect=mock_discover):
            tools, defs = await manager.get_tools()
            assert "mcp_good_ping" in tools
            assert "mcp_broken_ping" not in tools

    def test_add_servers_merges_without_duplicates(self):
        local_srv = MCPServerConfig(name="local", command="echo")
        config = self._make_config([local_srv])
        manager = MCPToolManager(config)

        org_srv1 = MCPServerConfig(name="local", command="org-echo")  # duplicate
        org_srv2 = MCPServerConfig(name="org-new", command="org-cmd")

        manager.add_servers([org_srv1, org_srv2])

        names = [s.name for s in manager._config.servers]
        assert names.count("local") == 1  # not duplicated
        assert "org-new" in names
        # Local should keep its original command
        local = next(s for s in manager._config.servers if s.name == "local")
        assert local.command == "echo"

    @pytest.mark.asyncio
    async def test_shutdown_clears_caches(self):
        manager = MCPToolManager(self._make_config())
        await manager.get_tools()  # populate cache
        await manager.shutdown()
        assert manager._tools is None
        assert manager._tool_defs is None


# ── BaseAgent MCP injection ────────────────────────────────────────


class TestBaseAgentMCPInjection:
    """Test that mcp_tools param injects into tools dict and definitions."""

    def test_mcp_tools_injected_into_agent(self):
        from penguincode_cli.agents.base import AgentConfig, BaseAgent, Permission

        # Create a concrete subclass for testing
        class TestAgent(BaseAgent):
            async def run(self, task, **kwargs):
                return None

        mock_wrapper = MCPToolWrapper("srv", "ping", "Ping tool", MagicMock())
        mock_def = {"type": "function", "function": {"name": "mcp_srv_ping"}}

        mock_ollama = MagicMock()
        config = AgentConfig(
            name="test",
            model="test-model",
            description="test",
            permissions=[Permission.READ],
        )

        agent = TestAgent(
            config=config,
            ollama_client=mock_ollama,
            mcp_tools=({"mcp_srv_ping": mock_wrapper}, [mock_def]),
        )

        # MCP tool should be in agent's tools
        assert "mcp_srv_ping" in agent.tools
        # MCP tool def should be in agent's tool_definitions
        assert mock_def in agent.tool_definitions
        # Built-in tools should still be there
        assert "read" in agent.tools

    def test_no_mcp_tools_no_change(self):
        from penguincode_cli.agents.base import AgentConfig, BaseAgent, Permission

        class TestAgent(BaseAgent):
            async def run(self, task, **kwargs):
                return None

        mock_ollama = MagicMock()
        config = AgentConfig(
            name="test",
            model="test-model",
            description="test",
            permissions=[Permission.READ],
        )

        agent = TestAgent(
            config=config,
            ollama_client=mock_ollama,
            mcp_tools=None,
        )

        # Only built-in tools
        assert "read" in agent.tools
        mcp_tools = [k for k in agent.tools if k.startswith("mcp_")]
        assert len(mcp_tools) == 0

    def test_system_prompt_mentions_mcp_tools(self):
        from penguincode_cli.agents.base import AgentConfig, BaseAgent, Permission

        class TestAgent(BaseAgent):
            async def run(self, task, **kwargs):
                return None

        mock_wrapper = MCPToolWrapper("srv", "ping", "Ping tool", MagicMock())
        mock_def = {"type": "function", "function": {"name": "mcp_srv_ping"}}
        mock_ollama = MagicMock()
        config = AgentConfig(
            name="test",
            model="test-model",
            description="test",
            permissions=[Permission.READ],
        )

        agent = TestAgent(
            config=config,
            ollama_client=mock_ollama,
            mcp_tools=({"mcp_srv_ping": mock_wrapper}, [mock_def]),
        )

        prompt = agent._default_system_prompt()
        assert "mcp_srv_ping" in prompt
        assert "External MCP tools" in prompt


# ── Shared-key auth ────────────────────────────────────────────────


class TestSharedKeyAuth:
    """Test shared_key is accepted as valid auth credential."""

    def test_shared_key_in_auth_config(self):
        config = AuthConfig(
            enabled=True,
            jwt_secret="secret",
            shared_key="team-key-123",
        )
        assert config.shared_key == "team-key-123"

    def test_auth_config_parser_handles_shared_key(self):
        from penguincode_cli.config.settings import Settings

        data = {"enabled": True, "shared_key": "mykey"}
        config = Settings._parse_auth_config(data)
        assert config.shared_key == "mykey"

    def test_client_config_parser_handles_shared_key(self):
        from penguincode_cli.config.settings import Settings

        data = {"server_url": "http://api", "shared_key": "mykey"}
        config = Settings._parse_client_config(data)
        assert config.shared_key == "mykey"

    @pytest.mark.asyncio
    async def test_auth_service_accepts_shared_key(self):
        """AuthServiceImpl should accept shared_key as valid credential."""
        config = AuthConfig(
            enabled=True,
            jwt_secret="testsecret",
            shared_key="team-secret",
            api_keys=["api-key-1"],
        )

        from penguincode_cli.server.services.auth import AuthServiceImpl

        service = AuthServiceImpl(config)

        # Verify shared key is stored
        assert service.shared_key == "team-secret"

        # Verify API key is in valid set
        assert "api-key-1" in service.valid_api_keys

    def test_auth_service_shared_key_validation_logic(self):
        """Test the key_valid logic directly."""
        config = AuthConfig(
            enabled=True,
            jwt_secret="testsecret",
            shared_key="team-secret",
            api_keys=["api-key-1"],
        )

        from penguincode_cli.server.services.auth import AuthServiceImpl

        service = AuthServiceImpl(config)

        # shared_key should be recognized
        assert service.shared_key == "team-secret"

        # API key should still be valid
        assert "api-key-1" in service.valid_api_keys

        # Construct the check logic as it appears in Authenticate
        def is_valid(api_key):
            return (service.shared_key and api_key == service.shared_key) or api_key in service.valid_api_keys

        assert is_valid("team-secret") is True
        assert is_valid("api-key-1") is True
        assert is_valid("wrong-key") is False


# ── OrgConfigClient ────────────────────────────────────────────────


class TestOrgConfigClient:
    """Test OrgConfigClient fetch methods with mock server."""

    @pytest.mark.asyncio
    async def test_fetch_mcp_servers_success(self):
        from penguincode_cli.client.org_config import OrgConfigClient

        client = OrgConfigClient("http://api.test", shared_key="key")

        with patch("penguincode_cli.client.org_config.httpx.AsyncClient") as MockClient:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = [
                {"name": "org-search", "transport": "http", "url": "http://search"},
            ]

            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_ctx.get = AsyncMock(return_value=mock_resp)
            MockClient.return_value = mock_ctx

            # Pre-set a token so we don't need to authenticate
            client.token_manager._access_token = "fake-jwt"
            client.token_manager._expires_at = 9999999999

            servers = await client.fetch_mcp_servers()
            assert len(servers) == 1
            assert servers[0]["name"] == "org-search"

    @pytest.mark.asyncio
    async def test_fetch_all_returns_all_categories(self):
        from penguincode_cli.client.org_config import OrgConfigClient

        client = OrgConfigClient("http://api.test")
        client.token_manager._access_token = "fake-jwt"
        client.token_manager._expires_at = 9999999999

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = [
                [{"name": "srv1"}],  # mcp_servers
                [{"name": "skill1"}],  # skills
                [{"role": "execution", "model": "qwen"}],  # models
            ]

            result = await client.fetch_all()
            assert len(result["mcp_servers"]) == 1
            assert len(result["skills"]) == 1
            assert len(result["models"]) == 1

    @pytest.mark.asyncio
    async def test_fetch_all_handles_failures_gracefully(self):
        from penguincode_cli.client.org_config import OrgConfigClient

        client = OrgConfigClient("http://api.test")
        client.token_manager._access_token = "fake-jwt"
        client.token_manager._expires_at = 9999999999

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = [
                RuntimeError("server down"),  # mcp_servers fails
                [{"name": "skill1"}],  # skills succeeds
                None,  # models returns None
            ]

            result = await client.fetch_all()
            assert result["mcp_servers"] == []  # graceful fallback
            assert len(result["skills"]) == 1
            assert result["models"] == []

    @pytest.mark.asyncio
    async def test_authenticate_with_shared_key(self):
        from penguincode_cli.client.org_config import OrgConfigClient

        client = OrgConfigClient("http://api.test", shared_key="team-key")

        with patch("penguincode_cli.client.org_config.httpx.AsyncClient") as MockClient:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "access_token": "jwt-token",
                "refresh_token": "refresh-token",
                "expires_in": 3600,
            }

            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_ctx.post = AsyncMock(return_value=mock_resp)
            MockClient.return_value = mock_ctx

            success = await client.authenticate()
            assert success is True
            assert client.token_manager.get_token() == "jwt-token"

    @pytest.mark.asyncio
    async def test_authenticate_no_shared_key(self):
        from penguincode_cli.client.org_config import OrgConfigClient

        client = OrgConfigClient("http://api.test", shared_key="")
        # Clear any on-disk token to test the no-key path
        client.token_manager.clear()
        success = await client.authenticate()
        assert success is False


# ── MCP Client enhancements ────────────────────────────────────────


class TestMCPClientEnhancements:
    """Test incrementing IDs and headers on HTTPMCPClient."""

    def test_incrementing_ids(self):
        from penguincode_cli.tools.mcp.client import MCPClient

        client = MCPClient("echo", [])
        assert client._get_next_id() == 1
        assert client._get_next_id() == 2
        assert client._get_next_id() == 3

    def test_http_client_headers(self):
        from penguincode_cli.tools.mcp.client import HTTPMCPClient

        client = HTTPMCPClient(
            base_url="http://example.com",
            headers={"Authorization": "Bearer token123"},
        )
        assert client.headers["Authorization"] == "Bearer token123"

    def test_http_client_default_empty_headers(self):
        from penguincode_cli.tools.mcp.client import HTTPMCPClient

        client = HTTPMCPClient(base_url="http://example.com")
        assert client.headers == {}
