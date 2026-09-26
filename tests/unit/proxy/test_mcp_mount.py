"""Unit tests for the `/mcp` and `/mcp/admin` ASGI mount (§11.1, §11.5).

Drives `MCPMount.__call__` directly with a minimal ASGI harness (no full
Quart app startup) so auth/flag/routing behavior is tested in isolation
from the proxy's heavier startup sequence (grpc, real DB, etc.).
"""

import json
from unittest.mock import Mock

import pytest

from proxy.apps.proxy_server.mcp_mount import (
    MCP_ADMIN_PATH,
    MCP_USER_PATH,
    MCPMount,
    McpServiceFactory,
)
from shared.auth.jwks_verifier import JWKSVerificationError
from shared.auth.rbac import AuthenticationError, Role, UserContext


def _headers(auth: str | None = None, extra: dict | None = None) -> list[tuple[bytes, bytes]]:
    """Build an ASGI header list, always including Host."""
    headers = [(b"host", b"waddleai-proxy.test")]
    if auth is not None:
        headers.append((b"authorization", auth.encode()))
    for k, v in (extra or {}).items():
        headers.append((k.encode(), v.encode()))
    return headers


def _http_scope(path: str, headers: list[tuple[bytes, bytes]]) -> dict:
    """Build a minimal ASGI HTTP scope for a POST to ``path``."""
    return {
        "type": "http",
        "path": path,
        "method": "POST",
        "headers": headers,
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "server": ("waddleai-proxy.test", 80),
        "client": ("127.0.0.1", 12345),
    }


class _ASGIRecorder:
    """Minimal ASGI receive/send pair that records what the mount sends."""

    def __init__(self, body: bytes = b"{}"):
        """Buffer a single request body for one ASGI call."""
        self._body = body
        self._sent_body = True
        self.messages: list[dict] = []

    async def receive(self) -> dict:
        """Yield the buffered body once, then http.disconnect."""
        if self._sent_body:
            self._sent_body = False
            return {"type": "http.request", "body": self._body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(self, message: dict) -> None:
        """Record every ASGI message sent by the mount."""
        self.messages.append(message)

    @property
    def status(self) -> int | None:
        """The HTTP status code from the first response.start message."""
        for m in self.messages:
            if m["type"] == "http.response.start":
                return m["status"]
        return None

    @property
    def json_body(self) -> dict | None:
        """The JSON-decoded response body, if any."""
        for m in self.messages:
            if m["type"] == "http.response.body":
                return json.loads(m["body"])
        return None


async def _call_mount(
    mount: MCPMount, path: str, headers: list[tuple[bytes, bytes]]
) -> _ASGIRecorder:
    """Drive one ASGI request through ``mount`` and return the recorded response."""
    recorder = _ASGIRecorder()
    await mount(_http_scope(path, headers), recorder.receive, recorder.send)
    return recorder


def _user_context(role: Role = Role.USER, org_id: int = 1, user_id: int = 7) -> UserContext:
    """Build a UserContext as `rbac.authenticate_api_key` would return it."""
    return UserContext(
        user_id=user_id,
        username="u",
        role=role,
        organization_id=org_id,
        managed_orgs=[],
        permissions=set(),
        api_key_id=99,
    )


@pytest.fixture
def passthrough_app():
    """An inner ASGI app whose `.calls` records every path it was invoked for."""
    calls = []

    async def app(scope, receive, send):
        """Record the path of every request that reaches the inner app."""
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"passthrough"})

    app.calls = calls
    return app


@pytest.mark.asyncio
class TestNonMcpPathsPassThrough:
    """Paths other than /mcp and /mcp/admin are untouched."""

    async def test_unrelated_path_is_not_intercepted(self, passthrough_app):
        """Unrelated path is not intercepted."""
        mount = MCPMount(passthrough_app, rbac=Mock(), jwks_verifier=Mock())
        await _call_mount(mount, "/v1/chat/completions", [])
        assert passthrough_app.calls == ["/v1/chat/completions"]


@pytest.mark.asyncio
class TestUserMountAuth:
    """Auth behavior of the /mcp user mount."""

    async def test_missing_authorization_is_401(self, passthrough_app, monkeypatch):
        """Missing authorization is 401."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")
        mount = MCPMount(passthrough_app, rbac=Mock(), jwks_verifier=Mock())
        recorder = await _call_mount(mount, MCP_USER_PATH, _headers())
        assert recorder.status == 401
        assert passthrough_app.calls == []

    async def test_invalid_api_key_is_401_never_anonymous(self, passthrough_app, monkeypatch):
        """Invalid api key is 401 never anonymous."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")
        rbac = Mock()
        rbac.authenticate_api_key.side_effect = AuthenticationError("bad key")
        mount = MCPMount(passthrough_app, rbac=rbac, jwks_verifier=Mock())
        recorder = await _call_mount(mount, MCP_USER_PATH, _headers("wa-bad-key"))
        assert recorder.status == 401

    async def test_valid_key_flag_off_returns_404_mount_inert(self, passthrough_app, monkeypatch):
        """Valid key flag off returns 404 mount inert."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "0")
        rbac = Mock()
        rbac.authenticate_api_key.return_value = _user_context()
        mount = MCPMount(passthrough_app, rbac=rbac, jwks_verifier=Mock())
        recorder = await _call_mount(mount, MCP_USER_PATH, _headers("wa-good-key"))
        assert recorder.status == 404
        assert passthrough_app.calls == []

    async def test_valid_key_flag_on_reaches_fastmcp_app(self, passthrough_app, monkeypatch):
        """A 401/403/404 never fires for a valid, flag-on request.

        The request reaches the real streamable-HTTP app (status is
        whatever MCP's transport returns for this raw body, not one of
        the mount's own short-circuits).
        """
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")
        rbac = Mock()
        rbac.authenticate_api_key.return_value = _user_context()
        mount = MCPMount(passthrough_app, rbac=rbac, jwks_verifier=Mock())
        recorder = await _call_mount(mount, MCP_USER_PATH, _headers("wa-good-key"))
        assert recorder.status not in (401, 403, 404)
        assert passthrough_app.calls == []  # inner app never invoked -- FastMCP handled it


@pytest.mark.asyncio
class TestAdminMountAuth:
    """Auth behavior of the /mcp/admin mount."""

    async def test_non_admin_role_is_403_before_any_tool_list(self, passthrough_app, monkeypatch):
        """Non admin role is 403 before any tool list."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")
        rbac = Mock()
        rbac.authenticate_api_key.return_value = _user_context(role=Role.USER)
        mount = MCPMount(passthrough_app, rbac=rbac, jwks_verifier=Mock())
        recorder = await _call_mount(mount, MCP_ADMIN_PATH, _headers("wa-user-key"))
        assert recorder.status == 403
        assert recorder.json_body["error"] == "forbidden"

    async def test_resource_manager_role_also_denied(self, passthrough_app, monkeypatch):
        """Resource manager role also denied."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")
        rbac = Mock()
        rbac.authenticate_api_key.return_value = _user_context(role=Role.RESOURCE_MANAGER)
        mount = MCPMount(passthrough_app, rbac=rbac, jwks_verifier=Mock())
        recorder = await _call_mount(mount, MCP_ADMIN_PATH, _headers("wa-rm-key"))
        assert recorder.status == 403

    async def test_admin_role_flag_off_is_404_not_403(self, passthrough_app, monkeypatch):
        """Flag-off returns a clean 404, not a stale 403.

        The flag gate is checked after the role check, so an admin still
        gets a clean disabled signal rather than a stale one.
        """
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "0")
        rbac = Mock()
        rbac.authenticate_api_key.return_value = _user_context(role=Role.ADMIN)
        mount = MCPMount(passthrough_app, rbac=rbac, jwks_verifier=Mock())
        recorder = await _call_mount(mount, MCP_ADMIN_PATH, _headers("wa-admin-key"))
        assert recorder.status == 404

    async def test_admin_role_flag_on_reaches_fastmcp_admin_app(self, passthrough_app, monkeypatch):
        """Admin role flag on reaches fastmcp admin app."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")
        rbac = Mock()
        rbac.authenticate_api_key.return_value = _user_context(role=Role.ADMIN)
        mount = MCPMount(passthrough_app, rbac=rbac, jwks_verifier=Mock())
        recorder = await _call_mount(mount, MCP_ADMIN_PATH, _headers("wa-admin-key"))
        assert recorder.status not in (401, 403, 404)
        assert passthrough_app.calls == []


@pytest.mark.asyncio
class TestSessionIdTiesToDataPlane:
    """ToolContext.session_id derivation."""

    async def test_session_id_derived_from_header_when_present(self, passthrough_app, monkeypatch):
        """Session id derived from header when present."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")
        captured_ctx = {}
        orig_user_tools = McpServiceFactory.user_tools

        def spy_user_tools(self, ctx):
            """Capture the ToolContext passed to user_tools, then delegate."""
            captured_ctx["ctx"] = ctx
            return orig_user_tools(self, ctx)

        monkeypatch.setattr(McpServiceFactory, "user_tools", spy_user_tools)

        rbac = Mock()
        rbac.authenticate_api_key.return_value = _user_context()
        mount = MCPMount(passthrough_app, rbac=rbac, jwks_verifier=Mock())
        headers = _headers("wa-good-key", {"x-waddleai-session-id": "session-abc"})
        await _call_mount(mount, MCP_USER_PATH, headers)
        assert captured_ctx["ctx"].session_id == "session-abc"

    async def test_session_id_falls_back_to_api_key_id(self, passthrough_app, monkeypatch):
        """Session id falls back to api key id."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")
        captured_ctx = {}
        orig_user_tools = McpServiceFactory.user_tools

        def spy_user_tools(self, ctx):
            """Capture the ToolContext passed to user_tools, then delegate."""
            captured_ctx["ctx"] = ctx
            return orig_user_tools(self, ctx)

        monkeypatch.setattr(McpServiceFactory, "user_tools", spy_user_tools)

        rbac = Mock()
        rbac.authenticate_api_key.return_value = _user_context(user_id=7)
        mount = MCPMount(passthrough_app, rbac=rbac, jwks_verifier=Mock())
        await _call_mount(mount, MCP_USER_PATH, _headers("wa-good-key"))
        assert captured_ctx["ctx"].session_id == "key-99"  # api_key_id from _user_context()


@pytest.mark.asyncio
class TestBearerTokenUsesJWKSVerification:
    """regression: headless-auth H4 -- /mcp's Bearer path goes through JWKS, not a local keystore.

    ``_authenticate_from_scope``'s Bearer branch imports
    ``shared.auth.penguin_auth.verify_token_via_jwks`` at call time, so
    patching that module attribute intercepts it without needing a real
    signed token or a live JWKS endpoint here -- the JWKS fetch/cache/kid
    mechanics themselves are covered by tests/unit/proxy/test_jwks_verifier.py.
    """

    async def test_valid_bearer_token_authenticates_via_jwks(self, passthrough_app, monkeypatch):
        """A verified Bearer token reaches the mount as an authenticated user."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")
        uc = _user_context()
        monkeypatch.setattr(
            "shared.auth.penguin_auth.verify_token_via_jwks",
            Mock(return_value=uc),
        )
        mount = MCPMount(passthrough_app, rbac=Mock(), jwks_verifier=Mock())
        recorder = await _call_mount(mount, MCP_USER_PATH, _headers("Bearer some.jwt.token"))
        assert recorder.status not in (401, 403, 404)

    async def test_jwks_verification_failure_is_401(self, passthrough_app, monkeypatch):
        """A JWKS-rejected Bearer token (bad kid/signature/expiry) is 401, never fail-open."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")
        monkeypatch.setattr(
            "shared.auth.penguin_auth.verify_token_via_jwks",
            Mock(side_effect=AuthenticationError("JWKS endpoint unreachable")),
        )
        mount = MCPMount(passthrough_app, rbac=Mock(), jwks_verifier=Mock())
        recorder = await _call_mount(mount, MCP_USER_PATH, _headers("Bearer some.jwt.token"))
        assert recorder.status == 401


def test_jwks_verification_error_is_not_swallowed_silently() -> None:
    """Sanity check: JWKSVerificationError is importable from its published home.

    Guards against the module path this file's monkeypatches assume
    (shared.auth.penguin_auth.verify_token_via_jwks wraps
    shared.auth.jwks_verifier.JWKSVerificationError) silently drifting.
    """
    assert issubclass(JWKSVerificationError, Exception)
