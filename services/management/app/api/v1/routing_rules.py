"""WaddleAI Management API v1 - Heuristic Routing Rule Endpoints (spec §7.2).

CRUD for ``routing_rules_v2``: cascade stage-1 heuristic rules (``priority``,
``match`` predicate, ``action``) evaluated cheapest-first before the stage-2
classifier runs. Admin surface for ``shared.routing.heuristics.evaluate_rules``.

Distinct from the older, unrelated ``routing_rules`` table (LLM
connection-link routing, a separate pre-existing feature) -- this module
only ever touches ``routing_rules_v2``.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from penguin_dal.db import DB
from quart import Blueprint, g, jsonify, request
from quart_schema import validate_request, validate_response

from shared.auth.rbac import Permission

from ...extensions import db
from ._pagination import PageRequest
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)

routing_rules_bp = Blueprint("routing_rules", __name__, url_prefix="/api/v1/routing/rules")

_WRITABLE_FIELDS = ("name", "priority", "match", "action", "enabled")


# ---------------------------------------------------------------------------
# OpenAPI request/response models (audit-2026-09-14). Request fields Optional
# so the handler's own presence/value checks stay authoritative; response
# models mirror EXACTLY the keys each handler returns.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CreateRuleRequest:
    """Request body for POST /api/v1/routing/rules/."""

    name: str | None = None
    match: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    priority: int | None = None
    enabled: bool | None = None
    organization_id: int | None = None


@dataclass(slots=True)
class UpdateRuleRequest:
    """Request body for PUT /api/v1/routing/rules/<id>. Every field is a partial update."""

    name: str | None = None
    priority: int | None = None
    match: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    enabled: bool | None = None


@dataclass(slots=True)
class RuleRow:
    """A single routing_rules_v2 row -- mirrors ``_row_to_dict`` exactly."""

    id: int
    name: str
    priority: int
    match: dict[str, Any]
    action: dict[str, Any]
    enabled: bool
    organization_id: int | None
    created_at: str | None


@dataclass(slots=True)
class RulePagination:
    """Pagination envelope merged into the list response."""

    page: int
    limit: int
    total: int | None
    pages: int | None


@dataclass(slots=True)
class RuleListMeta:
    """``meta`` for the list response."""

    total: int
    timestamp: str


@dataclass(slots=True)
class RuleTimestampMeta:
    """``meta`` carrying only a timestamp (get/update responses)."""

    timestamp: str


@dataclass(slots=True)
class RuleActionMeta:
    """``meta`` carrying an action verb plus timestamp (create/delete)."""

    action: str
    timestamp: str


@dataclass(slots=True)
class RuleDeletedRef:
    """``data`` for a delete response -- the deleted row's id."""

    id: int


@dataclass(slots=True)
class RuleListResponse:
    """Response body for GET /api/v1/routing/rules/."""

    status: str
    data: list[RuleRow]
    meta: RuleListMeta
    pagination: RulePagination


@dataclass(slots=True)
class RuleDetailResponse:
    """Response body for GET /api/v1/routing/rules/<id>."""

    status: str
    data: RuleRow
    meta: RuleTimestampMeta


@dataclass(slots=True)
class RuleCreateResponse:
    """Response body for a successful POST."""

    status: str
    data: RuleRow
    meta: RuleActionMeta


@dataclass(slots=True)
class RuleUpdateResponse:
    """Response body for a successful PUT."""

    status: str
    data: RuleRow
    meta: RuleTimestampMeta


@dataclass(slots=True)
class RuleDeleteResponse:
    """Response body for a successful DELETE."""

    status: str
    data: RuleDeletedRef
    meta: RuleActionMeta


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


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a penguin-dal routing_rules_v2 row into a serializable dict."""
    return {
        "id": row.id,
        "name": row.name,
        "priority": row.priority,
        "match": row.match,
        "action": row.action,
        "enabled": row.enabled,
        "organization_id": row.organization_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _has_scope(perm: Permission) -> bool:
    """True when the caller's OIDC ``scope`` claim carries ``perm``.

    Authoritative ``scope`` claim only, never the ``role`` claim (house
    scope-only policy, see ``auth.require_scope``). MUST be called from the
    request context, not a DB worker thread: handlers compute the
    admin-capability boolean here and capture it in the thread closures.

    audit-2026-09-14-wave2: replaces the ``role == "admin"`` cross-org bypass
    (write a global/other-org rule) with the admin-only ``routing_rule:admin``
    scope. Identical for a fresh admin token; an in-flight admin JWT gains it
    on next login (<=1h TTL), API-key admins immediately.
    """
    user = getattr(g, "user", None) or {}
    return perm.value in set(user.get("scope") or [])


def _visible_query(can_admin: bool, user_org_id: int | None):
    """Admin (routing_rule:admin) sees every rule; else global + their own org's."""
    table = _db().routing_rules_v2
    if can_admin:
        return table.id > 0
    return (table.organization_id == None) | (table.organization_id == user_org_id)  # noqa: E711


def _can_write(
    can_admin: bool, user_role: str, user_org_id: int | None, organization_id: int | None
) -> bool:
    """Admin (routing_rule:admin) manages any rule; resource_manager only own org's."""
    if can_admin:
        return True
    return (
        user_role == "resource_manager"
        and organization_id is not None
        and organization_id == user_org_id
    )


@routing_rules_bp.route("/", methods=["GET"])
@require_auth
@validate_response(RuleListResponse, 200)
async def list_rules() -> tuple:
    """List visible routing_rules_v2 rows, priority-ordered (bounded by ``?page=&limit=``)."""
    can_admin = _has_scope(Permission.ROUTING_RULE_ADMIN)
    user_org_id = g.user.get("organization_id")
    enabled_param: str | None = request.args.get("enabled")
    page = PageRequest.from_request()

    def _fetch():
        query = _visible_query(can_admin, user_org_id)
        if enabled_param is not None:
            enabled_val: bool = enabled_param.lower() in ("true", "1", "yes")
            query &= db.routing_rules_v2.enabled == enabled_val
        scoped = db(query)
        rows = scoped.select(limitby=page.limitby, orderby=db.routing_rules_v2.priority)
        return rows, scoped.count()

    rows, total = await asyncio.to_thread(_fetch)
    entries = [_row_to_dict(r) for r in rows]

    return (
        {
            "status": "success",
            "data": entries,
            "meta": {"total": len(entries), "timestamp": datetime.utcnow().isoformat() + "Z"},
            **page.meta(total),
        },
        200,
    )


@routing_rules_bp.route("/<int:rule_id>", methods=["GET"])
@require_auth
@validate_response(RuleDetailResponse, 200)
async def get_rule(rule_id: int) -> tuple:
    """Get a single routing_rules_v2 row by ID (org-visibility scoped)."""
    can_admin = _has_scope(Permission.ROUTING_RULE_ADMIN)
    user_org_id = g.user.get("organization_id")

    def _fetch():
        database = _db()
        query = _visible_query(can_admin, user_org_id) & (database.routing_rules_v2.id == rule_id)
        return database(query).select().first()

    row = await asyncio.to_thread(_fetch)
    if not row:
        return jsonify({"status": "error", "error": "Rule not found"}), 404

    return (
        {
            "status": "success",
            "data": _row_to_dict(row),
            "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
        },
        200,
    )


@routing_rules_bp.route("/", methods=["POST"])
@require_auth
@require_scope(Permission.ROUTING_RULE_WRITE)
@validate_response(RuleCreateResponse, 201)
@validate_request(CreateRuleRequest)
async def create_rule(data: CreateRuleRequest) -> tuple:
    """Create a routing_rules_v2 row."""
    if data.name is None:
        return jsonify({"status": "error", "error": "name is required"}), 400
    if data.match is None:
        return jsonify({"status": "error", "error": "match is required"}), 400
    if data.action is None:
        return jsonify({"status": "error", "error": "action is required"}), 400

    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    can_admin = _has_scope(Permission.ROUTING_RULE_ADMIN)
    organization_id: int | None = data.organization_id
    if not _can_write(can_admin, user_role, user_org_id, organization_id):
        return jsonify({"status": "error", "error": "Access denied for this organization_id"}), 403

    def _insert():
        new_id = db.routing_rules_v2.insert(
            name=data.name,
            priority=data.priority if data.priority is not None else 100,
            match=data.match,
            action=data.action,
            enabled=data.enabled if data.enabled is not None else True,
            organization_id=organization_id,
            created_at=datetime.utcnow(),
        )
        db.commit()
        return db(db.routing_rules_v2.id == new_id).select().first()

    row = await asyncio.to_thread(_insert)

    return (
        {
            "status": "success",
            "data": _row_to_dict(row),
            "meta": {"action": "created", "timestamp": datetime.utcnow().isoformat() + "Z"},
        },
        201,
    )


@routing_rules_bp.route("/<int:rule_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.ROUTING_RULE_WRITE)
@validate_response(RuleUpdateResponse, 200)
@validate_request(UpdateRuleRequest)
async def update_rule(rule_id: int, data: UpdateRuleRequest) -> tuple:
    """Update an existing routing_rules_v2 row by ID."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    can_admin = _has_scope(Permission.ROUTING_RULE_ADMIN)
    update_fields: dict[str, Any] = {
        f: getattr(data, f) for f in _WRITABLE_FIELDS if getattr(data, f) is not None
    }

    def _update():
        row = db(db.routing_rules_v2.id == rule_id).select().first()
        if not row:
            return "not_found", None
        if not _can_write(can_admin, user_role, user_org_id, row.organization_id):
            return "forbidden", None
        if not update_fields:
            return "no_fields", None

        db(db.routing_rules_v2.id == rule_id).update(**update_fields)
        db.commit()
        return "ok", db(db.routing_rules_v2.id == rule_id).select().first()

    result, row = await asyncio.to_thread(_update)

    if result == "not_found":
        return jsonify({"status": "error", "error": "Rule not found"}), 404
    if result == "forbidden":
        return jsonify({"status": "error", "error": "Access denied"}), 403
    if result == "no_fields":
        return jsonify({"status": "error", "error": "No valid fields to update"}), 400

    return (
        {
            "status": "success",
            "data": _row_to_dict(row),
            "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
        },
        200,
    )


@routing_rules_bp.route("/<int:rule_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.ROUTING_RULE_WRITE)
@validate_response(RuleDeleteResponse, 200)
async def delete_rule(rule_id: int) -> tuple:
    """Delete a routing_rules_v2 row by ID."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    can_admin = _has_scope(Permission.ROUTING_RULE_ADMIN)

    def _delete():
        row = db(db.routing_rules_v2.id == rule_id).select().first()
        if not row:
            return "not_found"
        if not _can_write(can_admin, user_role, user_org_id, row.organization_id):
            return "forbidden"
        db(db.routing_rules_v2.id == rule_id).delete()
        db.commit()
        return "ok"

    result = await asyncio.to_thread(_delete)
    if result == "not_found":
        return jsonify({"status": "error", "error": "Rule not found"}), 404
    if result == "forbidden":
        return jsonify({"status": "error", "error": "Access denied"}), 403

    return (
        {
            "status": "success",
            "data": {"id": rule_id},
            "meta": {"action": "deleted", "timestamp": datetime.utcnow().isoformat() + "Z"},
        },
        200,
    )
