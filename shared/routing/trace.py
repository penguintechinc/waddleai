"""First-class routing decision trace (spec §7.4).

Every routing decision logs the requirements vector, tool-type source, rules
fired, classifier output, assignment row applied, capability vetoes,
qualified candidates with sort scores, pressure signals, and the final
choice -- persisted to ``routing_decision_traces`` for the per-request WebUI
view, aggregate tuning views, and future heuristics/task_detect training
data. An opaque router is a router admins turn off.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RouteTrace:
    """One routing decision's full provenance, mirroring routing_decision_traces."""

    request_id: str
    organization_id: int
    requirements: dict | None = None
    tool_type: str | None = None
    tool_type_source: str | None = None
    rules_fired: list[str] = field(default_factory=list)
    classifier_output: dict | None = None
    assignment_model: str | None = None
    capability_veto: bool = False
    veto_reason: str | None = None
    qualified_candidates: list[dict] = field(default_factory=list)
    pressure_signals: dict | None = None
    final_model: str | None = None
    routed_from: dict | None = None
    escalated: bool = False


async def persist_trace(db: Any, trace: RouteTrace) -> None:
    """Insert one routing_decision_traces row, off the event loop.

    Best-effort: a persistence failure is logged and swallowed -- tracing
    must never break the request it's describing.

    Args:
        db: penguin-dal DB instance exposing a ``routing_decision_traces`` table.
        trace: The completed RouteTrace to persist.
    """
    try:
        await asyncio.to_thread(_insert, db, trace)
    except Exception as exc:  # pragma: no cover - defensive, DB I/O failure
        logger.warning(
            "persist_trace: failed to write trace for request %s: %s", trace.request_id, exc
        )


def _insert(db: Any, trace: RouteTrace) -> None:
    """Synchronous penguin-dal insert of a single decision trace row."""
    db.routing_decision_traces.insert(
        request_id=trace.request_id,
        organization_id=trace.organization_id,
        timestamp=datetime.utcnow(),
        requirements=trace.requirements,
        tool_type=trace.tool_type,
        tool_type_source=trace.tool_type_source,
        rules_fired=trace.rules_fired,
        classifier_output=trace.classifier_output,
        assignment_model=trace.assignment_model,
        capability_veto=trace.capability_veto,
        veto_reason=trace.veto_reason,
        qualified_candidates=trace.qualified_candidates,
        pressure_signals=trace.pressure_signals,
        final_model=trace.final_model,
        routed_from=trace.routed_from,
        escalated=trace.escalated,
    )
    db.commit()
