"""Adapts RoutingEngine to the MarchProxy gRPC ``EvaluateRoute`` contract.

The gRPC sidecar (``proxy.apps.proxy_server.grpc_server``) used to delegate
``EvaluateRoute`` to the retired ``shared.agents.routing_agent.RoutingAgent``
(matrix-factorization complexity scoring + a ``routing_matrix`` DB lookup,
spec §7.6). That wiring was dead in production -- ``ServerComponents.routing_agent``
was never actually populated (``LLMRequestRouter`` never set a
``routing_agent`` attribute), so every real call returned UNAVAILABLE
regardless. This module repoints the caller at the real
``shared.routing.RoutingEngine`` instead of leaving it permanently broken.

``RouteRequest`` carries no ``organization_id`` field (only
``prompt``/``tool_type``/``session_id``/``user_id``/``region``/``metadata``),
so decisions are made at ``org_id=0`` (global/no-org policy + assignments)
until that lands -- the same gap already documented (and hardcoded the same
way) for ``StoreTurn``/``GetContext`` in ``grpc_server.py``
("TODO (Feature A): Derive organization_id from verified gRPC credential").
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from shared.routing.engine import RoutingEngine, RoutingInput
from shared.routing.heuristics import RequestSignals
from shared.routing.offers import load_offers_from_model_configs

logger = logging.getLogger(__name__)

_FALLBACK_MODEL = "llama3.1:8b"

# shared.routing.classifier.Classification.complexity is an integer 1-5;
# the gRPC contract's `complexity` field is a string label (matching the
# retired RouteDecision's Literal["low", "medium", "high"]).
_LOW_COMPLEXITY_MAX = 2
_MEDIUM_COMPLEXITY_MAX = 3

_CODE_TOOLS = frozenset(
    {
        "python",
        "javascript",
        "typescript",
        "go",
        "rust",
        "java",
        "cpp",
        "code_review",
        "debug",
        "test_write",
        "refactor",
    }
)
_OPS_TOOLS = frozenset({"bash", "devops", "file_edit"})
_DATA_TOOLS = frozenset({"sql", "data_analysis"})

# tool_type_source -> base confidence, mirroring the retired RoutingAgent's
# score-from-classifier-raw_score approach but grounded in this engine's own
# transparency signal (spec §7.4) instead of a bespoke MF classifier score.
_SOURCE_CONFIDENCE = {"explicit": 0.95, "heuristic": 0.75, "classifier": 0.55}
_DEFAULT_CONFIDENCE = 0.40
_VETO_CONFIDENCE_PENALTY = 0.20


@dataclass(slots=True)
class RouteEvaluation:
    """gRPC ``RouteResponse``-shaped result (drop-in for the retired RouteDecision)."""

    model: str
    complexity: str = "low"
    target_type: str = "general"
    confidence: float = _DEFAULT_CONFIDENCE
    reasoning: str = ""


class RoutingEngineRouteEvaluator:
    """Evaluates a raw prompt via RoutingEngine, gRPC-``EvaluateRoute``-shaped.

    Args:
        engine: A already-constructed RoutingEngine (the same instance the
            proxy's pipeline uses for RoutingStage, spec §7).
        db: penguin-dal DB instance exposing ``model_configs`` (candidate
            offers -- see ``shared.routing.offers``).
    """

    def __init__(self, engine: RoutingEngine, db: Any) -> None:
        """Store the engine + db this evaluator delegates decisions to."""
        self.engine = engine
        self.db = db

    async def evaluate(self, prompt: str, tool_type: str, region: str = "NA") -> RouteEvaluation:
        """Classify prompt complexity and recommend a model via RoutingEngine.

        Never raises -- a candidate-offer load failure or engine exception
        degrades to a safe fallback response rather than propagating (gRPC
        callers should not see routing internals fail as a hard error).

        Args:
            prompt: The raw user prompt text.
            tool_type: Declared tool / language type (e.g. "python"), fed to
                the engine as the stage-0 explicit tool-type signal.
            region: Deployment region code (e.g. "NA", "EU"); accepted for
                contract compatibility but not yet consumed by RoutingEngine.

        Returns:
            A RouteEvaluation with the selected model, complexity label,
            target_type category, confidence, and reasoning.
        """
        try:
            offers = await load_offers_from_model_configs(self.db)
        except Exception as exc:
            logger.warning(
                "RoutingEngineRouteEvaluator: failed to load candidate offers, "
                "using fallback model: %s",
                exc,
            )
            return RouteEvaluation(
                model=_FALLBACK_MODEL,
                target_type=self._infer_target_type(tool_type),
                confidence=_DEFAULT_CONFIDENCE,
                reasoning="Candidate offer load failed; using hard-coded fallback model.",
            )

        routing_input = RoutingInput(
            org_id=0,
            request_id=uuid.uuid4().hex,
            body={"messages": [{"role": "user", "content": prompt}]},
            explicit_tool_type=tool_type or None,
            signals=RequestSignals(),
            offers=offers,
        )

        try:
            decision = await self.engine.decide(routing_input)
        except Exception as exc:
            logger.error(
                "RoutingEngineRouteEvaluator: engine.decide() failed, using fallback model: %s",
                exc,
                exc_info=True,
            )
            return RouteEvaluation(
                model=_FALLBACK_MODEL,
                target_type=self._infer_target_type(tool_type),
                confidence=_DEFAULT_CONFIDENCE,
                reasoning=f"Routing engine error ({exc}); using hard-coded fallback model.",
            )

        trace = decision.trace
        complexity_int = None
        if trace is not None and trace.classifier_output is not None:
            complexity_int = trace.classifier_output.get("complexity")

        return RouteEvaluation(
            model=decision.model,
            complexity=self._complexity_label(complexity_int),
            target_type=self._infer_target_type(tool_type),
            confidence=self._confidence(trace),
            reasoning=self._reasoning(trace, decision.model),
        )

    @staticmethod
    def _complexity_label(complexity_int: int | None) -> str:
        """Map the engine's 1-5 integer complexity to a low/medium/high label."""
        if complexity_int is None:
            return "low"
        if complexity_int <= _LOW_COMPLEXITY_MAX:
            return "low"
        if complexity_int <= _MEDIUM_COMPLEXITY_MAX:
            return "medium"
        return "high"

    @staticmethod
    def _infer_target_type(tool_type: str) -> str:
        """Map tool_type to a coarse target category: code | ops | data | general."""
        lower = (tool_type or "").lower()
        if lower in _CODE_TOOLS:
            return "code"
        if lower in _OPS_TOOLS:
            return "ops"
        if lower in _DATA_TOOLS:
            return "data"
        return "general"

    @staticmethod
    def _confidence(trace: Any) -> float:
        """Derive a confidence score from the decision trace's transparency signals."""
        if trace is None:
            return _DEFAULT_CONFIDENCE
        confidence = _SOURCE_CONFIDENCE.get(trace.tool_type_source, _DEFAULT_CONFIDENCE)
        if trace.capability_veto:
            confidence = max(0.0, confidence - _VETO_CONFIDENCE_PENALTY)
        return round(min(confidence, 1.0), 4)

    @staticmethod
    def _reasoning(trace: Any, final_model: str) -> str:
        """Build a short human-readable explanation from the decision trace."""
        if trace is None:
            return f"Routed to {final_model} (no trace available)."
        parts = [f"tool_type={trace.tool_type} (source={trace.tool_type_source})"]
        if trace.assignment_model:
            parts.append(f"assignment={trace.assignment_model}")
        if trace.capability_veto:
            parts.append(f"capability_veto={trace.veto_reason}")
        if trace.escalated:
            parts.append("escalated=true")
        parts.append(f"final_model={final_model}")
        return "; ".join(parts)
