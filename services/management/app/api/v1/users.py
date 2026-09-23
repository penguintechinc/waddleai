"""WaddleAI Management API v1 - User Management Endpoints."""

import asyncio
from dataclasses import dataclass
from datetime import datetime

from passlib.hash import bcrypt
from penguin_dal.db import DB
from quart import g, jsonify
from quart_schema import validate_request, validate_response

from shared.auth.rbac import Permission

from ...extensions import db
from . import api_v1_bp
from ._pagination import PageRequest
from .auth import require_auth, require_scope


def _db() -> DB:
    """Return the process-wide penguin-dal handle, narrowed away from ``None``.

    ``extensions.db`` is ``DB | None`` until ``init_db()`` runs at startup;
    every route here executes only afterwards, so this narrows the type for
    the annotated handlers (whose bodies mypy checks) without adding any
    reachable failure mode -- mirrors keys.py.
    """
    if db is None:
        raise RuntimeError("database not initialized")
    return db


def _has_scope(permission: Permission) -> bool:
    """True when the authenticated caller's OIDC scope claim carries *permission*.

    Mirrors ``auth.require_scope``'s own check -- the authoritative ``scope``
    claim on ``g.user``, never the ``role`` claim (house scope-only policy) --
    for the in-handler branch decisions a single ``@require_scope`` decorator
    cannot express, e.g. "admin lists every org's users, resource_manager only
    its own". Admin-tier scopes here are admin-exclusive on purpose so a
    non-admin role never falls into the broader branch.
    """
    user = getattr(g, "user", None) or {}
    return permission.value in set(user.get("scope") or [])


# ---------------------------------------------------------------------------
# OpenAPI request/response models. Request models make every field Optional
# with the handler's own default so quart-schema's automatic validation never
# fires where the handler's own presence/value checks (and their exact error
# messages) were the only gate -- see keys.py for the same rationale.
#
# Response models pin EXACTLY the fields returned today: this is the identity
# surface, so a later edit that widens a user record (an extra PII column, a
# password hash) fails @validate_response instead of silently shipping.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CreateUserRequest:
    """Request body for POST /api/v1/users."""

    username: str | None = None
    email: str | None = None
    password: str | None = None
    role: str | None = "user"
    organization_id: int | None = None
    token_quota_daily: int | None = 10000
    token_quota_monthly: int | None = 100000
    default_model: str | None = None


@dataclass(slots=True)
class UpdateUserRequest:
    """Request body for PUT /api/v1/users/<user_id>. Every field is a partial update."""

    email: str | None = None
    role: str | None = None
    token_quota_daily: int | None = None
    token_quota_monthly: int | None = None
    default_model: str | None = None
    enabled: bool | None = None
    password: str | None = None


@dataclass(slots=True)
class MessageResponse:
    """Generic ``{"message": str}`` envelope."""

    message: str


@dataclass(slots=True)
class PaginationMeta:
    """Bounded-window metadata attached to every list response."""

    page: int
    limit: int
    total: int | None
    pages: int | None


@dataclass(slots=True)
class UserListItem:
    """A single user in the list response -- the exact identity fields shipped today."""

    id: int
    username: str
    email: str
    role: str
    organization_id: int
    enabled: bool
    created_at: str | None
    last_login_at: str | None


@dataclass(slots=True)
class UsersListResponse:
    """Response body for GET /api/v1/users."""

    users: list[UserListItem]
    total: int
    pagination: PaginationMeta


@dataclass(slots=True)
class OrgRef:
    """Minimal organization reference embedded in a user detail response."""

    id: int
    name: str


@dataclass(slots=True)
class UserDetailResponse:
    """Response body for GET /api/v1/users/<user_id> -- exact fields shipped today."""

    id: int
    username: str
    email: str
    role: str
    organization: OrgRef | None
    token_quota_daily: int | None
    token_quota_monthly: int | None
    default_model: str | None
    enabled: bool
    created_at: str | None
    last_login_at: str | None
    login_count: int | None


@dataclass(slots=True)
class CreateUserResponse:
    """Response body for POST /api/v1/users."""

    id: int
    username: str
    email: str
    role: str
    organization_id: int
    message: str


@api_v1_bp.route("/users", methods=["GET"])
@require_auth
@validate_response(UsersListResponse, 200)
async def list_users():
    """List users, scoped by the caller's OIDC scopes and bounded by pagination."""
    org_id = g.user.get("organization_id")
    current_user_id = g.user["user_id"]

    # admin (USER_CREATE, admin-only) sees all users; resource_manager
    # (USER_MANAGE) sees its own org; everyone else only their own record --
    # the exact three tiers the former role-name checks produced.
    can_read_all = _has_scope(Permission.USER_CREATE)
    can_read_org = _has_scope(Permission.USER_MANAGE)
    page = PageRequest.from_request()

    def _fetch():
        if can_read_all:
            query = db.users.id > 0
        elif can_read_org:
            query = db.users.organization_id == org_id
        else:
            query = db.users.id == current_user_id
        return db(query).select(limitby=page.limitby, orderby=db.users.id)

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

    return {"users": result, "total": len(result), **page.meta()}


@api_v1_bp.route("/users/<int:user_id>", methods=["GET"])
@require_auth
@validate_response(UserDetailResponse, 200)
async def get_user(user_id):
    """Get user details."""
    org_id = g.user.get("organization_id")

    user = await asyncio.to_thread(lambda: db(db.users.id == user_id).select().first())

    if not user:
        return jsonify({"error": "User not found"}), 404

    # Permission check -- admin (USER_CREATE) may view any user; resource_manager
    # (USER_MANAGE) is confined to its own org; everyone else to their own
    # record. Only the role-NAME test became a scope test; the org/self
    # ownership comparisons are unchanged.
    if not _has_scope(Permission.USER_CREATE):
        if _has_scope(Permission.USER_MANAGE) and user.organization_id != org_id:
            return jsonify({"error": "Access denied"}), 403
        elif not _has_scope(Permission.USER_MANAGE) and user.id != g.user["user_id"]:
            return jsonify({"error": "Access denied"}), 403

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
        "default_model": user.default_model,
        "enabled": user.enabled,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "login_count": user.login_count,
    }


@api_v1_bp.route("/users", methods=["POST"])
@require_auth
@require_scope(Permission.USER_MANAGE)
@validate_response(CreateUserResponse, 201)
@validate_request(CreateUserRequest)
async def create_user(data: CreateUserRequest):
    """Create a new user."""
    for field_name, value in (
        ("username", data.username),
        ("email", data.email),
        ("password", data.password),
    ):
        if not value:
            return jsonify({"error": f"{field_name} is required"}), 400

    org_id = g.user.get("organization_id")
    database = _db()

    # Determine organization. Only a caller holding USER_CREATE (admin-only)
    # may target another org; everyone else (resource_manager) is forced to
    # their own -- the former `user_role == "resource_manager"` clamp.
    target_org_id = data.organization_id if data.organization_id is not None else org_id
    if not _has_scope(Permission.USER_CREATE):
        target_org_id = org_id

    # Check if organization exists
    org = await asyncio.to_thread(
        lambda: database(database.organizations.id == target_org_id).select().first()
    )
    if not org:
        return jsonify({"error": "Organization not found"}), 404

    # Check for existing user
    existing = await asyncio.to_thread(
        lambda: (
            database(
                (database.users.username == data.username) | (database.users.email == data.email)
            )
            .select()
            .first()
        )
    )

    if existing:
        return jsonify({"error": "Username or email already exists"}), 409

    # Determine role: without USER_CREATE (admin-only) a caller cannot mint an
    # admin, matching the former resource_manager downgrade.
    role = data.role if data.role is not None else "user"
    if not _has_scope(Permission.USER_CREATE) and role == "admin":
        role = "user"

    # Create user
    def _insert():
        new_user_id = database.users.insert(
            username=data.username,
            email=data.email,
            password_hash=bcrypt.hash(data.password),
            role=role,
            organization_id=target_org_id,
            token_quota_daily=data.token_quota_daily,
            token_quota_monthly=data.token_quota_monthly,
            default_model=data.default_model,
            enabled=True,
            created_at=datetime.utcnow(),
        )
        database.commit()
        return new_user_id

    user_id = await asyncio.to_thread(_insert)

    return (
        {
            "id": user_id,
            "username": data.username,
            "email": data.email,
            "role": role,
            "organization_id": target_org_id,
            "message": "User created successfully",
        },
        201,
    )


@api_v1_bp.route("/users/<int:user_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.USER_MANAGE)
@validate_response(MessageResponse, 200)
@validate_request(UpdateUserRequest)
async def update_user(user_id, data: UpdateUserRequest):
    """Update user."""
    org_id = g.user.get("organization_id")
    database = _db()

    user = await asyncio.to_thread(lambda: database(database.users.id == user_id).select().first())

    if not user:
        return jsonify({"error": "User not found"}), 404

    # Permission check -- Vuln C fix preserved: a caller without USER_CREATE
    # (admin-only) is confined to its own org and may never touch an admin user.
    is_admin_tier = _has_scope(Permission.USER_CREATE)
    if not is_admin_tier and user.organization_id != org_id:
        return jsonify({"error": "Access denied"}), 403
    if not is_admin_tier and user.role == "admin":
        return jsonify({"error": "Cannot modify admin user"}), 403

    # Build update fields
    update_fields: dict[str, object] = {}

    if data.email is not None:
        # Check email uniqueness
        existing = await asyncio.to_thread(
            lambda: (
                database((database.users.email == data.email) & (database.users.id != user_id))
                .select()
                .first()
            )
        )
        if existing:
            return jsonify({"error": "Email already exists"}), 409
        update_fields["email"] = data.email

    if data.role is not None:
        # Without USER_CREATE (admin-only) a caller cannot promote to admin.
        if not is_admin_tier and data.role == "admin":
            return jsonify({"error": "Cannot assign admin role"}), 403
        update_fields["role"] = data.role

    if data.token_quota_daily is not None:
        update_fields["token_quota_daily"] = data.token_quota_daily

    if data.token_quota_monthly is not None:
        update_fields["token_quota_monthly"] = data.token_quota_monthly

    if data.default_model is not None:
        update_fields["default_model"] = data.default_model

    if data.enabled is not None:
        update_fields["enabled"] = data.enabled

    if data.password is not None:
        update_fields["password_hash"] = bcrypt.hash(data.password)

    if update_fields:

        def _update():
            database(database.users.id == user_id).update(**update_fields)
            database.commit()

        await asyncio.to_thread(_update)

    return {"message": "User updated successfully"}


@api_v1_bp.route("/users/<int:user_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.USER_DELETE)
@validate_response(MessageResponse, 200)
async def delete_user(user_id):
    """Delete user (admin only)."""
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

    return {"message": "User disabled successfully"}


@api_v1_bp.route("/users/<int:user_id>/enable", methods=["POST"])
@require_auth
@require_scope(Permission.USER_MANAGE)
@validate_response(MessageResponse, 200)
async def enable_user(user_id):
    """Enable a disabled user."""
    org_id = g.user.get("organization_id")

    user = await asyncio.to_thread(lambda: db(db.users.id == user_id).select().first())

    if not user:
        return jsonify({"error": "User not found"}), 404

    # Vuln C fix preserved: a caller without USER_CREATE (admin-only) is
    # confined to its own org and may never enable an admin user.
    is_admin_tier = _has_scope(Permission.USER_CREATE)
    if not is_admin_tier and user.organization_id != org_id:
        return jsonify({"error": "Access denied"}), 403
    if not is_admin_tier and user.role == "admin":
        return jsonify({"error": "Cannot enable admin user"}), 403

    def _enable():
        db(db.users.id == user_id).update(enabled=True)
        db.commit()

    await asyncio.to_thread(_enable)

    return {"message": "User enabled successfully"}
