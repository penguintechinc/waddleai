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
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from penguin_dal.db import DB
from quart import Blueprint, g, jsonify, request
from quart_schema import validate_request, validate_response

from shared.auth.rbac import Permission

from ...extensions import db, redis_client
from ._pagination import PageRequest
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)


def _db() -> DB:
    """Return the process-wide penguin-dal handle, narrowed away from ``None``.

    ``extensions.db`` is declared ``DB | None`` because it starts unset
    before ``init_db()`` runs at startup; every route below only executes
    after that point, so this narrows the type for mypy without adding any
    reachable failure mode (mirrors the same helper in ``fleet.py``).
    """
    if db is None:
        raise RuntimeError("database not initialized")
    return db


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


# ---------------------------------------------------------------------------
# OpenAPI request/response models (audit-2026-09-14). Request fields Optional
# so the handler's own presence/value checks stay authoritative; response
# models mirror EXACTLY the keys ``_row_to_dict`` / each handler returns.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CreateAssignmentRequest:
    """Request body for POST /api/v1/routing/assignments/."""

    tool_type: str | None = None
    model_name: str | None = None
    scope: str | None = None
    scope_ref: int | None = None
    model_params: dict[str, Any] | None = None
    vram_gb: float | None = None
    capability_score: float | None = None
    enabled: bool | None = None
    credential_label: str | None = None
    escalation_model: str | None = None
    fallback_models: list[str] | None = None


@dataclass(slots=True)
class UpdateAssignmentRequest:
    """Request body for PUT /api/v1/routing/assignments/<id>. Every field is a partial update."""

    model_name: str | None = None
    model_params: dict[str, Any] | None = None
    vram_gb: float | None = None
    capability_score: float | None = None
    enabled: bool | None = None
    credential_label: str | None = None
    escalation_model: str | None = None
    fallback_models: list[str] | None = None


@dataclass(slots=True)
class AssignmentRow:
    """A single model_assignments row -- mirrors ``_row_to_dict`` exactly."""

    id: int
    tool_type: str
    complexity: Any
    region: Any
    model_name: str
    model_params: Any
    vram_gb: Any
    capability_score: Any
    enabled: bool
    credential_label: str | None
    escalation_model: str | None
    fallback_models: list[str]
    scope: str
    scope_ref: int | None
    created_at: str | None


@dataclass(slots=True)
class AssignmentPagination:
    """Pagination envelope merged into the list response."""

    page: int
    limit: int
    total: int | None
    pages: int | None


@dataclass(slots=True)
class AssignmentListMeta:
    """``meta`` for the list response."""

    total: int
    timestamp: str


@dataclass(slots=True)
class AssignmentGetMeta:
    """``meta`` for the get-by-id response."""

    timestamp: str


@dataclass(slots=True)
class AssignmentWriteMeta:
    """``meta`` for create/upsert -- carries the action verb, capability warnings, timestamp."""

    action: str
    warnings: list[str]
    timestamp: str


@dataclass(slots=True)
class AssignmentUpdateMeta:
    """``meta`` for update -- capability warnings plus timestamp."""

    warnings: list[str]
    timestamp: str


@dataclass(slots=True)
class AssignmentDeleteMeta:
    """``meta`` for delete -- the deleted action plus timestamp."""

    action: str
    timestamp: str


@dataclass(slots=True)
class AssignmentDeletedRef:
    """``data`` for a delete response -- the deleted row's id."""

    id: int


@dataclass(slots=True)
class SeedResult:
    """``data`` for the seed response -- counts of created/updated rows."""

    created: int
    updated: int
    total: int


@dataclass(slots=True)
class AssignmentListResponse:
    """Response body for GET /api/v1/routing/assignments/."""

    status: str
    data: list[AssignmentRow]
    meta: AssignmentListMeta
    pagination: AssignmentPagination


@dataclass(slots=True)
class AssignmentDetailResponse:
    """Response body for GET /api/v1/routing/assignments/<id>."""

    status: str
    data: AssignmentRow
    meta: AssignmentGetMeta


@dataclass(slots=True)
class AssignmentWriteResponse:
    """Response body for a successful POST (create/upsert)."""

    status: str
    data: AssignmentRow
    meta: AssignmentWriteMeta


@dataclass(slots=True)
class AssignmentUpdateResponse:
    """Response body for a successful PUT."""

    status: str
    data: AssignmentRow
    meta: AssignmentUpdateMeta


@dataclass(slots=True)
class AssignmentDeleteResponse:
    """Response body for a successful DELETE."""

    status: str
    data: AssignmentDeletedRef
    meta: AssignmentDeleteMeta


@dataclass(slots=True)
class SeedResponse:
    """Response body for POST /api/v1/routing/assignments/seed."""

    status: str
    data: SeedResult
    meta: AssignmentGetMeta


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


def _visible_query(scopes: set[str], user_org_id: int | None):
    """Build the org-scoping filter.

    Scope-based (audit-2026-09-14): a caller holding ``routing_assignment:admin``
    sees every org's rows; everyone else sees global rows plus their own org's
    rows (never another org's). The ``scope_ref == user_org_id`` tenant
    comparison is preserved.
    """
    table = _db().model_assignments
    if Permission.ROUTING_ASSIGNMENT_ADMIN.value in scopes:
        return table.id > 0
    return (table.scope == "global") | ((table.scope == "org") & (table.scope_ref == user_org_id))


def _can_write(
    scopes: set[str], user_org_id: int | None, scope: str, scope_ref: int | None
) -> bool:
    """True when the caller may create/modify a row with this scope.

    Scope-based (audit-2026-09-14): the cross-tenant/global-write privilege is
    ``routing_assignment:admin``; the own-org write privilege is
    ``routing_assignment:write``. A WRITE-only caller may write org-scoped rows
    only for their own org -- never a global row (those affect every tenant)
    and never another org's. The ``scope_ref == user_org_id`` tenant comparison
    is preserved verbatim.
    """
    if Permission.ROUTING_ASSIGNMENT_ADMIN.value in scopes:
        return True
    if Permission.ROUTING_ASSIGNMENT_WRITE.value not in scopes:
        return False
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
@validate_response(AssignmentListResponse, 200)
async def list_entries() -> tuple:
    """List visible model_assignments entries with optional filters.

    Query params: tool_type, scope, enabled, page, limit. Non-admin callers
    only ever see global rows plus their own organization's rows.
    """
    tool_type: str | None = request.args.get("tool_type")
    scope_param: str | None = request.args.get("scope")
    enabled_param: str | None = request.args.get("enabled")
    scopes = set(g.user.get("scope") or [])
    user_org_id = g.user.get("organization_id")
    page = PageRequest.from_request()

    def _fetch():
        database = _db()
        query = _visible_query(scopes, user_org_id)

        if tool_type:
            query &= database.model_assignments.tool_type == tool_type
        if scope_param:
            query &= database.model_assignments.scope == scope_param
        if enabled_param is not None:
            enabled_val: bool = enabled_param.lower() in ("true", "1", "yes")
            query &= database.model_assignments.enabled == enabled_val

        scoped = database(query)
        rows = scoped.select(limitby=page.limitby, orderby=database.model_assignments.id)
        return rows, scoped.count()

    rows, total = await asyncio.to_thread(_fetch)
    entries: list[dict[str, Any]] = [_row_to_dict(r) for r in rows]

    return (
        {
            "status": "success",
            "data": entries,
            "meta": {"total": len(entries), "timestamp": datetime.utcnow().isoformat() + "Z"},
            **page.meta(total),
        },
        200,
    )


@routing_assignments_bp.route("/<int:entry_id>", methods=["GET"])
@require_auth
@validate_response(AssignmentDetailResponse, 200)
async def get_entry(entry_id: int) -> tuple:
    """Get a single model_assignments entry by ID (org-visibility scoped)."""
    scopes = set(g.user.get("scope") or [])
    user_org_id = g.user.get("organization_id")

    def _fetch():
        database = _db()
        query = _visible_query(scopes, user_org_id) & (database.model_assignments.id == entry_id)
        return database(query).select().first()

    row = await asyncio.to_thread(_fetch)
    if not row:
        return jsonify({"status": "error", "error": "Assignment not found"}), 404

    return (
        {
            "status": "success",
            "data": _row_to_dict(row),
            "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
        },
        200,
    )


@routing_assignments_bp.route("/", methods=["POST"])
@require_auth
@require_scope(Permission.ROUTING_ASSIGNMENT_WRITE)
@validate_response(AssignmentWriteResponse, 200)
@validate_response(AssignmentWriteResponse, 201)
@validate_request(CreateAssignmentRequest)
async def create_or_upsert_entry(data: CreateAssignmentRequest) -> tuple:
    """Create or upsert a model_assignments entry.

    Upserts by (tool_type, scope, scope_ref). A capability mismatch (e.g. an
    assignment the registry can't actually satisfy) is a save-time
    **warning**, not a hard error -- the row is still saved (spec §7.1
    validate_assignment / Task 14 step 1).
    """
    if data.tool_type is None:
        return jsonify({"status": "error", "error": "tool_type is required"}), 400
    if data.model_name is None:
        return jsonify({"status": "error", "error": "model_name is required"}), 400

    tool_type: str = data.tool_type
    if len(tool_type) > 50:
        return jsonify({"status": "error", "error": "tool_type must be <= 50 characters"}), 400

    scope: str = data.scope if data.scope is not None else "global"
    if scope not in ("global", "org"):
        return jsonify({"status": "error", "error": "scope must be 'global' or 'org'"}), 400
    scope_ref: int | None = data.scope_ref
    if scope == "org" and scope_ref is None:
        return jsonify({"status": "error", "error": "scope_ref is required when scope='org'"}), 400
    if scope == "global":
        scope_ref = None

    scopes = set(g.user.get("scope") or [])
    user_org_id = g.user.get("organization_id")
    if not _can_write(scopes, user_org_id, scope, scope_ref):
        return jsonify({"status": "error", "error": "Access denied for this scope"}), 403

    update_fields: dict[str, Any] = {
        f: getattr(data, f) for f in _ALLOWED_WRITE_FIELDS if getattr(data, f) is not None
    }
    update_fields.setdefault("enabled", True)
    warnings = await _capability_warnings(data.model_name)

    def _upsert():
        database = _db()
        existing = (
            database(
                (database.model_assignments.tool_type == tool_type)
                & (database.model_assignments.scope == scope)
                & (database.model_assignments.scope_ref == scope_ref)
            )
            .select()
            .first()
        )

        if existing:
            database(database.model_assignments.id == existing.id).update(**update_fields)
            database.commit()
            return "updated", database(
                database.model_assignments.id == existing.id
            ).select().first()

        new_id: int = database.model_assignments.insert(
            tool_type=tool_type,
            scope=scope,
            scope_ref=scope_ref,
            **update_fields,
            created_at=datetime.utcnow(),
        )
        database.commit()
        return "created", database(database.model_assignments.id == new_id).select().first()

    action, row = await asyncio.to_thread(_upsert)
    await _invalidate_assignment_cache(scope_ref if scope == "org" else None, tool_type)

    return (
        {
            "status": "success",
            "data": _row_to_dict(row),
            "meta": {
                "action": action,
                "warnings": warnings,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            },
        },
        200 if action == "updated" else 201,
    )


@routing_assignments_bp.route("/<int:entry_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.ROUTING_ASSIGNMENT_WRITE)
@validate_response(AssignmentUpdateResponse, 200)
@validate_request(UpdateAssignmentRequest)
async def update_entry(entry_id: int, data: UpdateAssignmentRequest) -> tuple:
    """Update an existing model_assignments entry by ID."""
    scopes = set(g.user.get("scope") or [])
    user_org_id = g.user.get("organization_id")

    update_fields: dict[str, Any] = {
        f: getattr(data, f) for f in _ALLOWED_WRITE_FIELDS if getattr(data, f) is not None
    }
    warnings: list[str] = []
    if "model_name" in update_fields:
        warnings = await _capability_warnings(update_fields["model_name"])

    def _update():
        database = _db()
        row = database(database.model_assignments.id == entry_id).select().first()
        if not row:
            return "not_found", None, None
        scope = getattr(row, "scope", "global")
        scope_ref = getattr(row, "scope_ref", None)
        if not _can_write(scopes, user_org_id, scope, scope_ref):
            return "forbidden", None, None
        if not update_fields:
            return "no_fields", None, None

        database(database.model_assignments.id == entry_id).update(**update_fields)
        database.commit()
        updated_row = database(database.model_assignments.id == entry_id).select().first()
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
        {
            "status": "success",
            "data": _row_to_dict(row),
            "meta": {"warnings": warnings, "timestamp": datetime.utcnow().isoformat() + "Z"},
        },
        200,
    )


@routing_assignments_bp.route("/<int:entry_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.ROUTING_ASSIGNMENT_WRITE)
@validate_response(AssignmentDeleteResponse, 200)
async def delete_entry(entry_id: int) -> tuple:
    """Delete a model_assignments entry by ID."""
    scopes = set(g.user.get("scope") or [])
    user_org_id = g.user.get("organization_id")

    def _delete():
        database = _db()
        row = database(database.model_assignments.id == entry_id).select().first()
        if not row:
            return "not_found", None
        scope = getattr(row, "scope", "global")
        scope_ref = getattr(row, "scope_ref", None)
        if not _can_write(scopes, user_org_id, scope, scope_ref):
            return "forbidden", None

        database(database.model_assignments.id == entry_id).delete()
        database.commit()
        return "ok", (row.tool_type, scope, scope_ref)

    result, meta = await asyncio.to_thread(_delete)

    if result == "not_found":
        return jsonify({"status": "error", "error": "Assignment not found"}), 404
    if result == "forbidden":
        return jsonify({"status": "error", "error": "Access denied for this scope"}), 403

    tool_type, scope, scope_ref = meta
    await _invalidate_assignment_cache(scope_ref if scope == "org" else None, tool_type)

    return (
        {
            "status": "success",
            "data": {"id": entry_id},
            "meta": {"action": "deleted", "timestamp": datetime.utcnow().isoformat() + "Z"},
        },
        200,
    )


@routing_assignments_bp.route("/seed", methods=["POST"])
@require_auth
@require_scope(Permission.ROUTING_ASSIGNMENT_ADMIN)
@validate_response(SeedResponse, 200)
async def seed_assignments() -> tuple:
    """Populate global model_assignments from DEFAULT_ASSIGNMENTS (admin only).

    Upserts by (tool_type, scope='global', scope_ref=None). Existing entries
    are updated; new ones are created.
    """

    def _seed():
        database = _db()
        created = 0
        updated = 0
        for entry in DEFAULT_ASSIGNMENTS:
            existing = (
                database(
                    (database.model_assignments.tool_type == entry["tool_type"])
                    & (database.model_assignments.scope == "global")
                    & (database.model_assignments.scope_ref == None)  # noqa: E711
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
                database(database.model_assignments.id == existing.id).update(**fields)
                updated += 1
            else:
                database.model_assignments.insert(
                    tool_type=entry["tool_type"],
                    scope="global",
                    scope_ref=None,
                    **fields,
                    created_at=datetime.utcnow(),
                )
                created += 1
        database.commit()
        return created, updated

    created, updated = await asyncio.to_thread(_seed)

    return (
        {
            "status": "success",
            "data": {"created": created, "updated": updated, "total": created + updated},
            "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
        },
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
