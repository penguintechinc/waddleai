"""Authenticated OpenAI-compatible Ray Serve app (AM11/AM16, spec 6.3 layer 2).

Ray token auth does not protect Serve HTTP ingress, so every request must carry
the per-deployment bearer key. The key is read from the mounted Secret file at
request time (mtime-cached) so rotation needs no redeploy. Fails closed: a
missing/unreadable/empty key file is a 401, never open.

Ray internals used (Ray 2.60.0): ``ray.serve.llm.build_openai_app(dict)`` and
``ray.llm._internal.serve.core.ingress.ingress.init`` (creates the ingress
FastAPI app inside ``make_fastapi_ingress``); the latter is briefly wrapped at
build time to add the middleware. Ray imports stay lazy.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ray import serve

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class BearerKeyMiddleware:
    """Pure-ASGI bearer check against ``<keys_dir>/<slug>`` (mtime-cached)."""

    def __init__(self, app: ASGIApp, slug: str, keys_dir: str = "/secrets/deployment-keys") -> None:
        """Bind the wrapped app to one deployment's key file."""
        if not slug or slug in (".", "..") or "/" in slug or "\\" in slug or "\x00" in slug:
            raise ValueError("invalid deployment slug")
        self._app = app
        self._path = Path(keys_dir) / slug
        self._cache: tuple[int, bytes] | None = None

    def _expected(self) -> bytes | None:
        """Return the current key bytes, or None if missing/unreadable/empty."""
        try:
            mtime = self._path.stat().st_mtime_ns
            if self._cache is None or self._cache[0] != mtime:
                self._cache = (mtime, self._path.read_bytes().strip())
        except OSError:
            self._cache = None
            return None
        return self._cache[1] or None

    def _authorized(self, scope: Scope) -> bool:
        """Constant-time compare of the presented bearer with the key file."""
        presented = b""
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                scheme, _, token = bytes(value).partition(b" ")
                if scheme.lower() == b"bearer":
                    presented = token.strip()
                break
        expected = self._expected()
        if expected is None or not presented:
            return False
        return hmac.compare_digest(presented, expected)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass lifespan through; 401/close every unauthenticated request."""
        kind = scope["type"]
        if kind == "lifespan" or (kind in ("http", "websocket") and self._authorized(scope)):
            await self._app(scope, receive, send)
        elif kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            body = b'{"error":{"message":"unauthorized","type":"invalid_api_key"}}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})


def build_authenticated_openai_app(
    llm_config: dict[str, Any], slug: str, keys_dir: str = "/secrets/deployment-keys"
) -> serve.Application:
    """Build ``ray.serve.llm`` OpenAI app with the bearer middleware on ingress."""
    from ray.llm._internal.serve.core.ingress import ingress as ingress_mod
    from ray.serve.llm import build_openai_app

    original_init = ingress_mod.init
    attached = False

    def init_with_auth() -> Any:
        nonlocal attached
        fastapi_app = original_init()
        # Added last => outermost: auth runs before every other middleware.
        fastapi_app.add_middleware(BearerKeyMiddleware, slug=slug, keys_dir=keys_dir)
        attached = True
        return fastapi_app

    ingress_mod.init = init_with_auth
    try:
        app = build_openai_app({"llm_configs": [llm_config]})
    finally:
        ingress_mod.init = original_init
    if not attached:
        raise RuntimeError("auth middleware was not attached to the Serve ingress app")
    return app
