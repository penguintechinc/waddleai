"""Unit tests for `shared/mcp/tools.py` -- WaddleAITools and AdminTools.

Collaborators are stubbed (AsyncMock against the Protocols); real §9/§7
services land with feature/knowledge-layer and feature/smart-routing.
"""

import time
from unittest.mock import AsyncMock

import pytest

from shared.mcp.tools import (
    ADMIN_READ_MAX_AGE_SECONDS,
    ADMIN_WRITE_MAX_AGE_SECONDS,
    AdminTools,
    ServiceUnavailableError,
    StaleSessionError,
    ToolContext,
    ToolDisabledError,
    WaddleAITools,
)


def _ctx(
    org_id: int = 1, user_uuid: str = "u-1", authenticated_at: float | None = None
) -> ToolContext:
    """Build a default ToolContext for tests."""
    return ToolContext(
        org_id=org_id,
        user_uuid=user_uuid,
        session_id="sess-1",
        workspace_hint=None,
        scopes=frozenset({"proxy:use"}),
        authenticated_at=authenticated_at if authenticated_at is not None else time.time(),
    )


@pytest.fixture
def collaborators():
    """AsyncMock stand-ins for the four tool collaborators."""
    return {
        "knowledge": AsyncMock(),
        "memory": AsyncMock(),
        "routing": AsyncMock(),
        "usage": AsyncMock(),
    }


@pytest.fixture
def user_tools(collaborators, monkeypatch):
    """A WaddleAITools instance with the flag on and mocked collaborators."""
    monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")
    return WaddleAITools(_ctx(), **collaborators)


@pytest.mark.asyncio
class TestWaddleAIToolsHappyPath:
    """Happy-path delegation for every WaddleAITools method."""

    async def test_search_code_scopes_to_caller_org_and_tags_provenance(
        self, user_tools, collaborators
    ):
        """Search code scopes to caller org and tags provenance."""
        collaborators["knowledge"].search_code.return_value = [{"path": "a.py", "text": "..."}]
        results = await user_tools.search_code("foo", repo="r", branch="main")
        collaborators["knowledge"].search_code.assert_awaited_once_with(
            org_id=1, query="foo", repo="r", branch="main"
        )
        assert results[0]["_provenance"]["source"] == "search_code"

    async def test_get_symbol_tags_provenance(self, user_tools, collaborators):
        """Get symbol tags provenance."""
        collaborators["knowledge"].get_symbol.return_value = {"symbol": "Foo"}
        result = await user_tools.get_symbol("Foo")
        assert result["_provenance"]["source"] == "get_symbol"

    async def test_get_symbol_none_passthrough(self, user_tools, collaborators):
        """Get symbol none passthrough."""
        collaborators["knowledge"].get_symbol.return_value = None
        assert await user_tools.get_symbol("Missing") is None

    async def test_search_docs(self, user_tools, collaborators):
        """Search docs."""
        collaborators["knowledge"].search_docs.return_value = [{"page": "x"}]
        results = await user_tools.search_docs("requests")
        collaborators["knowledge"].search_docs.assert_awaited_once_with(
            query="requests", ecosystem=None
        )
        assert results[0]["_provenance"]["source"] == "search_docs"

    async def test_fetch_docs_invokes_fetch_then_cache_path(self, user_tools, collaborators):
        """Fetch docs invokes fetch then cache path."""
        collaborators["knowledge"].fetch_docs.return_value = {"page": "y"}
        result = await user_tools.fetch_docs("python", "requests", version="2.31")
        collaborators["knowledge"].fetch_docs.assert_awaited_once_with(
            ecosystem="python", package="requests", version="2.31"
        )
        assert result["_provenance"]["source"] == "fetch_docs"

    async def test_get_call_graph_scopes_to_caller_org_and_defaults(
        self, user_tools, collaborators
    ):
        """get_call_graph is subject-free: org comes from ctx, direction/depth default."""
        collaborators["knowledge"].get_call_graph.return_value = [{"nodes": [], "edges": []}]
        result = await user_tools.get_call_graph("widgets", "handler")
        collaborators["knowledge"].get_call_graph.assert_awaited_once_with(
            org_id=1, repo="widgets", branch=None, symbol="handler", direction="out", depth=3
        )
        assert result == [{"nodes": [], "edges": []}]

    async def test_get_call_graph_forwards_explicit_branch_direction_depth(
        self, user_tools, collaborators
    ):
        """Explicit branch/direction/depth pass through unchanged."""
        collaborators["knowledge"].get_call_graph.return_value = []
        await user_tools.get_call_graph(
            "widgets", "handler", branch="dev", direction="both", depth=5
        )
        collaborators["knowledge"].get_call_graph.assert_awaited_once_with(
            org_id=1, repo="widgets", branch="dev", symbol="handler", direction="both", depth=5
        )

    async def test_get_class_hierarchy_scopes_to_caller_org_and_defaults(
        self, user_tools, collaborators
    ):
        """get_class_hierarchy is subject-free: org comes from ctx, direction defaults."""
        collaborators["knowledge"].get_class_hierarchy.return_value = [{"nodes": [], "edges": []}]
        result = await user_tools.get_class_hierarchy("widgets", "Base")
        collaborators["knowledge"].get_class_hierarchy.assert_awaited_once_with(
            org_id=1, repo="widgets", branch=None, symbol="Base", direction="out"
        )
        assert result == [{"nodes": [], "edges": []}]

    async def test_memory_add_defaults_to_session_scope(self, user_tools, collaborators):
        """Memory add defaults to session scope."""
        collaborators["memory"].write.return_value = "mem-1"
        result = await user_tools.memory_add("remember this")
        collaborators["memory"].write.assert_awaited_once_with(
            org_id=1, user_uuid="u-1", session_id="sess-1", content="remember this", scope="session"
        )
        assert result == "mem-1"

    async def test_memory_search_tags_provenance_and_trust_tier(self, user_tools, collaborators):
        """Memory search tags provenance and trust tier."""
        collaborators["memory"].search.return_value = [
            {"content": "prior note", "trust_tier": "user_write"}
        ]
        results = await user_tools.memory_search("prior")
        assert results[0]["_provenance"] == {"source": "memory", "trust_tier": "user_write"}

    async def test_list_models(self, user_tools, collaborators):
        """List models."""
        collaborators["routing"].list_models.return_value = [{"id": "anthropic:claude-opus"}]
        results = await user_tools.list_models()
        collaborators["routing"].list_models.assert_awaited_once_with(org_id=1)
        assert results == [{"id": "anthropic:claude-opus"}]

    async def test_get_routing_policy(self, user_tools, collaborators):
        """Get routing policy."""
        collaborators["routing"].get_routing_policy.return_value = {"policy": "balanced"}
        result = await user_tools.get_routing_policy()
        collaborators["routing"].get_routing_policy.assert_awaited_once_with(org_id=1)
        assert result == {"policy": "balanced"}

    async def test_usage_summary_is_self_only(self, user_tools, collaborators):
        """Usage summary is self only."""
        collaborators["usage"].usage_summary.return_value = {"tokens": 42}
        result = await user_tools.usage_summary(window="7d")
        collaborators["usage"].usage_summary.assert_awaited_once_with(
            org_id=1, user_uuid="u-1", window="7d"
        )
        assert result == {"tokens": 42}

    async def test_set_preference_clamps_weight_and_delegates(self, user_tools, collaborators):
        """Set preference clamps weight and delegates."""
        collaborators["routing"].set_preference.return_value = {"applied": True, "weight": 1.0}
        await user_tools.set_preference("anthropic:claude-opus", weight=5.0)
        collaborators["routing"].set_preference.assert_awaited_once_with(
            org_id=1, user_uuid="u-1", model_or_tag="anthropic:claude-opus", weight=1.0
        )

    async def test_set_preference_clamps_negative_weight(self, user_tools, collaborators):
        """Set preference clamps negative weight."""
        collaborators["routing"].set_preference.return_value = {"applied": True}
        await user_tools.set_preference("anthropic:claude-opus", weight=-3.0)
        collaborators["routing"].set_preference.assert_awaited_once_with(
            org_id=1, user_uuid="u-1", model_or_tag="anthropic:claude-opus", weight=0.0
        )


@pytest.mark.asyncio
class TestSetPreferenceLosesToRoutingConstraints:
    """§11.6 acceptance: set_preference loses to an org allow-list, a tier cap, and local_only.

    The actual precedence algorithm lives in the (not-yet-merged) §7.3
    routing engine; at the MCP-tool-contract level, this asserts the tool
    always reports back whatever the routing engine decided rather than
    treating its own weight as authoritative -- it never short-circuits
    or masks the routing engine's override.
    """

    async def test_loses_to_org_allow_list(self, user_tools, collaborators):
        """Loses to org allow list."""
        collaborators["routing"].set_preference.return_value = {
            "applied": False,
            "reason": "org_allow_list",
            "effective_model": "anthropic:claude-sonnet",
        }
        result = await user_tools.set_preference("openai:gpt-5", weight=1.0)
        assert result["applied"] is False
        assert result["reason"] == "org_allow_list"

    async def test_loses_to_tier_cap(self, user_tools, collaborators):
        """Loses to tier cap."""
        collaborators["routing"].set_preference.return_value = {
            "applied": False,
            "reason": "tier_cap",
            "effective_model": "anthropic:claude-haiku",
        }
        result = await user_tools.set_preference("anthropic:claude-opus", weight=1.0)
        assert result["applied"] is False
        assert result["reason"] == "tier_cap"

    async def test_loses_to_local_only_clamp(self, user_tools, collaborators):
        """Loses to local only clamp."""
        collaborators["routing"].set_preference.return_value = {
            "applied": False,
            "reason": "local_only",
            "effective_model": "ollama:llama3-local",
        }
        result = await user_tools.set_preference("anthropic:claude-opus", weight=1.0)
        assert result["applied"] is False
        assert result["reason"] == "local_only"


@pytest.mark.asyncio
class TestFlagOff:
    """Flag OFF -> every tool raises a disabled error, no service call."""

    @pytest.fixture(autouse=True)
    def _flag_off(self, monkeypatch):
        """Force waddleai.mcp_v2 off for this test."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "0")

    async def test_all_user_tools_disabled_without_service_call(self, collaborators):
        """All user tools disabled without service call."""
        tools = WaddleAITools(_ctx(), **collaborators)
        checks = [
            (tools.search_code, ("q",), {}),
            (tools.get_symbol, ("s",), {}),
            (tools.search_docs, ("q",), {}),
            (tools.fetch_docs, ("eco", "pkg"), {}),
            (tools.get_call_graph, ("repo", "sym"), {}),
            (tools.get_class_hierarchy, ("repo", "sym"), {}),
            (tools.memory_add, ("c",), {}),
            (tools.memory_search, ("q",), {}),
            (tools.list_models, (), {}),
            (tools.get_routing_policy, (), {}),
            (tools.usage_summary, (), {}),
            (tools.set_preference, ("m",), {}),
        ]
        for method, args, kwargs in checks:
            with pytest.raises(ToolDisabledError):
                await method(*args, **kwargs)
        assert not collaborators["knowledge"].search_code.called
        assert not collaborators["memory"].write.called
        assert not collaborators["routing"].list_models.called
        assert not collaborators["usage"].usage_summary.called

    async def test_all_admin_tools_disabled_without_service_call(self):
        """All admin tools disabled without service call."""
        usage = AsyncMock()
        config = AsyncMock()
        tools = AdminTools(_ctx(), usage=usage, config=config)
        with pytest.raises(ToolDisabledError):
            await tools.usage_by_user("target-user")
        with pytest.raises(ToolDisabledError):
            await tools.add_model("gpt-5", "openai")
        assert not usage.usage_by_user.called
        assert not config.add_model.called


@pytest.mark.asyncio
class TestOrgIsolation:
    """A foreign org_id never sees another org's chunks/memory.

    The wrapper always passes the caller's own ctx.org_id, never
    anything else, to the collaborator.
    """

    async def test_search_code_only_ever_queries_callers_org(self, collaborators, monkeypatch):
        """Search code only ever queries callers org."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")
        collaborators["knowledge"].search_code.return_value = []
        org_a_tools = WaddleAITools(_ctx(org_id=1), **collaborators)
        org_b_tools = WaddleAITools(_ctx(org_id=2), **collaborators)

        await org_a_tools.search_code("q")
        await org_b_tools.search_code("q")

        calls = collaborators["knowledge"].search_code.await_args_list
        assert calls[0].kwargs["org_id"] == 1
        assert calls[1].kwargs["org_id"] == 2

    async def test_memory_search_scoped_to_callers_identity(self, collaborators, monkeypatch):
        """Memory search scoped to callers identity."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")
        collaborators["memory"].search.return_value = []
        tools_a = WaddleAITools(_ctx(org_id=1, user_uuid="user-a"), **collaborators)
        tools_b = WaddleAITools(_ctx(org_id=2, user_uuid="user-b"), **collaborators)

        await tools_a.memory_search("q")
        await tools_b.memory_search("q")

        calls = collaborators["memory"].search.await_args_list
        assert calls[0].kwargs["org_id"] == 1 and calls[0].kwargs["user_uuid"] == "user-a"
        assert calls[1].kwargs["org_id"] == 2 and calls[1].kwargs["user_uuid"] == "user-b"

    async def test_get_call_graph_only_ever_queries_callers_org(self, collaborators, monkeypatch):
        """get_call_graph forwards each caller's own org_id, never a foreign one."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")
        collaborators["knowledge"].get_call_graph.return_value = []
        org_a_tools = WaddleAITools(_ctx(org_id=1), **collaborators)
        org_b_tools = WaddleAITools(_ctx(org_id=2), **collaborators)

        await org_a_tools.get_call_graph("repo", "sym")
        await org_b_tools.get_call_graph("repo", "sym")

        calls = collaborators["knowledge"].get_call_graph.await_args_list
        assert calls[0].kwargs["org_id"] == 1
        assert calls[1].kwargs["org_id"] == 2


@pytest.mark.asyncio
class TestAdminToolsSubjectParameters:
    """Admin tools take an explicit subject.

    User output is UUID-scoped by default, resolving to names only on
    explicit request.
    """

    @pytest.fixture(autouse=True)
    def _flag_on(self, monkeypatch):
        """Force waddleai.mcp_v2 on for this test."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")

    async def test_usage_by_user_returns_uuid_by_default(self):
        """Usage by user returns uuid by default."""
        usage = AsyncMock()
        usage.usage_by_user.return_value = {"user_uuid": "target-user", "tokens": 100}
        config = AsyncMock()
        tools = AdminTools(_ctx(), usage=usage, config=config)
        result = await tools.usage_by_user("target-user")
        assert result["resolve_names"] is False
        assert result["sensitivity"] == "internal"
        usage.usage_by_user.assert_awaited_once_with(org_id=1, user_uuid="target-user", window=None)

    async def test_usage_by_user_resolve_names_explicit(self):
        """Usage by user resolve names explicit."""
        usage = AsyncMock()
        usage.usage_by_user.return_value = {"user_uuid": "target-user", "tokens": 100}
        config = AsyncMock()
        tools = AdminTools(_ctx(), usage=usage, config=config)
        result = await tools.usage_by_user("target-user", resolve_names=True)
        assert result["resolve_names"] is True

    async def test_admin_read_tools_mark_sensitivity(self):
        """Admin read tools mark sensitivity."""
        usage = AsyncMock()
        usage.usage_by_org.return_value = {"tokens": 999}
        usage.cost_attribution.return_value = {"usd": 12.5}
        usage.quota_status.return_value = {"remaining": 10}
        usage.provider_budget_headroom.return_value = {"headroom_usd": 500}
        config = AsyncMock()
        tools = AdminTools(_ctx(), usage=usage, config=config)

        assert (await tools.usage_by_org(org_id=1))["sensitivity"] == "internal"
        assert (await tools.cost_attribution(org_id=1))["sensitivity"] == "internal"
        assert (await tools.quota_status(org_id=1))["sensitivity"] == "internal"
        assert (await tools.provider_budget_headroom(org_id=1))["sensitivity"] == "internal"


@pytest.mark.asyncio
class TestAdminSessionExpiry:
    """§11.5 Authentication: on expiry, write tools fail before read tools."""

    @pytest.fixture(autouse=True)
    def _flag_on(self, monkeypatch):
        """Force waddleai.mcp_v2 on for this test."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")

    async def test_write_tool_fails_past_write_ceiling(self):
        """Write tool fails past write ceiling."""
        usage = AsyncMock()
        config = AsyncMock()
        stale_ctx = _ctx(authenticated_at=time.time() - (ADMIN_WRITE_MAX_AGE_SECONDS + 1))
        tools = AdminTools(stale_ctx, usage=usage, config=config)
        with pytest.raises(StaleSessionError):
            await tools.add_model("gpt-5", "openai")
        assert not config.add_model.called

    async def test_read_tool_still_succeeds_when_write_ceiling_exceeded_but_read_ceiling_is_not(
        self,
    ):
        """Read tool still succeeds when write ceiling exceeded but read ceiling is not."""
        usage = AsyncMock()
        usage.usage_by_org.return_value = {"tokens": 1}
        config = AsyncMock()
        # Older than the write ceiling, younger than the read ceiling.
        stale_age = ADMIN_WRITE_MAX_AGE_SECONDS + 1
        stale_for_writes_ctx = _ctx(authenticated_at=time.time() - stale_age)
        tools = AdminTools(stale_for_writes_ctx, usage=usage, config=config)
        result = await tools.usage_by_org(org_id=1)
        assert result["tokens"] == 1

    async def test_read_tool_fails_past_read_ceiling(self):
        """Read tool fails past read ceiling."""
        usage = AsyncMock()
        config = AsyncMock()
        very_stale_ctx = _ctx(authenticated_at=time.time() - (ADMIN_READ_MAX_AGE_SECONDS + 1))
        tools = AdminTools(very_stale_ctx, usage=usage, config=config)
        with pytest.raises(StaleSessionError):
            await tools.usage_by_org(org_id=1)


@pytest.mark.asyncio
async def test_stub_adapters_raise_service_unavailable(monkeypatch):
    """Interim adapters (shared/mcp/stub_adapters.py) fail loudly and typed.

    Rather than silently returning wrong data.
    """
    monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")
    from shared.mcp.stub_adapters import (
        NotWiredKnowledgeService,
        NotWiredMemoryService,
        NotWiredRoutingService,
        NotWiredUsageService,
    )

    tools = WaddleAITools(
        _ctx(),
        knowledge=NotWiredKnowledgeService(),
        memory=NotWiredMemoryService(),
        routing=NotWiredRoutingService(),
        usage=NotWiredUsageService(),
    )
    with pytest.raises(ServiceUnavailableError):
        await tools.search_code("q")
    with pytest.raises(ServiceUnavailableError):
        await tools.get_call_graph("repo", "sym")
    with pytest.raises(ServiceUnavailableError):
        await tools.get_class_hierarchy("repo", "sym")
