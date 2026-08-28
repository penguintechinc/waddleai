"""Capability matching -- the co-equal, veto-capable decision surface (spec §7.1.2).

Every registry model carries offers: capability score, tool/vision support,
context window, cost, and location (local|commercial). When the assignment
resolver's pick fails a hard requirement (images to a text-only model,
context overflow), capability matching vetoes and re-routes to the best
qualified candidate instead of failing the request. The same predicate powers
save-time validation warnings on the assignments admin screen.
"""

from collections.abc import Callable
from dataclasses import dataclass

from shared.routing.requirements import RequirementsVector


@dataclass(slots=True)
class ModelOffer:
    """Registry model capability offer (interim: derived from model_configs).

    Once migration 008 (model_registry) lands, this should be populated from
    that table's richer capability data (capability_score, supports_vision,
    live fleet state) instead of being inferred from model_configs.
    """

    model_name: str
    capability_score: float = 3.0
    supports_tools: bool = True
    supports_vision: bool = False
    context_window: int = 4096
    cost_per_token: float = 0.0
    location: str = "commercial"  # "local" | "commercial"
    available: bool = True


def qualifies(offer: ModelOffer, reqs: RequirementsVector) -> bool:
    """True when ``offer`` satisfies every hard requirement in ``reqs``.

    Hard requirements: available, sufficient context window, tool support
    when tools are needed, vision support when images are present.
    """
    if not offer.available:
        return False
    if offer.context_window < reqs.min_context:
        return False
    if reqs.needs_tools and not offer.supports_tools:
        return False
    if reqs.needs_vision and not offer.supports_vision:
        return False
    return True


def best_candidate(
    offers: list[ModelOffer],
    reqs: RequirementsVector,
    sort_key: Callable[[ModelOffer], float] | None = None,
) -> ModelOffer | None:
    """Return the highest-ranked qualified offer, or None if none qualify.

    Args:
        offers: Candidate offers to consider.
        reqs: Requirements the winner must satisfy.
        sort_key: Optional ranking key (higher is better); defaults to
            capability_score.

    """
    qualified = [o for o in offers if qualifies(o, reqs)]
    if not qualified:
        return None
    key = sort_key or (lambda o: o.capability_score)
    return max(qualified, key=key)


def veto_and_reroute(
    assigned: ModelOffer | None,
    offers: list[ModelOffer],
    reqs: RequirementsVector,
) -> tuple[ModelOffer | None, str | None]:
    """Keep the assigned offer if it qualifies; otherwise veto and re-route.

    Args:
        assigned: The offer picked by the model-assignments resolver, or
            None when there was no assignment row (capability matching alone
            decides).
        offers: The full candidate universe to re-route within.
        reqs: Requirements the final choice must satisfy.

    Returns:
        (chosen, veto_reason) -- veto_reason is None when the assignment was
        kept unchanged; otherwise a short machine-readable reason string.

    """
    if assigned is not None and qualifies(assigned, reqs):
        return assigned, None

    reason = "no_assignment" if assigned is None else _veto_reason(assigned, reqs)
    chosen = best_candidate(offers, reqs)
    return chosen, reason


def _veto_reason(offer: ModelOffer, reqs: RequirementsVector) -> str:
    """Classify why ``offer`` failed ``reqs`` (first failing hard requirement)."""
    if not offer.available:
        return "unavailable"
    if offer.context_window < reqs.min_context:
        return "context_overflow"
    if reqs.needs_tools and not offer.supports_tools:
        return "tools_unsupported"
    if reqs.needs_vision and not offer.supports_vision:
        return "vision_unsupported"
    return "unqualified"


def validate_assignment(
    offer: ModelOffer | None, reqs: RequirementsVector | None = None
) -> list[str]:
    """Save-time validation warnings for an assignment row (admin screen).

    Args:
        offer: The offer for the model an admin is about to assign, or None
            if the model is unknown to the registry.
        reqs: Representative requirements to validate against (e.g. "this
            tool type typically needs vision"); when omitted, only registry
            presence/availability is checked.

    Returns:
        A list of human-readable warnings; empty when no issues are found.

    """
    warnings: list[str] = []
    if offer is None:
        return ["model is not present in the registry"]
    if not offer.available:
        warnings.append("model is currently unavailable in the fleet")
    if reqs is not None and not qualifies(offer, reqs):
        warnings.append(f"assignment fails hard requirement: {_veto_reason(offer, reqs)}")
    return warnings
