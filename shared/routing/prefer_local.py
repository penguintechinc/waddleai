"""Prefer-local routing rules: send matching requests to a local deployment, else go commercial.

A prefer-local rule is a ``routing_rules_v2`` row whose action carries a ``route`` block
(``destination``, ``model``, ``fallback_model``, ``deployment``). The routing engine resolves it
against live offers: the local deployment when it is available, otherwise the commercial
fallback model -- never an error. The ``destination`` rides the RouteDecision to dispatch, where
``select_provider`` honours it only from the org-visible, healthy, routable connector set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shared.routing.capability import ModelOffer
from shared.routing.heuristics import HeuristicRule, RequestSignals, rule_matches
from shared.routing.providers import LOCAL_PROVIDERS, visible_model_configs

__all__ = [
    "PREFER_LOCAL_PRIORITY",
    "PreferLocalDecision",
    "create_prefer_local_rule",
    "resolve_prefer_local",
    "validate_prefer_local",
]

PREFER_LOCAL_PRIORITY = 50
_PREDICATES = frozenset({"model_prefix", "endpoint", "has_image", "tool_name_present"})


@dataclass(slots=True)
class PreferLocalDecision:
    """Outcome of resolving a prefer-local rule against live offers."""

    model: str
    destination: str | None
    fell_back: bool


def validate_prefer_local(match: Any, fallback_model: str) -> None:
    """Admin-readable ``ValueError`` for a malformed match or a blank fallback model."""
    if not isinstance(match, dict) or not match:
        raise ValueError("prefer_local match must be a non-empty object")
    unknown = sorted(set(match) - _PREDICATES)
    if unknown:
        raise ValueError(
            f"prefer_local match has unsupported predicate(s) {unknown}; "
            f"allowed: {sorted(_PREDICATES)}"
        )
    if not isinstance(fallback_model, str) or not fallback_model.strip():
        raise ValueError("prefer_local needs a fallback model")


def _commercial_fallback_exists(db: Any, fallback_model: str, org_id: int) -> bool:
    """True when an enabled row visible to ``org_id`` serves the model from commercial only."""
    table = db.model_configs
    query = (table.model_name == fallback_model) & (table.enabled == True)  # noqa: E712
    rows = db(query).select()
    for row in visible_model_configs(list(rows), org_id):
        providers = row.preferred_providers or []
        if providers and not any(p in LOCAL_PROVIDERS for p in providers):
            return True
    return False


def create_prefer_local_rule(
    db: Any, org_id: int, deployment: Any, fallback_model: str, match: dict[str, Any]
) -> int:
    """Insert the prefer-local ``routing_rules_v2`` row for ``deployment`` and return its id.

    Synchronous (penguin-dal); async callers wrap it in ``asyncio.to_thread``.
    """
    validate_prefer_local(match, fallback_model)
    if not _commercial_fallback_exists(db, fallback_model, org_id):
        raise ValueError("The fallback must be a commercial model.")
    slug = deployment.slug
    row_id = db.routing_rules_v2.insert(
        name=f"prefer-local:{slug}",
        priority=PREFER_LOCAL_PRIORITY,
        match=match,
        action={
            "route": {
                "destination": f"vllm-{org_id}-{slug}",
                "model": deployment.served_model_name,
                "fallback_model": fallback_model,
                "deployment": str(deployment.public_id),
            }
        },
        enabled=True,
        organization_id=org_id,
    )
    return int(row_id)


def _available(offers: list[ModelOffer], model: str | None) -> bool:
    """True when an available offer exists for ``model``."""
    return model is not None and any(o.model_name == model and o.available for o in offers)


def resolve_prefer_local(
    rules: list[HeuristicRule], signals: RequestSignals, offers: list[ModelOffer]
) -> PreferLocalDecision | None:
    """First matching prefer-local rule: local when available, else its commercial fallback.

    Returns ``None`` when no prefer-local rule matches or neither side is available (the
    engine then routes as it would without the rule).
    """
    for rule in sorted(rules, key=lambda r: r.priority):
        route = rule.action.get("route") if isinstance(rule.action, dict) else None
        if not isinstance(route, dict) or not rule_matches(rule.match, signals):
            continue
        if _available(offers, route.get("model")):
            return PreferLocalDecision(str(route["model"]), route.get("destination"), False)
        fallback = route.get("fallback_model")
        if _available(offers, fallback):
            return PreferLocalDecision(str(fallback), None, True)
        return None
    return None
