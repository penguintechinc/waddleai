"""
WaddleAI Management API v1 - Organization Management Endpoints
"""

import asyncio
from datetime import datetime

from quart import g, jsonify, request

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth, require_role


@api_v1_bp.route("/organizations", methods=["GET"])
@require_auth
async def list_organizations():
    """List organizations"""
    user_role = g.user.get("role")
    org_id = g.user.get("organization_id")

    def _fetch():
        if user_role == "admin":
            orgs = db(db.organizations.id > 0).select()
        else:
            orgs = db(db.organizations.id == org_id).select()

        result = []
        for org in orgs:
            # Get user count
            user_count = db(db.users.organization_id == org.id).count()
            result.append((org, user_count))
        return result

    orgs_with_counts = await asyncio.to_thread(_fetch)

    result = []
    for org, user_count in orgs_with_counts:
        result.append(
            {
                "id": org.id,
                "name": org.name,
                "description": org.description,
                "token_quota_daily": org.token_quota_daily,
                "token_quota_monthly": org.token_quota_monthly,
                "default_model": org.default_model,
                "enabled": org.enabled,
                "user_count": user_count,
                "created_at": org.created_at.isoformat() if org.created_at else None,
            }
        )

    return jsonify({"organizations": result, "total": len(result)})


@api_v1_bp.route("/organizations/<int:org_id>", methods=["GET"])
@require_auth
async def get_organization(org_id):
    """Get organization details"""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    # Permission check
    if user_role not in ["admin"] and org_id != user_org_id:
        return jsonify({"error": "Access denied"}), 403

    org = await asyncio.to_thread(lambda: db(db.organizations.id == org_id).select().first())

    if not org:
        return jsonify({"error": "Organization not found"}), 404

    # Get statistics
    def _stats():
        return (
            db(db.users.organization_id == org_id).count(),
            db(db.virtual_keys.organization_id == org_id).count(),
        )

    user_count, key_count = await asyncio.to_thread(_stats)

    return jsonify(
        {
            "id": org.id,
            "name": org.name,
            "description": org.description,
            "token_quota_daily": org.token_quota_daily,
            "token_quota_monthly": org.token_quota_monthly,
            "default_model": org.default_model,
            "enabled": org.enabled,
            "created_at": org.created_at.isoformat() if org.created_at else None,
            "statistics": {"user_count": user_count, "key_count": key_count},
        }
    )


@api_v1_bp.route("/organizations", methods=["POST"])
@require_auth
@require_role("admin")
async def create_organization():
    """Create a new organization (admin only)"""
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    if "name" not in data:
        return jsonify({"error": "name is required"}), 400

    # Check for existing organization
    existing = await asyncio.to_thread(lambda: db(db.organizations.name == data["name"]).select().first())
    if existing:
        return jsonify({"error": "Organization name already exists"}), 409

    def _insert():
        new_org_id = db.organizations.insert(
            name=data["name"],
            description=data.get("description", ""),
            token_quota_daily=data.get("token_quota_daily", 100000),
            token_quota_monthly=data.get("token_quota_monthly", 1000000),
            default_model=data.get("default_model"),
            enabled=True,
            created_at=datetime.utcnow(),
        )
        db.commit()
        return new_org_id

    org_id = await asyncio.to_thread(_insert)

    return jsonify({"id": org_id, "name": data["name"], "message": "Organization created successfully"}), 201


@api_v1_bp.route("/organizations/<int:org_id>", methods=["PUT"])
@require_auth
@require_role("admin")
async def update_organization(org_id):
    """Update organization (admin only)"""
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    org = await asyncio.to_thread(lambda: db(db.organizations.id == org_id).select().first())

    if not org:
        return jsonify({"error": "Organization not found"}), 404

    update_fields = {}

    if "name" in data:
        # Check name uniqueness
        existing = await asyncio.to_thread(
            lambda: db((db.organizations.name == data["name"]) & (db.organizations.id != org_id)).select().first()
        )
        if existing:
            return jsonify({"error": "Organization name already exists"}), 409
        update_fields["name"] = data["name"]

    if "description" in data:
        update_fields["description"] = data["description"]

    if "token_quota_daily" in data:
        update_fields["token_quota_daily"] = data["token_quota_daily"]

    if "token_quota_monthly" in data:
        update_fields["token_quota_monthly"] = data["token_quota_monthly"]

    if "default_model" in data:
        update_fields["default_model"] = data["default_model"]

    if "enabled" in data:
        update_fields["enabled"] = data["enabled"]

    if update_fields:

        def _update():
            db(db.organizations.id == org_id).update(**update_fields)
            db.commit()

        await asyncio.to_thread(_update)

    return jsonify({"message": "Organization updated successfully"})


@api_v1_bp.route("/organizations/<int:org_id>", methods=["DELETE"])
@require_auth
@require_role("admin")
async def delete_organization(org_id):
    """Delete organization (admin only)"""
    org = await asyncio.to_thread(lambda: db(db.organizations.id == org_id).select().first())

    if not org:
        return jsonify({"error": "Organization not found"}), 404

    # Prevent deletion of default organization
    if org.name == "default":
        return jsonify({"error": "Cannot delete default organization"}), 400

    # Check for users
    user_count = await asyncio.to_thread(lambda: db(db.users.organization_id == org_id).count())
    if user_count > 0:
        return jsonify({"error": "Cannot delete organization with users", "user_count": user_count}), 400

    # Soft delete by disabling
    def _disable():
        db(db.organizations.id == org_id).update(enabled=False)
        db.commit()

    await asyncio.to_thread(_disable)

    return jsonify({"message": "Organization disabled successfully"})


@api_v1_bp.route("/organizations/<int:org_id>/usage", methods=["GET"])
@require_auth
async def get_organization_usage(org_id):
    """Get organization usage statistics"""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    # Permission check — Vuln B fix: always scope to caller's org, never skip for reporter
    if user_role == "admin":
        # Admin can access any org
        pass
    elif user_role in ["resource_manager", "reporter"]:
        # Both must be scoped to their own org
        if org_id != user_org_id:
            return jsonify({"error": "Access denied"}), 403
    else:
        # Other roles scoped to their org
        if org_id != user_org_id:
            return jsonify({"error": "Access denied"}), 403

    org = await asyncio.to_thread(lambda: db(db.organizations.id == org_id).select().first())

    if not org:
        return jsonify({"error": "Organization not found"}), 404

    # Get usage from token_usage table
    from datetime import date

    today = date.today()
    month_start = today.replace(day=1)

    def _fetch_usage():
        daily = db((db.token_usage.organization_id == org_id) & (db.token_usage.date == today)).select()
        monthly = db((db.token_usage.organization_id == org_id) & (db.token_usage.date >= month_start)).select()
        return daily, monthly

    daily_usage, monthly_usage = await asyncio.to_thread(_fetch_usage)

    daily_tokens = sum(u.waddleai_tokens or 0 for u in daily_usage)
    monthly_tokens = sum(u.waddleai_tokens or 0 for u in monthly_usage)
    monthly_cost = sum(u.cost_usd_total or 0 for u in monthly_usage)

    return jsonify(
        {
            "organization_id": org_id,
            "organization_name": org.name,
            "usage": {
                "daily": {
                    "tokens": daily_tokens,
                    "quota": org.token_quota_daily,
                    "percentage": (daily_tokens / org.token_quota_daily * 100) if org.token_quota_daily else 0,
                },
                "monthly": {
                    "tokens": monthly_tokens,
                    "quota": org.token_quota_monthly,
                    "percentage": (monthly_tokens / org.token_quota_monthly * 100) if org.token_quota_monthly else 0,
                    "cost_usd": monthly_cost,
                },
            },
        }
    )
