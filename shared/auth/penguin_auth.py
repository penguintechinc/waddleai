"""
penguin-aaa integration for WaddleAI
Provides OIDC token issuance, validation, and scope-based authorization
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import List

import jwt as _jwt
from penguin_aaa.authn import Claims, OIDCProvider, OIDCProviderConfig, OIDCRelyingParty, OIDCRPConfig
from penguin_aaa.authz.rbac import RBACEnforcer
from penguin_aaa.authz.rbac import Role as AAARole
from penguin_aaa.crypto.keystore import FileKeyStore, MemoryKeyStore

from shared.auth.rbac import ROLE_PERMISSIONS, AuthenticationError, Role, UserContext

# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def create_oidc_provider() -> OIDCProvider:
    """Create OIDC token provider for WaddleAI."""
    issuer = os.getenv("OIDC_ISSUER_URL", "https://waddleai.localhost.local")
    config = OIDCProviderConfig(
        issuer=issuer,
        audiences=[os.getenv("OIDC_CLIENT_ID", "waddleai-api")],
        algorithm="RS256",
        token_ttl=timedelta(hours=int(os.getenv("TOKEN_TTL_HOURS", "1"))),
        refresh_ttl=timedelta(days=int(os.getenv("REFRESH_TTL_DAYS", "30"))),
    )

    key_file = os.getenv("SIGNING_KEY_FILE")
    if key_file and os.path.exists(key_file):
        keystore = FileKeyStore(path=Path(key_file))
    else:
        keystore = MemoryKeyStore(algorithm="RS256")

    return OIDCProvider(config, keystore)


def create_oidc_rp() -> OIDCRelyingParty:
    """Create OIDC relying party for token validation."""
    config = OIDCRPConfig(
        issuer_url=os.getenv("OIDC_ISSUER_URL", "https://waddleai.localhost.local"),
        client_id=os.getenv("OIDC_CLIENT_ID", "waddleai-api"),
        client_secret=os.getenv("OIDC_CLIENT_SECRET", ""),
        redirect_url=os.getenv(
            "OIDC_REDIRECT_URL",
            "https://waddleai.localhost.local/auth/callback",
        ),
        algorithms=["RS256"],
    )
    return OIDCRelyingParty(config)


class LocalOIDCRelyingParty:
    """Relying party for WaddleAI's own self-issued RS256 tokens.

    WaddleAI's proxy is self-contained: ``create_oidc_provider()`` builds an
    in-memory/file keystore that both issues (``issue_token``) and validates
    (``verify_token``) tokens for this same process -- there is no external
    OIDC issuer and no published JWKS endpoint. ``penguin_aaa.authn.oidc_rp
    .OIDCRelyingParty`` validates tokens by fetching JWKS from an external
    issuer's discovery document over HTTP, which cannot work against a
    self-issued token: there is nothing to discover. This class gives
    ``penguin_aaa.middleware.asgi.OIDCAuthMiddleware`` (which requires an
    object exposing ``async def verify_token(raw_token) -> Claims``) a
    relying party that validates self-issued tokens the same way
    ``get_current_user()``'s Bearer-JWT path already does, against the same
    provider keystore -- no network call, no external issuer.

    ``create_oidc_rp()``/``OIDCRelyingParty`` remain available for a future
    external-IdP/SSO integration (Pro tier, enterprise-only, license-gated)
    but are not wired into the proxy's ASGI middleware.
    """

    def __init__(self, provider: OIDCProvider) -> None:
        self._provider = provider

    async def verify_token(self, raw_token: str) -> dict:
        """Validate a self-issued token; raises AuthenticationError on failure.

        Returns a full claims dict (see user_context_to_claims_dict) carrying
        the complete user context, enabling downstream code to reconstruct
        the UserContext without re-running token verification.
        """
        user_context = verify_token(raw_token, self._provider)
        return user_context_to_claims_dict(user_context)


def create_local_oidc_rp(provider: OIDCProvider) -> LocalOIDCRelyingParty:
    """Create the relying party used by the proxy's ASGI OIDC middleware."""
    return LocalOIDCRelyingParty(provider)


def build_rbac_enforcer() -> RBACEnforcer:
    """Build penguin-aaa RBACEnforcer from WaddleAI role/permission mappings."""
    enforcer = RBACEnforcer()
    for role, perms in ROLE_PERMISSIONS.items():
        scope_list = [p.value for p in perms]
        enforcer.register(AAARole(name=role.value, scopes=scope_list))
    return enforcer


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


def user_context_to_claims(user_context: UserContext) -> Claims:
    """Convert WaddleAI UserContext to penguin-aaa Claims."""
    scopes: List[str] = []
    if isinstance(user_context.permissions, set):
        scopes = [p.value if hasattr(p, "value") else str(p) for p in user_context.permissions]
    elif isinstance(user_context.permissions, list):
        scopes = [str(p) for p in user_context.permissions]

    return Claims(
        sub=str(user_context.user_id),
        iss=os.getenv("OIDC_ISSUER_URL", "https://waddleai.localhost.local"),
        aud=[os.getenv("OIDC_CLIENT_ID", "waddleai-api")],
        iat=datetime.now(UTC),
        exp=datetime.now(UTC) + timedelta(hours=24),
        scope=scopes,
        roles=[user_context.role.value],
        tenant=str(user_context.organization_id),
        teams=[str(org) for org in (user_context.managed_orgs or [])],
        ext={
            "username": user_context.username,
            "api_key_id": (
                str(user_context.api_key_id) if user_context.api_key_id is not None else None
            ),
        },
    )


def claims_to_user_context(claims: Claims) -> UserContext:
    """Convert penguin-aaa Claims back to WaddleAI UserContext for backward compat."""
    role_str = claims.roles[0] if claims.roles else "user"
    try:
        role = Role(role_str)
    except ValueError:
        role = Role.USER

    # Rebuild permission set from scopes
    permissions: set = set()
    for scope in claims.scope or []:
        permissions.add(scope)

    raw_api_key_id = claims.ext.get("api_key_id") if claims.ext else None
    api_key_id = (
        int(raw_api_key_id) if raw_api_key_id is not None and str(raw_api_key_id).isdigit() else None
    )

    return UserContext(
        user_id=int(claims.sub) if claims.sub.isdigit() else 0,
        username=(claims.ext.get("username", claims.sub) if claims.ext else claims.sub),
        role=role,
        organization_id=(int(claims.tenant) if claims.tenant and claims.tenant.isdigit() else 0),
        managed_orgs=[int(t) for t in (claims.teams or []) if t.isdigit()],
        permissions=permissions,
        api_key_id=api_key_id,
    )


def user_context_to_claims_dict(uc: UserContext) -> dict:
    """Convert WaddleAI UserContext to a plain JSON-safe claims dict.

    This dict carries the full context and is safe for AuditMiddleware
    (which reads scope["state"]["claims"] as a plain dict and calls .get()).
    """
    scopes: List[str] = []
    if isinstance(uc.permissions, set):
        scopes = [p.value if hasattr(p, "value") else str(p) for p in uc.permissions]
    elif isinstance(uc.permissions, list):
        scopes = [p.value if hasattr(p, "value") else str(p) for p in uc.permissions]

    return {
        "sub": str(uc.user_id),
        "username": uc.username,
        "roles": [uc.role.value],
        "scope": scopes,
        "tenant": str(uc.organization_id),
        "teams": [str(o) for o in (uc.managed_orgs or [])],
        "api_key_id": (str(uc.api_key_id) if uc.api_key_id is not None else None),
    }


def claims_dict_to_user_context(d: dict) -> UserContext:
    """Rebuild WaddleAI UserContext from a plain claims dict.

    Inverse of user_context_to_claims_dict; used by get_current_user
    to reconstruct the full context from middleware-populated claims
    without re-running bcrypt/verify.
    """
    role_str = (d.get("roles") or ["user"])[0]
    try:
        role = Role(role_str)
    except ValueError:
        role = Role.USER

    # Rebuild permission set from scopes
    permissions: set = set()
    for scope in d.get("scope", []):
        permissions.add(scope)

    raw_api_key_id = d.get("api_key_id")
    api_key_id = (
        int(raw_api_key_id) if raw_api_key_id is not None and str(raw_api_key_id).isdigit() else None
    )

    return UserContext(
        user_id=int(d.get("sub", "0")) if str(d.get("sub", "0")).isdigit() else 0,
        username=d.get("username", d.get("sub", "unknown")),
        role=role,
        organization_id=(int(d.get("tenant", "0")) if str(d.get("tenant", "0")).isdigit() else 0),
        managed_orgs=[int(t) for t in (d.get("teams", [])) if str(t).isdigit()],
        permissions=permissions,
        api_key_id=api_key_id,
    )


def issue_token(user_context: UserContext, provider: OIDCProvider) -> str:
    """Issue RS256 JWT token for a WaddleAI user."""
    claims = user_context_to_claims(user_context)
    token_set = provider.issue_token_set(claims)
    return token_set.access_token


def verify_token(token: str, provider: OIDCProvider) -> UserContext:
    """Verify a WaddleAI-issued RS256 token and return UserContext."""
    private_key, _kid = provider._keystore.get_signing_key()
    public_key = private_key.public_key()

    issuer = os.getenv("OIDC_ISSUER_URL", "https://waddleai.localhost.local")
    audience = os.getenv("OIDC_CLIENT_ID", "waddleai-api")

    try:
        payload = _jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=[audience],
            issuer=issuer,
        )
    except _jwt.ExpiredSignatureError:
        raise AuthenticationError("Token has expired")
    except _jwt.InvalidTokenError as exc:
        raise AuthenticationError(f"Invalid token: {exc}")

    claims = Claims(
        sub=payload["sub"],
        iss=payload["iss"],
        aud=payload["aud"] if isinstance(payload["aud"], list) else [payload["aud"]],
        iat=datetime.fromtimestamp(payload["iat"], UTC),
        exp=datetime.fromtimestamp(payload["exp"], UTC),
        scope=payload.get("scope", []),
        roles=payload.get("roles", []),
        tenant=payload.get("tenant", "default"),
        teams=payload.get("teams", []),
        ext=payload.get("ext", {}),
    )
    return claims_to_user_context(claims)
