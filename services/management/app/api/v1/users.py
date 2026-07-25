"""
WaddleAI Management API v1 - User Management Endpoints
"""

import asyncio
from datetime import datetime

from passlib.hash import bcrypt
from quart import g, jsonify, request

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth, require_role


@api_v1_bp.route("/users", methods=["GET"])
@require_auth
async def list_users():
    """List users (filtered by role permissions)"""
    user_role = g.user.get("role")
    org_id = g.user.get("organization_id")
    current_user_id = g.user["user_id"]

    def _fetch():
        if user_role == "admin":
            return db(db.users.id > 0).select()
        elif user_role == "resource_manager":
            return db(db.users.organization_id == org_id).select()
        else:
            return db(db.users.id == current_user_id).select()

    users = await asyncio.to_thread(_fetch)

    result = []
    for user in users:
        result.append(
            {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": user.role,
                "organization_id": user.organization_id,
                "enabled": user.enabled,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
            }
        )

    return jsonify({"users": result, "total": len(result)})


@api_v1_bp.route("/users/<int:user_id>", methods=["GET"])
@require_auth
async def get_user(user_id):
    """Get user details"""
    user_role = g.user.get("role")
    org_id = g.user.get("organization_id")

    user = await asyncio.to_thread(lambda: db(db.users.id == user_id).select().first())

    if not user:
        return jsonify({"error": "User not found"}), 404

    # Permission check
    if user_role not in ["admin"]:
        if user_role == "resource_manager" and user.organization_id != org_id:
            return jsonify({"error": "Access denied"}), 403
        elif user_role not in ["resource_manager"] and user.id != g.user["user_id"]:
            return jsonify({"error": "Access denied"}), 403

    org = await asyncio.to_thread(lambda: db(db.organizations.id == user.organization_id).select().first())

    return jsonify(
        {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role,
            "organization": {"id": org.id, "name": org.name} if org else None,
            "token_quota_daily": user.token_quota_daily,
            "token_quota_monthly": user.token_quota_monthly,
            "default_model": user.default_model,
            "enabled": user.enabled,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
            "login_count": user.login_count,
        }
    )


@api_v1_bp.route("/users", methods=["POST"])
@require_auth
@require_role("admin", "resource_manager")
async def create_user():
    """Create a new user"""
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    required_fields = ["username", "email", "password"]
    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"{field} is required"}), 400

    user_role = g.user.get("role")
    org_id = g.user.get("organization_id")

    # Determine organization
    target_org_id = data.get("organization_id", org_id)
    if user_role == "resource_manager":
        target_org_id = org_id  # Force own organization

    # Check if organization exists
    org = await asyncio.to_thread(lambda: db(db.organizations.id == target_org_id).select().first())
    if not org:
        return jsonify({"error": "Organization not found"}), 404

    # Check for existing user
    existing = await asyncio.to_thread(
        lambda: db((db.users.username == data["username"]) | (db.users.email == data["email"])).select().first()
    )

    if existing:
        return jsonify({"error": "Username or email already exists"}), 409

    # Determine role (resource managers can only create users, not admins)
    role = data.get("role", "user")
    if user_role == "resource_manager" and role == "admin":
        role = "user"

    # Create user
    def _insert():
        new_user_id = db.users.insert(
            username=data["username"],
            email=data["email"],
            password_hash=bcrypt.hash(data["password"]),
            role=role,
            organization_id=target_org_id,
            token_quota_daily=data.get("token_quota_daily", 10000),
            token_quota_monthly=data.get("token_quota_monthly", 100000),
            default_model=data.get("default_model"),
            enabled=True,
            created_at=datetime.utcnow(),
        )
        db.commit()
        return new_user_id

    user_id = await asyncio.to_thread(_insert)

    return (
        jsonify(
            {
                "id": user_id,
                "username": data["username"],
                "email": data["email"],
                "role": role,
                "organization_id": target_org_id,
                "message": "User created successfully",
            }
        ),
        201,
    )


@api_v1_bp.route("/users/<int:user_id>", methods=["PUT"])
@require_auth
@require_role("admin", "resource_manager")
async def update_user(user_id):
    """Update user"""
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    user_role = g.user.get("role")
    org_id = g.user.get("organization_id")

    user = await asyncio.to_thread(lambda: db(db.users.id == user_id).select().first())

    if not user:
        return jsonify({"error": "User not found"}), 404

    # Permission check
    if user_role == "resource_manager" and user.organization_id != org_id:
        return jsonify({"error": "Access denied"}), 403

    # Build update fields
    update_fields = {}

    if "email" in data:
        # Check email uniqueness
        existing = await asyncio.to_thread(
            lambda: db((db.users.email == data["email"]) & (db.users.id != user_id)).select().first()
        )
        if existing:
            return jsonify({"error": "Email already exists"}), 409
        update_fields["email"] = data["email"]

    if "role" in data:
        # Resource managers cannot promote to admin
        if user_role == "resource_manager" and data["role"] == "admin":
            return jsonify({"error": "Cannot assign admin role"}), 403
        update_fields["role"] = data["role"]

    if "token_quota_daily" in data:
        update_fields["token_quota_daily"] = data["token_quota_daily"]

    if "token_quota_monthly" in data:
        update_fields["token_quota_monthly"] = data["token_quota_monthly"]

    if "default_model" in data:
        update_fields["default_model"] = data["default_model"]

    if "enabled" in data:
        update_fields["enabled"] = data["enabled"]

    if "password" in data:
        update_fields["password_hash"] = bcrypt.hash(data["password"])

    if update_fields:

        def _update():
            db(db.users.id == user_id).update(**update_fields)
            db.commit()

        await asyncio.to_thread(_update)

    return jsonify({"message": "User updated successfully"})


@api_v1_bp.route("/users/<int:user_id>", methods=["DELETE"])
@require_auth
@require_role("admin")
async def delete_user(user_id):
    """Delete user (admin only)"""
    user = await asyncio.to_thread(lambda: db(db.users.id == user_id).select().first())

    if not user:
        return jsonify({"error": "User not found"}), 404

    # Prevent self-deletion
    if user_id == g.user["user_id"]:
        return jsonify({"error": "Cannot delete own account"}), 400

    # Soft delete by disabling
    def _disable():
        db(db.users.id == user_id).update(enabled=False)
        db.commit()

    await asyncio.to_thread(_disable)

    return jsonify({"message": "User disabled successfully"})


@api_v1_bp.route("/users/<int:user_id>/enable", methods=["POST"])
@require_auth
@require_role("admin", "resource_manager")
async def enable_user(user_id):
    """Enable a disabled user"""
    user_role = g.user.get("role")
    org_id = g.user.get("organization_id")

    user = await asyncio.to_thread(lambda: db(db.users.id == user_id).select().first())

    if not user:
        return jsonify({"error": "User not found"}), 404

    if user_role == "resource_manager" and user.organization_id != org_id:
        return jsonify({"error": "Access denied"}), 403

    def _enable():
        db(db.users.id == user_id).update(enabled=True)
        db.commit()

    await asyncio.to_thread(_enable)

    return jsonify({"message": "User enabled successfully"})
