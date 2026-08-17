"""WaddleAI Management API v1 - Routing Dry-Run Endpoint (spec §7, §7.4).

A genuine replacement for the retired ``/routing-matrix/test`` endpoint,
whose own docstring admitted it returned "a static illustrative response
rather than a live call into the routing LLM" (hardcoded ``claude-3-sonnet``
/ "Programming task detected" / confidence 0.85 for every input). This
route runs the real ``shared.routing.RoutingEngine`` over an admin-supplied
prompt and returns exactly what it decided.

**No side effects**: ``RoutingEngine.decide()`` is called with
``persist=False`` -- no ``routing_decision_traces`` row is written, no
token usage is recorded, and no upstream provider is dispatched to. This
is purely an admin what-if tool; ``routing_decision_traces`` stays a durable
record of real, already-routed requests only (spec §7.4).

Stage-2 classification is skipped here (``classifier_client=None``): the
management service has no ``LLMConnectionManager`` wired to a live model
fleet (that only exists in the proxy service -- see
``proxy.apps.proxy_server.main``), so this dry run exercises the full
engine -- assignment resolution, alias redirect, capability veto, policy
filter/sort, escalation, sensitivity, and budget pressure -- against the
real explicit/heuristic tool-type cascade. That is the same documented
degrade path ``RoutingEngine`` already takes for any request where no
classifier connector is configured (``shared.routing.classifier.classify``
never raises); it is not a stub added for this endpoint.

The response carries no confidence score -- ``RoutingEngine`` doesn't
produce one, so none is fabricated (unlike the retired endpoint, and unlike
``shared.routing.grpc_adapter.RoutingEngineRouteEvaluator``'s own derived
gRPC-contract-compatibility confidence, which this endpoint deliberately
does not reuse).
"""

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from quart import Blueprint, g, jsonify, request

from shared.routing.engine import RoutingEngine, RoutingInput
from shared.routing.heuristics import RequestSignals
from shared.routing.offers import load_offers_from_model_configs
from shared.utils.feature_flags import is_feature_enabled

from ...extensions import db, redis_client
from .auth import require_auth, require_role

logger = logging.getLogger(__name__)

routing_dry_run_bp = Blueprint("routing_dry_run", __name__, url_prefix="/api/v1/routing/dry-run")

_FEATURE_FLAG = "waddleai.routing-dry-run"
_MAX_PROMPT_LENGTH = 8000
_MAX_TOOL_TYPE_LENGTH = 50


@dataclass(slots=True)
class RoutingDryRunResult:
    """Explicit response schema for a dry-run decision -- never a raw trace/model object."""

    model: str
    fallback_chain: list[str] = field(default_factory=list)
    routed_from: dict[str, Any] | None = None
    tool_type: str | None = None
    tool_type_source: str | None = None
    rules_fired: list[str] = field(default_factory=list)
    classifier_output: dict[str, Any] | None = None
    assignment_model: str | None = None
    capability_veto: bool = False
    veto_reason: str | None = None
    qualified_candidates: list[dict[str, Any]] = field(default_factory=list)
    escalated: bool = False


@routing_dry_run_bp.route("/", methods=["POST"])
@require_auth
@require_role("admin")
async def dry_run_decision() -> tuple:
    """Run RoutingEngine.decide() over a supplied prompt with zero side effects.

    Request body: ``{"prompt": str, "tool_type": str (optional explicit
    hint), "organization_id": int (optional -- defaults to the caller's own
    org; admin may target any org's policy/assignments, same as every other
    admin-only routing-admin route -- see ``_visible_query`` in
    ``routing_assignments.py``: "Admin sees everything")}``. No separate
    org-membership check is needed beyond ``@require_role("admin")`` above --
    this route is admin-only, and admins already have unrestricted org
    visibility everywhere else in this API.
    """
    user_org_id = g.user.get("organization_id")

    data: dict[str, Any] | None = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return jsonify({"status": "error", "error": "prompt is required"}), 400
    if len(prompt) > _MAX_PROMPT_LENGTH:
        return (
            jsonify(
                {"status": "error", "error": f"prompt must be <= {_MAX_PROMPT_LENGTH} characters"}
            ),
            400,
        )

    explicit_tool_type = data.get("tool_type")
    if explicit_tool_type is not None:
        if not isinstance(explicit_tool_type, str) or not explicit_tool_type.strip():
            return (
                jsonify({"status": "error", "error": "tool_type must be a non-empty string"}),
                400,
            )
        if len(explicit_tool_type) > _MAX_TOOL_TYPE_LENGTH:
            return (
                jsonify(
                    {
                        "status": "error",
                        "error": f"tool_type must be <= {_MAX_TOOL_TYPE_LENGTH} characters",
                    }
                ),
                400,
            )

    org_param = data.get("organization_id", user_org_id)
    try:
        target_org_id = int(org_param)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "error": "organization_id must be an integer"}), 400

    if not is_feature_enabled(_FEATURE_FLAG, distinct_id=str(target_org_id)):
        return jsonify({"status": "error", "error": "Feature not enabled"}), 403

    try:
        offers = await load_offers_from_model_configs(db)
    except Exception as exc:  # pragma: no cover - defensive, DB I/O failure
        logger.warning("routing_dry_run: failed to load candidate offers: %s", exc)
        return jsonify({"status": "error", "error": "Failed to load candidate models"}), 500

    engine = RoutingEngine(db, valkey=redis_client, classifier_client=None)
    routing_input = RoutingInput(
        org_id=target_org_id,
        request_id=f"dryrun-{uuid.uuid4().hex}",
        body={"messages": [{"role": "user", "content": prompt}]},
        explicit_tool_type=explicit_tool_type,
        signals=RequestSignals(),
        offers=offers,
        # session_id intentionally left unset -- a dry run must never mark a
        # real session sticky-escalated (shared.routing.escalation.StickyState
        # no-ops on a falsy session_id).
    )

    try:
        decision = await engine.decide(routing_input, persist=False)
    except Exception as exc:
        logger.error("routing_dry_run: engine.decide() failed: %s", exc, exc_info=True)
        return (
            jsonify({"status": "error", "error": "Routing engine failed to produce a decision"}),
            500,
        )

    trace = decision.trace
    result = RoutingDryRunResult(
        model=decision.model,
        fallback_chain=decision.fallback_chain,
        routed_from=decision.routed_from,
        tool_type=trace.tool_type if trace else None,
        tool_type_source=trace.tool_type_source if trace else None,
        rules_fired=trace.rules_fired if trace else [],
        classifier_output=trace.classifier_output if trace else None,
        assignment_model=trace.assignment_model if trace else None,
        capability_veto=trace.capability_veto if trace else False,
        veto_reason=trace.veto_reason if trace else None,
        qualified_candidates=trace.qualified_candidates if trace else [],
        escalated=trace.escalated if trace else False,
    )

    return (
        jsonify(
            {
                "status": "success",
                "data": asdict(result),
                "meta": {
                    "organization_id": target_org_id,
                    "persisted": False,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                },
            }
        ),
        200,
    )
