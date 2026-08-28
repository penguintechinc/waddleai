"""WaddleAI Management API v1 - Authentication Endpoints."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache, wraps

from passlib.hash import bcrypt
from quart import g, jsonify, request
from quart_schema import security_scheme, tag, validate_request, validate_response

from shared.auth.penguin_auth import create_oidc_provider, issue_token
from shared.auth.penguin_auth import verify_token as _aaa_verify_token
from shared.auth.rbac import ROLE_PERMISSIONS, Permission, Role, UserContext

from ...extensions import db
from . import api_v1_bp

_BEARER_AUTH = [{"bearerAuth": []}]


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


def create_token(
    user_id: int, username: str, role: str, organization_id: int, expires_hours: int = 24
) -> str:
    """Create RS256 JWT token via penguin-aaa."""
    try:
        role_enum = Role(role)
    except ValueError:
        role_enum = Role.USER
    permissions = {p.value for p in ROLE_PERMISSIONS.get(role_enum, set())}
    user_context = UserContext(
        user_id=user_id,
        username=username,
        role=role_enum,
        organization_id=organization_id,
        managed_orgs=[],
        permissions=permissions,
    )
    return issue_token(user_context, _get_oidc_provider())


def verify_token(token: str) -> dict | None:
    """Verify RS256 JWT token and return payload dict, including OIDC scopes.

    `scope` here is authoritative for authorization (see `require_scope`);
    `role` is retained on `g.user` for audit/display only and MUST NOT be
    branched on for access decisions.
    """
    try:
        user_context = _aaa_verify_token(token, _get_oidc_provider())
        return {
            "user_id": user_context.user_id,
            "username": user_context.username,
            "role": user_context.role.value,
            "organization_id": user_context.organization_id,
            "scope": sorted(user_context.permissions),
        }
    except Exception:
        return None


def verify_api_key(api_key: str) -> dict:
    """Verify API key and return user context, including OIDC scopes.

    API keys never carry a JWT `scope` claim (there is no token to decode),
    so scopes are derived from the key owner's current role via
    `_scopes_for_role` -- the same bundle `create_token` would issue them.
    """
    # Check virtual_keys table
    # penguin-dal query expression, not a bool comparison
    enabled_query = db.virtual_keys.enabled == True  # noqa: E712
    keys = db(enabled_query).select()
    for key in keys:
        if bcrypt.verify(api_key, key.key_hash):
            user = db(db.users.id == key.user_id).select().first()
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

            # Try JWT first (CPU-only, no DB access -- safe to call directly)
            payload = verify_token(token)
            if payload:
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


@api_v1_bp.route("/auth/login", methods=["POST"])
@tag(["Auth"])
@validate_response(LoginResponse, 200)
@validate_request(LoginRequest)
async def login(data: LoginRequest):
    """User login endpoint."""
    username = data.username
    password = data.password

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400

    # Find user
    user = await asyncio.to_thread(lambda: db(db.users.username == username).select().first())

    if not user:
        return jsonify({"error": "Invalid credentials"}), 401

    if not user.enabled:
        return jsonify({"error": "Account disabled"}), 401

    # Verify password
    if not bcrypt.verify(password, user.password_hash):
        return jsonify({"error": "Invalid credentials"}), 401

    # Update login tracking
    remote_addr = request.remote_addr

    def _update_login():
        db(db.users.id == user.id).update(
            last_login_at=user.current_login_at,
            current_login_at=datetime.utcnow(),
            last_login_ip=user.current_login_ip,
            current_login_ip=remote_addr,
            login_count=(user.login_count or 0) + 1,
        )
        db.commit()

    await asyncio.to_thread(_update_login)

    # Create token
    token = create_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        organization_id=user.organization_id,
    )

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": 86400,
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
    """User logout endpoint."""
    # In a stateless JWT system, logout is handled client-side
    # Server can optionally blacklist the token in Redis
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
    token = create_token(
        user_id=user["user_id"],
        username=user["username"],
        role=user["role"],
        organization_id=user["organization_id"],
    )

    return {"access_token": token, "token_type": "bearer", "expires_in": 86400}


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
    user_id = g.user["user_id"]
    user = await asyncio.to_thread(lambda: db(db.users.id == user_id).select().first())

    if not user:
        return jsonify({"error": "User not found"}), 404

    org = await asyncio.to_thread(
        lambda: db(db.organizations.id == user.organization_id).select().first()
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

    user_id = g.user["user_id"]
    user = await asyncio.to_thread(lambda: db(db.users.id == user_id).select().first())

    if not user:
        return jsonify({"error": "User not found"}), 404

    # Verify current password
    if not bcrypt.verify(current_password, user.password_hash):
        return jsonify({"error": "Current password is incorrect"}), 401

    # Update password
    def _update_password():
        db(db.users.id == user_id).update(password_hash=bcrypt.hash(new_password))
        db.commit()

    await asyncio.to_thread(_update_password)

    return {"message": "Password changed successfully"}
