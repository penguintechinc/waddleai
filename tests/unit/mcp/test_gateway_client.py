"""§11.4 gateway-client tests — connect/discover/invoke over both transports.

Runs against the real `tests/fixtures/mcp_fixture_server.py` (streamable-
HTTP via an in-process `httpx.ASGITransport`, stdio via a real subprocess)
so these are genuine wire-level round trips, not a mocked object graph —
same testing philosophy as `tests/unit/mcp/test_server.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

from shared.mcp.gateway.client import (
    GatewayClient,
    GatewayClientError,
    GatewayEndpointConfig,
    namespace_tool_name,
    split_namespaced_name,
)
from tests.fixtures.mcp_fixture_server import FixtureAuthConfig, build_streamable_http_app

REPO_ROOT = Path(__file__).resolve().parents[3]


def _asgi_httpx_factory(app):
    """Build an httpx client factory bound to an in-process ASGI app (no real socket)."""

    def factory(headers=None, timeout=None, auth=None):
        kwargs: dict = {
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


def _http_endpoint(namespace: str = "elder") -> GatewayEndpointConfig:
    return GatewayEndpointConfig(
        id=1,
        org_id=1,
        name="fixture",
        url="http://fixture.test/mcp",
        transport="streamable_http",
        namespace=namespace,
    )


def _stdio_endpoint(namespace: str = "elder") -> GatewayEndpointConfig:
    command = f"{sys.executable} -m tests.fixtures.mcp_fixture_server --transport stdio"
    return GatewayEndpointConfig(
        id=2,
        org_id=1,
        name="fixture-stdio",
        url=command,
        transport="stdio",
        namespace=namespace,
    )


class TestNamespacing:
    """Namespace helper round-trips and rejects foreign namespaces."""

    def test_namespace_and_split_round_trip(self):
        """namespace_tool_name and split_namespaced_name are inverses."""
        namespaced = namespace_tool_name("elder", "search")
        assert namespaced == "elder.search"
        assert split_namespaced_name(namespaced, "elder") == "search"

    def test_split_rejects_foreign_namespace(self):
        """Splitting a name outside the given namespace raises."""
        with pytest.raises(GatewayClientError):
            split_namespaced_name("other.search", "elder")


@pytest.mark.asyncio
class TestStreamableHttpTransport:
    """GatewayClient over the streamable-HTTP transport (in-process ASGI)."""

    async def test_connect_discover_invoke(self):
        """Connect, discover the fixture's two tools, and invoke one over streamable-HTTP."""
        app = build_streamable_http_app(FixtureAuthConfig())
        endpoint = _http_endpoint()
        async with GatewayClient(endpoint, httpx_client_factory=_asgi_httpx_factory(app)) as client:
            tools = await client.discover()
            names = {t.namespaced_name for t in tools}
            assert names == {"elder.ping", "elder.whoami"}

            result = await client.invoke("elder.ping", {"message": "hello"})
            assert result.isError is False
            assert "hello" in result.content[0].text

    async def test_invoke_rejects_a_name_outside_the_endpoint_namespace(self):
        """Invoking a tool name outside this endpoint's namespace raises."""
        app = build_streamable_http_app(FixtureAuthConfig())
        endpoint = _http_endpoint()
        async with GatewayClient(endpoint, httpx_client_factory=_asgi_httpx_factory(app)) as client:
            with pytest.raises(GatewayClientError):
                await client.invoke("other.ping", {})

    async def test_discover_before_connect_raises(self):
        """Calling discover() before connect() raises rather than hanging."""
        endpoint = _http_endpoint()
        client = GatewayClient(endpoint)
        with pytest.raises(GatewayClientError):
            await client.discover()


@pytest.mark.asyncio
class TestStdioTransport:
    """GatewayClient over the stdio transport (real subprocess)."""

    async def test_connect_discover_invoke(self):
        """Connect, discover the fixture's two tools, and invoke one over stdio."""
        endpoint = _stdio_endpoint()
        async with GatewayClient(endpoint, cwd=str(REPO_ROOT)) as client:
            tools = await client.discover()
            names = {t.namespaced_name for t in tools}
            assert names == {"elder.ping", "elder.whoami"}

            result = await client.invoke("elder.whoami", {})
            # No FIXTURE_IDENTITY set for this subprocess -- echoes None.
            assert result.isError is False
            assert "null" in result.content[0].text or "None" in result.content[0].text

    async def test_env_reaches_the_subprocess(self):
        """`env` passed to GatewayClient reaches the spawned stdio subprocess."""
        endpoint = _stdio_endpoint()
        async with GatewayClient(
            endpoint, env={"FIXTURE_IDENTITY": "stdio-caller"}, cwd=str(REPO_ROOT)
        ) as client:
            result = await client.invoke("elder.whoami", {})
            assert "stdio-caller" in result.content[0].text


@pytest.mark.asyncio
class TestBothTransportsAgreeOnDiscoveredToolShape:
    """Both transports discover the same namespaced tool set (§11.5 acceptance groundwork)."""

    async def test_same_tool_names_both_transports(self):
        """streamable-HTTP and stdio discover an identical namespaced tool set."""
        app = build_streamable_http_app(FixtureAuthConfig())
        async with GatewayClient(
            _http_endpoint(), httpx_client_factory=_asgi_httpx_factory(app)
        ) as http_client:
            http_names = {t.namespaced_name for t in await http_client.discover()}

        async with GatewayClient(_stdio_endpoint(), cwd=str(REPO_ROOT)) as stdio_client_obj:
            stdio_names = {t.namespaced_name for t in await stdio_client_obj.discover()}

        assert http_names == stdio_names == {"elder.ping", "elder.whoami"}
