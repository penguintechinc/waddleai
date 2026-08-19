"""WaddleAI Management API v1 - Routing Assignment Endpoints (spec §7.1, §7.6).

CRUD operations for ``model_assignments`` (evolved from the legacy
``routing_matrix`` table by migration 010): maps a tool type to a default
model plus an optional escalation model + ordered fallback models, scoped
global or per-organization. Renamed from ``routing_matrix.py`` -- this is
the admin surface for ``shared.routing.AssignmentResolver`` (spec §7.1.1).

The legacy natural-language ``/routing-matrix/instructions`` and
``/routing-matrix/test`` surfaces (Valkey ``routing:instructions`` key,
consumed by the retired ``LLMRequestRouter`` intelligent-routing path) are
retired along with that code path -- the equivalent admin control is now
``routing_policies.classifier_prompt`` (see ``routing_policies.py``).
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from quart import Blueprint, g, jsonify, request

from shared.auth.rbac import Permission

from ...extensions import db, redis_client
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)

routing_assignments_bp = Blueprint(
    "routing_assignments", __name__, url_prefix="/api/v1/routing/assignments"
)

# Default assignment spec used by the /seed endpoint. tool_type here maps to
# a single default_model (complexity is now a classifier output, spec §7.2,
# not a per-row axis) -- global scope so every org gets a sane default.
DEFAULT_ASSIGNMENTS: list[dict[str, Any]] = [
    {"tool_type": "chat", "model_name": "gpt-4o-mini", "capability_score": 0.7},
    {"tool_type": "code", "model_name": "gpt-4o", "capability_score": 0.88},
    {"tool_type": "embed", "model_name": "nomic-embed-text", "capability_score": 0.8},
]

_ALLOWED_WRITE_FIELDS = (
    "model_name",
    "model_params",
    "vram_gb",
    "capability_score",
    "enabled",
    "credential_label",
    "escalation_model",
    "fallback_models",
)


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a penguin-dal model_assignments row into a serializable dict."""
    return {
        "id": row.id,
        "tool_type": row.tool_type,
        "complexity": row.complexity,
        "region": row.region,
        "model_name": row.model_name,
        "model_params": row.model_params,
        "vram_gb": row.vram_gb,
        "capability_score": row.capability_score,
        "enabled": row.enabled,
        "credential_label": getattr(row, "credential_label", None),
        "escalation_model": getattr(row, "escalation_model", None),
        "fallback_models": getattr(row, "fallback_models", None) or [],
        "scope": getattr(row, "scope", "global"),
        "scope_ref": getattr(row, "scope_ref", None),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _visible_query(user_role: str, user_org_id: int | None):
    """Build the org-scoping filter.

    Admin sees everything; everyone else sees global rows plus their own
    org's rows (never another org's).
    """
    table = db.model_assignments
    if user_role == "admin":
        return table.id > 0
    return (table.scope == "global") | (
        (table.scope == "org") & (table.scope_ref == user_org_id)
    )


def _can_write(user_role: str, user_org_id: int | None, scope: str, scope_ref: int | None) -> bool:
    """True when the caller may create/modify a row with this scope."""
    if user_role == "admin":
        return True
    if user_role != "resource_manager":
        return False
    # resource_manager may only write org-scoped rows for their own org --
    # never global rows (those affect every tenant).
    return scope == "org" and scope_ref == user_org_id


async def _invalidate_assignment_cache(org_id: int | None, tool_type: str) -> None:
    """Best-effort Valkey cache invalidation via the shared AssignmentResolver."""
    if redis_client is None:
        return
    try:
        from shared.routing.assignments import AssignmentResolver

        resolver = AssignmentResolver(db=None, valkey=redis_client)
        await resolver.invalidate(org_id, tool_type)
    except Exception as exc:  # pragma: no cover - defensive, cache-only failure
        logger.warning("routing_assignments: cache invalidation failed: %s", exc)


@routing_assignments_bp.route("/", methods=["GET"])
@require_auth
async def list_entries() -> tuple:
    """List visible model_assignments entries with optional filters.

    Query params: tool_type, scope, enabled. Non-admin callers only ever see
    global rows plus their own organization's rows.
    """
    tool_type: str | None = request.args.get("tool_type")
    scope_param: str | None = request.args.get("scope")
    enabled_param: str | None = request.args.get("enabled")
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    def _fetch():
        query = _visible_query(user_role, user_org_id)

        if tool_type:
            query &= db.model_assignments.tool_type == tool_type
        if scope_param:
            query &= db.model_assignments.scope == scope_param
        if enabled_param is not None:
            enabled_val: bool = enabled_param.lower() in ("true", "1", "yes")
            query &= db.model_assignments.enabled == enabled_val

        return db(query).select(orderby=db.model_assignments.id)

    rows = await asyncio.to_thread(_fetch)
    entries: list[dict[str, Any]] = [_row_to_dict(r) for r in rows]

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


@routing_assignments_bp.route("/<int:entry_id>", methods=["GET"])
@require_auth
async def get_entry(entry_id: int) -> tuple:
    """Get a single model_assignments entry by ID (org-visibility scoped)."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    def _fetch():
        query = _visible_query(user_role, user_org_id) & (db.model_assignments.id == entry_id)
        return db(query).select().first()

    row = await asyncio.to_thread(_fetch)
    if not row:
        return jsonify({"status": "error", "error": "Assignment not found"}), 404

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


@routing_assignments_bp.route("/", methods=["POST"])
@require_auth
@require_scope(Permission.ROUTING_ASSIGNMENT_WRITE)
async def create_or_upsert_entry() -> tuple:
    """Create or upsert a model_assignments entry.

    Upserts by (tool_type, scope, scope_ref). A capability mismatch (e.g. an
    assignment the registry can't actually satisfy) is a save-time
    **warning**, not a hard error -- the row is still saved (spec §7.1
    validate_assignment / Task 14 step 1).
    """
    data: dict[str, Any] | None = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    for required in ("tool_type", "model_name"):
        if required not in data:
            return jsonify({"status": "error", "error": f"{required} is required"}), 400

    tool_type: str = data["tool_type"]
    if len(tool_type) > 50:
        return jsonify({"status": "error", "error": "tool_type must be <= 50 characters"}), 400

    scope: str = data.get("scope", "global")
    if scope not in ("global", "org"):
        return jsonify({"status": "error", "error": "scope must be 'global' or 'org'"}), 400
    scope_ref: int | None = data.get("scope_ref")
    if scope == "org" and scope_ref is None:
        return jsonify({"status": "error", "error": "scope_ref is required when scope='org'"}), 400
    if scope == "global":
        scope_ref = None

    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    if not _can_write(user_role, user_org_id, scope, scope_ref):
        return jsonify({"status": "error", "error": "Access denied for this scope"}), 403

    update_fields: dict[str, Any] = {f: data[f] for f in _ALLOWED_WRITE_FIELDS if f in data}
    update_fields.setdefault("enabled", data.get("enabled", True))
    warnings = await _capability_warnings(data["model_name"])

    def _upsert():
        existing = (
            db(
                (db.model_assignments.tool_type == tool_type)
                & (db.model_assignments.scope == scope)
                & (db.model_assignments.scope_ref == scope_ref)
            )
            .select()
            .first()
        )

        if existing:
            db(db.model_assignments.id == existing.id).update(**update_fields)
            db.commit()
            return "updated", db(db.model_assignments.id == existing.id).select().first()

        new_id: int = db.model_assignments.insert(
            tool_type=tool_type,
            scope=scope,
            scope_ref=scope_ref,
            **update_fields,
            created_at=datetime.utcnow(),
        )
        db.commit()
        return "created", db(db.model_assignments.id == new_id).select().first()

    action, row = await asyncio.to_thread(_upsert)
    await _invalidate_assignment_cache(scope_ref if scope == "org" else None, tool_type)

    return (
        jsonify(
            {
                "status": "success",
                "data": _row_to_dict(row),
                "meta": {
                    "action": action,
                    "warnings": warnings,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                },
            }
        ),
        200 if action == "updated" else 201,
    )


@routing_assignments_bp.route("/<int:entry_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.ROUTING_ASSIGNMENT_WRITE)
async def update_entry(entry_id: int) -> tuple:
    """Update an existing model_assignments entry by ID."""
    data: dict[str, Any] | None = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    update_fields: dict[str, Any] = {f: data[f] for f in _ALLOWED_WRITE_FIELDS if f in data}
    warnings: list[str] = []
    if "model_name" in update_fields:
        warnings = await _capability_warnings(update_fields["model_name"])

    def _update():
        row = db(db.model_assignments.id == entry_id).select().first()
        if not row:
            return "not_found", None, None
        scope = getattr(row, "scope", "global")
        scope_ref = getattr(row, "scope_ref", None)
        if not _can_write(user_role, user_org_id, scope, scope_ref):
            return "forbidden", None, None
        if not update_fields:
            return "no_fields", None, None

        db(db.model_assignments.id == entry_id).update(**update_fields)
        db.commit()
        updated_row = db(db.model_assignments.id == entry_id).select().first()
        return "ok", updated_row, (row.tool_type, scope, scope_ref)

    result, row, meta = await asyncio.to_thread(_update)

    if result == "not_found":
        return jsonify({"status": "error", "error": "Assignment not found"}), 404
    if result == "forbidden":
        return jsonify({"status": "error", "error": "Access denied for this scope"}), 403
    if result == "no_fields":
        return jsonify({"status": "error", "error": "No valid fields to update"}), 400

    tool_type, scope, scope_ref = meta
    await _invalidate_assignment_cache(scope_ref if scope == "org" else None, tool_type)

    return (
        jsonify(
            {
                "status": "success",
                "data": _row_to_dict(row),
                "meta": {"warnings": warnings, "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


@routing_assignments_bp.route("/<int:entry_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.ROUTING_ASSIGNMENT_WRITE)
async def delete_entry(entry_id: int) -> tuple:
    """Delete a model_assignments entry by ID."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    def _delete():
        row = db(db.model_assignments.id == entry_id).select().first()
        if not row:
            return "not_found", None
        scope = getattr(row, "scope", "global")
        scope_ref = getattr(row, "scope_ref", None)
        if not _can_write(user_role, user_org_id, scope, scope_ref):
            return "forbidden", None

        db(db.model_assignments.id == entry_id).delete()
        db.commit()
        return "ok", (row.tool_type, scope, scope_ref)

    result, meta = await asyncio.to_thread(_delete)

    if result == "not_found":
        return jsonify({"status": "error", "error": "Assignment not found"}), 404
    if result == "forbidden":
        return jsonify({"status": "error", "error": "Access denied for this scope"}), 403

    tool_type, scope, scope_ref = meta
    await _invalidate_assignment_cache(scope_ref if scope == "org" else None, tool_type)

    return (
        jsonify(
            {
                "status": "success",
                "data": {"id": entry_id},
                "meta": {"action": "deleted", "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


@routing_assignments_bp.route("/seed", methods=["POST"])
@require_auth
@require_scope(Permission.ROUTING_ASSIGNMENT_ADMIN)
async def seed_assignments() -> tuple:
    """Populate global model_assignments from DEFAULT_ASSIGNMENTS (admin only).

    Upserts by (tool_type, scope='global', scope_ref=None). Existing entries
    are updated; new ones are created.
    """

    def _seed():
        created = 0
        updated = 0
        for entry in DEFAULT_ASSIGNMENTS:
            existing = (
                db(
                    (db.model_assignments.tool_type == entry["tool_type"])
                    & (db.model_assignments.scope == "global")
                    & (db.model_assignments.scope_ref == None)  # noqa: E711
                )
                .select()
                .first()
            )
            fields = {
                "model_name": entry["model_name"],
                "capability_score": entry.get("capability_score"),
                "enabled": entry.get("enabled", True),
            }
            if existing:
                db(db.model_assignments.id == existing.id).update(**fields)
                updated += 1
            else:
                db.model_assignments.insert(
                    tool_type=entry["tool_type"],
                    scope="global",
                    scope_ref=None,
                    **fields,
                    created_at=datetime.utcnow(),
                )
                created += 1
        db.commit()
        return created, updated

    created, updated = await asyncio.to_thread(_seed)

    return (
        jsonify(
            {
                "status": "success",
                "data": {"created": created, "updated": updated, "total": created + updated},
                "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


async def _capability_warnings(model_name: str) -> list[str]:
    """Save-time capability-validation warnings (spec §7.1) -- never blocks the save.

    Reuses ``shared.routing.capability.validate_assignment`` against the
    ``model_configs`` candidate universe (the same interim capability source
    RoutingStage/RoutingEngineRouteEvaluator use, see
    ``shared.routing.offers``).
    """
    try:
        from shared.routing.capability import validate_assignment
        from shared.routing.offers import load_offers_from_model_configs

        offers = await load_offers_from_model_configs(db)
        offer = next((o for o in offers if o.model_name == model_name), None)
        return validate_assignment(offer)
    except Exception as exc:  # pragma: no cover - defensive, must never block a save
        logger.warning("routing_assignments: capability validation skipped: %s", exc)
        return []
