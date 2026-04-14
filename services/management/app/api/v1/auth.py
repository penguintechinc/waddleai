"""
WaddleAI Management API v1 - Authentication Endpoints
"""

from datetime import datetime
from functools import lru_cache, wraps

from flask import g, jsonify, request
from passlib.hash import bcrypt

from shared.auth.penguin_auth import create_oidc_provider, issue_token
from shared.auth.penguin_auth import verify_token as _aaa_verify_token
from shared.auth.rbac import ROLE_PERMISSIONS, Role, UserContext

from ...extensions import db
from . import api_v1_bp


@lru_cache(maxsize=1)
def _get_oidc_provider():
    return create_oidc_provider()


def create_token(user_id: int, username: str, role: str, organization_id: int, expires_hours: int = 24) -> str:
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
    """Verify RS256 JWT token and return payload dict."""
    try:
        user_context = _aaa_verify_token(token, _get_oidc_provider())
        return {
            "user_id": user_context.user_id,
            "username": user_context.username,
            "role": user_context.role.value,
            "organization_id": user_context.organization_id,
        }
    except Exception:
        return None


def verify_api_key(api_key: str) -> dict:
    """Verify API key and return user context"""
    # Check virtual_keys table
    keys = db(db.virtual_keys.enabled == True).select()
    for key in keys:
        if bcrypt.verify(api_key, key.key_hash):
            user = db(db.users.id == key.user_id).select().first()
            if user and user.enabled:
                return {
                    "user_id": user.id,
                    "username": user.username,
                    "role": user.role,
                    "organization_id": user.organization_id,
                    "key_id": key.id,
                }
    return None


def require_auth(f):
    """Decorator to require authentication"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get("Authorization")

        if not auth_header:
            return jsonify({"error": "Authorization header required"}), 401

        # Handle Bearer token
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

            # Try JWT first
            payload = verify_token(token)
            if payload:
                g.user = payload
                return f(*args, **kwargs)

            # Try API key
            user_ctx = verify_api_key(token)
            if user_ctx:
                g.user = user_ctx
                return f(*args, **kwargs)

        return jsonify({"error": "Invalid or expired token"}), 401

    return decorated_function


def require_role(*roles):
    """Decorator to require specific role(s)"""

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not hasattr(g, "user") or not g.user:
                return jsonify({"error": "Authentication required"}), 401

            user_role = g.user.get("role")
            if user_role not in roles:
                return jsonify({"error": "Insufficient permissions", "required_roles": roles}), 403

            return f(*args, **kwargs)

        return decorated_function

    return decorator


@api_v1_bp.route("/auth/login", methods=["POST"])
def login():
    """User login endpoint"""
    data = request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    username = data.get("username")
    password = data.get("password")

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400

    # Find user
    user = db(db.users.username == username).select().first()

    if not user:
        return jsonify({"error": "Invalid credentials"}), 401

    if not user.enabled:
        return jsonify({"error": "Account disabled"}), 401

    # Verify password
    if not bcrypt.verify(password, user.password_hash):
        return jsonify({"error": "Invalid credentials"}), 401

    # Update login tracking
    db(db.users.id == user.id).update(
        last_login_at=user.current_login_at,
        current_login_at=datetime.utcnow(),
        last_login_ip=user.current_login_ip,
        current_login_ip=request.remote_addr,
        login_count=(user.login_count or 0) + 1,
    )
    db.commit()

    # Create token
    token = create_token(user_id=user.id, username=user.username, role=user.role, organization_id=user.organization_id)

    return jsonify(
        {
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
    )


@api_v1_bp.route("/auth/logout", methods=["POST"])
@require_auth
def logout():
    """User logout endpoint"""
    # In a stateless JWT system, logout is handled client-side
    # Server can optionally blacklist the token in Redis
    return jsonify({"message": "Logged out successfully"})


@api_v1_bp.route("/auth/refresh", methods=["POST"])
@require_auth
def refresh_token():
    """Refresh JWT token"""
    user = g.user

    # Create new token
    token = create_token(
        user_id=user["user_id"], username=user["username"], role=user["role"], organization_id=user["organization_id"]
    )

    return jsonify({"access_token": token, "token_type": "bearer", "expires_in": 86400})


@api_v1_bp.route("/auth/verify", methods=["GET"])
@require_auth
def verify_auth():
    return jsonify(
        {
            "user": {
                "id": g.user["user_id"],
                "username": g.user["username"],
                "role": g.user["role"],
            }
        }
    )


@api_v1_bp.route("/auth/me", methods=["GET"])
@require_auth
def get_current_user():
    """Get current user info"""
    user_id = g.user["user_id"]
    user = db(db.users.id == user_id).select().first()

    if not user:
        return jsonify({"error": "User not found"}), 404

    org = db(db.organizations.id == user.organization_id).select().first()

    return jsonify(
        {
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
    )


@api_v1_bp.route("/auth/change-password", methods=["POST"])
@require_auth
def change_password():
    """Change user password"""
    data = request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    current_password = data.get("current_password")
    new_password = data.get("new_password")

    if not current_password or not new_password:
        return jsonify({"error": "Current password and new password required"}), 400

    if len(new_password) < 8:
        return jsonify({"error": "New password must be at least 8 characters"}), 400

    user_id = g.user["user_id"]
    user = db(db.users.id == user_id).select().first()

    if not user:
        return jsonify({"error": "User not found"}), 404

    # Verify current password
    if not bcrypt.verify(current_password, user.password_hash):
        return jsonify({"error": "Current password is incorrect"}), 401

    # Update password
    db(db.users.id == user_id).update(password_hash=bcrypt.hash(new_password))
    db.commit()

    return jsonify({"message": "Password changed successfully"})
