"""penguin-aaa integration for WaddleAI.

Provides OIDC token issuance, validation, and scope-based authorization.
"""

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt as _jwt
from penguin_aaa.authn import (
    Claims,
    OIDCProvider,
    OIDCProviderConfig,
    OIDCRelyingParty,
    OIDCRPConfig,
)
from penguin_aaa.authz.rbac import RBACEnforcer
from penguin_aaa.authz.rbac import Role as AAARole
from penguin_aaa.crypto.keystore import FileKeyStore, KeyStore, MemoryKeyStore

from shared.auth.jwks_verifier import JWKSVerificationError, JWKSVerifier, create_jwks_verifier
from shared.auth.rbac import ROLE_PERMISSIONS, AuthenticationError, Role, UserContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

# Environments where an ephemeral, per-process signing keystore is tolerable.
# Mirrors services/management/{asgi,wsgi}.py's own FLASK_ENV switch (default
# "production" -- unset means production, fail closed), so this guard always
# agrees with which Config class the service actually booted with.
_DEV_ENVIRONMENTS = frozenset({"development", "testing"})

# Opt-in: raise instead of warn when falling back to an ephemeral keystore
# outside development/testing. Off by default so this function stays a
# no-op for every *existing* caller. services/management opts in (see
# app/__init__.py) because it is the service responsible for publishing a
# JWKS other validators rely on (H3's /.well-known/jwks.json), where a
# per-replica keypair is a real outage, not a tolerable default. The proxy
# (H4) now validates against that published JWKS via JWKSOIDCRelyingParty
# below rather than this process's own keystore, but still calls
# create_oidc_provider() to mint its WADDLEAI_STUB_UPSTREAM=1 contract-test
# tokens -- see main.py's ProxyServer.startup()/_seed_contract_test_data().
_STRICT_KEYSTORE_ENV_VAR = "OIDC_REQUIRE_DURABLE_KEYSTORE"


class OIDCKeystoreMisconfiguredError(RuntimeError):
    """Raised when create_oidc_provider() cannot build a durable signing keystore.

    ``MemoryKeyStore`` generates a fresh RSA keypair per process and never
    persists it. In a single-process dev/test run that is harmless; outside
    one, it silently desyncs every replica's (and every restart's) signing
    key from the JWKS this service publishes at ``/.well-known/jwks.json``,
    so a token signed by one pod fails verification everywhere the pod's
    key was never published (regression: headless-auth H3).
    """


def _running_in_dev_environment() -> bool:
    """True when ``FLASK_ENV`` names a non-production environment."""
    return os.getenv("FLASK_ENV", "production").lower() in _DEV_ENVIRONMENTS


def _durable_keystore_required() -> bool:
    """True when the caller has opted into refusing an ephemeral keystore fallback."""
    return os.getenv(_STRICT_KEYSTORE_ENV_VAR, "false").lower() == "true"


def create_oidc_provider() -> OIDCProvider:
    """Create OIDC token provider for WaddleAI.

    Outside development/testing, the signing keystore should be durable and
    identical across every replica -- ``SIGNING_KEY_FILE`` pointing at a path
    every pod mounts from the same Secret/PVC is the only supported option
    today. A caller that sets ``OIDC_REQUIRE_DURABLE_KEYSTORE=true`` (see
    ``services/management/app/__init__.py``, the service this task makes
    responsible for publishing a JWKS other validators rely on) gets a hard
    failure instead of a fallback: an ephemeral, per-process keypair there is
    a deployment misconfiguration that surfaces later as intermittent 401s
    spread across replicas, not a condition to discover in production (see
    ``OIDCKeystoreMisconfiguredError``). Every other caller gets a loud
    warning and keeps working exactly as before.
    """
    issuer = os.getenv("OIDC_ISSUER_URL", "https://waddleai.localhost.local")
    config = OIDCProviderConfig(
        issuer=issuer,
        audiences=[os.getenv("OIDC_CLIENT_ID", "waddleai-api")],
        algorithm="RS256",
        token_ttl=timedelta(hours=int(os.getenv("TOKEN_TTL_HOURS", "1"))),
        refresh_ttl=timedelta(days=int(os.getenv("REFRESH_TTL_DAYS", "30"))),
    )

    key_file = os.getenv("SIGNING_KEY_FILE")
    keystore: KeyStore
    if key_file and os.path.exists(key_file):
        keystore = FileKeyStore(path=Path(key_file))
    elif _running_in_dev_environment():
        keystore = MemoryKeyStore(algorithm="RS256")
    elif not _durable_keystore_required():
        flask_env = os.getenv("FLASK_ENV", "production")
        logger.warning(
            "OIDC signing keystore is an ephemeral MemoryKeyStore outside a "
            "development/testing environment (FLASK_ENV=%r) -- each replica "
            "mints its own keypair, so a token signed by one pod can fail "
            "verification anywhere that pod's key was never published. Set "
            "SIGNING_KEY_FILE to a path backed by a Secret/PVC shared by "
            "every replica.",
            flask_env,
        )
        keystore = MemoryKeyStore(algorithm="RS256")
    else:
        flask_env = os.getenv("FLASK_ENV", "production")
        raise OIDCKeystoreMisconfiguredError(
            "SIGNING_KEY_FILE is not set (or its path does not exist), "
            f"FLASK_ENV={flask_env!r} is not development/testing, and "
            f"{_STRICT_KEYSTORE_ENV_VAR}=true. Refusing to fall back to an "
            "in-memory signing key: every replica would mint its own "
            "keypair, and the JWKS this service publishes would never match "
            "the key that actually signed a given token. Set "
            "SIGNING_KEY_FILE to a path backed by a Secret/PVC shared by "
            "every replica, or set FLASK_ENV=development for a "
            "single-process local run."
        )

    return OIDCProvider(config, keystore)


def rotate_signing_key(provider: OIDCProvider) -> None:
    """Rotate *provider*'s active signing key.

    The keystore (``MemoryKeyStore``/``FileKeyStore``, both cap at 3 keys)
    retains prior keys in its published JWKS until they age out, so
    validators mid-rotation still accept tokens signed moments earlier under
    the old ``kid``. ``provider._keystore`` is accessed directly because
    ``OIDCProvider`` does not expose keystore mutation publicly -- the same
    precedent ``verify_token()`` below already relies on to fetch the
    signing key for local verification.
    """
    provider._keystore.rotate_key()


def active_signing_kid(provider: OIDCProvider) -> str:
    """Return the ``kid`` of *provider*'s current signing key.

    Same private-attribute precedent as ``rotate_signing_key()``/
    ``verify_token()`` above -- centralised here so callers (e.g. the
    signing-key rotation route) never need to reach into
    ``provider._keystore`` themselves.
    """
    _, kid = provider._keystore.get_signing_key()
    return kid


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


def verify_token_via_jwks(token: str, verifier: JWKSVerifier) -> UserContext:
    """Verify a WaddleAI-issued RS256 token via a published JWKS; return UserContext.

    The JWKS-backed counterpart to ``verify_token()`` above -- same
    ``claims_to_user_context`` conversion, so every downstream consumer
    (RBAC checks, audit logging, MCP tool context) is identical regardless
    of whether the signing key was resolved from this process's own
    keystore or fetched from an external issuer's JWKS by ``kid``.

    Raises:
        AuthenticationError: The token failed JWKS-backed verification for
            any reason (bad signature, expired, wrong issuer/audience,
            unknown ``kid``, or the JWKS endpoint being unreachable with
            nothing usable cached) -- see JWKSVerifier.verify_token.
    """
    try:
        claims = verifier.verify_token(token)
    except JWKSVerificationError as exc:
        raise AuthenticationError(str(exc)) from exc
    return claims_to_user_context(claims)


class JWKSOIDCRelyingParty:
    """Relying party validating WaddleAI RS256 tokens against a published JWKS.

    Headless-auth H4: replaces the proxy's former ``LocalOIDCRelyingParty``
    (which validated only against this same process's own keystore, and so
    silently desynced across replicas without a shared ``SIGNING_KEY_FILE``
    mount). This relying party instead fetches the issuer's *public* keys
    from its JWKS endpoint (management's ``/.well-known/jwks.json``, H3) and
    selects the verification key by the token's ``kid`` header -- any
    process holding the private key can issue a token this validates,
    without ever sharing key material. See ``shared.auth.jwks_verifier``
    for the caching/rotation/fail-closed contract.

    Gives ``penguin_aaa.middleware.asgi.OIDCAuthMiddleware`` (which requires
    an object exposing ``async def verify_token(raw_token) -> Claims``) a
    relying party returning the same claims-dict shape
    ``LocalOIDCRelyingParty`` did, so ``AuditMiddleware``/``get_current_user``
    ``.get("sub")``-style consumers are unaffected by the swap.
    """

    def __init__(self, verifier: JWKSVerifier) -> None:
        """Bind this relying party to *verifier* (see create_jwks_verifier())."""
        self._verifier = verifier

    async def verify_token(self, raw_token: str) -> dict:
        """Validate *raw_token* against the JWKS; raises AuthenticationError on failure.

        ``JWKSVerifier.verify_token`` does blocking network I/O on a JWKS
        cache miss (``jwt.PyJWKClient`` uses ``urllib``, not an async HTTP
        client) -- offloaded to a worker thread so a cache-cold request
        never blocks the event loop from servicing other connections.
        """
        user_context = await asyncio.to_thread(verify_token_via_jwks, raw_token, self._verifier)
        return user_context_to_claims_dict(user_context)


def create_jwks_oidc_rp(verifier: JWKSVerifier | None = None) -> JWKSOIDCRelyingParty:
    """Create the relying party used by the proxy's ASGI OIDC middleware.

    *verifier* defaults to ``create_jwks_verifier()`` (env-configured, real
    HTTP JWKS fetch); callers with no live external JWKS endpoint to fetch
    from (the contract-test harness -- see main.py's ProxyServer.startup())
    may inject one built around an in-process signing-key resolver instead.
    """
    return JWKSOIDCRelyingParty(verifier or create_jwks_verifier())


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
    scopes: list[str] = []
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
        int(raw_api_key_id)
        if raw_api_key_id is not None and str(raw_api_key_id).isdigit()
        else None
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
    scopes: list[str] = []
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
        int(raw_api_key_id)
        if raw_api_key_id is not None and str(raw_api_key_id).isdigit()
        else None
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
        raise AuthenticationError("Token has expired")  # noqa: B904 -- re-raise without chaining is existing control flow, out of scope for this pass
    except _jwt.InvalidTokenError as exc:
        raise AuthenticationError(f"Invalid token: {exc}")  # noqa: B904 -- re-raise without chaining is existing control flow, out of scope for this pass

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
