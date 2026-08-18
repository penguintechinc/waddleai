"""WaddleAI Management API v1 - Model Alias Endpoints (spec §7.2 stage 0).

CRUD for ``model_aliases``: redirects a client-supplied model name (e.g.
``gpt-4o``) to a target model, optionally pinning a target provider. A NULL
``organization_id`` row is a global default; an org-scoped row overrides it
for that org. Admin surface for ``shared.routing.aliases.AliasResolver``.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from quart import Blueprint, g, jsonify, request

from ...extensions import db
from .auth import require_auth, require_role

logger = logging.getLogger(__name__)

model_aliases_bp = Blueprint("model_aliases", __name__, url_prefix="/api/v1/routing/aliases")

_WRITABLE_FIELDS = ("source_model", "target_model", "target_provider", "enabled")


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a penguin-dal model_aliases row into a serializable dict."""
    return {
        "id": row.id,
        "organization_id": row.organization_id,
        "source_model": row.source_model,
        "target_model": row.target_model,
        "target_provider": row.target_provider,
        "enabled": row.enabled,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _visible_query(user_role: str, user_org_id: int | None):
    """Admin sees every alias; everyone else sees global + their own org's aliases."""
    table = db.model_aliases
    if user_role == "admin":
        return table.id > 0
    return (table.organization_id == None) | (table.organization_id == user_org_id)  # noqa: E711


def _can_write(user_role: str, user_org_id: int | None, organization_id: int | None) -> bool:
    """Admin manages any alias; resource_manager only their own org's (never global)."""
    if user_role == "admin":
        return True
    return (
        user_role == "resource_manager"
        and organization_id is not None
        and organization_id == user_org_id
    )


@model_aliases_bp.route("/", methods=["GET"])
@require_auth
async def list_aliases() -> tuple:
    """List visible model_aliases rows."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    source_model: str | None = request.args.get("source_model")

    def _fetch():
        query = _visible_query(user_role, user_org_id)
        if source_model:
            query &= db.model_aliases.source_model == source_model
        return db(query).select(orderby=db.model_aliases.id)

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


@model_aliases_bp.route("/<int:alias_id>", methods=["GET"])
@require_auth
async def get_alias(alias_id: int) -> tuple:
    """Get a single model_aliases row by ID (org-visibility scoped)."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    row = await asyncio.to_thread(
        lambda: db(_visible_query(user_role, user_org_id) & (db.model_aliases.id == alias_id))
        .select()
        .first()
    )
    if not row:
        return jsonify({"status": "error", "error": "Alias not found"}), 404

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


@model_aliases_bp.route("/", methods=["POST"])
@require_auth
@require_role("admin", "resource_manager")
async def create_alias() -> tuple:
    """Create a model_aliases row.

    Upserts by (organization_id, source_model) -- matching the table's
    unique constraint.
    """
    data: dict[str, Any] | None = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    for required in ("source_model", "target_model"):
        if required not in data:
            return jsonify({"status": "error", "error": f"{required} is required"}), 400
    if data["source_model"] == data["target_model"]:
        return (
            jsonify({"status": "error", "error": "source_model and target_model must differ"}),
            400,
        )

    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    organization_id: int | None = data.get("organization_id")
    if not _can_write(user_role, user_org_id, organization_id):
        return jsonify({"status": "error", "error": "Access denied for this organization_id"}), 403

    def _upsert():
        existing = (
            db(
                (db.model_aliases.organization_id == organization_id)
                & (db.model_aliases.source_model == data["source_model"])
            )
            .select()
            .first()
        )
        fields = {
            "target_model": data["target_model"],
            "target_provider": data.get("target_provider"),
            "enabled": data.get("enabled", True),
        }
        if existing:
            db(db.model_aliases.id == existing.id).update(**fields)
            db.commit()
            return "updated", db(db.model_aliases.id == existing.id).select().first()

        new_id = db.model_aliases.insert(
            organization_id=organization_id,
            source_model=data["source_model"],
            **fields,
            created_at=datetime.utcnow(),
        )
        db.commit()
        return "created", db(db.model_aliases.id == new_id).select().first()

    action, row = await asyncio.to_thread(_upsert)

    return (
        jsonify(
            {
                "status": "success",
                "data": _row_to_dict(row),
                "meta": {"action": action, "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200 if action == "updated" else 201,
    )


@model_aliases_bp.route("/<int:alias_id>", methods=["PUT"])
@require_auth
@require_role("admin", "resource_manager")
async def update_alias(alias_id: int) -> tuple:
    """Update an existing model_aliases row by ID."""
    data: dict[str, Any] | None = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    update_fields: dict[str, Any] = {f: data[f] for f in _WRITABLE_FIELDS if f in data}

    def _update():
        row = db(db.model_aliases.id == alias_id).select().first()
        if not row:
            return "not_found", None
        if not _can_write(user_role, user_org_id, row.organization_id):
            return "forbidden", None
        if not update_fields:
            return "no_fields", None

        db(db.model_aliases.id == alias_id).update(**update_fields)
        db.commit()
        return "ok", db(db.model_aliases.id == alias_id).select().first()

    result, row = await asyncio.to_thread(_update)

    if result == "not_found":
        return jsonify({"status": "error", "error": "Alias not found"}), 404
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


@model_aliases_bp.route("/<int:alias_id>", methods=["DELETE"])
@require_auth
@require_role("admin", "resource_manager")
async def delete_alias(alias_id: int) -> tuple:
    """Delete a model_aliases row by ID."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    def _delete():
        row = db(db.model_aliases.id == alias_id).select().first()
        if not row:
            return "not_found"
        if not _can_write(user_role, user_org_id, row.organization_id):
            return "forbidden"
        db(db.model_aliases.id == alias_id).delete()
        db.commit()
        return "ok"

    result = await asyncio.to_thread(_delete)
    if result == "not_found":
        return jsonify({"status": "error", "error": "Alias not found"}), 404
    if result == "forbidden":
        return jsonify({"status": "error", "error": "Access denied"}), 403

    return (
        jsonify(
            {
                "status": "success",
                "data": {"id": alias_id},
                "meta": {"action": "deleted", "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )
