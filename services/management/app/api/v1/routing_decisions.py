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
from datetime import datetime
from typing import Any

from quart import Blueprint, g, jsonify, request

from ...extensions import db
from .auth import require_auth

logger = logging.getLogger(__name__)

routing_decisions_bp = Blueprint(
    "routing_decisions", __name__, url_prefix="/api/v1/routing/decisions"
)


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


def _visible_org_filter(user_role: str, user_org_id: int | None):
    """Admin sees every org's traces; everyone else only their own org's."""
    table = db.routing_decision_traces
    if user_role == "admin":
        return table.id > 0
    return table.organization_id == user_org_id


@routing_decisions_bp.route("/<string:request_id>", methods=["GET"])
@require_auth
async def get_trace(request_id: str) -> tuple:
    """Return the full decision trace for one request_id (org-visibility scoped)."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    def _fetch() -> Any:
        rows = db(
            _visible_org_filter(user_role, user_org_id)
            & (db.routing_decision_traces.request_id == request_id)
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
        jsonify(
            {
                "status": "success",
                "data": _row_to_dict(row),
                "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


@routing_decisions_bp.route("/", methods=["GET"])
@require_auth
async def list_decisions_summary() -> tuple:
    """Aggregate summary over a filtered window.

    Reports counts by tool-type source, veto rate, and pressure-shift rate.
    Query params: org (int, admin only -- others are always scoped to their
    own org), from/to (ISO 8601 timestamps, inclusive/exclusive bounds).
    """
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    org_param = request.args.get("org")
    from_param = request.args.get("from")
    to_param = request.args.get("to")

    if user_role == "admin" and org_param is not None:
        try:
            target_org_id: int | None = int(org_param)
        except ValueError:
            return jsonify({"status": "error", "error": "org must be an integer"}), 400
    elif user_role == "admin":
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
        table = db.routing_decision_traces
        query = table.id > 0 if target_org_id is None else table.organization_id == target_org_id
        if from_ts is not None:
            query &= table.timestamp >= from_ts
        if to_ts is not None:
            query &= table.timestamp < to_ts
        return db(query).select()

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
        jsonify(
            {
                "status": "success",
                "data": summary,
                "meta": {
                    "organization_id": target_org_id,
                    "from": from_param,
                    "to": to_param,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                },
            }
        ),
        200,
    )
