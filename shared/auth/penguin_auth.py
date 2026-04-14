"""
penguin-aaa integration for WaddleAI
Provides OIDC token issuance, validation, and scope-based authorization
"""

import os
from datetime import UTC, datetime, timedelta
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
        keystore = FileKeyStore(path=key_file)
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

    return UserContext(
        user_id=int(claims.sub) if claims.sub.isdigit() else 0,
        username=(claims.ext.get("username", claims.sub) if claims.ext else claims.sub),
        role=role,
        organization_id=(int(claims.tenant) if claims.tenant and claims.tenant.isdigit() else 0),
        managed_orgs=[int(t) for t in (claims.teams or []) if t.isdigit()],
        permissions=permissions,
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
