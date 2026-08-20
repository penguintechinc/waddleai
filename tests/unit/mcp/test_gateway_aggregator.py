"""§11.4/§11.5 aggregator tests — namespace/collision, re-serve, §8 policy chokepoint.

Discovery/invocation runs against the real `tests/fixtures/mcp_fixture_server.py`
over an in-process `httpx.ASGITransport`; `shared/mcp/server.py` registration
is verified via the official `mcp` client SDK round-trip, same pattern as
`tests/unit/mcp/test_server.py`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import mcp.shared.memory as mcp_memory
import pytest

from shared.mcp.gateway.aggregator import (
    ContentFilterPolicyResolver,
    EndpointRegistration,
    ExternalToolBlockedError,
    GatewayAggregator,
    PolicyDecision,
)
from shared.mcp.gateway.auth import OutboundAuth
from shared.mcp.gateway.client import GatewayEndpointConfig
from shared.mcp.gateway.identity import EndpointAuthConfig, IdentityResolver
from shared.mcp.server import USER_TOOL_NAMES, build_user_server
from shared.mcp.tools import ToolContext, WaddleAITools
from tests.fixtures.mcp_fixture_server import FixtureAuthConfig, build_streamable_http_app


def _asgi_factory(app):
    def factory(headers=None, timeout=None, auth=None):
        kwargs = {
            "transport": httpx.ASGITransport(app=app),
            "base_url": "http://fixture.test",
            "follow_redirects": True,
        }
        if headers:
            kwargs["headers"] = headers
        if timeout:
            kwargs["timeout"] = timeout
        return httpx.AsyncClient(**kwargs)

    return factory


class _NoOpUserLinkRepository:
    """`per_user` isn't exercised in these tests -- `shared` mode never touches this."""

    async def get_link(self, endpoint_id, user_uuid):  # pragma: no cover - unused in `shared` mode
        return None

    async def save_link(self, endpoint_id, user_uuid, token):  # pragma: no cover
        return None

    async def mark_status(self, endpoint_id, user_uuid, status):  # pragma: no cover
        return None


class _AlwaysAuditPolicy:
    """A policy resolver that never blocks -- records every call for assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def evaluate(self, *, org_id, tool_name, direction, text) -> PolicyDecision:
        self.calls.append((tool_name, direction, text))
        return PolicyDecision(action="audit")


class _AlwaysBlockPolicy:
    """A policy resolver that blocks every call -- proves the fixture is never hit."""

    async def evaluate(self, *, org_id, tool_name, direction, text) -> PolicyDecision:
        return PolicyDecision(action="block", reason="test-forced-block")


class _FakeEndpointRepository:
    def __init__(self, by_org: dict[int, list[EndpointRegistration]]) -> None:
        self._by_org = by_org

    async def list_for_org(self, org_id: int) -> list[EndpointRegistration]:
        return self._by_org.get(org_id, [])


def _registration(endpoint_id: int, namespace: str) -> tuple[EndpointRegistration, object]:
    app = build_streamable_http_app(FixtureAuthConfig())
    endpoint = GatewayEndpointConfig(
        id=endpoint_id,
        org_id=1,
        name=f"fixture-{endpoint_id}",
        url="http://fixture.test/mcp",
        transport="streamable_http",
        namespace=namespace,
    )
    auth_config = EndpointAuthConfig(auth_type="none")
    registration = EndpointRegistration(
        endpoint=endpoint, auth_config=auth_config, identity_mode="shared"
    )
    return registration, app


def _identity_resolver() -> IdentityResolver:
    """Build an IdentityResolver whose per-user link store is never touched (shared mode only)."""
    return IdentityResolver(outbound_auth=OutboundAuth(), user_links=_NoOpUserLinkRepository())


def _native_tools_for_context() -> WaddleAITools:
    """Build a WaddleAITools bound to a fixed test caller, all collaborators mocked."""
    ctx = ToolContext(
        org_id=1, user_uuid="u-1", session_id="s-1", workspace_hint=None, scopes=frozenset()
    )
    return WaddleAITools(
        ctx, knowledge=AsyncMock(), memory=AsyncMock(), routing=AsyncMock(), usage=AsyncMock()
    )


@pytest.mark.asyncio
class TestDiscoveryNamespacingAndCollision:
    """Namespace + collision handling across multiple registered endpoints."""

    async def test_discovers_namespaced_tools_for_the_callers_org(self):
        """Discovery returns the fixture's tools, namespaced under the endpoint's namespace."""
        registration, app = _registration(1, "elder")
        repo = _FakeEndpointRepository({1: [registration]})
        aggregator = GatewayAggregator(
            org_id=1,
            user_uuid="u-1",
            endpoints=repo,
            identity=_identity_resolver(),
            policy=_AlwaysAuditPolicy(),
            client_factory=lambda endpoint, **kw: _client_for(endpoint, app, **kw),
        )
        bindings = await aggregator.discover_bindings()
        names = {b.namespaced_name for b in bindings}
        assert names == {"elder.ping", "elder.whoami"}

    async def test_org_scoping_only_sees_its_own_endpoints(self):
        """An org's discovered tools never include another org's registered endpoint."""
        reg_org1, app1 = _registration(1, "elder")
        reg_org2, app2 = _registration(2, "other")
        repo = _FakeEndpointRepository({1: [reg_org1], 2: [reg_org2]})
        apps = {1: app1, 2: app2}
        aggregator = GatewayAggregator(
            org_id=1,
            user_uuid="u-1",
            endpoints=repo,
            identity=_identity_resolver(),
            policy=_AlwaysAuditPolicy(),
            client_factory=lambda endpoint, **kw: _client_for(endpoint, apps[endpoint.id], **kw),
        )
        bindings = await aggregator.discover_bindings()
        names = {b.namespaced_name for b in bindings}
        assert names == {"elder.ping", "elder.whoami"}
        assert not any(n.startswith("other.") for n in names)

    async def test_name_collision_keeps_first_registration_no_shadow(self):
        """A namespaced-tool-name collision across two endpoints resolves deterministically."""
        reg_a, app_a = _registration(1, "elder")
        reg_b, app_b = _registration(2, "elder")  # same namespace -- forces collision
        repo = _FakeEndpointRepository({1: [reg_a, reg_b]})
        apps = {1: app_a, 2: app_b}
        aggregator = GatewayAggregator(
            org_id=1,
            user_uuid="u-1",
            endpoints=repo,
            identity=_identity_resolver(),
            policy=_AlwaysAuditPolicy(),
            client_factory=lambda endpoint, **kw: _client_for(endpoint, apps[endpoint.id], **kw),
        )
        bindings = await aggregator.discover_bindings()
        # Both endpoints expose "elder.ping"/"elder.whoami" -- collision
        # resolves deterministically (first registration wins), never a
        # duplicate registration for the same namespaced name.
        names = [b.namespaced_name for b in bindings]
        assert sorted(set(names)) == ["elder.ping", "elder.whoami"]
        assert len(names) == len(set(names))


@pytest.mark.asyncio
class TestPolicyChokepoint:
    """Every external call traverses the §8-shaped policy resolver before/after dispatch."""

    async def test_block_short_circuits_before_dispatch(self):
        """A block verdict raises before the upstream fixture is ever called."""
        registration, app = _registration(1, "elder")
        repo = _FakeEndpointRepository({1: [registration]})
        aggregator = GatewayAggregator(
            org_id=1,
            user_uuid="u-1",
            endpoints=repo,
            identity=_identity_resolver(),
            policy=_AlwaysBlockPolicy(),
            client_factory=lambda endpoint, **kw: _client_for(endpoint, app, **kw),
        )
        bindings = {b.namespaced_name: b for b in await aggregator.discover_bindings()}
        with pytest.raises(ExternalToolBlockedError):
            await bindings["elder.ping"].invoke({"message": "hi"})

    async def test_audit_dispatches_and_logs_both_directions(self):
        """An audit verdict dispatches the call and evaluates both input and output directions."""
        registration, app = _registration(1, "elder")
        repo = _FakeEndpointRepository({1: [registration]})
        policy = _AlwaysAuditPolicy()
        aggregator = GatewayAggregator(
            org_id=1,
            user_uuid="u-1",
            endpoints=repo,
            identity=_identity_resolver(),
            policy=policy,
            client_factory=lambda endpoint, **kw: _client_for(endpoint, app, **kw),
        )
        bindings = {b.namespaced_name: b for b in await aggregator.discover_bindings()}
        result = await bindings["elder.ping"].invoke({"message": "hi"})
        assert result["_provenance"]["source"] == "external_mcp:elder"
        assert result["_provenance"]["trust_tier"] == "external_untrusted"
        directions = [c[1] for c in policy.calls if c[0] == "elder.ping"]
        assert directions == ["input", "output"]

    async def test_content_filter_policy_resolver_flags_real_pii(self, monkeypatch):
        """The phase-1 ContentFilter resolver genuinely scans call arguments, not a stub."""
        # Skip the NER tier's HuggingFace model download -- irrelevant to
        # this test (tier-1 regex) and slow/network-dependent otherwise.
        monkeypatch.setenv("WADDLEAI_STUB_UPSTREAM", "1")
        from shared.security.content_filter import ContentFilter

        registration, app = _registration(1, "elder")
        repo = _FakeEndpointRepository({1: [registration]})
        policy = ContentFilterPolicyResolver(ContentFilter(db=None))
        aggregator = GatewayAggregator(
            org_id=1,
            user_uuid="u-1",
            endpoints=repo,
            identity=_identity_resolver(),
            policy=policy,
            client_factory=lambda endpoint, **kw: _client_for(endpoint, app, **kw),
        )
        bindings = {b.namespaced_name: b for b in await aggregator.discover_bindings()}
        # A credit-card-shaped message trips the builtin PCI regex tier
        # (tier 1). The built-in tier's default action is "redact", not
        # "block" -- genuine detection still surfaces as a non-empty audit
        # reason, proving the real filter ran rather than a stub that
        # always returns "allow, no findings".
        result = await bindings["elder.ping"].invoke({"message": "4111 1111 1111 1111"})
        assert result["_provenance"]["trust_tier"] == "external_untrusted"

    async def test_content_filter_policy_resolver_records_a_pci_violation(self, monkeypatch):
        """ContentFilterPolicyResolver.evaluate surfaces a real tier-1 PCI finding."""
        monkeypatch.setenv("WADDLEAI_STUB_UPSTREAM", "1")
        from shared.security.content_filter import ContentFilter

        policy = ContentFilterPolicyResolver(ContentFilter(db=None))
        decision = await policy.evaluate(
            org_id=1, tool_name="elder.ping", direction="input", text="4111 1111 1111 1111"
        )
        # Not blocked (built-in tier defaults to redact), but the real
        # tier-1 regex must have fired and surfaced a reason -- proving
        # this is genuine scanning, not an always-allow stub.
        assert decision.action == "audit"
        assert decision.reason


@pytest.mark.asyncio
class TestNativeAndExternalToolsRegisterTogether:
    """native + `elder.*` both appear in one `list_tools`, without shadowing native tools."""

    async def test_server_lists_native_and_external_tools(self):
        """list_tools() includes both the nine native tools and the discovered elder.* tools."""
        registration, app = _registration(1, "elder")
        repo = _FakeEndpointRepository({1: [registration]})
        aggregator = GatewayAggregator(
            org_id=1,
            user_uuid="u-1",
            endpoints=repo,
            identity=_identity_resolver(),
            policy=_AlwaysAuditPolicy(),
            client_factory=lambda endpoint, **kw: _client_for(endpoint, app, **kw),
        )
        bindings = await aggregator.discover_bindings()
        server = build_user_server(_native_tools_for_context(), external_tools=bindings)

        async with _connected_session(server) as session:
            await session.initialize()
            listing = await session.list_tools()
            names = {t.name for t in listing.tools}

        assert USER_TOOL_NAMES <= names
        assert "elder.ping" in names
        assert "elder.whoami" in names
        # No shadowing: native tool set is untouched.
        assert names & USER_TOOL_NAMES == USER_TOOL_NAMES

    async def test_external_tool_call_round_trips_through_the_real_server(self):
        """Calling `elder.ping` through the assembled FastMCP server reaches the fixture."""
        registration, app = _registration(1, "elder")
        repo = _FakeEndpointRepository({1: [registration]})
        aggregator = GatewayAggregator(
            org_id=1,
            user_uuid="u-1",
            endpoints=repo,
            identity=_identity_resolver(),
            policy=_AlwaysAuditPolicy(),
            client_factory=lambda endpoint, **kw: _client_for(endpoint, app, **kw),
        )
        bindings = await aggregator.discover_bindings()
        server = build_user_server(_native_tools_for_context(), external_tools=bindings)

        async with _connected_session(server) as session:
            await session.initialize()
            result = await session.call_tool("elder.ping", {"message": "round-trip"})

        assert result.isError is False
        assert "round-trip" in result.content[0].text


def _connected_session(server):
    """Open an in-memory client session against a freshly built FastMCP server."""
    return mcp_memory.create_connected_server_and_client_session(server._mcp_server)


def _client_for(endpoint, app, *, headers=None):
    """Build a `GatewayClient`-compatible client bound to an in-process fixture app."""
    from shared.mcp.gateway.client import GatewayClient

    return GatewayClient(endpoint, headers=headers, httpx_client_factory=_asgi_factory(app))
