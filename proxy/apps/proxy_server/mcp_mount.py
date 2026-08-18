"""ASGI mount for the §11.1/§11.5 `/mcp` and `/mcp/admin` streamable-HTTP servers.

``MCPMount`` wraps ``app.asgi_app`` ahead of the OIDC/audit middleware
chain (see ``main.py::on_startup``) so `/mcp*` gets its own auth path
before any other request handling runs -- consistent with the rest of the
proxy's ASGI-middleware-wrapping pattern (``OIDCAuthMiddleware``,
``AuditMiddleware``).

Two separate ``FastMCP`` apps are built per authenticated request -- never
one app filtered by role -- so a user-scoped connection's ``list_tools()``
cannot contain an admin tool (§11.5). Auth (and, for `/mcp/admin`, the
``Role.ADMIN`` check) happens *before* either FastMCP app is constructed,
so an unauthorized or non-admin caller never gets far enough to see a tool
list at all, not even a filtered one -- avoiding the "advertise then fail"
disclosure the spec calls out.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from shared.auth.rbac import AuthenticationError, RBACManager, Role, UserContext
from shared.mcp.server import build_admin_server, build_user_server
from shared.mcp.stub_adapters import (
    NotWiredAdminConfigService,
    NotWiredKnowledgeService,
    NotWiredMemoryService,
    NotWiredRoutingService,
    NotWiredUsageService,
)
from shared.mcp.tools import AdminTools, ToolContext, WaddleAITools
from shared.utils.feature_flags import is_feature_enabled

logger = logging.getLogger(__name__)

MCP_V2_FLAG = "waddleai.mcp_v2"
MCP_USER_PATH = "/mcp"
MCP_ADMIN_PATH = "/mcp/admin"

ASGIApp = Callable[
    [dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]
]


@dataclass(slots=True)
class McpServiceFactory:
    """Constructs the per-request tool collaborators.

    Defaults to the "not wired yet" stub adapters (see
    ``shared/mcp/stub_adapters.py``) -- swap in real adapters here once
    ``feature/knowledge-layer``/``feature/smart-routing`` merge. Kept as a
    factory (not a singleton) so tests can inject mocks without touching
    the mount itself.
    """

    knowledge_factory: Callable[[], Any] = NotWiredKnowledgeService
    memory_factory: Callable[[], Any] = NotWiredMemoryService
    routing_factory: Callable[[], Any] = NotWiredRoutingService
    usage_factory: Callable[[], Any] = NotWiredUsageService
    admin_config_factory: Callable[[], Any] = NotWiredAdminConfigService

    def user_tools(self, ctx: ToolContext) -> WaddleAITools:
        """Build a ``WaddleAITools`` bound to ``ctx`` and this factory's collaborators."""
        return WaddleAITools(
            ctx,
            knowledge=self.knowledge_factory(),
            memory=self.memory_factory(),
            routing=self.routing_factory(),
            usage=self.usage_factory(),
        )

    def admin_tools(self, ctx: ToolContext) -> AdminTools:
        """Build an ``AdminTools`` bound to ``ctx`` and this factory's collaborators."""
        return AdminTools(ctx, usage=self.usage_factory(), config=self.admin_config_factory())


def _decode_headers(scope: dict) -> dict[str, str]:
    """Decode an ASGI scope's raw header list into a lowercase-keyed dict."""
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


async def _authenticate_from_scope(
    scope: dict, rbac: RBACManager, oidc_provider: Any
) -> UserContext:
    """Resolve the caller's ``UserContext`` from raw ASGI headers.

    Mirrors ``main.py::get_current_user``'s wa-/sk- and Bearer-JWT paths
    (same underlying ``rbac``/``verify_token`` calls), but operates on the
    ASGI ``scope`` directly since this middleware runs *before* Quart
    builds its ``request`` object.
    """
    headers = _decode_headers(scope)
    authorization = headers.get("authorization")
    if not authorization:
        raise AuthenticationError("Authorization header required")

    if authorization.startswith("sk-") or authorization.startswith("wa-"):
        return await asyncio.to_thread(rbac.authenticate_api_key, authorization)
    if authorization.startswith("Bearer "):
        from shared.auth.penguin_auth import verify_token

        return verify_token(authorization[7:], oidc_provider)
    raise AuthenticationError("Invalid authorization format")


def _build_tool_context(scope: dict, user: UserContext) -> ToolContext:
    """Derive the per-request ``ToolContext``.

    Ties ``session_id`` to the same virtual key the data plane would use,
    so memory/scratchpad scope matches between `/v1/*` and `/mcp`.
    """
    headers = _decode_headers(scope)
    session_id = headers.get("x-waddleai-session-id") or f"key-{user.api_key_id or user.user_id}"
    workspace_hint = headers.get("x-waddleai-workspace")
    scopes = frozenset(
        p.value if hasattr(p, "value") else str(p) for p in (user.permissions or set())
    )
    return ToolContext(
        org_id=user.organization_id,
        # No native UUID column on `users` in this schema yet -- the
        # opaque, non-PII stand-in is the integer id as a string; a real
        # UUID migration is a separate future change, not part of 014.
        user_uuid=str(user.user_id),
        session_id=session_id,
        workspace_hint=workspace_hint,
        scopes=scopes,
    )


async def _send_json(send: Callable[[dict], Awaitable[None]], status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


class MCPMount:
    """ASGI middleware serving `/mcp` and `/mcp/admin`.

    All other paths are passed through unchanged to the wrapped app.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        rbac: RBACManager,
        oidc_provider: Any,
        service_factory: McpServiceFactory | None = None,
    ) -> None:
        """Wrap ``app``, resolving `/mcp*` auth via ``rbac``/``oidc_provider``."""
        self._app = app
        self._rbac = rbac
        self._oidc_provider = oidc_provider
        self._service_factory = service_factory or McpServiceFactory()

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        """Dispatch `/mcp`/`/mcp/admin` to a fresh scoped FastMCP app; pass through otherwise."""
        if scope.get("type") != "http" or scope.get("path") not in (MCP_USER_PATH, MCP_ADMIN_PATH):
            await self._app(scope, receive, send)
            return

        is_admin_path = scope["path"] == MCP_ADMIN_PATH

        try:
            user = await _authenticate_from_scope(scope, self._rbac, self._oidc_provider)
        except AuthenticationError as exc:
            logger.info("mcp_mount auth failed", extra={"path": scope["path"]})
            await _send_json(send, 401, {"error": "unauthorized", "detail": str(exc)})
            return
        except Exception:
            logger.exception("mcp_mount authentication error")
            await _send_json(
                send, 401, {"error": "unauthorized", "detail": "authentication failed"}
            )
            return

        if is_admin_path and user.role != Role.ADMIN:
            # 403 before any FastMCP app exists -- a non-admin caller never
            # sees even a filtered admin tool list (§11.5 disclosure rule).
            await _send_json(send, 403, {"error": "forbidden", "detail": "admin role required"})
            return

        if not is_feature_enabled(MCP_V2_FLAG, distinct_id=str(user.organization_id)):
            await _send_json(send, 404, {"error": "not_found"})
            return

        ctx = _build_tool_context(scope, user)

        if is_admin_path:
            server = build_admin_server(self._service_factory.admin_tools(ctx))
        else:
            server = build_user_server(self._service_factory.user_tools(ctx))

        # FastMCP's Starlette app has a single internal route at its
        # `streamable_http_path` (default "/mcp", left at the default for
        # both servers). Rewrite the outer "/mcp/admin" path down to "/mcp"
        # for that inner app only -- the outer scope/path is what the
        # admin-vs-user routing decision above already used.
        if is_admin_path:
            scope = {**scope, "path": MCP_USER_PATH, "raw_path": MCP_USER_PATH.encode()}

        streamable_app = server.streamable_http_app()
        # A fresh FastMCP is minted per request (see class docstring), so
        # its StreamableHTTPSessionManager task group has never been
        # started -- normally an ASGI `lifespan` event does this once at
        # process startup, but there is no persistent lifespan for a
        # per-request server. `run()` starts/stops it around this single
        # call, matching `stateless_http=True`'s per-request contract.
        async with server.session_manager.run():
            await streamable_app(scope, receive, send)
