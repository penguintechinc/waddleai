"""
WaddleAI Dual-Agent System

Provides routing intelligence and security enforcement for the AI load
balancer.  The two primary agents — RoutingAgent and SecurityAgent — work
in tandem on every inbound request:

1. **SecurityAgent** evaluates the raw prompt/command for threats using a
   combination of RAG-based pattern matching and the existing
   PromptSecurityScanner regex engine.
2. **RoutingAgent** classifies prompt complexity via a deterministic matrix
   factorisation heuristic and maps the request to the most appropriate
   model and provider through the RoutingMatrix database.

Supporting components:
- MatrixFactorizationClassifier — deterministic prompt-complexity scorer
- RoutingMatrix — database-backed model-selection lookup
- UsageTracker — per-user / per-key usage recording with license gating
"""

from shared.agents.mf_classifier import MatrixFactorizationClassifier
from shared.agents.routing_agent import RoutingAgent
from shared.agents.routing_matrix import RouteDecision, RoutingMatrix, RoutingMatrixEntry
from shared.agents.security_agent import SecurityAgent, SecurityDecision
from shared.agents.usage_tracker import AILBUsageRecord, UsageAck, UsageTracker

__all__ = [
    "MatrixFactorizationClassifier",
    "RoutingMatrix",
    "RoutingMatrixEntry",
    "RouteDecision",
    "RoutingAgent",
    "SecurityAgent",
    "SecurityDecision",
    "UsageTracker",
    "UsageAck",
    "AILBUsageRecord",
]
