"""
WaddleAI Management API v1 - Quota Management Endpoints
"""

import asyncio
from datetime import date

from quart import g, jsonify, request

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth, require_role


@api_v1_bp.route("/quotas", methods=["GET"])
@require_auth
@require_role("admin", "resource_manager")
async def list_quotas():
    """List all quota configurations"""
    user_role = g.user.get("role")
    org_id = g.user.get("organization_id")

    def _fetch():
        if user_role == "admin":
            orgs = db(db.organizations.id > 0).select()
        else:
            orgs = db(db.organizations.id == org_id).select()

        if user_role == "admin":
            users = db(db.users.id > 0).select()
        else:
            users = db(db.users.organization_id == org_id).select()

        if user_role == "admin":
            keys = db(db.virtual_keys.id > 0).select()
        else:
            keys = db(db.virtual_keys.organization_id == org_id).select()

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

    return jsonify({"quotas": quotas, "total": len(quotas)})


@api_v1_bp.route("/quotas/user/<int:user_id>", methods=["PUT"])
@require_auth
@require_role("admin", "resource_manager")
async def set_user_quota(user_id):
    """Set user quota"""
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

    update_fields = {}

    if "token_quota_daily" in data:
        update_fields["token_quota_daily"] = data["token_quota_daily"]

    if "token_quota_monthly" in data:
        update_fields["token_quota_monthly"] = data["token_quota_monthly"]

    if update_fields:

        def _update():
            db(db.users.id == user_id).update(**update_fields)
            db.commit()

        await asyncio.to_thread(_update)

    return jsonify({"user_id": user_id, "username": user.username, "message": "User quota updated successfully"})


@api_v1_bp.route("/quotas/org/<int:org_id>", methods=["PUT"])
@require_auth
@require_role("admin")
async def set_organization_quota(org_id):
    """Set organization quota (admin only)"""
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    org = await asyncio.to_thread(lambda: db(db.organizations.id == org_id).select().first())

    if not org:
        return jsonify({"error": "Organization not found"}), 404

    update_fields = {}

    if "token_quota_daily" in data:
        update_fields["token_quota_daily"] = data["token_quota_daily"]

    if "token_quota_monthly" in data:
        update_fields["token_quota_monthly"] = data["token_quota_monthly"]

    if update_fields:

        def _update():
            db(db.organizations.id == org_id).update(**update_fields)
            db.commit()

        await asyncio.to_thread(_update)

    return jsonify(
        {"organization_id": org_id, "organization_name": org.name, "message": "Organization quota updated successfully"}
    )


@api_v1_bp.route("/quotas/key/<int:key_id>", methods=["PUT"])
@require_auth
async def set_key_quota(key_id):
    """Set virtual key quota"""
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    key = await asyncio.to_thread(lambda: db(db.virtual_keys.id == key_id).select().first())

    if not key:
        return jsonify({"error": "Key not found"}), 404

    # Permission check
    if user_role not in ["admin"]:
        if user_role == "resource_manager" and key.organization_id != org_id:
            return jsonify({"error": "Access denied"}), 403
        elif user_role not in ["resource_manager"] and key.user_id != user_id:
            return jsonify({"error": "Access denied"}), 403

    update_fields = {}

    if "budget_limit_daily" in data:
        update_fields["budget_limit_daily"] = data["budget_limit_daily"]

    if "budget_limit_monthly" in data:
        update_fields["budget_limit_monthly"] = data["budget_limit_monthly"]

    if "tpm_limit" in data:
        update_fields["tpm_limit"] = data["tpm_limit"]

    if "rpm_limit" in data:
        update_fields["rpm_limit"] = data["rpm_limit"]

    if update_fields:
        # Mark for re-sync
        update_fields["ailb_sync_status"] = "pending"

        def _update():
            db(db.virtual_keys.id == key_id).update(**update_fields)
            db.commit()

        await asyncio.to_thread(_update)

    return jsonify(
        {"key_id": key_id, "key_name": key.name, "message": "Key quota updated successfully. Re-sync to AILB required."}
    )


@api_v1_bp.route("/quotas/status/<int:entity_id>", methods=["GET"])
@require_auth
async def get_quota_status(entity_id):
    """Get current quota status for an entity"""
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
                db((db.token_usage.virtual_key_id == entity_id) & (db.token_usage.date == today)).select().first()
            )
            monthly_usage = db(
                (db.token_usage.virtual_key_id == entity_id) & (db.token_usage.date >= month_start)
            ).select()
            return key, daily_usage, monthly_usage

        key, daily_usage, monthly_usage = await asyncio.to_thread(_fetch_key)

        if not key:
            return jsonify({"error": "Key not found"}), 404

        # Permission check
        if user_role not in ["admin", "reporter"]:
            if user_role == "resource_manager" and key.organization_id != org_id:
                return jsonify({"error": "Access denied"}), 403
            elif user_role not in ["resource_manager"] and key.user_id != user_id:
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
                            (monthly_cost / key.budget_limit_monthly * 100) if key.budget_limit_monthly else 0
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
            daily_usage = db((db.token_usage.user_id == entity_id) & (db.token_usage.date == today)).select()
            monthly_usage = db(
                (db.token_usage.user_id == entity_id) & (db.token_usage.date >= month_start)
            ).select()
            return user, daily_usage, monthly_usage

        user, daily_usage, monthly_usage = await asyncio.to_thread(_fetch_user)

        if not user:
            return jsonify({"error": "User not found"}), 404

        # Permission check
        if user_role not in ["admin"]:
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
                        "percentage": (daily_tokens / user.token_quota_daily * 100) if user.token_quota_daily else 0,
                    },
                    "monthly": {
                        "limit": user.token_quota_monthly,
                        "used": monthly_tokens,
                        "remaining": max(0, (user.token_quota_monthly or 0) - monthly_tokens),
                        "percentage": (
                            (monthly_tokens / user.token_quota_monthly * 100) if user.token_quota_monthly else 0
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

        # Permission check
        if user_role not in ["admin"] and entity_id != org_id:
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
                        "percentage": (daily_tokens / org.token_quota_daily * 100) if org.token_quota_daily else 0,
                    },
                    "monthly": {
                        "limit": org.token_quota_monthly,
                        "used": monthly_tokens,
                        "remaining": max(0, (org.token_quota_monthly or 0) - monthly_tokens),
                        "percentage": (
                            (monthly_tokens / org.token_quota_monthly * 100) if org.token_quota_monthly else 0
                        ),
                    },
                },
            }
        )

    return jsonify({"error": "Invalid entity type"}), 400
