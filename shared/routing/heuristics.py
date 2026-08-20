"""Cascade stage 1 -- heuristic routing rules (spec §7.2).

``routing_rules_v2(priority, match jsonb, action jsonb)`` evaluated in
priority order (lower number = higher priority, matching the existing
routing_matrix/routing_rules convention); the first rule whose ``match``
predicate fits the request fires its ``action``. Deterministic, <1ms, no LLM
call -- target ~70% of ``auto`` requests resolve here. A non-matching request
(or an empty rule table) returns None, punting to stage 2.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HeuristicRule:
    """A single stage-1 heuristic rule."""

    priority: int
    match: dict
    action: dict
    name: str = ""


@dataclass(slots=True)
class RequestSignals:
    """Cheap, pre-computed signals a heuristic rule can match against."""

    tool_names: list[str] = field(default_factory=list)
    endpoint: str = ""
    has_image: bool = False
    model: str = ""


# Supported match predicate keys. Each maps a match value to a check against
# RequestSignals; an unrecognized key or malformed match value causes that
# single predicate (and therefore that rule) to be skipped, not to raise.
def _matches_tool_name_present(match_value: Any, signals: RequestSignals) -> bool:
    """True when any of the request's tool names is in match_value (a list)."""
    if not isinstance(match_value, list):
        return False
    return any(name in signals.tool_names for name in match_value)


def _matches_endpoint(match_value: Any, signals: RequestSignals) -> bool:
    """True when the request endpoint equals match_value."""
    return isinstance(match_value, str) and signals.endpoint == match_value


def _matches_has_image(match_value: Any, signals: RequestSignals) -> bool:
    """True when signals.has_image equals the requested boolean."""
    return isinstance(match_value, bool) and signals.has_image == match_value


def _matches_model_prefix(match_value: Any, signals: RequestSignals) -> bool:
    """True when the requested model starts with match_value."""
    return isinstance(match_value, str) and signals.model.startswith(match_value)


_PREDICATES = {
    "tool_name_present": _matches_tool_name_present,
    "endpoint": _matches_endpoint,
    "has_image": _matches_has_image,
    "model_prefix": _matches_model_prefix,
}


def rule_matches(match: dict, signals: RequestSignals) -> bool:
    """True when every predicate key in ``match`` is satisfied by ``signals``.

    Unknown predicate keys are ignored (forward-compatible); a malformed
    match value fails that predicate rather than raising.
    """
    if not isinstance(match, dict) or not match:
        return False
    for key, value in match.items():
        predicate = _PREDICATES.get(key)
        if predicate is None:
            continue
        try:
            if not predicate(value, signals):
                return False
        except Exception as exc:  # pragma: no cover - defensive, malformed rule data
            logger.warning("heuristics: malformed match predicate %s=%r: %s", key, value, exc)
            return False
    return True


def evaluate_rules(signals: RequestSignals, rules: list[HeuristicRule]) -> dict | None:
    """Evaluate rules in priority order, returning the first match's action.

    Args:
        signals: Pre-computed cheap request signals.
        rules: Candidate rules (need not be pre-sorted).

    Returns:
        The matching rule's ``action`` dict, or None when no rule matches
        (punt to the stage-2 classifier).

    """
    for rule in sorted(rules, key=lambda r: r.priority):
        try:
            if rule_matches(rule.match, signals):
                return rule.action
        except Exception as exc:  # pragma: no cover - defensive, never crash the cascade
            logger.warning("heuristics: skipping malformed rule %r: %s", rule.name, exc)
            continue
    return None
