"""WaddleAI Management API v1 - Quota Management Endpoints."""

import asyncio
import math
from dataclasses import dataclass
from datetime import date

from penguin_dal.db import DB
from quart import g, jsonify, request
from quart.typing import ResponseReturnValue
from quart_schema import validate_request, validate_response

from shared.auth.rbac import Permission

from ...extensions import db
from . import api_v1_bp
from ._pagination import PageRequest
from .auth import require_auth, require_scope
from .keys import privileged_fields_error, privileged_key_fields_denied


def _db() -> DB:
    """Return the process-wide penguin-dal handle, narrowed away from ``None``.

    ``extensions.db`` is declared ``DB | None`` because it starts unset
    before ``init_db()`` runs at startup; every route below only executes
    after that point, so this narrows the type for mypy without adding any
    reachable failure mode.
    """
    if db is None:
        raise RuntimeError("database not initialized")
    return db


def _has_scope(perm: Permission) -> bool:
    """True when the caller's OIDC ``scope`` claim carries ``perm``.

    Authoritative ``scope`` claim only, never the ``role`` claim (house
    scope-only policy, see ``auth.require_scope``). audit-2026-09-14-wave2:
    replaces the ``role == "admin"`` cross-org "any entity's quota" bypasses
    with the admin-only ``quota:admin`` scope. Identical for a fresh admin
    token; an in-flight admin JWT gains it on next login (<=1h TTL), API-key
    admins immediately.
    """
    user = getattr(g, "user", None) or {}
    return perm.value in set(user.get("scope") or [])


# Bounds for the virtual-key quota columns. These values previously went
# from the raw request JSON straight into the DB with no type or range
# check at all, so a string, a negative number, NaN, or an absurd magnitude
# were all persisted verbatim and only surfaced later as a broken
# enforcement calculation. Ceilings are deliberately generous -- they exist
# to reject nonsense, not to express product policy.
MAX_BUDGET_USD = 1_000_000.0
MAX_RATE_LIMIT = 10_000_000

# Token-quota columns (org/user) previously took raw request JSON straight into
# the DB with no type or range check -- a string, negative, or absurd magnitude
# was persisted verbatim, exactly the flaw @validate_request + these bounds now
# close for the key quotas. Ceiling is deliberately generous: it rejects
# nonsense, not product policy.
MAX_TOKEN_QUOTA = 1_000_000_000_000


@dataclass(slots=True)
class SetUserQuotaRequest:
    """Request body for PUT /api/v1/quotas/user/<user_id>.

    Both fields are optional partial updates; ``None`` means "leave this
    column alone", matching the ``"field" in data`` presence test the handler
    used before it was given a schema.
    """

    token_quota_daily: int | None = None
    token_quota_monthly: int | None = None


@dataclass(slots=True)
class SetOrgQuotaRequest:
    """Request body for PUT /api/v1/quotas/org/<org_id>. Optional partial update."""

    token_quota_daily: int | None = None
    token_quota_monthly: int | None = None


@dataclass(slots=True)
class SetUserQuotaResponse:
    """Response body for a successful PUT /api/v1/quotas/user/<user_id>."""

    user_id: int
    username: str
    message: str


@dataclass(slots=True)
class SetOrgQuotaResponse:
    """Response body for a successful PUT /api/v1/quotas/org/<org_id>."""

    organization_id: int
    organization_name: str
    message: str


@dataclass(slots=True)
class SetKeyQuotaResponse:
    """Response body for a successful PUT /api/v1/quotas/key/<key_id>."""

    key_id: int
    key_name: str
    message: str


def _validate_token_quota_bounds(daily: int | None, monthly: int | None) -> str | None:
    """Return an error message for the first out-of-range token quota, else ``None``.

    Type coercion is handled by ``@validate_request``; this adds the finiteness
    and range checks a type annotation cannot express and rejects a boolean
    smuggled in where an integer is expected (``True``/``False`` are ``int``
    subclasses in Python).
    """
    for name, value in (("token_quota_daily", daily), ("token_quota_monthly", monthly)):
        if value is None:
            continue
        if isinstance(value, bool):
            return f"{name} must be an integer, not a boolean"
        if value < 0 or value > MAX_TOKEN_QUOTA:
            return f"{name} must be between 0 and {MAX_TOKEN_QUOTA}"
    return None


@dataclass(slots=True)
class SetKeyQuotaRequest:
    """Request body for PUT /api/v1/quotas/key/<key_id>.

    Every field is an optional partial update; ``None`` means "leave this
    column alone", matching the ``"field" in data`` presence test the
    handler used before it was given a schema.
    """

    budget_limit_daily: float | None = None
    budget_limit_monthly: float | None = None
    tpm_limit: int | None = None
    rpm_limit: int | None = None


def _validate_key_quota_bounds(data: SetKeyQuotaRequest) -> str | None:
    """Return an error message for the first out-of-range field, else ``None``.

    Type coercion is handled by ``@validate_request``; this adds the range
    and finiteness checks a type annotation cannot express.
    """
    for name, limit in (
        ("budget_limit_daily", MAX_BUDGET_USD),
        ("budget_limit_monthly", MAX_BUDGET_USD),
    ):
        value = getattr(data, name)
        if value is None:
            continue
        if not math.isfinite(value):
            return f"{name} must be a finite number"
        if value < 0 or value > limit:
            return f"{name} must be between 0 and {limit:g}"

    for name in ("tpm_limit", "rpm_limit"):
        value = getattr(data, name)
        if value is None:
            continue
        if isinstance(value, bool):
            return f"{name} must be an integer, not a boolean"
        if value < 0 or value > MAX_RATE_LIMIT:
            return f"{name} must be between 0 and {MAX_RATE_LIMIT}"

    return None


@api_v1_bp.route("/quotas", methods=["GET"])
@require_auth
@require_scope(Permission.QUOTA_LIST)
async def list_quotas():
    """List all quota configurations."""
    org_id = g.user.get("organization_id")
    page = PageRequest.from_request()
    # audit-2026-09-14-wave2: admin "see every org's quotas" now keys on the
    # admin-only quota:admin scope, not the role name.
    can_admin = _has_scope(Permission.QUOTA_ADMIN)

    def _fetch():
        # Each entity select is bounded and stably ordered (audit-2026-09-14
        # DoS finding): the admin branches select every org/user/key otherwise.
        limitby = page.limitby
        if can_admin:
            orgs = db(db.organizations.id > 0).select(limitby=limitby, orderby=db.organizations.id)
        else:
            orgs = db(db.organizations.id == org_id).select(
                limitby=limitby, orderby=db.organizations.id
            )

        if can_admin:
            users = db(db.users.id > 0).select(limitby=limitby, orderby=db.users.id)
        else:
            users = db(db.users.organization_id == org_id).select(
                limitby=limitby, orderby=db.users.id
            )

        if can_admin:
            keys = db(db.virtual_keys.id > 0).select(limitby=limitby, orderby=db.virtual_keys.id)
        else:
            keys = db(db.virtual_keys.organization_id == org_id).select(
                limitby=limitby, orderby=db.virtual_keys.id
            )

        return orgs, users, keys

    orgs, users, keys = await asyncio.to_thread(_fetch)

    quotas = []

    # Organization quotas
    for org in orgs:
        quotas.append(
            {
                "type": "organization",
                "id": org.id,
                "name": org.name,
                "token_quota_daily": org.token_quota_daily,
                "token_quota_monthly": org.token_quota_monthly,
                "enabled": org.enabled,
            }
        )

    # User quotas
    for user in users:
        quotas.append(
            {
                "type": "user",
                "id": user.id,
                "name": user.username,
                "organization_id": user.organization_id,
                "token_quota_daily": user.token_quota_daily,
                "token_quota_monthly": user.token_quota_monthly,
                "enabled": user.enabled,
            }
        )

    # Virtual key quotas
    for key in keys:
        quotas.append(
            {
                "type": "key",
                "id": key.id,
                "name": key.name,
                "user_id": key.user_id,
                "organization_id": key.organization_id,
                "budget_limit_daily": key.budget_limit_daily,
                "budget_limit_monthly": key.budget_limit_monthly,
                "tpm_limit": key.tpm_limit,
                "rpm_limit": key.rpm_limit,
                "enabled": key.enabled,
            }
        )

    return jsonify({"quotas": quotas, "total": len(quotas), **page.meta()})


@api_v1_bp.route("/quotas/user/<int:user_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.QUOTA_UPDATE)
@validate_response(SetUserQuotaResponse, 200)
@validate_request(SetUserQuotaRequest)
async def set_user_quota(user_id: int, data: SetUserQuotaRequest) -> ResponseReturnValue:
    """Set user quota."""
    user_role = g.user.get("role")
    org_id = g.user.get("organization_id")

    update_fields: dict[str, int] = {
        name: value
        for name in ("token_quota_daily", "token_quota_monthly")
        if (value := getattr(data, name)) is not None
    }
    if not update_fields:
        return jsonify({"error": "Request body required"}), 400

    database = _db()
    user = await asyncio.to_thread(lambda: database(database.users.id == user_id).select().first())

    if not user:
        return jsonify({"error": "User not found"}), 404

    # Permission check -- audit-2026-09-14-wave2: the admin "any org's user
    # quota" cross-org bypass now keys on the admin-only quota:admin scope, not
    # the role name. The Vuln C role-hierarchy guard below is NOT a cross-org
    # bypass (it stops a non-admin editing an admin USER's quota) and stays.
    if not _has_scope(Permission.QUOTA_ADMIN) and user.organization_id != org_id:
        return jsonify({"error": "Access denied"}), 403
    # Vuln C fix: prevent non-admin from modifying admin quota
    if user_role != "admin" and user.role == "admin":
        return jsonify({"error": "Cannot modify admin quota"}), 403

    bounds_error = _validate_token_quota_bounds(data.token_quota_daily, data.token_quota_monthly)
    if bounds_error:
        return jsonify({"error": bounds_error}), 400

    def _update() -> None:
        database(database.users.id == user_id).update(**update_fields)
        database.commit()

    await asyncio.to_thread(_update)

    return {
        "user_id": user_id,
        "username": user.username,
        "message": "User quota updated successfully",
    }


@api_v1_bp.route("/quotas/org/<int:org_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.QUOTA_ORG_UPDATE)
@validate_response(SetOrgQuotaResponse, 200)
@validate_request(SetOrgQuotaRequest)
async def set_organization_quota(org_id: int, data: SetOrgQuotaRequest) -> ResponseReturnValue:
    """Set organization quota (admin only)."""
    update_fields: dict[str, int] = {
        name: value
        for name in ("token_quota_daily", "token_quota_monthly")
        if (value := getattr(data, name)) is not None
    }
    if not update_fields:
        return jsonify({"error": "Request body required"}), 400

    database = _db()
    org = await asyncio.to_thread(
        lambda: database(database.organizations.id == org_id).select().first()
    )

    if not org:
        return jsonify({"error": "Organization not found"}), 404

    bounds_error = _validate_token_quota_bounds(data.token_quota_daily, data.token_quota_monthly)
    if bounds_error:
        return jsonify({"error": bounds_error}), 400

    def _update() -> None:
        database(database.organizations.id == org_id).update(**update_fields)
        database.commit()

    await asyncio.to_thread(_update)

    return {
        "organization_id": org_id,
        "organization_name": org.name,
        "message": "Organization quota updated successfully",
    }


@api_v1_bp.route("/quotas/key/<int:key_id>", methods=["PUT"])
@require_auth
@validate_response(SetKeyQuotaResponse, 200)
@validate_request(SetKeyQuotaRequest)
async def set_key_quota(key_id: int, data: SetKeyQuotaRequest) -> ResponseReturnValue:
    """Set virtual key quota.

    Every column this route writes is privileged (see
    ``keys.PRIVILEGED_KEY_FIELDS``): owning the key proves only that the
    caller may touch it, not that they may raise their own ceiling. The
    field split is shared verbatim with ``PUT /api/v1/keys/<key_id>`` so
    the two routes cannot diverge.
    """
    update_fields = {
        name: value
        for name in ("budget_limit_daily", "budget_limit_monthly", "tpm_limit", "rpm_limit")
        if (value := getattr(data, name)) is not None
    }

    if not update_fields:
        return jsonify({"error": "Request body required"}), 400

    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    database = _db()
    key = await asyncio.to_thread(
        lambda: database(database.virtual_keys.id == key_id).select().first()
    )

    if not key:
        return jsonify({"error": "Key not found"}), 404

    # Ownership check -- proves the caller may touch this key at all.
    # audit-2026-09-14-wave2: cross-org "touch any key" bypass keys on the
    # admin-only quota:admin scope, not the role name.
    if not _has_scope(Permission.QUOTA_ADMIN):
        if user_role == "resource_manager" and key.organization_id != org_id:
            return jsonify({"error": "Access denied"}), 403
        elif user_role not in ["resource_manager"] and key.user_id != user_id:
            return jsonify({"error": "Access denied"}), 403

    # Privilege check -- refuses outright rather than dropping the fields,
    # which would report success for a write that never happened.
    denied = privileged_key_fields_denied(update_fields)
    if denied:
        return privileged_fields_error(denied)

    bounds_error = _validate_key_quota_bounds(data)
    if bounds_error:
        return jsonify({"error": bounds_error}), 400

    def _update() -> None:
        database(database.virtual_keys.id == key_id).update(**update_fields)
        database.commit()

    await asyncio.to_thread(_update)

    return {"key_id": key_id, "key_name": key.name, "message": "Key quota updated successfully."}


@api_v1_bp.route("/quotas/status/<int:entity_id>", methods=["GET"])
@require_auth
async def get_quota_status(entity_id):
    """Get current quota status for an entity."""
    entity_type = request.args.get("type", "key")  # key, user, or org

    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    today = date.today()
    month_start = today.replace(day=1)

    if entity_type == "key":

        def _fetch_key():
            key = db(db.virtual_keys.id == entity_id).select().first()
            if not key:
                return None, None, None
            daily_usage = (
                db((db.token_usage.virtual_key_id == entity_id) & (db.token_usage.date == today))
                .select()
                .first()
            )
            monthly_usage = db(
                (db.token_usage.virtual_key_id == entity_id) & (db.token_usage.date >= month_start)
            ).select()
            return key, daily_usage, monthly_usage

        key, daily_usage, monthly_usage = await asyncio.to_thread(_fetch_key)

        if not key:
            return jsonify({"error": "Key not found"}), 404

        # Permission check — Vuln B fix: always scope to caller's org, never skip for reporter.
        # audit-2026-09-14-wave2: the admin "any key" bypass keys on quota:admin.
        if _has_scope(Permission.QUOTA_ADMIN):
            # Admin can access any key
            pass
        elif user_role == "resource_manager":
            if key.organization_id != org_id:
                return jsonify({"error": "Access denied"}), 403
        else:  # user, reporter, or any other role
            if key.user_id != user_id:
                return jsonify({"error": "Access denied"}), 403

        daily_tokens = daily_usage.waddleai_tokens if daily_usage else 0
        monthly_tokens = sum(u.waddleai_tokens or 0 for u in monthly_usage)
        monthly_cost = sum(u.cost_usd_total or 0 for u in monthly_usage)

        return jsonify(
            {
                "type": "key",
                "id": entity_id,
                "name": key.name,
                "quotas": {
                    "daily": {
                        "budget_limit": key.budget_limit_daily,
                        "used_cost": 0,  # TODO: Calculate from daily usage
                        "percentage": 0,
                    },
                    "monthly": {
                        "budget_limit": key.budget_limit_monthly,
                        "used_cost": monthly_cost,
                        "percentage": (
                            (monthly_cost / key.budget_limit_monthly * 100)
                            if key.budget_limit_monthly
                            else 0
                        ),
                    },
                    "rate_limits": {"tpm_limit": key.tpm_limit, "rpm_limit": key.rpm_limit},
                },
                "usage": {
                    "daily_tokens": daily_tokens,
                    "monthly_tokens": monthly_tokens,
                    "monthly_cost_usd": monthly_cost,
                },
            }
        )

    elif entity_type == "user":

        def _fetch_user():
            user = db(db.users.id == entity_id).select().first()
            if not user:
                return None, None, None
            daily_usage = db(
                (db.token_usage.user_id == entity_id) & (db.token_usage.date == today)
            ).select()
            monthly_usage = db(
                (db.token_usage.user_id == entity_id) & (db.token_usage.date >= month_start)
            ).select()
            return user, daily_usage, monthly_usage

        user, daily_usage, monthly_usage = await asyncio.to_thread(_fetch_user)

        if not user:
            return jsonify({"error": "User not found"}), 404

        # Permission check -- audit-2026-09-14-wave2: admin "any user" bypass on quota:admin.
        if not _has_scope(Permission.QUOTA_ADMIN):
            if user_role == "resource_manager" and user.organization_id != org_id:
                return jsonify({"error": "Access denied"}), 403
            elif user_role not in ["resource_manager"] and user.id != user_id:
                return jsonify({"error": "Access denied"}), 403

        daily_tokens = sum(u.waddleai_tokens or 0 for u in daily_usage)
        monthly_tokens = sum(u.waddleai_tokens or 0 for u in monthly_usage)

        return jsonify(
            {
                "type": "user",
                "id": entity_id,
                "name": user.username,
                "quotas": {
                    "daily": {
                        "limit": user.token_quota_daily,
                        "used": daily_tokens,
                        "remaining": max(0, (user.token_quota_daily or 0) - daily_tokens),
                        "percentage": (daily_tokens / user.token_quota_daily * 100)
                        if user.token_quota_daily
                        else 0,
                    },
                    "monthly": {
                        "limit": user.token_quota_monthly,
                        "used": monthly_tokens,
                        "remaining": max(0, (user.token_quota_monthly or 0) - monthly_tokens),
                        "percentage": (
                            (monthly_tokens / user.token_quota_monthly * 100)
                            if user.token_quota_monthly
                            else 0
                        ),
                    },
                },
            }
        )

    elif entity_type == "org":

        def _fetch_org():
            org = db(db.organizations.id == entity_id).select().first()
            if not org:
                return None, None, None
            daily_usage = db(
                (db.token_usage.organization_id == entity_id) & (db.token_usage.date == today)
            ).select()
            monthly_usage = db(
                (db.token_usage.organization_id == entity_id) & (db.token_usage.date >= month_start)
            ).select()
            return org, daily_usage, monthly_usage

        org, daily_usage, monthly_usage = await asyncio.to_thread(_fetch_org)

        if not org:
            return jsonify({"error": "Organization not found"}), 404

        # Permission check -- audit-2026-09-14-wave2: admin "any org" bypass on quota:admin.
        if not _has_scope(Permission.QUOTA_ADMIN) and entity_id != org_id:
            return jsonify({"error": "Access denied"}), 403

        daily_tokens = sum(u.waddleai_tokens or 0 for u in daily_usage)
        monthly_tokens = sum(u.waddleai_tokens or 0 for u in monthly_usage)

        return jsonify(
            {
                "type": "organization",
                "id": entity_id,
                "name": org.name,
                "quotas": {
                    "daily": {
                        "limit": org.token_quota_daily,
                        "used": daily_tokens,
                        "remaining": max(0, (org.token_quota_daily or 0) - daily_tokens),
                        "percentage": (daily_tokens / org.token_quota_daily * 100)
                        if org.token_quota_daily
                        else 0,
                    },
                    "monthly": {
                        "limit": org.token_quota_monthly,
                        "used": monthly_tokens,
                        "remaining": max(0, (org.token_quota_monthly or 0) - monthly_tokens),
                        "percentage": (
                            (monthly_tokens / org.token_quota_monthly * 100)
                            if org.token_quota_monthly
                            else 0
                        ),
                    },
                },
            }
        )

    return jsonify({"error": "Invalid entity type"}), 400
