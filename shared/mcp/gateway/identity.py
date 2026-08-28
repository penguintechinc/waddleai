"""Per-endpoint identity resolution — `shared` vs `per_user` (§11.4).

An `McpEndpoint` is configured with one org-wide `shared` credential, or
`per_user` — the real caller's own linked identity at the external
server, so the upstream applies its own permissions rather than a single
service-account's. This module decides, for one call from one
authenticated caller, *which* credential ``auth.py`` should produce:

* ``shared`` — always the endpoint's own configured credential (static
  header or OAuth2 client-credentials).
* ``per_user``, linked — the caller's stored, encrypted token, refreshed
  via ``auth.py`` if it's past (or near) expiry.
* ``per_user``, unlinked — no upstream call is made; the result is a
  structured "link your account" payload carrying the link URL, so a
  calling agent can surface it to the user instead of failing silently.
* ``per_user``, unattributed caller (no ``user_uuid`` at all — e.g. an
  org-wide service key) — falls back to a configured shared credential if
  the endpoint has one, else the tool is withheld.

Repository access is ``Protocol``-typed and injected, not a direct
SQLAlchemy import — this module runs inside the proxy's `/mcp` mount,
which does not import ``services/management/app/models_sqlalchemy.py``
(a separate deployable). Mirrors ``shared/mcp/tools.py``'s pattern: the
real backend (querying `mcp_user_links` via penguin-dal/SQLAlchemy) is
wired in by whichever service constructs the resolver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from shared.mcp.gateway.auth import (
    HeaderAuthConfig,
    OAuth2AuthCodeConfig,
    OAuth2ClientCredentialsConfig,
    OutboundAuth,
    TokenSet,
)
from shared.mcp.gateway.client import GatewayEndpointConfig

IDENTITY_SHARED = "shared"
IDENTITY_PER_USER = "per_user"

LINK_URL_TEMPLATE = "/api/v1/integrations/mcp-endpoints/{endpoint_id}/link"


class IdentityResolutionError(RuntimeError):
    """Raised for a resolver misconfiguration (e.g. unknown identity_mode)."""


@dataclass(slots=True, frozen=True)
class UserLinkRecord:
    """One caller's decrypted external-MCP credential for one endpoint.

    Decryption happens below the repository boundary (the real adapter
    decrypts ``access_token_enc``/``refresh_token_enc`` via
    ``shared.security.credential_encryption`` before handing this record
    back) — plaintext tokens exist only transiently, in memory, for the
    duration of one outbound call.
    """

    access_token: str
    refresh_token: str | None
    expires_at: float | None
    status: str  # "linked" | "expired" | "revoked"


@runtime_checkable
class McpUserLinkRepository(Protocol):
    """Read/write access to `mcp_user_links` (§13.1 migration 014)."""

    async def get_link(self, endpoint_id: int, user_uuid: str) -> UserLinkRecord | None:
        """Return the caller's link for this endpoint, or ``None`` if never linked."""
        ...

    async def save_link(self, endpoint_id: int, user_uuid: str, token: TokenSet) -> None:
        """Persist a (re)issued token for this caller+endpoint (encrypted at rest)."""
        ...

    async def mark_status(self, endpoint_id: int, user_uuid: str, status: str) -> None:
        """Update link status (e.g. ``"revoked"``) without touching the token fields."""
        ...


@dataclass(slots=True, frozen=True)
class ResolvedCredential:
    """Headers ready for ``GatewayClient``, plus which caller identity produced them."""

    headers: dict[str, str]
    identity_source: str  # "shared" | "per_user"


@dataclass(slots=True, frozen=True)
class LinkRequired:
    """Returned instead of a credential when a `per_user` caller hasn't linked yet.

    Carries the WebUI/CLI link URL so a calling agent can surface it to
    the user rather than the call failing opaquely (§11.4).
    """

    endpoint_id: int
    link_url: str
    reason: str = "unlinked"


@dataclass(slots=True, frozen=True)
class ToolWithheld:
    """Returned when no credential can be resolved and there is no fallback.

    Distinct from ``LinkRequired`` — this covers an unattributed caller
    (no ``user_uuid``) hitting a `per_user` endpoint with no shared
    fallback configured, not "you personally haven't linked yet".
    """

    endpoint_id: int
    reason: str


ResolutionResult = ResolvedCredential | LinkRequired | ToolWithheld


@dataclass(slots=True, frozen=True)
class EndpointAuthConfig:
    """The auth shape for one endpoint, bundling `auth_type` with its typed config.

    Exactly one of ``header``/``client_credentials``/``auth_code`` is set,
    matching ``auth_type``; a shared-fallback for a `per_user` endpoint
    reuses whichever of these is configured for the *shared* credential.
    """

    auth_type: str  # "none" | "header" | "oauth2_client_credentials" | "oauth2_auth_code"
    header: HeaderAuthConfig | None = None
    client_credentials: OAuth2ClientCredentialsConfig | None = None
    auth_code: OAuth2AuthCodeConfig | None = None


class IdentityResolver:
    """Resolves the outbound credential for one (endpoint, caller) pair (§11.4)."""

    def __init__(self, *, outbound_auth: OutboundAuth, user_links: McpUserLinkRepository) -> None:
        """Bind the OAuth2/header mechanics and the per-user link store this resolver uses."""
        self._auth = outbound_auth
        self._links = user_links

    async def resolve(
        self,
        endpoint: GatewayEndpointConfig,
        auth_config: EndpointAuthConfig,
        *,
        identity_mode: str,
        user_uuid: str | None,
        shared_fallback: bool = False,
    ) -> ResolutionResult:
        """Resolve headers for an outbound call to ``endpoint`` on behalf of ``user_uuid``.

        ``identity_mode``/``shared_fallback`` come from the persisted
        `McpEndpoint` row (``identity_mode``, plus an admin-set fallback
        flag carried in ``auth_config``-adjacent endpoint config) — kept
        as explicit parameters here rather than fields on
        ``GatewayEndpointConfig`` so this module stays agnostic of
        exactly which columns the caller sourced them from.
        """
        if identity_mode == IDENTITY_SHARED:
            headers = await self._shared_headers(endpoint.id, auth_config)
            return ResolvedCredential(headers=headers, identity_source=IDENTITY_SHARED)

        if identity_mode != IDENTITY_PER_USER:
            raise IdentityResolutionError(f"unknown identity_mode {identity_mode!r}")

        if user_uuid is None:
            if shared_fallback:
                headers = await self._shared_headers(endpoint.id, auth_config)
                return ResolvedCredential(headers=headers, identity_source=IDENTITY_SHARED)
            return ToolWithheld(endpoint_id=endpoint.id, reason="unattributed_caller_no_fallback")

        link = await self._links.get_link(endpoint.id, user_uuid)
        if link is None or link.status == "revoked":
            return LinkRequired(
                endpoint_id=endpoint.id,
                link_url=LINK_URL_TEMPLATE.format(endpoint_id=endpoint.id),
                reason="unlinked" if link is None else "revoked",
            )

        token = TokenSet(
            access_token=link.access_token,
            refresh_token=link.refresh_token,
            expires_at=link.expires_at,
        )
        if token.is_expired():
            if link.refresh_token is None or auth_config.auth_code is None:
                await self._links.mark_status(endpoint.id, user_uuid, "expired")
                return LinkRequired(
                    endpoint_id=endpoint.id,
                    link_url=LINK_URL_TEMPLATE.format(endpoint_id=endpoint.id),
                    reason="expired",
                )
            token = await self._refresh_user_token(
                endpoint.id, user_uuid, auth_config, link.refresh_token
            )

        return ResolvedCredential(headers=token.as_header(), identity_source=IDENTITY_PER_USER)

    async def _shared_headers(
        self, endpoint_id: int, auth_config: EndpointAuthConfig
    ) -> dict[str, str]:
        if auth_config.auth_type == "none":
            return {}
        if auth_config.auth_type == "header":
            if auth_config.header is None:
                raise IdentityResolutionError("auth_type=header requires auth_config.header")
            return OutboundAuth.header_auth(auth_config.header)
        if auth_config.auth_type == "oauth2_client_credentials":
            if auth_config.client_credentials is None:
                raise IdentityResolutionError(
                    "auth_type=oauth2_client_credentials requires auth_config.client_credentials"
                )
            token = await self._auth.client_credentials_token(
                endpoint_id, auth_config.client_credentials
            )
            return token.as_header()
        raise IdentityResolutionError(
            f"auth_type {auth_config.auth_type!r} has no shared-credential path"
        )

    async def _refresh_user_token(
        self, endpoint_id: int, user_uuid: str, auth_config: EndpointAuthConfig, refresh_token: str
    ) -> TokenSet:
        if auth_config.auth_code is None:
            raise IdentityResolutionError("refresh requires auth_config.auth_code")
        cfg = auth_config.auth_code
        client_id = cfg.client_id
        client_secret = cfg.client_secret or ""
        if client_id is None:
            raise IdentityResolutionError("auth_code config missing client_id for refresh")
        refreshed = await self._auth.refresh(
            cfg, client_id=client_id, client_secret=client_secret, refresh_token=refresh_token
        )
        await self._links.save_link(endpoint_id, user_uuid, refreshed)
        return refreshed
