"""Unified Smart Routing engine (spec §7).

DB-driven routing that replaces three legacy systems (the hardcoded
model_configs dict, the Valkey NL routing:instructions key, and the
standalone routing_matrix agent) with one engine composing model assignments,
capability matching, org policy, escalation, sensitivity, and budget
pressure. See ``shared.routing.engine.RoutingEngine`` for the facade.
"""

from shared.routing.assignments import Assignment, AssignmentResolver
from shared.routing.budgets import BudgetPressure, PlanBudgetWindow, compute_pressure
from shared.routing.capability import ModelOffer, best_candidate, qualifies, veto_and_reroute
from shared.routing.classifier_connector import LLMConnectorClassifierClient
from shared.routing.engine import RouteDecision, RoutingEngine, RoutingInput
from shared.routing.escalation import EscalationDecision, StickyState, should_escalate
from shared.routing.grpc_adapter import RouteEvaluation, RoutingEngineRouteEvaluator
from shared.routing.offers import load_offers_from_model_configs
from shared.routing.requirements import RequirementsVector, derive_requirements
from shared.routing.trace import RouteTrace

__all__ = [
    "Assignment",
    "AssignmentResolver",
    "BudgetPressure",
    "PlanBudgetWindow",
    "compute_pressure",
    "ModelOffer",
    "best_candidate",
    "qualifies",
    "veto_and_reroute",
    "LLMConnectorClassifierClient",
    "RouteDecision",
    "RoutingEngine",
    "RoutingInput",
    "EscalationDecision",
    "StickyState",
    "should_escalate",
    "RouteEvaluation",
    "RoutingEngineRouteEvaluator",
    "load_offers_from_model_configs",
    "RequirementsVector",
    "derive_requirements",
    "RouteTrace",
]
