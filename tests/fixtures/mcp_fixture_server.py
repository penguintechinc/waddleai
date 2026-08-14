"""Reusable fixture external MCP server for gateway tests (§11.4, §11.5).

A real ``mcp``-SDK server — not a mock — exposing two tools (``ping``,
``whoami``) over both transports the gateway supports (streamable-HTTP,
stdio), with pluggable inbound-auth modes so ``GatewayClient``/
``OutboundAuth``/``IdentityResolver`` tests exercise genuine wire-level
behavior instead of a mocked object graph. Reused by Tasks 7-9 (this
branch) and the future §11.5 acceptance suite (Task 16).

``whoami()`` echoes back the identity the fixture resolved for the
current caller, which is what gateway tests assert on to prove a given
header/OAuth2 token/stdio-env credential actually reached the upstream
server — not just that WaddleAI's own client code ran without error.

Streamable-HTTP: a fresh ``FastMCP`` is built per inbound HTTP request
(mirroring ``proxy/apps/proxy_server/mcp_mount.py``'s pattern) because
``StreamableHTTPSessionManager.run()`` may only be entered once per
instance; auth is checked in a thin ASGI wrapper ahead of the FastMCP
app, and the resolved identity is stashed in a ``contextvars.ContextVar``
so ``whoami()`` can read it back within that request's task.

stdio: there are no per-call headers on stdio, so credentials are
established once at process spawn via the ``FIXTURE_IDENTITY``
environment variable — the realistic pattern for stdio MCP servers
(compare: how most stdio MCP servers take an API key via env, not per
call).
"""

from __future__ import annotations

import argparse
import contextvars
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

FIXTURE_SERVER_NAME = "mcp-fixture"

AUTH_MODE_NONE = "none"
AUTH_MODE_HEADER = "header"
AUTH_MODE_BEARER = "bearer"

_current_identity: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_identity", default=None
)

ASGIApp = Callable[
    [dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]
]


@dataclass(slots=True, frozen=True)
class FixtureAuthConfig:
    """How the fixture's streamable-HTTP transport checks inbound auth.

    Mirrors the outbound-auth shapes ``shared/mcp/gateway/auth.py``
    produces (a header, or a bearer token from any OAuth2 flow) so one
    fixture config exercises header auth and both OAuth2 flows — the
    resource server only ever sees "a bearer token arrived", regardless
    of whether client-credentials or authorization-code+DCR produced it.
    """

    mode: str = AUTH_MODE_NONE
    header_name: str = "X-Api-Key"
    expected_header_value: str | None = None
    expected_bearer_token: str | None = None


class FixtureAuthError(RuntimeError):
    """Raised when an inbound request fails the fixture's auth check."""


def _check_headers(config: FixtureAuthConfig, headers: dict[str, str]) -> str:
    """Validate ``headers`` per ``config``; return the identity string ``whoami()`` echoes."""
    if config.mode == AUTH_MODE_NONE:
        return "anonymous"
    if config.mode == AUTH_MODE_HEADER:
        value = headers.get(config.header_name.lower())
        if not value or value != config.expected_header_value:
            raise FixtureAuthError(f"missing/invalid {config.header_name} header")
        return f"header:{value}"
    if config.mode == AUTH_MODE_BEARER:
        authorization = headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise FixtureAuthError("missing bearer token")
        token = authorization[len("Bearer ") :]
        if config.expected_bearer_token is not None and token != config.expected_bearer_token:
            raise FixtureAuthError("bearer token mismatch")
        return f"bearer:{token}"
    raise FixtureAuthError(f"unknown auth mode {config.mode!r}")


def build_fixture_mcp() -> FastMCP:
    """Build the two-tool ``FastMCP`` instance (``ping``, ``whoami``).

    DNS-rebinding Host/Origin checks are disabled — this is an
    in-process/loopback test double, not an Internet-facing server, and
    tests connect via synthetic hostnames (`fixture.test`) or an
    ``httpx.ASGITransport``, neither of which is a real DNS name the
    upstream SDK's default allowlist would accept.
    """
    mcp = FastMCP(
        FIXTURE_SERVER_NAME,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool(name="ping")
    async def ping(message: str = "") -> dict[str, Any]:
        """Echo ``message`` back — proves a round-trip call/response works."""
        return {"pong": message}

    @mcp.tool(name="whoami")
    async def whoami() -> dict[str, Any]:
        """Return the identity the fixture resolved for the current caller."""
        return {"identity": _current_identity.get()}

    return mcp


def _decode_headers(scope: dict) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


async def _send_error(send: Callable, status: int, detail: str) -> None:
    body = json.dumps({"error": detail}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def build_streamable_http_app(config: FixtureAuthConfig | None = None) -> ASGIApp:
    """Build a raw ASGI app: authenticate, stash identity, delegate to a fresh FastMCP.

    Auth happens in this thin wrapper ahead of the FastMCP app, not
    inside a tool — the same shape as
    ``proxy/apps/proxy_server/mcp_mount.py``, kept consistent so the
    fixture is a faithful stand-in for a real MCP server, not just
    whatever is convenient to test against.
    """
    resolved_config = config or FixtureAuthConfig()

    async def app(scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await _send_error(send, 404, "fixture only serves http")
            return

        headers = _decode_headers(scope)
        try:
            identity = _check_headers(resolved_config, headers)
        except FixtureAuthError as exc:
            await _send_error(send, 401, str(exc))
            return

        token = _current_identity.set(identity)
        try:
            # Fresh FastMCP per request: StreamableHTTPSessionManager.run()
            # may only be entered once per instance (see module docstring).
            mcp = build_fixture_mcp()
            inner = mcp.streamable_http_app()
            async with mcp.session_manager.run():
                await inner(scope, receive, send)
        finally:
            _current_identity.reset(token)

    return app


def run_stdio() -> None:
    """Run the fixture over stdio, echoing ``FIXTURE_IDENTITY`` (if set) via ``whoami()``."""
    _current_identity.set(os.environ.get("FIXTURE_IDENTITY"))
    mcp = build_fixture_mcp()
    mcp.run(transport="stdio")


def main() -> None:
    """Entry point for ``python3 -m tests.fixtures.mcp_fixture_server`` (stdio subprocess use)."""
    parser = argparse.ArgumentParser(description="Fixture external MCP server (stdio transport)")
    parser.add_argument("--transport", choices=["stdio"], default="stdio")
    parser.parse_args()
    run_stdio()


if __name__ == "__main__":
    main()
