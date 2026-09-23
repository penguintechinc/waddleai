"""WaddleAI Management API v1 - Organization Management Endpoints."""

import asyncio
from dataclasses import dataclass
from datetime import datetime

from penguin_dal.db import DB
from quart import g, jsonify
from quart_schema import validate_request, validate_response

from shared.auth.rbac import Permission

from ...extensions import db
from ...services.cilium_policy import CiliumPolicyReconciler
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

    Mirrors ``auth.require_scope``'s own scope-only check (the ``scope`` claim
    on ``g.user``, never the ``role`` claim) for the in-handler "admin sees
    every org, everyone else only their own" branch decisions. The admin-tier
    scope here is admin-exclusive so a non-admin role never widens its reach.
    """
    user = getattr(g, "user", None) or {}
    return permission.value in set(user.get("scope") or [])


# ---------------------------------------------------------------------------
# OpenAPI request/response models. Request fields are all-Optional so
# quart-schema never pre-empts the handler's own presence checks and their
# exact 400 messages. Response models pin exactly today's fields.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CreateOrgRequest:
    """Request body for POST /api/v1/organizations."""

    name: str | None = None
    description: str | None = ""
    token_quota_daily: int | None = 100000
    token_quota_monthly: int | None = 1000000
    default_model: str | None = None


@dataclass(slots=True)
class UpdateOrgRequest:
    """Request body for PUT /api/v1/organizations/<org_id>. Partial update."""

    name: str | None = None
    description: str | None = None
    token_quota_daily: int | None = None
    token_quota_monthly: int | None = None
    default_model: str | None = None
    enabled: bool | None = None


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
class OrgListItem:
    """A single organization in the list response -- exact fields shipped today."""

    id: int
    name: str
    description: str | None
    token_quota_daily: int | None
    token_quota_monthly: int | None
    default_model: str | None
    enabled: bool
    user_count: int
    created_at: str | None


@dataclass(slots=True)
class OrgsListResponse:
    """Response body for GET /api/v1/organizations."""

    organizations: list[OrgListItem]
    total: int
    pagination: PaginationMeta


@dataclass(slots=True)
class OrgStatistics:
    """Per-organization counts embedded in the org detail response."""

    user_count: int
    key_count: int


@dataclass(slots=True)
class OrgDetailResponse:
    """Response body for GET /api/v1/organizations/<org_id> -- exact fields today."""

    id: int
    name: str
    description: str | None
    token_quota_daily: int | None
    token_quota_monthly: int | None
    default_model: str | None
    enabled: bool
    created_at: str | None
    statistics: OrgStatistics


@dataclass(slots=True)
class CreateOrgResponse:
    """Response body for POST /api/v1/organizations."""

    id: int
    name: str
    message: str


@dataclass(slots=True)
class OrgUsageWindow:
    """Daily usage block in the organization-usage response."""

    tokens: int
    quota: int | None
    percentage: float


@dataclass(slots=True)
class OrgUsageMonthly:
    """Monthly usage block (adds cost) in the organization-usage response."""

    tokens: int
    quota: int | None
    percentage: float
    cost_usd: float


@dataclass(slots=True)
class OrgUsageBlock:
    """The daily/monthly usage pair."""

    daily: OrgUsageWindow
    monthly: OrgUsageMonthly


@dataclass(slots=True)
class OrgUsageResponse:
    """Response body for GET /api/v1/organizations/<org_id>/usage."""

    organization_id: int
    organization_name: str
    usage: OrgUsageBlock


def _trigger_cilium_reconcile() -> None:
    """Fire a non-blocking Cilium policy reconcile after an org write.

    Fire-and-forget: `CiliumPolicyReconciler.reconcile()` never raises (it
    degrades to a `ReconcileStatus` internally and logs), so this can only
    ever affect Cilium CRDs, never the calling request's response. Keeps org
    create/update CEC churn scoped to just the org that changed rather than
    reconciling on every key CRUD (spec §12.1 change note).
    """
    try:
        reconciler = CiliumPolicyReconciler(db)
        asyncio.create_task(asyncio.to_thread(reconciler.reconcile))
    except RuntimeError:
        # No running event loop in this context (e.g. some test harnesses) — skip.
        pass


@api_v1_bp.route("/organizations", methods=["GET"])
@require_auth
@validate_response(OrgsListResponse, 200)
async def list_organizations():
    """List organizations, scoped by the caller's OIDC scopes and bounded by pagination."""
    org_id = g.user.get("organization_id")

    # admin (ORG_ADMIN_UPDATE, admin-only) sees every org; everyone else only
    # their own -- the exact two tiers the former role-name check produced.
    can_read_all = _has_scope(Permission.ORG_ADMIN_UPDATE)
    page = PageRequest.from_request()

    def _fetch():
        if can_read_all:
            query = db.organizations.id > 0
        else:
            query = db.organizations.id == org_id
        orgs = db(query).select(limitby=page.limitby, orderby=db.organizations.id)

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

    return {"organizations": result, "total": len(result), **page.meta()}


@api_v1_bp.route("/organizations/<int:org_id>", methods=["GET"])
@require_auth
@validate_response(OrgDetailResponse, 200)
async def get_organization(org_id):
    """Get organization details."""
    user_org_id = g.user.get("organization_id")

    # Permission check -- admin (ORG_ADMIN_UPDATE) may view any org; everyone
    # else is confined to their own. Only the role-NAME test became a scope
    # test; the tenant comparison is unchanged.
    if not _has_scope(Permission.ORG_ADMIN_UPDATE) and org_id != user_org_id:
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

    return {
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


@api_v1_bp.route("/organizations", methods=["POST"])
@require_auth
@require_scope(Permission.ORG_CREATE)
@validate_response(CreateOrgResponse, 201)
@validate_request(CreateOrgRequest)
async def create_organization(data: CreateOrgRequest):
    """Create a new organization (admin only)."""
    if not data.name:
        return jsonify({"error": "name is required"}), 400

    database = _db()

    # Check for existing organization
    existing = await asyncio.to_thread(
        lambda: database(database.organizations.name == data.name).select().first()
    )
    if existing:
        return jsonify({"error": "Organization name already exists"}), 409

    def _insert():
        new_org_id = database.organizations.insert(
            name=data.name,
            description=data.description if data.description is not None else "",
            token_quota_daily=data.token_quota_daily,
            token_quota_monthly=data.token_quota_monthly,
            default_model=data.default_model,
            enabled=True,
            created_at=datetime.utcnow(),
        )
        database.commit()
        return new_org_id

    org_id = await asyncio.to_thread(_insert)
    _trigger_cilium_reconcile()

    return {"id": org_id, "name": data.name, "message": "Organization created successfully"}, 201


@api_v1_bp.route("/organizations/<int:org_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.ORG_ADMIN_UPDATE)
@validate_response(MessageResponse, 200)
@validate_request(UpdateOrgRequest)
async def update_organization(org_id, data: UpdateOrgRequest):
    """Update organization (admin only)."""
    database = _db()
    org = await asyncio.to_thread(
        lambda: database(database.organizations.id == org_id).select().first()
    )

    if not org:
        return jsonify({"error": "Organization not found"}), 404

    update_fields: dict[str, object] = {}

    if data.name is not None:
        # Check name uniqueness
        existing = await asyncio.to_thread(
            lambda: (
                database(
                    (database.organizations.name == data.name)
                    & (database.organizations.id != org_id)
                )
                .select()
                .first()
            )
        )
        if existing:
            return jsonify({"error": "Organization name already exists"}), 409
        update_fields["name"] = data.name

    if data.description is not None:
        update_fields["description"] = data.description

    if data.token_quota_daily is not None:
        update_fields["token_quota_daily"] = data.token_quota_daily

    if data.token_quota_monthly is not None:
        update_fields["token_quota_monthly"] = data.token_quota_monthly

    if data.default_model is not None:
        update_fields["default_model"] = data.default_model

    if data.enabled is not None:
        update_fields["enabled"] = data.enabled

    if update_fields:

        def _update():
            database(database.organizations.id == org_id).update(**update_fields)
            database.commit()

        await asyncio.to_thread(_update)
        _trigger_cilium_reconcile()

    return {"message": "Organization updated successfully"}


@api_v1_bp.route("/organizations/<int:org_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.ORG_DELETE)
@validate_response(MessageResponse, 200)
async def delete_organization(org_id):
    """Delete organization (admin only)."""
    org = await asyncio.to_thread(lambda: db(db.organizations.id == org_id).select().first())

    if not org:
        return jsonify({"error": "Organization not found"}), 404

    # Prevent deletion of default organization
    if org.name == "default":
        return jsonify({"error": "Cannot delete default organization"}), 400

    # Check for users
    user_count = await asyncio.to_thread(lambda: db(db.users.organization_id == org_id).count())
    if user_count > 0:
        return jsonify(
            {"error": "Cannot delete organization with users", "user_count": user_count}
        ), 400

    # Soft delete by disabling
    def _disable():
        db(db.organizations.id == org_id).update(enabled=False)
        db.commit()

    await asyncio.to_thread(_disable)

    return {"message": "Organization disabled successfully"}


@api_v1_bp.route("/organizations/<int:org_id>/usage", methods=["GET"])
@require_auth
@validate_response(OrgUsageResponse, 200)
async def get_organization_usage(org_id):
    """Get organization usage statistics."""
    user_org_id = g.user.get("organization_id")

    # Permission check -- Vuln B fix preserved: only a caller holding
    # ANALYTICS_SYSTEM (admin-only, cross-org analytics) may read another org's
    # usage; every other caller is confined to their own org. The role-NAME
    # test became a scope test; the tenant comparison is unchanged.
    if not _has_scope(Permission.ANALYTICS_SYSTEM) and org_id != user_org_id:
        return jsonify({"error": "Access denied"}), 403

    org = await asyncio.to_thread(lambda: db(db.organizations.id == org_id).select().first())

    if not org:
        return jsonify({"error": "Organization not found"}), 404

    # Get usage from token_usage table
    from datetime import date

    today = date.today()
    month_start = today.replace(day=1)

    def _fetch_usage():
        daily = db(
            (db.token_usage.organization_id == org_id) & (db.token_usage.date == today)
        ).select()
        monthly = db(
            (db.token_usage.organization_id == org_id) & (db.token_usage.date >= month_start)
        ).select()
        return daily, monthly

    daily_usage, monthly_usage = await asyncio.to_thread(_fetch_usage)

    daily_tokens = sum(u.waddleai_tokens or 0 for u in daily_usage)
    monthly_tokens = sum(u.waddleai_tokens or 0 for u in monthly_usage)
    monthly_cost = sum(u.cost_usd_total or 0 for u in monthly_usage)

    return {
        "organization_id": org_id,
        "organization_name": org.name,
        "usage": {
            "daily": {
                "tokens": daily_tokens,
                "quota": org.token_quota_daily,
                "percentage": (daily_tokens / org.token_quota_daily * 100)
                if org.token_quota_daily
                else 0,
            },
            "monthly": {
                "tokens": monthly_tokens,
                "quota": org.token_quota_monthly,
                "percentage": (monthly_tokens / org.token_quota_monthly * 100)
                if org.token_quota_monthly
                else 0,
                "cost_usd": monthly_cost,
            },
        },
    }
