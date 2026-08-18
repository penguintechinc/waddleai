"""Outbound gateway auth — static header + OAuth2 client-credentials & auth-code/DCR (§11.4).

Tokens are minted, cached, and refreshed **here** (and, for per-user
tokens, persisted encrypted by ``identity.py``/the Management API) and
are **never returned to an MCP client** — every method on
``OutboundAuth`` returns a ``TokenSet``/header dict destined for
``GatewayClient``'s outbound request only, never a tool result.

Three outbound-auth shapes, all admin-configured on an ``McpEndpoint``
(§13.1 migration 014 ``auth_type``):

* ``header`` — a static header (``Authorization: Bearer <token>``, or a
  custom API-key header) — no network call, just a lookup.
* ``oauth2_client_credentials`` — M2M token, cached in Valkey per
  endpoint, refreshed ahead of expiry.
* ``oauth2_auth_code`` — the MCP-spec authorization-code flow with
  dynamic client registration (RFC 7591); this module owns the OAuth2
  protocol mechanics (DCR, authorization-URL construction, code exchange,
  refresh) — *whose* token applies to a given call (shared vs per-user,
  and persistence of the per-user refresh token) is ``identity.py``'s job.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client

# Refresh a bit before actual expiry so a call in flight doesn't race a
# token that expires mid-request.
TOKEN_EXPIRY_LEEWAY_SECONDS = 30
CACHE_KEY_PREFIX = "waddleai:mcp_gateway:token"

OAuth2ClientFactory = Callable[..., AsyncOAuth2Client]
HttpClientFactory = Callable[..., httpx.AsyncClient]


class OutboundAuthError(RuntimeError):
    """Raised for outbound-auth configuration or provider failures.

    Message text must never include a raw token value — callers building
    error messages from a ``TokenSet`` must not interpolate
    ``access_token``/``refresh_token`` into this exception.
    """


@dataclass(slots=True, frozen=True)
class TokenSet:
    """A resolved outbound credential. Never serialized back to an MCP client."""

    access_token: str
    token_type: str = "Bearer"  # noqa: S105 -- an RFC 6749 token_type value ("Bearer"), not a secret
    refresh_token: str | None = None
    expires_at: float | None = None  # epoch seconds; None = no expiry info

    def is_expired(self, *, leeway_seconds: int = TOKEN_EXPIRY_LEEWAY_SECONDS) -> bool:
        """True if this token is expired, or within ``leeway_seconds`` of expiring."""
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - leeway_seconds)

    def as_header(self) -> dict[str, str]:
        """Render this token as an ``Authorization`` header for an outbound request."""
        return {"Authorization": f"{self.token_type} {self.access_token}"}


@dataclass(slots=True, frozen=True)
class HeaderAuthConfig:
    """Static-header outbound auth (``auth_type="header"``)."""

    header_name: str = "Authorization"
    header_value: str = ""


@dataclass(slots=True, frozen=True)
class OAuth2ClientCredentialsConfig:
    """M2M OAuth2 outbound auth (``auth_type="oauth2_client_credentials"``)."""

    token_url: str
    client_id: str
    client_secret: str
    scope: str | None = None


@dataclass(slots=True, frozen=True)
class OAuth2AuthCodeConfig:
    """Authorization-code + DCR outbound auth (``auth_type="oauth2_auth_code"``)."""

    authorization_endpoint: str
    token_endpoint: str
    redirect_uri: str
    registration_endpoint: str | None = None
    scope: str | None = None
    # Pre-registered client, when the upstream doesn't support DCR.
    client_id: str | None = None
    client_secret: str | None = None


class TokenCache:
    """Thin Valkey wrapper for outbound OAuth2 tokens, keyed by endpoint (+ user).

    Injected, not a module-level singleton — same DI shape as
    ``shared/utils/token_limiter.py``'s ``valkey`` constructor argument,
    so tests supply an in-memory double instead of a live Valkey.
    """

    def __init__(self, valkey: Any) -> None:
        """Wrap an async Valkey/redis client exposing get/set/setex/delete."""
        self._valkey = valkey

    @staticmethod
    def _key(endpoint_id: int, user_uuid: str | None) -> str:
        if user_uuid is None:
            return f"{CACHE_KEY_PREFIX}:{endpoint_id}"
        return f"{CACHE_KEY_PREFIX}:{endpoint_id}:{user_uuid}"

    async def get(self, endpoint_id: int, user_uuid: str | None = None) -> TokenSet | None:
        """Return the cached token for this endpoint (+user), or ``None`` if absent."""
        raw = await self._valkey.get(self._key(endpoint_id, user_uuid))
        if not raw:
            return None
        data = json.loads(raw)
        return TokenSet(**data)

    async def set(self, endpoint_id: int, token: TokenSet, *, user_uuid: str | None = None) -> None:
        """Cache ``token``, expiring the Valkey entry alongside the token's own expiry."""
        payload = json.dumps(
            {
                "access_token": token.access_token,
                "token_type": token.token_type,
                "refresh_token": token.refresh_token,
                "expires_at": token.expires_at,
            }
        )
        key = self._key(endpoint_id, user_uuid)
        if token.expires_at is not None:
            ttl = max(1, int(token.expires_at - time.time()))
            await self._valkey.setex(key, ttl, payload)
        else:
            await self._valkey.set(key, payload)

    async def delete(self, endpoint_id: int, user_uuid: str | None = None) -> None:
        """Evict a cached token, e.g. after a refresh failure."""
        await self._valkey.delete(self._key(endpoint_id, user_uuid))


class OutboundAuth:
    """Resolves outbound OAuth2 credentials for ``GatewayClient`` requests (§11.4).

    Static-header auth needs no state and is a plain function
    (``header_auth``); OAuth2 needs an HTTP round trip (and, for
    client-credentials, a cache) so it lives on this instance.
    """

    def __init__(
        self,
        *,
        cache: TokenCache | None = None,
        oauth2_client_factory: OAuth2ClientFactory | None = None,
        http_client_factory: HttpClientFactory | None = None,
    ) -> None:
        """Bind optional token caching and injectable HTTP client factories (for tests)."""
        self._cache = cache
        self._oauth2_client_factory = oauth2_client_factory or AsyncOAuth2Client
        self._http_client_factory = http_client_factory or httpx.AsyncClient

    @staticmethod
    def header_auth(config: HeaderAuthConfig) -> dict[str, str]:
        """Render a static-header ``auth_config`` as an outbound header dict."""
        if not config.header_value:
            raise OutboundAuthError("header auth_config is missing header_value")
        return {config.header_name: config.header_value}

    async def client_credentials_token(
        self, endpoint_id: int, config: OAuth2ClientCredentialsConfig
    ) -> TokenSet:
        """Fetch (or reuse a cached, unexpired) OAuth2 client-credentials token."""
        if self._cache is not None:
            cached = await self._cache.get(endpoint_id)
            if cached is not None and not cached.is_expired():
                return cached

        client = self._oauth2_client_factory(
            client_id=config.client_id, client_secret=config.client_secret
        )
        try:
            async with client:
                raw = await client.fetch_token(
                    config.token_url, grant_type="client_credentials", scope=config.scope
                )
        except Exception as exc:
            raise OutboundAuthError(
                f"client-credentials token fetch failed for endpoint {endpoint_id}"
            ) from exc

        token = _token_set_from_raw(raw)
        if self._cache is not None:
            await self._cache.set(endpoint_id, token)
        return token

    async def register_client(self, config: OAuth2AuthCodeConfig) -> tuple[str, str]:
        """Dynamic client registration (RFC 7591); returns ``(client_id, client_secret)``."""
        if not config.registration_endpoint:
            raise OutboundAuthError(
                "registration_endpoint is required for dynamic client registration"
            )

        async with self._http_client_factory() as http:
            try:
                response = await http.post(
                    config.registration_endpoint,
                    json={
                        "redirect_uris": [config.redirect_uri],
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "token_endpoint_auth_method": "client_secret_post",
                    },
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise OutboundAuthError("dynamic client registration failed") from exc

        data = response.json()
        client_id = data.get("client_id")
        if not client_id:
            raise OutboundAuthError("registration response missing client_id")
        return client_id, data.get("client_secret", "")

    def build_authorization_url(
        self, config: OAuth2AuthCodeConfig, *, client_id: str, state: str
    ) -> str:
        """Build the authorization URL a user's browser is redirected to (§11.4 link flow)."""
        client = self._oauth2_client_factory(
            client_id=client_id, redirect_uri=config.redirect_uri, scope=config.scope
        )
        url, _state = client.create_authorization_url(config.authorization_endpoint, state=state)
        return url

    async def exchange_code(
        self, config: OAuth2AuthCodeConfig, *, client_id: str, client_secret: str, code: str
    ) -> TokenSet:
        """Exchange an authorization code for an access/refresh token pair."""
        client = self._oauth2_client_factory(
            client_id=client_id, client_secret=client_secret, redirect_uri=config.redirect_uri
        )
        try:
            async with client:
                raw = await client.fetch_token(
                    config.token_endpoint, grant_type="authorization_code", code=code
                )
        except Exception as exc:
            raise OutboundAuthError("authorization-code exchange failed") from exc
        return _token_set_from_raw(raw)

    async def refresh(
        self,
        config: OAuth2AuthCodeConfig,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> TokenSet:
        """Refresh an expired (or soon-to-expire) per-user token."""
        client = self._oauth2_client_factory(client_id=client_id, client_secret=client_secret)
        try:
            async with client:
                raw = await client.refresh_token(config.token_endpoint, refresh_token=refresh_token)
        except Exception as exc:
            raise OutboundAuthError("token refresh failed") from exc
        return _token_set_from_raw(raw)


def _token_set_from_raw(raw: dict[str, Any]) -> TokenSet:
    """Build a ``TokenSet`` from an RFC 6749 token-endpoint JSON response."""
    expires_at: float | None = None
    if raw.get("expires_at") is not None:
        expires_at = float(raw["expires_at"])
    elif raw.get("expires_in") is not None:
        expires_at = time.time() + float(raw["expires_in"])
    return TokenSet(
        access_token=raw["access_token"],
        token_type=raw.get("token_type", "Bearer"),
        refresh_token=raw.get("refresh_token"),
        expires_at=expires_at,
    )
