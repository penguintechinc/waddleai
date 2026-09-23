"""WaddleAI Management API v1 - Routing Decision Trace Endpoints (spec §7.4).

Read-only views over ``routing_decision_traces``, the first-class durable
corpus ``shared.routing.trace.persist_trace`` writes one row to per routed
request: requirements vector, tool-type source, rules fired, classifier
output, assignment applied, capability vetoes, qualified candidates + sort
scores, pressure signals, and the final choice.

- ``GET /api/v1/routing/decisions/<request_id>`` -- the full trace for one request.
- ``GET /api/v1/routing/decisions/?org=&from=&to=`` -- an aggregate summary
  (counts by tool-type source, veto rate, pressure-shift rate) over a
  filtered window.

Read-only: traces are written exclusively by RoutingEngine at request time,
never edited or created through this API.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from penguin_dal.db import DB
from quart import Blueprint, g, jsonify, request
from quart_schema import validate_response

from shared.auth.rbac import Permission

from ...extensions import db
from .auth import require_auth

logger = logging.getLogger(__name__)

routing_decisions_bp = Blueprint(
    "routing_decisions", __name__, url_prefix="/api/v1/routing/decisions"
)


# ---------------------------------------------------------------------------
# OpenAPI response models (audit-2026-09-14). Read-only endpoints; response
# models mirror EXACTLY the keys each handler returns. Both routes stay
# authenticated-only at the decorator; the admin cross-org visibility is gated
# in-handler on the admin-only ``routing_decision:read`` scope
# (audit-2026-09-14-wave2, see ``_visible_org_filter``).
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TraceRow:
    """A single routing_decision_traces row -- mirrors ``_row_to_dict`` exactly."""

    id: int
    request_id: str
    organization_id: int | None
    timestamp: str | None
    requirements: Any
    tool_type: str | None
    tool_type_source: str | None
    rules_fired: Any
    classifier_output: Any
    assignment_model: str | None
    capability_veto: Any
    veto_reason: str | None
    qualified_candidates: Any
    pressure_signals: Any
    final_model: str | None
    routed_from: Any
    escalated: Any


@dataclass(slots=True)
class TraceTimestampMeta:
    """``meta`` carrying only a timestamp."""

    timestamp: str


@dataclass(slots=True)
class TraceGetResponse:
    """Response body for GET /api/v1/routing/decisions/<request_id>."""

    status: str
    data: TraceRow
    meta: TraceTimestampMeta


@dataclass(slots=True)
class DecisionSummary:
    """The aggregate summary payload -- mirrors the handler's ``summary`` dict."""

    total: int
    by_tool_type_source: dict[str, int]
    veto_rate: Any
    pressure_shift_rate: Any
    escalation_rate: Any


@dataclass(slots=True)
class DecisionSummaryResponse:
    """Response body for GET /api/v1/routing/decisions/.

    ``meta`` is a loose dict because one of its keys is ``from`` -- a Python
    keyword that cannot be a dataclass field name. The exact meta field set is
    pinned by the route's regression test instead.
    """

    status: str
    data: DecisionSummary
    meta: dict[str, Any]


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
    """Convert a penguin-dal routing_decision_traces row into a serializable dict."""
    return {
        "id": row.id,
        "request_id": row.request_id,
        "organization_id": row.organization_id,
        "timestamp": row.timestamp.isoformat() if row.timestamp else None,
        "requirements": row.requirements,
        "tool_type": row.tool_type,
        "tool_type_source": row.tool_type_source,
        "rules_fired": row.rules_fired,
        "classifier_output": row.classifier_output,
        "assignment_model": row.assignment_model,
        "capability_veto": row.capability_veto,
        "veto_reason": row.veto_reason,
        "qualified_candidates": row.qualified_candidates,
        "pressure_signals": row.pressure_signals,
        "final_model": row.final_model,
        "routed_from": row.routed_from,
        "escalated": row.escalated,
    }


def _has_scope(perm: Permission) -> bool:
    """True when the caller's OIDC ``scope`` claim carries ``perm``.

    Authoritative ``scope`` claim only, never the ``role`` claim (house
    scope-only policy, see ``auth.require_scope``). MUST be called from the
    request context, not a DB worker thread: ``get_trace`` computes the
    admin-capability boolean here and captures it in its thread closure.

    audit-2026-09-14-wave2: replaces the ``role == "admin"`` cross-org
    trace-visibility bypass with the admin-only ``routing_decision:read``
    scope (this table had no dedicated scope before). Identical for a fresh
    admin token; an in-flight admin JWT gains it on next login (<=1h TTL),
    API-key admins immediately.
    """
    user = getattr(g, "user", None) or {}
    return perm.value in set(user.get("scope") or [])


def _visible_org_filter(can_admin: bool, user_org_id: int | None):
    """Admin (routing_decision:read) sees every org's traces; else only their own org's."""
    table = _db().routing_decision_traces
    if can_admin:
        return table.id > 0
    return table.organization_id == user_org_id


@routing_decisions_bp.route("/<string:request_id>", methods=["GET"])
@require_auth
@validate_response(TraceGetResponse, 200)
async def get_trace(request_id: str) -> tuple:
    """Return the full decision trace for one request_id (org-visibility scoped)."""
    can_admin = _has_scope(Permission.ROUTING_DECISION_READ)
    user_org_id = g.user.get("organization_id")

    def _fetch() -> Any:
        database = _db()
        rows = database(
            _visible_org_filter(can_admin, user_org_id)
            & (database.routing_decision_traces.request_id == request_id)
        ).select()
        if len(rows) == 0:
            return None
        # Most recent row wins when a request_id has more than one trace
        # (e.g. a client retry re-using the same id) -- max() rather than a
        # descending orderby so this works against every penguin-dal backend
        # uniformly.
        return max(rows, key=lambda r: r.id)

    row = await asyncio.to_thread(_fetch)
    if not row:
        return jsonify({"status": "error", "error": "Decision trace not found"}), 404

    return (
        {
            "status": "success",
            "data": _row_to_dict(row),
            "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
        },
        200,
    )


@routing_decisions_bp.route("/", methods=["GET"])
@require_auth
@validate_response(DecisionSummaryResponse, 200)
async def list_decisions_summary() -> tuple:
    """Aggregate summary over a filtered window.

    Reports counts by tool-type source, veto rate, and pressure-shift rate.
    Query params: org (int, admin only -- others are always scoped to their
    own org), from/to (ISO 8601 timestamps, inclusive/exclusive bounds).
    """
    can_admin = _has_scope(Permission.ROUTING_DECISION_READ)
    user_org_id = g.user.get("organization_id")
    org_param = request.args.get("org")
    from_param = request.args.get("from")
    to_param = request.args.get("to")

    # audit-2026-09-14-wave2: cross-org trace visibility (honouring ?org= and
    # the "every org" default) keys on the admin-only routing_decision:read
    # scope, not the role name.
    if can_admin and org_param is not None:
        try:
            target_org_id: int | None = int(org_param)
        except ValueError:
            return jsonify({"status": "error", "error": "org must be an integer"}), 400
    elif can_admin:
        target_org_id = None  # no org filter -- every org
    else:
        target_org_id = user_org_id

    def _parse_ts(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    from_ts = _parse_ts(from_param)
    to_ts = _parse_ts(to_param)

    def _fetch() -> list[Any]:
        database = _db()
        table = database.routing_decision_traces
        query = table.id > 0 if target_org_id is None else table.organization_id == target_org_id
        if from_ts is not None:
            query &= table.timestamp >= from_ts
        if to_ts is not None:
            query &= table.timestamp < to_ts
        return list(database(query).select())

    rows = await asyncio.to_thread(_fetch)

    total = len(rows)
    by_source: dict[str, int] = {}
    veto_count = 0
    pressure_shift_count = 0
    escalated_count = 0
    for row in rows:
        source = row.tool_type_source or "unknown"
        by_source[source] = by_source.get(source, 0) + 1
        if row.capability_veto:
            veto_count += 1
        if row.escalated:
            escalated_count += 1
        pressure = row.pressure_signals or {}
        if pressure.get("threshold_delta") or pressure.get("clamp_local"):
            pressure_shift_count += 1

    summary = {
        "total": total,
        "by_tool_type_source": by_source,
        "veto_rate": round(veto_count / total, 4) if total else 0.0,
        "pressure_shift_rate": round(pressure_shift_count / total, 4) if total else 0.0,
        "escalation_rate": round(escalated_count / total, 4) if total else 0.0,
    }

    return (
        {
            "status": "success",
            "data": summary,
            "meta": {
                "organization_id": target_org_id,
                "from": from_param,
                "to": to_param,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            },
        },
        200,
    )
