"""WaddleAI Management API v1 - Authentication Endpoints."""

import asyncio
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache, wraps
from typing import Any

import jwt as _jwt
from passlib.hash import bcrypt
from penguin_aaa.authn import OIDCProvider, OIDCProviderConfig
from penguin_dal.db import DB
from quart import Response, g, jsonify, request
from quart_schema import security_scheme, tag, validate_request, validate_response

from shared.auth.penguin_auth import create_oidc_provider, issue_token
from shared.auth.penguin_auth import verify_token as _aaa_verify_token
from shared.auth.rbac import ROLE_PERMISSIONS, Permission, Role, UserContext

from ...extensions import db
from ...services.login_throttle import ThrottleDecision, account_key, get_login_throttle
from ...services.token_denylist import get_token_denylist
from . import api_v1_bp

logger = logging.getLogger(__name__)

_BEARER_AUTH: list[dict[str, list[str]]] = [{"bearerAuth": []}]

# House policy for bearer-token lifetime: one hour by default, twenty-four
# hours as the outer maximum an explicit caller may request. penguin-aaa's
# OIDCProviderConfig defaults max_token_ttl to 1h and raises at construction
# when token_ttl exceeds it, which is why every provider built here declares
# the 24h ceiling explicitly -- without it, TOKEN_TTL_HOURS=2 crashes the
# service at startup instead of issuing a 2h token.
_DEFAULT_TOKEN_TTL_HOURS = 1
_MAX_TOKEN_TTL_HOURS = 24

_GENERIC_LOGIN_FAILURE = "Invalid credentials"


def _db() -> DB:
    """Return the process-wide penguin-dal handle, narrowed away from ``None``.

    ``extensions.db`` is declared ``DB | None`` because it starts unset
    before ``init_db()`` runs at startup; every route below only executes
    after that point, so this narrows the type for mypy without adding any
    reachable failure mode (mirrors the same helper in ``fleet.py``).
    """
    if db is None:
        raise RuntimeError("database not initialized")
    return db


# ---------------------------------------------------------------------------
# OpenAPI request/response models.
#
# Request models deliberately make every field Optional (matching the dict
# .get() semantics the handlers already used) rather than schema-required --
# the handlers keep their own presence/message checks below, so switching to
# quart-schema's automatic 400 here would silently change the error message
# tests assert on. See openapi.py module docstring for the auth/full split
# these feed into.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LoginRequest:
    """Credentials for POST /api/v1/auth/login."""

    username: str | None = None
    password: str | None = None


@dataclass(slots=True)
class LoginUser:
    """User summary embedded in a successful login response."""

    id: int
    username: str
    email: str
    role: str
    organization_id: int


@dataclass(slots=True)
class LoginResponse:
    """Response body for a successful login."""

    access_token: str
    token_type: str
    expires_in: int
    user: LoginUser


@dataclass(slots=True)
class MessageResponse:
    """Generic `{"message": str}` envelope used by several auth endpoints."""

    message: str


@dataclass(slots=True)
class RefreshTokenResponse:
    """Response body for a successful token refresh."""

    access_token: str
    token_type: str
    expires_in: int


@dataclass(slots=True)
class VerifyUser:
    """User summary embedded in the auth-verify response."""

    id: int
    username: str
    role: str
    organization_id: int


@dataclass(slots=True)
class VerifyResponse:
    """Response body for GET /api/v1/auth/verify."""

    user: VerifyUser


@dataclass(slots=True)
class CurrentUserOrganization:
    """Organization summary embedded in the current-user response."""

    id: int
    name: str


@dataclass(slots=True)
class CurrentUserResponse:
    """Response body for GET /api/v1/auth/me."""

    id: int
    username: str
    email: str
    role: str
    organization: CurrentUserOrganization | None
    token_quota_daily: int | None
    token_quota_monthly: int | None
    enabled: bool
    created_at: str | None
    last_login_at: str | None


@dataclass(slots=True)
class ChangePasswordRequest:
    """Request body for POST /api/v1/auth/change-password."""

    current_password: str | None = None
    new_password: str | None = None


@lru_cache(maxsize=1)
def _get_oidc_provider():
    return create_oidc_provider()


def _scopes_for_role(role: str) -> list[str]:
    """Return the OIDC scope bundle (resource:action strings) for a role name.

    Single source of truth for role -> scope expansion outside of token
    issuance -- used wherever a user/API-key is authenticated without going
    through `issue_token()` (e.g. the API-key path, which never mints a JWT).
    Unknown role names fall back to Role.USER's (narrowest) bundle.
    """
    try:
        role_enum = Role(role)
    except ValueError:
        role_enum = Role.USER
    return [p.value for p in ROLE_PERMISSIONS.get(role_enum, set())]


@dataclass(slots=True, frozen=True)
class IssuedToken:
    """A freshly minted access token together with its real lifetime."""

    access_token: str
    expires_in: int


def _clamp_token_ttl_hours(expires_hours: int) -> int:
    """Clamp a requested lifetime into the [1h, 24h] house-policy range."""
    if expires_hours < _DEFAULT_TOKEN_TTL_HOURS:
        return _DEFAULT_TOKEN_TTL_HOURS
    return min(expires_hours, _MAX_TOKEN_TTL_HOURS)


def _default_token_ttl_hours() -> int:
    """Return the configured default token lifetime in hours.

    Honours the pre-existing TOKEN_TTL_HOURS knob but clamps it rather than
    letting an out-of-range value through: the old code path raised
    ValueError at provider construction for anything above 1h, taking the
    whole service down at startup.
    """
    raw = os.getenv("TOKEN_TTL_HOURS")
    if raw is None:
        return _DEFAULT_TOKEN_TTL_HOURS
    try:
        hours = int(raw)
    except ValueError:
        logger.warning(
            "auth: TOKEN_TTL_HOURS=%r is not an integer; using %dh",
            raw,
            _DEFAULT_TOKEN_TTL_HOURS,
        )
        return _DEFAULT_TOKEN_TTL_HOURS
    clamped = _clamp_token_ttl_hours(hours)
    if clamped != hours:
        logger.warning("auth: TOKEN_TTL_HOURS=%d clamped to %dh by house policy", hours, clamped)
    return clamped


def _provider_for_ttl(expires_hours: int) -> OIDCProvider:
    """Build a token provider that issues *expires_hours*-lived access tokens.

    Shares the process-wide provider's keystore so issued tokens still verify
    against `verify_token`; only the TTL policy differs. Reading `_keystore`
    follows the existing precedent in `shared.auth.penguin_auth.verify_token`
    -- penguin-aaa's OIDCProvider exposes no public keystore accessor.
    """
    base = _get_oidc_provider()
    config = OIDCProviderConfig(
        issuer=os.getenv("OIDC_ISSUER_URL", "https://waddleai.localhost.local"),
        audiences=[os.getenv("OIDC_CLIENT_ID", "waddleai-api")],
        algorithm="RS256",
        token_ttl=timedelta(hours=expires_hours),
        max_token_ttl=timedelta(hours=_MAX_TOKEN_TTL_HOURS),
        refresh_ttl=timedelta(days=int(os.getenv("REFRESH_TTL_DAYS", "30"))),
    )
    return OIDCProvider(config, base._keystore)


def issue_access_token(
    user_id: int,
    username: str,
    role: str,
    organization_id: int,
    expires_hours: int | None = None,
) -> IssuedToken:
    """Issue an RS256 access token and report the lifetime actually applied.

    Returning the real TTL alongside the token is the point: the previous code
    advertised `expires_in: 86400` on every login while penguin-aaa signed a
    3600s token, so clients scheduled their refresh eleven hours after the
    session had already died (audit-2026-09-14).
    """
    hours = (
        _default_token_ttl_hours()
        if expires_hours is None
        else _clamp_token_ttl_hours(expires_hours)
    )
    try:
        role_enum = Role(role)
    except ValueError:
        role_enum = Role.USER
    permissions: set[Permission] = ROLE_PERMISSIONS.get(role_enum, set())
    user_context = UserContext(
        user_id=user_id,
        username=username,
        role=role_enum,
        organization_id=organization_id,
        managed_orgs=[],
        permissions=permissions,
    )
    token = issue_token(user_context, _provider_for_ttl(hours))
    return IssuedToken(access_token=token, expires_in=hours * 3600)


def create_token(
    user_id: int,
    username: str,
    role: str,
    organization_id: int,
    expires_hours: int | None = None,
) -> str:
    """Create an RS256 JWT via penguin-aaa, returning only the token string."""
    return issue_access_token(user_id, username, role, organization_id, expires_hours).access_token


def _decode_token(token: str) -> dict | None:
    """Validate an RS256 JWT and return its payload dict. CPU-only, no I/O.

    Does NOT consult the revocation denylist -- callers must do that
    separately (`verify_token` for synchronous callers, `require_auth` for the
    request path, which offloads the cache lookup to a thread).
    """
    try:
        user_context = _aaa_verify_token(token, _get_oidc_provider())
    except Exception:
        return None
    try:
        # Signature, issuer, audience and expiry were all validated above, so
        # this second pass is a pure field read for the two claims penguin-aaa
        # does not surface on UserContext (jti, exp) -- never a trust decision.
        raw = _jwt.decode(token, options={"verify_signature": False})
    except Exception:
        return None
    return {
        "user_id": user_context.user_id,
        "username": user_context.username,
        "role": user_context.role.value,
        "organization_id": user_context.organization_id,
        "scope": sorted(user_context.permissions),
        "jti": raw.get("jti"),
        "exp": raw.get("exp"),
    }


def verify_token(token: str) -> dict | None:
    """Verify an RS256 JWT and return its payload dict, including OIDC scopes.

    `scope` here is authoritative for authorization (see `require_scope`);
    `role` is retained on `g.user` for audit/display only and MUST NOT be
    branched on for access decisions. Tokens revoked via `POST /auth/logout`
    are rejected here exactly like an invalid signature.
    """
    payload = _decode_token(token)
    if payload is None:
        return None
    if get_token_denylist().is_revoked(payload.get("jti")):
        return None
    return payload


def verify_api_key(api_key: str) -> dict[str, Any] | None:
    """Verify API key and return user context, including OIDC scopes.

    API keys never carry a JWT `scope` claim (there is no token to decode),
    so scopes are derived from the key owner's current role via
    `_scopes_for_role` -- the same bundle `create_token` would issue them.
    """
    database = _db()
    # Check virtual_keys table
    # penguin-dal query expression, not a bool comparison
    enabled_query = database.virtual_keys.enabled == True  # noqa: E712
    keys = database(enabled_query).select()
    for key in keys:
        if bcrypt.verify(api_key, key.key_hash):
            user = database(database.users.id == key.user_id).select().first()
            if user and user.enabled:
                # Vuln A fix: Validate key's org matches user's org
                if key.organization_id != user.organization_id:
                    return None
                return {
                    "user_id": user.id,
                    "username": user.username,
                    "role": user.role,
                    "organization_id": user.organization_id,
                    "key_id": key.id,
                    "scope": _scopes_for_role(user.role),
                }
    return None


def require_auth(f):
    """Decorator to require authentication."""

    @wraps(f)
    async def decorated_function(*args, **kwargs):
        auth_header = request.headers.get("Authorization")

        if not auth_header:
            return jsonify({"error": "Authorization header required"}), 401

        # Handle Bearer token
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

            # Try JWT first (CPU-only, no DB access -- safe to call directly).
            # _decode_token deliberately skips the revocation check so the
            # cache lookup below can be offloaded instead of blocking the
            # event loop on every authenticated request.
            payload = _decode_token(token)
            if payload:
                revoked = await asyncio.to_thread(
                    get_token_denylist().is_revoked, payload.get("jti")
                )
                if revoked:
                    logger.info("auth: rejected a revoked token")
                    return jsonify({"error": "Invalid or expired token"}), 401
                g.user = payload
                if asyncio.iscoroutinefunction(f):
                    return await f(*args, **kwargs)
                return f(*args, **kwargs)

            # Try API key (does DB lookups -- offload to a thread)
            user_ctx = await asyncio.to_thread(verify_api_key, token)
            if user_ctx:
                g.user = user_ctx
                if asyncio.iscoroutinefunction(f):
                    return await f(*args, **kwargs)
                return f(*args, **kwargs)

        return jsonify({"error": "Invalid or expired token"}), 401

    return decorated_function


def require_scope(*scopes: Permission | str):
    """Decorator requiring at least one of the given OIDC scopes.

    Authorization is scope-only per house policy: the `roles` claim (and
    `g.user["role"]`) is informational/audit display, never branched on here.
    `scopes` is resolved at decoration time (not per-request) and MUST be
    non-empty -- a route wired to `require_scope()` with no scopes is a
    programming error and fails at import time rather than silently
    allowing every caller through. There is no role-derived fallback: a
    caller whose token carries an empty or missing `scope` claim is refused
    exactly like one with the wrong scope.
    """
    if not scopes:
        raise ValueError("require_scope() requires at least one scope")

    normalized = tuple(s.value if isinstance(s, Permission) else s for s in scopes)

    def decorator(f):
        @wraps(f)
        async def decorated_function(*args, **kwargs):
            if not hasattr(g, "user") or not g.user:
                return jsonify({"error": "Authentication required"}), 401

            user_scopes = set(g.user.get("scope") or [])
            if not user_scopes.intersection(normalized):
                return (
                    jsonify({"error": "Insufficient permissions", "required_scope": normalized}),
                    403,
                )

            if asyncio.iscoroutinefunction(f):
                return await f(*args, **kwargs)
            return f(*args, **kwargs)

        # Attached for programmatic route enumeration (tests walk the
        # blueprint's url_map and inspect this to verify every migrated
        # route still declares a required scope -- see
        # tests/unit/management/test_scope_authz.py).
        decorated_function._required_scopes = normalized
        return decorated_function

    return decorator


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """Return a throwaway bcrypt hash used to equalise login timing.

    Verifying a submitted password against this hash costs the same as
    verifying it against a real user's hash, which is what removes the
    timing signal that previously distinguished "user exists" from "user
    does not exist" (audit-2026-09-14). Built lazily so the ~0.8s bcrypt
    cost is not paid at import time.
    """
    return str(bcrypt.hash(secrets.token_urlsafe(32)))


def _verify_password(password: str, password_hash: str) -> bool:
    """Return True when *password* matches *password_hash*.

    A malformed or missing stored hash is a failed verification, not an
    exception -- raising here would hand back a 500 that distinguishes such
    accounts from every other failure.
    """
    try:
        return bool(bcrypt.verify(password, password_hash))
    except Exception:
        return False


def _throttled_response(decision: ThrottleDecision) -> Response:
    """Return the generic 429 issued to a throttled account.

    Carries no hint about whether the account exists: the throttle counts
    failures for submitted usernames whether or not they resolve to a user,
    so a 429 says only "too many failures for this name".
    """
    retry_after = max(1, decision.retry_after_seconds)
    response = jsonify({"error": "Too many failed login attempts. Try again later."})
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    return response


@api_v1_bp.route("/auth/login", methods=["POST"])
@tag(["Auth"])
@validate_response(LoginResponse, 200)
@validate_request(LoginRequest)
async def login(data: LoginRequest):
    """Authenticate a user and issue a bearer access token.

    Repeated failed attempts against the same account are rate limited and
    answered with 429 and a Retry-After header.
    """
    # NOTE: this docstring is published verbatim in the UNAUTHENTICATED
    # OpenAPI document (build_public_schema copies the login path item, and
    # quart-schema derives `description` from the docstring), so it must not
    # describe the defences below. Keep the rationale in comments.
    #
    # Hardened for audit-2026-09-14. Two properties are load-bearing and must
    # survive any future edit:
    #
    # 1. Every failure mode -- unknown user, disabled account, wrong password,
    #    malformed stored hash -- returns the same status and the same body,
    #    and performs the same bcrypt work before deciding. Short-circuiting
    #    any of them re-opens user enumeration by response *or* by timing.
    # 2. Every failure increments a per-account throttle counter, including
    #    failures for usernames that do not exist. Counting only real accounts
    #    would make "throttled vs not throttled" its own enumeration oracle.
    username = data.username
    password = data.password

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400

    throttle = get_login_throttle()
    decision = await asyncio.to_thread(throttle.check, username)
    if not decision.allowed:
        logger.warning(
            "auth: login refused, account throttled (account_hash=%s)",
            account_key(username)[:12],
        )
        return _throttled_response(decision)

    database = _db()

    # Find user
    user = await asyncio.to_thread(
        lambda: database(database.users.username == username).select().first()
    )

    # Constant work regardless of whether the account exists: a non-existent
    # user is checked against a dummy hash of the same cost, and the enabled
    # flag is only consulted after that work has already been paid for.
    password_hash = user.password_hash if user is not None else _dummy_password_hash()
    password_ok = await asyncio.to_thread(_verify_password, password, password_hash)

    if user is None or not user.enabled or not password_ok:
        failure = await asyncio.to_thread(throttle.register_failure, username)
        logger.info(
            "auth: failed login attempt (account_hash=%s, failures=%d)",
            account_key(username)[:12],
            failure.failure_count,
        )
        if not failure.allowed:
            return _throttled_response(failure)
        return jsonify({"error": _GENERIC_LOGIN_FAILURE}), 401

    # Successful authentication clears the counter so a user who mistypes a
    # few times and then succeeds is not carrying failures toward a lockout.
    await asyncio.to_thread(throttle.reset, username)

    # Update login tracking
    remote_addr = request.remote_addr

    def _update_login():
        database(database.users.id == user.id).update(
            last_login_at=user.current_login_at,
            current_login_at=datetime.utcnow(),
            last_login_ip=user.current_login_ip,
            current_login_ip=remote_addr,
            login_count=(user.login_count or 0) + 1,
        )
        database.commit()

    await asyncio.to_thread(_update_login)

    # Create token
    issued = issue_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        organization_id=user.organization_id,
    )

    return {
        "access_token": issued.access_token,
        "token_type": "bearer",
        "expires_in": issued.expires_in,
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role,
            "organization_id": user.organization_id,
        },
    }


@api_v1_bp.route("/auth/logout", methods=["POST"])
@tag(["Auth"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
async def logout():
    """Revoke the caller's bearer token server-side.

    The token is rejected on every subsequent request from here on, until the
    point it would have expired on its own.
    """
    # The token's `jti` goes on a denylist that `require_auth` consults on
    # every request, with the entry expiring at the token's own `exp` so the
    # store self-cleans. Before audit-2026-09-14 this endpoint reported
    # success while doing nothing at all.
    jti = g.user.get("jti")
    exp = g.user.get("exp")

    if not jti or not exp:
        # API-key authentication carries no JWT ID, so there is nothing to
        # revoke. Logged rather than silently ignored: a jti-less *JWT*
        # would mean token issuance had regressed.
        logger.warning("auth: logout on a credential with no jti; nothing was revoked")
        return {"message": "Logged out successfully"}

    result = await asyncio.to_thread(
        get_token_denylist().revoke, str(jti), datetime.fromtimestamp(int(exp), UTC)
    )
    if not result.durable:
        logger.warning(
            "auth: token revoked in this process only -- other replicas will "
            "keep honouring it until it expires at %s",
            datetime.fromtimestamp(int(exp), UTC).isoformat(),
        )
    return {"message": "Logged out successfully"}


@api_v1_bp.route("/auth/refresh", methods=["POST"])
@tag(["Auth"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(RefreshTokenResponse, 200)
async def refresh_token():
    """Refresh JWT token."""
    user = g.user

    # Create new token
    issued = issue_access_token(
        user_id=user["user_id"],
        username=user["username"],
        role=user["role"],
        organization_id=user["organization_id"],
    )

    return {
        "access_token": issued.access_token,
        "token_type": "bearer",
        "expires_in": issued.expires_in,
    }


@api_v1_bp.route("/auth/verify", methods=["GET"])
@tag(["Auth"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(VerifyResponse, 200)
async def verify_auth():
    """Verify the caller's bearer token and echo back its identity claims."""
    return {
        "user": {
            "id": g.user["user_id"],
            "username": g.user["username"],
            "role": g.user["role"],
            "organization_id": g.user["organization_id"],
        }
    }


@api_v1_bp.route("/auth/me", methods=["GET"])
@tag(["Auth"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(CurrentUserResponse, 200)
async def get_current_user():
    """Get current user info."""
    database = _db()
    user_id = g.user["user_id"]
    user = await asyncio.to_thread(lambda: database(database.users.id == user_id).select().first())

    if not user:
        return jsonify({"error": "User not found"}), 404

    org = await asyncio.to_thread(
        lambda: database(database.organizations.id == user.organization_id).select().first()
    )

    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "organization": {"id": org.id, "name": org.name} if org else None,
        "token_quota_daily": user.token_quota_daily,
        "token_quota_monthly": user.token_quota_monthly,
        "enabled": user.enabled,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


@api_v1_bp.route("/auth/change-password", methods=["POST"])
@tag(["Auth"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
@validate_request(ChangePasswordRequest)
async def change_password(data: ChangePasswordRequest):
    """Change user password."""
    current_password = data.current_password
    new_password = data.new_password

    if not current_password or not new_password:
        return jsonify({"error": "Current password and new password required"}), 400

    if len(new_password) < 8:
        return jsonify({"error": "New password must be at least 8 characters"}), 400

    database = _db()
    user_id = g.user["user_id"]
    user = await asyncio.to_thread(lambda: database(database.users.id == user_id).select().first())

    if not user:
        return jsonify({"error": "User not found"}), 404

    # Verify current password
    if not bcrypt.verify(current_password, user.password_hash):
        return jsonify({"error": "Current password is incorrect"}), 401

    # Update password
    def _update_password():
        database(database.users.id == user_id).update(password_hash=bcrypt.hash(new_password))
        database.commit()

    await asyncio.to_thread(_update_password)

    return {"message": "Password changed successfully"}
