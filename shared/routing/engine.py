"""RoutingEngine facade -- composes the full smart-routing decision (spec §7).

Orchestrates: tool-type cascade -> assignment lookup -> requirements +
capability veto (co-equal) -> policy filter/sort -> escalation -> sensitivity
clamp -> budget pressure -> final choice + fallback chain, emitting a
RouteTrace. This is the single entry point RoutingStage (pipeline stage 5)
calls; concrete-endpoint selection within the chosen model is delegated
downstream to the merged request_router (§7.5).
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from shared.routing.assignments import AssignmentResolver
from shared.routing.budgets import BudgetPressure, compute_pressure
from shared.routing.capability import ModelOffer, veto_and_reroute
from shared.routing.classifier import ClassifierClient
from shared.routing.escalation import (
    EscalationDecision,
    StickyState,
    escalation_target,
    should_escalate,
)
from shared.routing.heuristics import HeuristicRule, RequestSignals
from shared.routing.policy import PolicyResolver, filter_and_sort
from shared.routing.requirements import derive_requirements
from shared.routing.sensitivity import apply_sensitivity
from shared.routing.tool_type import determine_tool_type
from shared.routing.trace import RouteTrace, persist_trace

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RouteDecision:
    """The engine's final output: chosen model, fallback chain, transparency."""

    model: str
    fallback_chain: list[str] = field(default_factory=list)
    routed_from: dict | None = None
    trace: RouteTrace | None = None


@dataclass(slots=True)
class RoutingInput:
    """Everything the engine needs to decide a single request's route."""

    org_id: int
    request_id: str
    body: dict
    explicit_tool_type: str | None = None
    signals: RequestSignals = field(default_factory=RequestSignals)
    rules: list[HeuristicRule] = field(default_factory=list)
    offers: list[ModelOffer] = field(default_factory=list)
    session_id: str | None = None
    pii_detected: bool = False
    local_unhealthy: bool = False
    failure_signal: bool = False
    explicit_escalate_hint: str | None = None
    new_conversation: bool = False
    allowed_models: set | None = None
    tier_cap: float | None = None
    latency_by_model: dict | None = None
    token_consumed_fraction: float | None = None
    dollar_consumed_fraction: float | None = None
    plan_consumed_fraction: float | None = None


class RoutingEngine:
    """Composes model assignments, capability matching, policy, escalation,
    sensitivity, and budget pressure into a single routing decision.
    """  # noqa: D205 -- two-sentence summary intentionally spans the class docstring

    def __init__(
        self,
        db: Any,
        valkey: Any = None,
        classifier_client: ClassifierClient | None = None,
    ) -> None:
        """Initialize the engine and its resolvers.

        Args:
            db: penguin-dal DB instance (model_assignments, model_configs,
                routing_policies, routing_decision_traces, etc.).
            valkey: Optional redis.asyncio-compatible client for caching and
                sticky-escalation state.
            classifier_client: The stage-2 classifier connector; None skips
                stage 2 (heuristics-only cascade with a safe fallback).
        """
        self.db = db
        self.valkey = valkey
        self.classifier_client = classifier_client
        self.assignments = AssignmentResolver(db, valkey)
        self.policies = PolicyResolver(db, valkey)
        self.sticky = StickyState(valkey)

    async def decide(self, request: RoutingInput) -> RouteDecision:
        """Run the full routing decision cascade for one request.

        Never raises for ordinary routing ambiguity (missing assignment,
        capability mismatch, budget pressure) -- those are absorbed into the
        decision itself; only truly exceptional (e.g. no candidates qualify
        at all) situations fall through to whatever model was assigned.

        Args:
            request: The composed RoutingInput for this request.

        Returns:
            RouteDecision with the chosen model, ordered fallback chain, and
            routed_from transparency metadata.
        """
        trace = RouteTrace(request_id=request.request_id, organization_id=request.org_id)

        tool_type_decision = await determine_tool_type(
            explicit=request.explicit_tool_type,
            signals=request.signals,
            rules=request.rules,
            prompt_text=_last_user_message(request.body),
            classifier_client=self.classifier_client,
        )
        trace.tool_type = tool_type_decision.tool_type
        trace.tool_type_source = tool_type_decision.source
        trace.rules_fired = tool_type_decision.rules_fired
        if tool_type_decision.classification is not None:
            trace.classifier_output = {
                "tool_type": tool_type_decision.classification.tool_type,
                "complexity": tool_type_decision.classification.complexity,
                "domain": tool_type_decision.classification.domain,
                "needs_reasoning": tool_type_decision.classification.needs_reasoning,
            }

        assignment = await self.assignments.resolve(tool_type_decision.tool_type, request.org_id)
        trace.assignment_model = assignment.default_model if assignment else None

        classification = tool_type_decision.classification
        complexity = classification.complexity if classification else None
        reqs = derive_requirements(request.body, complexity=complexity)

        assigned_model = assignment.default_model if assignment else None
        assigned_offer = _find_offer(request.offers, assigned_model)
        chosen_offer, veto_reason = veto_and_reroute(assigned_offer, request.offers, reqs)
        trace.capability_veto = veto_reason is not None and veto_reason != "no_assignment"
        trace.veto_reason = veto_reason

        policy = await self.policies.resolve(request.org_id)

        qualified = filter_and_sort(
            request.offers,
            policy,
            allowed_models=request.allowed_models,
            tier_cap=request.tier_cap,
            latency_by_model=request.latency_by_model,
        )
        trace.qualified_candidates = [
            {"model": o.model_name, "capability_score": o.capability_score, "location": o.location}
            for o in qualified
        ]

        pressure = compute_pressure(
            token_consumed_fraction=request.token_consumed_fraction,
            dollar_consumed_fraction=request.dollar_consumed_fraction,
            plan_consumed_fraction=request.plan_consumed_fraction,
            enabled=policy.budget_pressure_enabled,
        )
        trace.pressure_signals = {
            "level": pressure.level,
            "binding_type": pressure.binding_type,
            "threshold_delta": pressure.threshold_delta,
            "clamp_local": pressure.clamp_local,
        }

        escalation = await self._resolve_escalation(request, complexity, policy, pressure, trace)

        chain = qualified
        pii_flagged = request.pii_detected
        clamp_local = pressure.clamp_local
        if pii_flagged or clamp_local:
            sensitivity_result = apply_sensitivity(
                chain,
                pii_detected=pii_flagged or clamp_local,
                org_sensitivity_routing=policy.sensitivity_routing if pii_flagged else "local_only",
            )
            chain = sensitivity_result.candidates

        final_model, routed_from = self._pick_final(
            chosen_offer, chain, assignment, escalation, policy, veto_reason
        )
        trace.final_model = final_model
        trace.routed_from = routed_from

        await persist_trace(self.db, trace)

        fallback_chain = [o.model_name for o in chain if o.model_name != final_model]
        return RouteDecision(
            model=final_model, fallback_chain=fallback_chain, routed_from=routed_from, trace=trace
        )

    async def _resolve_escalation(
        self,
        request: RoutingInput,
        complexity: int | None,
        policy: Any,
        pressure: BudgetPressure,
        trace: RouteTrace,
    ) -> EscalationDecision:
        """Evaluate escalation triggers, adjusted for budget-pressure threshold shifts."""
        sticky = await self.sticky.is_sticky(
            request.session_id or "",
            de_escalation=policy.de_escalation,
            idle_reset_minutes=policy.idle_reset_minutes,
            new_conversation=request.new_conversation,
        )
        if sticky:
            trace.escalated = True
            return EscalationDecision(escalate=True, trigger="sticky")

        decision = should_escalate(
            complexity=complexity,
            escalation_threshold=policy.escalation_threshold + pressure.threshold_delta,
            local_unhealthy=request.local_unhealthy,
            failure_signal=request.failure_signal,
            explicit_hint=request.explicit_escalate_hint,
        )
        if decision.escalate and request.session_id:
            await self.sticky.mark_escalated(request.session_id)
        trace.escalated = decision.escalate
        return decision

    @staticmethod
    def _pick_final(
        chosen_offer: ModelOffer | None,
        chain: list[ModelOffer],
        assignment: Any,
        escalation: EscalationDecision,
        policy: Any,
        veto_reason: str | None,
    ) -> tuple[str, dict | None]:
        """Select the final model name + routed_from metadata from the composed state."""
        routed_from: dict | None = None

        if escalation.escalate:
            target = escalation_target(
                assignment.escalation_model if assignment else None,
                policy.escalation_target,
            )
            if target:
                routed_from = {"cause": "escalation", "trigger": escalation.trigger}
                return target, routed_from

        if veto_reason is not None and veto_reason != "no_assignment" and chosen_offer is not None:
            routed_from = {"cause": "capability_veto", "reason": veto_reason}
            return chosen_offer.model_name, routed_from

        if chosen_offer is not None and any(o.model_name == chosen_offer.model_name for o in chain):
            return chosen_offer.model_name, routed_from

        if chain:
            return chain[0].model_name, routed_from

        if chosen_offer is not None:
            return chosen_offer.model_name, routed_from

        if assignment is not None:
            return assignment.default_model, routed_from

        return "gpt-4", routed_from


def _find_offer(offers: list[ModelOffer], model_name: str | None) -> ModelOffer | None:
    """Find the ModelOffer matching model_name, or None."""
    if model_name is None:
        return None
    for offer in offers:
        if offer.model_name == model_name:
            return offer
    return None


def _last_user_message(body: dict) -> str:
    """Extract the last user message's text content, for classifier input."""
    messages = body.get("messages", []) or []
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content", "")
            if isinstance(content, str):
                return content
    return ""
