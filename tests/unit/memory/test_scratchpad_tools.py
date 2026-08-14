"""scratchpad_put/get/list MCP tool tests: contract + caller-identity isolation."""

from dataclasses import dataclass

import pytest

from shared.memory.config import ALL_DISABLED, ProxyMemoryConfig
from shared.memory.scratchpad import ScratchpadStore
from shared.memory.scratchpad_tools import (
    SCRATCHPAD_TOOL_SCHEMAS,
    SCRATCHPAD_TOOLS,
    scratchpad_get,
    scratchpad_list,
    scratchpad_put,
)
from shared.security.content_filter import ContentFilter
from shared.security.prompt_security import PromptSecurityScanner
from shared.utils.mcp_interface import MCPServer, ToolCallContext
from tests.unit.memory.test_scratchpad import FakeScratchpadDB, FakeValkey


@dataclass(slots=True)
class FakeUserContext:
    """Minimal authenticated-caller identity for tool-handler tests."""

    user_id: int
    organization_id: int


ENABLED_CONFIG = ProxyMemoryConfig(
    scratchpad_enabled=True,
    scratchpad_substitution=False,
    summarization_enabled=False,
    threshold_tokens=8000,
    keep_recent=4,
    ratio=0.3,
    embedding_cache=True,
    schema_dedup=True,
)


@pytest.fixture
def store() -> ScratchpadStore:
    """ScratchpadStore backed by in-memory fakes and real security tiers."""
    return ScratchpadStore(
        FakeValkey(), FakeScratchpadDB(), PromptSecurityScanner(db=None), ContentFilter(db=None)
    )


class TestToolSchemas:
    """Each registered tool has a valid MCP tools/list descriptor."""

    def test_each_tool_has_a_valid_mcp_descriptor(self):
        """Each tool has a non-empty description and an object-typed inputSchema."""
        for name in ("scratchpad_put", "scratchpad_get", "scratchpad_list"):
            schema = SCRATCHPAD_TOOL_SCHEMAS[name]
            assert isinstance(schema["description"], str) and schema["description"]
            assert schema["inputSchema"]["type"] == "object"
            assert name in SCRATCHPAD_TOOLS


class TestScratchpadPutHandler:
    """scratchpad_put: identity scoping, validation, and feature-disabled errors."""

    @pytest.mark.asyncio
    async def test_put_scopes_to_caller_identity_ignoring_argument_overrides(self, store):
        """Put stores under the caller's identity, ignoring smuggled session_id/user_id args."""
        caller = FakeUserContext(user_id=10, organization_id=1)
        # Attacker-controlled arguments try to smuggle a different session/user.
        result = await scratchpad_put(
            store,
            ENABLED_CONFIG,
            caller,
            "real-session",
            {"key": "k1", "value": "v1", "session_id": "other-session", "user_id": 999},
        )
        assert result == {"ok": True, "key": "k1"}

        # Stored under the caller's real identity, not the smuggled one.
        assert await store.get(1, "real-session", 10, "k1") == "v1"
        assert await store.get(1, "other-session", 999, "k1") is None

    @pytest.mark.asyncio
    async def test_put_missing_arguments_returns_structured_error(self, store):
        """Missing key/value returns a structured invalid_arguments error."""
        caller = FakeUserContext(user_id=10, organization_id=1)
        result = await scratchpad_put(store, ENABLED_CONFIG, caller, "sess", {})
        assert result["error"]["type"] == "invalid_arguments"

    @pytest.mark.asyncio
    async def test_put_feature_disabled_returns_structured_error(self, store):
        """A disabled config returns a structured feature_disabled error."""
        caller = FakeUserContext(user_id=10, organization_id=1)
        result = await scratchpad_put(
            store, ALL_DISABLED, caller, "sess", {"key": "k1", "value": "v1"}
        )
        assert result["error"]["type"] == "feature_disabled"


class TestScratchpadGetHandler:
    """scratchpad_get: not-found errors and caller-identity scoping."""

    @pytest.mark.asyncio
    async def test_get_unknown_key_returns_structured_not_found(self, store):
        """An unknown key returns a structured not_found error, not an exception."""
        caller = FakeUserContext(user_id=10, organization_id=1)
        result = await scratchpad_get(store, ENABLED_CONFIG, caller, "sess", {"key": "nope"})
        assert result["error"]["type"] == "not_found"

    @pytest.mark.asyncio
    async def test_get_scopes_to_caller_identity(self, store):
        """A different caller cannot get another caller's key; the owner still can."""
        caller_a = FakeUserContext(user_id=10, organization_id=1)
        caller_b = FakeUserContext(user_id=20, organization_id=1)
        await scratchpad_put(
            store, ENABLED_CONFIG, caller_a, "sess", {"key": "k1", "value": "secret"}
        )

        result_b = await scratchpad_get(store, ENABLED_CONFIG, caller_b, "sess", {"key": "k1"})
        assert result_b["error"]["type"] == "not_found"

        result_a = await scratchpad_get(store, ENABLED_CONFIG, caller_a, "sess", {"key": "k1"})
        assert result_a == {"key": "k1", "value": "secret"}


class TestScratchpadListHandler:
    """scratchpad_list: key metadata only, never values."""

    @pytest.mark.asyncio
    async def test_list_returns_key_metadata(self, store):
        """List returns the key name but never its value."""
        caller = FakeUserContext(user_id=10, organization_id=1)
        await scratchpad_put(store, ENABLED_CONFIG, caller, "sess", {"key": "k1", "value": "v1"})
        result = await scratchpad_list(store, ENABLED_CONFIG, caller, "sess", {})
        assert result["keys"][0]["key"] == "k1"
        assert "value" not in result["keys"][0]


class TestMCPServerRegistration:
    """MCPServer: tool registration, dispatch, and feature-disabled/unknown-tool errors."""

    @pytest.mark.asyncio
    async def test_tools_registered_and_dispatchable_via_tools_call(self, store):
        """All three scratchpad tools are registered, listed, and dispatchable via call_tool."""

        async def resolver(_user_context):
            return ENABLED_CONFIG

        server = MCPServer(scratchpad_store=store, proxy_memory_config_resolver=resolver)
        assert {"scratchpad_put", "scratchpad_get", "scratchpad_list"} <= set(server.tools)

        names = {t["name"] for t in server.list_tools()}
        assert {"scratchpad_put", "scratchpad_get", "scratchpad_list"} <= names

        caller = FakeUserContext(user_id=10, organization_id=1)
        ctx = ToolCallContext(user_context=caller, session_id="sess")
        result = await server.call_tool("scratchpad_put", {"key": "k1", "value": "v1"}, ctx)
        assert result == {"ok": True, "key": "k1"}

    @pytest.mark.asyncio
    async def test_flag_off_config_yields_structured_feature_disabled_error(self, store):
        """A resolver returning ALL_DISABLED yields a structured feature_disabled error."""

        async def resolver(_user_context):
            return ALL_DISABLED

        server = MCPServer(scratchpad_store=store, proxy_memory_config_resolver=resolver)
        caller = FakeUserContext(user_id=10, organization_id=1)
        ctx = ToolCallContext(user_context=caller, session_id="sess")
        result = await server.call_tool("scratchpad_put", {"key": "k1", "value": "v1"}, ctx)
        assert result["error"]["type"] == "feature_disabled"

    def test_no_store_injected_skips_registration(self):
        """With no scratchpad_store injected, no tools are registered."""
        server = MCPServer()
        assert server.tools == {}

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_structured_not_found(self):
        """Calling an unregistered tool name returns a structured not_found error."""
        server = MCPServer()
        caller = FakeUserContext(user_id=10, organization_id=1)
        ctx = ToolCallContext(user_context=caller, session_id="sess")
        result = await server.call_tool("does_not_exist", {}, ctx)
        assert result["error"]["type"] == "not_found"
