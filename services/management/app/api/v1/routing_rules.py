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
from datetime import datetime
from typing import Any

from quart import Blueprint, g, jsonify, request

from shared.auth.rbac import Permission

from ...extensions import db
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)

routing_rules_bp = Blueprint("routing_rules", __name__, url_prefix="/api/v1/routing/rules")

_WRITABLE_FIELDS = ("name", "priority", "match", "action", "enabled")


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


def _visible_query(user_role: str, user_org_id: int | None):
    """Admin sees every rule; everyone else sees global + their own org's rules."""
    table = db.routing_rules_v2
    if user_role == "admin":
        return table.id > 0
    return (table.organization_id == None) | (table.organization_id == user_org_id)  # noqa: E711


def _can_write(user_role: str, user_org_id: int | None, organization_id: int | None) -> bool:
    """Admin manages any rule; resource_manager only their own org's (never global)."""
    if user_role == "admin":
        return True
    return (
        user_role == "resource_manager"
        and organization_id is not None
        and organization_id == user_org_id
    )


@routing_rules_bp.route("/", methods=["GET"])
@require_auth
async def list_rules() -> tuple:
    """List visible routing_rules_v2 rows, priority-ordered."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    enabled_param: str | None = request.args.get("enabled")

    def _fetch():
        query = _visible_query(user_role, user_org_id)
        if enabled_param is not None:
            enabled_val: bool = enabled_param.lower() in ("true", "1", "yes")
            query &= db.routing_rules_v2.enabled == enabled_val
        return db(query).select(orderby=db.routing_rules_v2.priority)

    rows = await asyncio.to_thread(_fetch)
    entries = [_row_to_dict(r) for r in rows]

    return (
        jsonify(
            {
                "status": "success",
                "data": entries,
                "meta": {"total": len(entries), "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


@routing_rules_bp.route("/<int:rule_id>", methods=["GET"])
@require_auth
async def get_rule(rule_id: int) -> tuple:
    """Get a single routing_rules_v2 row by ID (org-visibility scoped)."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    row = await asyncio.to_thread(
        lambda: (
            db(_visible_query(user_role, user_org_id) & (db.routing_rules_v2.id == rule_id))
            .select()
            .first()
        )
    )
    if not row:
        return jsonify({"status": "error", "error": "Rule not found"}), 404

    return (
        jsonify(
            {
                "status": "success",
                "data": _row_to_dict(row),
                "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


@routing_rules_bp.route("/", methods=["POST"])
@require_auth
@require_scope(Permission.ROUTING_RULE_WRITE)
async def create_rule() -> tuple:
    """Create a routing_rules_v2 row."""
    data: dict[str, Any] | None = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    for required in ("name", "match", "action"):
        if required not in data:
            return jsonify({"status": "error", "error": f"{required} is required"}), 400
    if not isinstance(data["match"], dict) or not isinstance(data["action"], dict):
        return jsonify({"status": "error", "error": "match and action must be objects"}), 400

    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    organization_id: int | None = data.get("organization_id")
    if not _can_write(user_role, user_org_id, organization_id):
        return jsonify({"status": "error", "error": "Access denied for this organization_id"}), 403

    def _insert():
        new_id = db.routing_rules_v2.insert(
            name=data["name"],
            priority=data.get("priority", 100),
            match=data["match"],
            action=data["action"],
            enabled=data.get("enabled", True),
            organization_id=organization_id,
            created_at=datetime.utcnow(),
        )
        db.commit()
        return db(db.routing_rules_v2.id == new_id).select().first()

    row = await asyncio.to_thread(_insert)

    return (
        jsonify(
            {
                "status": "success",
                "data": _row_to_dict(row),
                "meta": {"action": "created", "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        201,
    )


@routing_rules_bp.route("/<int:rule_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.ROUTING_RULE_WRITE)
async def update_rule(rule_id: int) -> tuple:
    """Update an existing routing_rules_v2 row by ID."""
    data: dict[str, Any] | None = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    update_fields: dict[str, Any] = {f: data[f] for f in _WRITABLE_FIELDS if f in data}

    def _update():
        row = db(db.routing_rules_v2.id == rule_id).select().first()
        if not row:
            return "not_found", None
        if not _can_write(user_role, user_org_id, row.organization_id):
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
        jsonify(
            {
                "status": "success",
                "data": _row_to_dict(row),
                "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


@routing_rules_bp.route("/<int:rule_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.ROUTING_RULE_WRITE)
async def delete_rule(rule_id: int) -> tuple:
    """Delete a routing_rules_v2 row by ID."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    def _delete():
        row = db(db.routing_rules_v2.id == rule_id).select().first()
        if not row:
            return "not_found"
        if not _can_write(user_role, user_org_id, row.organization_id):
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
        jsonify(
            {
                "status": "success",
                "data": {"id": rule_id},
                "meta": {"action": "deleted", "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )
