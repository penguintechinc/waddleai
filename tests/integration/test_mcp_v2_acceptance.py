"""§11.5 cross-module MCP acceptance checks runnable without a deployed cluster.

These tests deliberately compose the official MCP client SDK, the WaddleAI
server assembly, the gateway aggregator, and the fixture's streamable-HTTP
server.  Deployment-only checks (the beta harnesses in the Playwright suite)
remain separate because they require a real authenticated load balancer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import mcp.shared.memory as mcp_memory
import mcp.types as mcp_types
import pytest

from shared.mcp.gateway.aggregator import (
    EndpointRegistration,
    ExternalToolBlockedError,
    GatewayAggregator,
    PolicyDecision,
)
from shared.mcp.gateway.auth import OutboundAuth
from shared.mcp.gateway.client import GatewayClient, GatewayEndpointConfig
from shared.mcp.gateway.identity import EndpointAuthConfig, IdentityResolver
from shared.mcp.server import USER_TOOL_NAMES, build_user_server
from shared.mcp.tools import ToolContext, WaddleAITools
from tests.fixtures.mcp_fixture_server import FixtureAuthConfig, build_streamable_http_app


def _client_factory(app):
    def factory(endpoint, *, headers=None):
        def http_factory(headers=None, timeout=None, auth=None):
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

        return GatewayClient(endpoint, headers=headers, httpx_client_factory=http_factory)

    return factory


class _Endpoints:
    def __init__(self, registrations: list[EndpointRegistration]) -> None:
        self.registrations = registrations

    async def list_for_org(self, org_id: int) -> list[EndpointRegistration]:
        return self.registrations if org_id == 1 else []


class _Links:
    async def get_link(self, endpoint_id, user_uuid):
        return None

    async def save_link(self, endpoint_id, user_uuid, token):
        return None

    async def mark_status(self, endpoint_id, user_uuid, status):
        return None


class _AuditPolicy:
    async def evaluate(self, *, org_id, tool_name, direction, text) -> PolicyDecision:
        return PolicyDecision(action="audit")


class _BlockPolicy:
    async def evaluate(self, *, org_id, tool_name, direction, text) -> PolicyDecision:
        return PolicyDecision(action="block", reason="acceptance test")


def _native_tools() -> WaddleAITools:
    context = ToolContext(
        org_id=1,
        user_uuid="user-1",
        session_id="session-1",
        workspace_hint=None,
        scopes=frozenset(),
    )
    return WaddleAITools(
        context, knowledge=AsyncMock(), memory=AsyncMock(), routing=AsyncMock(), usage=AsyncMock()
    )


def _registration(endpoint_id: int = 1, namespace: str = "elder") -> EndpointRegistration:
    return EndpointRegistration(
        endpoint=GatewayEndpointConfig(
            id=endpoint_id,
            org_id=1,
            name="fixture",
            url="http://fixture.test/mcp",
            transport="streamable_http",
            namespace=namespace,
        ),
        auth_config=EndpointAuthConfig(auth_type="none"),
        identity_mode="shared",
    )


def _identity() -> IdentityResolver:
    return IdentityResolver(outbound_auth=OutboundAuth(), user_links=_Links())


@pytest.fixture(autouse=True)
def _mcp_flag_on(monkeypatch):
    monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")


@pytest.mark.asyncio
async def test_official_client_sees_native_and_namespaced_gateway_tools() -> None:
    """One official SDK connection lists and calls both native and gateway tools."""
    app = build_streamable_http_app(FixtureAuthConfig())
    aggregator = GatewayAggregator(
        org_id=1,
        user_uuid="user-1",
        endpoints=_Endpoints([_registration()]),
        identity=_identity(),
        policy=_AuditPolicy(),
        client_factory=_client_factory(app),
    )
    server = build_user_server(_native_tools(), external_tools=await aggregator.discover_bindings())
    async with mcp_memory.create_connected_server_and_client_session(server._mcp_server) as session:
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        result = await session.call_tool("elder.ping", {"message": "acceptance"})

    assert USER_TOOL_NAMES <= names
    assert {"elder.ping", "elder.whoami"} <= names
    assert result.isError is False
    first = result.content[0]
    assert isinstance(first, mcp_types.TextContent)
    assert "acceptance" in first.text


@pytest.mark.asyncio
async def test_external_policy_block_prevents_fixture_dispatch() -> None:
    """The policy chokepoint blocks an external tool before the transport is opened."""
    app = build_streamable_http_app(FixtureAuthConfig())
    aggregator = GatewayAggregator(
        org_id=1,
        user_uuid="user-1",
        endpoints=_Endpoints([_registration()]),
        identity=_identity(),
        policy=_BlockPolicy(),
        client_factory=_client_factory(app),
    )
    binding = {tool.namespaced_name: tool for tool in await aggregator.discover_bindings()}[
        "elder.ping"
    ]
    with pytest.raises(ExternalToolBlockedError, match="blocked by policy"):
        await binding.invoke({"message": "must not dispatch"})


@pytest.mark.asyncio
async def test_colliding_external_names_do_not_shadow_native_or_each_other() -> None:
    """Two endpoints with the same namespace yield one deterministic external tool set."""
    app = build_streamable_http_app(FixtureAuthConfig())
    aggregator = GatewayAggregator(
        org_id=1,
        user_uuid="user-1",
        endpoints=_Endpoints([_registration(1, "elder"), _registration(2, "elder")]),
        identity=_identity(),
        policy=_AuditPolicy(),
        client_factory=_client_factory(app),
    )
    bindings = await aggregator.discover_bindings()
    names = [binding.namespaced_name for binding in bindings]
    server = build_user_server(_native_tools(), external_tools=bindings)
    async with mcp_memory.create_connected_server_and_client_session(server._mcp_server) as session:
        await session.initialize()
        advertised = {tool.name for tool in (await session.list_tools()).tools}

    assert names.count("elder.ping") == 1
    assert names.count("elder.whoami") == 1
    assert USER_TOOL_NAMES <= advertised
    assert {"elder.ping", "elder.whoami"} <= advertised


@pytest.mark.asyncio
async def test_flag_off_makes_native_tool_calls_fail(monkeypatch) -> None:
    """Flag-off is enforced at invocation time, after an existing session initializes."""
    server = build_user_server(_native_tools())
    monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "0")
    async with mcp_memory.create_connected_server_and_client_session(server._mcp_server) as session:
        await session.initialize()
        result = await session.call_tool("usage_summary", {})
    assert result.isError is True
