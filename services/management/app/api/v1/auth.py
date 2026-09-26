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
from quart import Response, after_this_request, g, jsonify, request
from quart_schema import security_scheme, tag, validate_request, validate_response

from shared.auth.penguin_auth import create_oidc_provider, issue_token
from shared.auth.penguin_auth import verify_token as _aaa_verify_token
from shared.auth.rbac import ROLE_PERMISSIONS, Permission, Role, UserContext

from ...extensions import db
from ...services.login_throttle import ThrottleDecision, account_key, get_login_throttle
from ...services.rate_limiter import (
    RateLimitDecision,
    client_rate_limit_key,
    get_auth_rate_limiter,
)
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

# ---------------------------------------------------------------------------
# Browser session cookie (audit-2026-09-14).
#
# The access token is additionally delivered to browsers in an HttpOnly +
# Secure + SameSite=Strict cookie so JavaScript can never read it (the webui
# previously kept it in localStorage, where any XSS could exfiltrate it). The
# JSON body of the login response still carries the token unchanged, so API and
# CLI clients are unaffected -- the cookie is purely additive for the browser.
# ---------------------------------------------------------------------------
_ACCESS_COOKIE_NAME = "waddleai_access_token"  # noqa: S105 -- cookie NAME, not a secret

# CSRF: an HttpOnly cookie is attached automatically by the browser, which
# reintroduces the CSRF exposure a localStorage bearer token did not have. Two
# layers defend it. First, SameSite=Strict on the cookie: a cross-site context
# never sends it at all. Second, for cookie-authenticated *state-changing*
# requests, a mandatory custom request header -- a cross-site attacker can
# forge a form/img/navigation POST but cannot attach a custom header without a
# CORS preflight, which this service's default-deny origin allowlist refuses,
# so the header's presence proves the request came from our own same-origin
# SPA (the OWASP "custom request header" CSRF defence for JSON APIs).
# Bearer-header (API/CLI) callers are exempt: CSRF only abuses ambient cookie
# credentials, and a browser never attaches an Authorization header on its own.
_CSRF_HEADER = "X-Requested-With"
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _access_cookie_secure() -> bool:
    """Return whether the access cookie must carry the ``Secure`` attribute.

    Reads a dedicated ``AUTH_COOKIE_SECURE`` env var rather than Quart's
    ``SESSION_COOKIE_SECURE`` config key: Quart pre-populates the latter to
    False on every app, so inheriting it would silently ship a non-Secure
    token cookie in any config that does not explicitly override it. Secure by
    default; an operator serving the stack over plain HTTP locally can set
    ``AUTH_COOKIE_SECURE=false``. Browsers treat ``localhost`` as a secure
    context, so the secure default still works behind the usual dev proxy.
    """
    raw = os.getenv("AUTH_COOKIE_SECURE")
    if raw is not None:
        return raw.strip().lower() not in {"0", "false", "no", "off"}
    return True


def _set_access_cookie(token: str, max_age_seconds: int) -> None:
    """Attach the HttpOnly access-token cookie to the outgoing response.

    Registered via ``after_this_request`` so it lands on the final Response
    that ``quart_schema.validate_response`` builds from the handler's returned
    dict, instead of fighting that decorator for control of the return value.
    """

    # `response` is intentionally left unannotated: quart's after_this_request
    # types its callback for the quart|werkzeug Response union, and narrowing
    # the parameter to quart's Response alone is a contravariance error.
    @after_this_request
    def _apply(response):
        response.set_cookie(
            _ACCESS_COOKIE_NAME,
            token,
            max_age=max_age_seconds,
            httponly=True,
            secure=_access_cookie_secure(),
            samesite="Strict",
            path="/",
        )
        return response


def _clear_access_cookie() -> None:
    """Expire the HttpOnly access-token cookie on the outgoing response."""

    # Unannotated `response` for the same after_this_request contravariance
    # reason documented on _set_access_cookie's callback above.
    @after_this_request
    def _apply(response):
        response.delete_cookie(
            _ACCESS_COOKIE_NAME,
            path="/",
            httponly=True,
            secure=_access_cookie_secure(),
            samesite="Strict",
        )
        return response


def _csrf_ok_for_cookie_auth() -> bool:
    """Return True when a cookie-authenticated request clears the CSRF gate.

    Safe methods (which change no state) always pass; unsafe methods must carry
    the custom header the SPA sends on every request (see ``_CSRF_HEADER``).
    """
    if request.method in _CSRF_SAFE_METHODS:
        return True
    return bool(request.headers.get(_CSRF_HEADER))


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
class TokenExchangeResponse:
    """Response body for POST /api/v1/auth/token (headless API-key exchange)."""

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


# A virtual key is `wa-{secret}` with `key_prefix = f"wa-{secret[:8]}..."`;
# the prefix therefore spans the first 11 characters of the presented key
# ("wa-" + 8 secret chars) plus the literal "..." suffix stored in the row.
_VIRTUAL_KEY_PREFIX_LEN = 11


def _virtual_key_prefix(api_key: str) -> str | None:
    """Return the stored ``key_prefix`` a presented virtual key must match.

    ``None`` for anything too short or without the ``wa-`` marker, so a
    malformed key is rejected before touching the database.
    """
    if not api_key.startswith("wa-") or len(api_key) < _VIRTUAL_KEY_PREFIX_LEN:
        return None
    return f"{api_key[:_VIRTUAL_KEY_PREFIX_LEN]}..."


def verify_api_key(api_key: str) -> dict[str, Any] | None:
    """Verify API key and return user context, including OIDC scopes.

    API keys never carry a JWT `scope` claim (there is no token to decode),
    so scopes are derived from the key owner's current role via
    `_scopes_for_role` -- the same bundle `create_token` would issue them.
    """
    database = _db()
    # audit-2026-09-23 M3: narrow by the indexed `key_prefix` instead of
    # loading every enabled virtual key and bcrypt-verifying each in turn
    # (an O(n) bcrypt scan on a per-request auth path). bcrypt still gates the
    # match, so a forged or renamed prefix cannot authenticate; the
    # constant-time secret comparison and the "unknown key -> None" semantics
    # are unchanged.
    expected_prefix = _virtual_key_prefix(api_key)
    if expected_prefix is None:
        return None
    # penguin-dal query expression, not a bool comparison
    prefix_query = (database.virtual_keys.key_prefix == expected_prefix) & (
        database.virtual_keys.enabled == True  # noqa: E712
    )
    keys = database(prefix_query).select()
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


def _jwt_subject_active(user_id: object) -> bool:
    """Return False when the token's subject no longer exists or is disabled.

    Closes M1 (audit-2026-09-23): unlike the API-key path, the JWT path never
    re-checked ``enabled``, so a disabled account kept working until its token
    expired (<=24h) and could roll the session forward via ``/auth/refresh``.
    Re-checking on every JWT-authenticated request makes a disable take effect
    immediately and cross-replica without a per-user token denylist. Uses the
    penguin_dal primary-key fetch (``db.users[id]``) -- an indexed O(1) lookup,
    not a scan.
    """
    database = _db()
    row = database.users[user_id]
    if row is None:
        return False
    return bool(getattr(row, "enabled", True))


async def _revoke_presented_token() -> None:
    """Revoke the JWT that authenticated the current request, when there is one.

    API-key credentials carry no ``jti``/``exp`` and cannot be revoked here;
    that is expected rather than an error (mirrors ``/auth/logout``). The entry
    expires at the token's own ``exp`` so the denylist self-cleans.
    """
    jti = g.user.get("jti")
    exp = g.user.get("exp")
    if not jti or not exp:
        return
    result = await asyncio.to_thread(
        get_token_denylist().revoke, str(jti), datetime.fromtimestamp(int(exp), UTC)
    )
    if not result.durable:
        logger.warning(
            "auth: token revoked in this process only -- other replicas will "
            "keep honouring it until it expires at %s",
            datetime.fromtimestamp(int(exp), UTC).isoformat(),
        )


def require_auth(f):
    """Decorator to require authentication.

    Accepts either the ``Authorization: Bearer`` header (API and CLI clients,
    authoritative when present) or, when that header is absent, the HttpOnly
    ``waddleai_access_token`` cookie the browser attaches automatically
    (audit-2026-09-14). Cookie-authenticated *state-changing* requests must
    additionally carry the CSRF header (``_CSRF_HEADER``); header-authenticated
    requests are exempt, because a browser never attaches an Authorization
    header on its own, so the cookie-borne CSRF risk cannot reach that path.
    """

    @wraps(f)
    async def decorated_function(*args, **kwargs):
        async def _invoke():
            if asyncio.iscoroutinefunction(f):
                return await f(*args, **kwargs)
            return f(*args, **kwargs)

        async def _authenticate_jwt(token: str) -> bool:
            """Set ``g.user`` from a valid, non-revoked JWT; report success.

            ``_decode_token`` deliberately skips the revocation check so the
            cache lookup can be offloaded off the event loop rather than
            blocking it on every authenticated request.
            """
            payload = _decode_token(token)
            if not payload:
                return False
            revoked = await asyncio.to_thread(get_token_denylist().is_revoked, payload.get("jti"))
            if revoked:
                logger.info("auth: rejected a revoked token")
                return False
            if not await asyncio.to_thread(_jwt_subject_active, payload.get("user_id")):
                logger.info("auth: rejected a token whose subject is disabled or removed")
                return False
            g.user = payload
            return True

        auth_header = request.headers.get("Authorization")

        # Authorization header is authoritative when present (API/CLI clients),
        # and exempt from the CSRF-header requirement (see the decorator's
        # docstring). A malformed or invalid header is refused here rather than
        # silently falling through to the cookie path.
        if auth_header:
            if auth_header.startswith("Bearer "):
                token = auth_header.split(" ", 1)[1]
                if await _authenticate_jwt(token):
                    return await _invoke()
                # Not a valid JWT -- an API key may occupy the same slot.
                user_ctx = await asyncio.to_thread(verify_api_key, token)
                if user_ctx:
                    g.user = user_ctx
                    return await _invoke()
            return jsonify({"error": "Invalid or expired token"}), 401

        # No Authorization header: fall back to the browser session cookie.
        cookie_token = request.cookies.get(_ACCESS_COOKIE_NAME)
        if cookie_token:
            if not _csrf_ok_for_cookie_auth():
                logger.info("auth: cookie-authenticated request refused: missing CSRF header")
                return jsonify({"error": "CSRF verification failed"}), 403
            if await _authenticate_jwt(cookie_token):
                return await _invoke()
            return jsonify({"error": "Invalid or expired token"}), 401

        # No credential at all. Preserve the exact legacy 401 body pinned by the
        # management contract snapshot (tests/contract/snapshots/mgmt_orgs_unauth).
        return jsonify({"error": "Authorization header required"}), 401

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


def _rate_limited_response(decision: RateLimitDecision) -> Response:
    """Return the generic 429 issued when the request-volume limiter trips.

    Distinct from :func:`_throttled_response`: this guards raw request
    *volume* on an unauthenticated credential-verification route (headless-
    auth-secrev M2), independent of whether any individual request went on
    to fail credential checks.
    """
    retry_after = max(1, decision.retry_after_seconds)
    response = jsonify({"error": "Too many requests. Try again later."})
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

    # M2 (headless-auth-secrev): raw request-volume limiter, independent of
    # (and checked before) the account-scoped failure lockout below -- see
    # rate_limiter.py's module docstring for why the two controls coexist.
    limiter = get_auth_rate_limiter()
    rate_decision = limiter.check(client_rate_limit_key(request.remote_addr, username))
    if not rate_decision.allowed:
        logger.warning(
            "auth: login refused, rate limit exceeded (account_hash=%s)",
            account_key(username)[:12],
        )
        return _rate_limited_response(rate_decision)

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

    # Additionally hand the browser an HttpOnly cookie carrying the same token
    # (audit-2026-09-14). The JSON body below is unchanged, so API/CLI clients
    # keep reading the token from it exactly as before.
    _set_access_cookie(issued.access_token, issued.expires_in)

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


@api_v1_bp.route("/auth/token", methods=["POST"])
@tag(["Auth"])
@security_scheme(_BEARER_AUTH)
@validate_response(TokenExchangeResponse, 200)
async def token_exchange():
    """Exchange a service-account API key for a short-lived bearer JWT.

    The headless/CI/service-to-service equivalent of ``/auth/login``:
    unauthenticated at the middleware layer (no ``@require_auth``) because the
    credential being exchanged -- a ``wa-`` virtual key presented as
    ``Authorization: Bearer wa-...`` -- *is* the authentication, exactly like a
    username/password pair on ``/auth/login``. Never a query parameter: query
    strings land in access logs and browser history, which is the opposite of
    what a short-lived credential exchange needs.

    Scope, role and organization all come from the key owner's row via
    ``verify_api_key`` -- nothing here is caller-supplied. A service-account
    owner (H1's ``is_service_account``) yields the same claim shape as a human
    owner and works identically; there is no separate machine-token claim
    shape for validators to special-case.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return jsonify({"error": "Authorization header required"}), 401

    api_key = auth_header.split(" ", 1)[1]

    # M2 (headless-auth-secrev): raw request-volume limiter, same control as
    # /auth/login above -- keyed on the presented key (hashed, never stored
    # or logged raw) so a guessed-key sweep is throttled regardless of which
    # source IP it comes from.
    limiter = get_auth_rate_limiter()
    rate_decision = limiter.check(client_rate_limit_key(request.remote_addr, api_key))
    if not rate_decision.allowed:
        logger.warning("auth: token exchange refused, rate limit exceeded")
        return _rate_limited_response(rate_decision)

    # Same generic 401 and log line regardless of *why* the key was refused
    # (unknown, disabled, wrong org) -- mirrors /auth/login's refusal to
    # distinguish failure modes, and the key value itself never appears in
    # the log or the response.
    user_ctx = await asyncio.to_thread(verify_api_key, api_key)
    if user_ctx is None:
        logger.info("auth: token exchange refused for an invalid or disabled API key")
        return jsonify({"error": "Invalid or expired token"}), 401

    issued = issue_access_token(
        user_id=user_ctx["user_id"],
        username=user_ctx["username"],
        role=user_ctx["role"],
        organization_id=user_ctx["organization_id"],
    )

    return {
        "access_token": issued.access_token,
        "token_type": "bearer",
        "expires_in": issued.expires_in,
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
    #
    # Also expire the browser session cookie so a logged-out browser stops
    # sending the token on its own. This is additive to (never a replacement
    # for) the jti revocation above: clearing the cookie stops future browser
    # requests, while the denylist rejects the token even if a copy was already
    # captured elsewhere.
    _clear_access_cookie()

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
    """Refresh JWT token.

    Rotates the session (audit-2026-09-23 M1): the presented token is revoked
    and a freshly minted one returned, so a refreshed session cannot be
    continued with the old credential nor rolled forward indefinitely.
    """
    user = g.user

    await _revoke_presented_token()

    issued = issue_access_token(
        user_id=user["user_id"],
        username=user["username"],
        role=user["role"],
        organization_id=user["organization_id"],
    )

    # Browser clients authenticate from the HttpOnly cookie, which still holds
    # the just-revoked token; replace it so the refreshed session keeps working.
    _set_access_cookie(issued.access_token, issued.expires_in)

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

    # A password change must invalidate the credential that made it
    # (audit-2026-09-23 M1). Revoke the presented token and hand the browser a
    # fresh cookie so the current session continues; API/CLI clients that
    # authenticated with the now-revoked bearer token re-authenticate with the
    # new password, which is the expected post-change behaviour.
    await _revoke_presented_token()
    reissued = issue_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        organization_id=user.organization_id,
    )
    _set_access_cookie(reissued.access_token, reissued.expires_in)

    return {"message": "Password changed successfully"}
