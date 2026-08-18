"""Minimal OAuth2 authorization-server test double (§11.4 gateway auth tests).

Not a general-purpose OAuth2 implementation — just enough real wire
surface for ``shared/mcp/gateway/auth.py`` to exercise genuine protocol
behavior instead of mocked HTTP responses:

* ``POST /register`` — dynamic client registration (RFC 7591), enough to
  return a ``client_id``/``client_secret`` pair.
* ``POST /token`` — ``client_credentials``, ``authorization_code``
  (single-use, pre-seeded since there is no real browser in a test), and
  ``refresh_token`` (rotates the refresh token, so reuse-after-rotation is
  observable) grants.

Client authentication accepts both HTTP Basic (authlib's default) and
body-embedded ``client_id``/``client_secret`` (RFC 6749 §2.3.1 allows
either).
"""

from __future__ import annotations

import base64
import json
import secrets
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

ASGIApp = Callable[
    [dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]
]


@dataclass(slots=True)
class MockOAuth2Server:
    """Stateful ASGI OAuth2 authorization server double.

    Construct with ``seed_client`` pre-populated for a pre-registered
    client-credentials flow, or leave it empty and drive registration
    through ``POST /register`` for the DCR + authorization-code flow.
    """

    expires_in: int = 3600
    issue_refresh_token: bool = True
    _clients: dict[str, str] = field(default_factory=dict)  # client_id -> client_secret
    _auth_codes: dict[str, str] = field(default_factory=dict)  # code -> client_id, single-use
    _refresh_tokens: dict[str, str] = field(default_factory=dict)  # refresh_token -> client_id
    token_requests: list[dict[str, str]] = field(default_factory=list)
    register_requests: list[dict] = field(default_factory=list)

    def seed_client(self, client_id: str, client_secret: str) -> None:
        """Pre-register a client (for a client-credentials-only test)."""
        self._clients[client_id] = client_secret

    def seed_authorization_code(self, code: str, client_id: str) -> None:
        """Pre-seed a single-use authorization code (stands in for a browser consent flow)."""
        self._auth_codes[code] = client_id

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        """ASGI entry point — routes ``POST /register`` and ``POST /token``."""
        if scope.get("type") != "http":
            await _send_json(send, 404, {"error": "not_found"})
            return

        body = await _read_body(receive)
        path = scope.get("path", "")
        method = scope.get("method", "")

        if method == "POST" and path == "/register":
            await self._handle_register(body, send)
        elif method == "POST" and path == "/token":
            await self._handle_token(body, scope, send)
        else:
            await _send_json(send, 404, {"error": "not_found"})

    async def _handle_register(self, body: bytes, send: Callable) -> None:
        data = json.loads(body or b"{}")
        self.register_requests.append(data)
        client_id = f"dcr-{secrets.token_hex(8)}"
        client_secret = secrets.token_hex(16)
        self._clients[client_id] = client_secret
        await _send_json(
            send,
            201,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uris": data.get("redirect_uris", []),
            },
        )

    async def _handle_token(self, body: bytes, scope: dict, send: Callable) -> None:
        form = urllib.parse.parse_qs(body.decode("utf-8"))
        data = {k: v[0] for k, v in form.items() if v}
        self.token_requests.append(data)

        client_id, client_secret = self._extract_client_auth(data, scope)
        if client_id not in self._clients or self._clients[client_id] != client_secret:
            await _send_json(send, 401, {"error": "invalid_client"})
            return

        grant_type = data.get("grant_type")
        if grant_type == "client_credentials":
            await _send_json(send, 200, self._issue_token(client_id))
            return
        if grant_type == "authorization_code":
            code = data.get("code", "")
            if self._auth_codes.pop(code, None) != client_id:
                await _send_json(send, 400, {"error": "invalid_grant"})
                return
            await _send_json(send, 200, self._issue_token(client_id))
            return
        if grant_type == "refresh_token":
            refresh_token = data.get("refresh_token", "")
            if self._refresh_tokens.pop(refresh_token, None) != client_id:
                await _send_json(send, 400, {"error": "invalid_grant"})
                return
            await _send_json(send, 200, self._issue_token(client_id))
            return
        await _send_json(send, 400, {"error": "unsupported_grant_type"})

    def _issue_token(self, client_id: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "access_token": f"at-{secrets.token_hex(12)}",
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "issued_at": time.time(),
        }
        if self.issue_refresh_token:
            refresh_token = f"rt-{secrets.token_hex(12)}"
            self._refresh_tokens[refresh_token] = client_id
            payload["refresh_token"] = refresh_token
        return payload

    def _extract_client_auth(
        self, data: dict[str, str], scope: dict
    ) -> tuple[str | None, str | None]:
        if "client_id" in data:
            return data["client_id"], data.get("client_secret", "")
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        authorization = headers.get("authorization", "")
        if authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(authorization[len("Basic ") :]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return None, None
            client_id, _, client_secret = decoded.partition(":")
            return client_id, client_secret
        return None, None


async def _read_body(receive: Callable) -> bytes:
    body = b""
    more_body = True
    while more_body:
        message = await receive()
        body += message.get("body", b"")
        more_body = message.get("more_body", False)
    return body


async def _send_json(send: Callable, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})
