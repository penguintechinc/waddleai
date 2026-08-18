"""
WaddleAI Agent System

Provides security enforcement and usage tracking for the AI load balancer.

- **SecurityAgent** evaluates the raw prompt/command for threats using a
  combination of RAG-based pattern matching and the existing
  PromptSecurityScanner regex engine.
- **UsageTracker** records per-user / per-key usage with license gating.

The former RoutingAgent/RoutingMatrix/MatrixFactorizationClassifier trio
(deterministic matrix-factorization complexity scoring + a routing_matrix
DB lookup) is retired per spec §7.6 in favor of
``shared.routing.RoutingEngine`` (spec §7): tool-type cascade ->
model_assignments -> capability matching -> org policy -> escalation ->
sensitivity -> budget pressure. See ``shared/routing/__init__.py``. The
gRPC ``EvaluateRoute`` caller that used to consume RoutingAgent now uses
``shared.routing.grpc_adapter.RoutingEngineRouteEvaluator``.
"""

from shared.agents.security_agent import SecurityAgent, SecurityDecision
from shared.agents.usage_tracker import UsageAck, UsageTracker

__all__ = [
    "SecurityAgent",
    "SecurityDecision",
    "UsageTracker",
    "UsageAck",
]
