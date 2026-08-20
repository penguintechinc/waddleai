"""Sensitivity-aware routing (spec §7.3): local clamp / redact-then-any.

``sensitivity_routing: local_only | redact_then_any | ignore``. Stage 3
(SecurityInStage) runs before routing, so PII/sensitivity flags are already
available; PII-flagged requests are clamped to the local partition (or
redacted before any commercial dispatch), overridable per tool-type
assignment row. Security x local-knowledge synergy: sensitive content never
leaves the deployment.
"""

from dataclasses import dataclass

from shared.routing.capability import ModelOffer

_VALID_MODES = frozenset({"local_only", "redact_then_any", "ignore"})


@dataclass(slots=True)
class SensitivityResult:
    """Outcome of applying sensitivity routing to a candidate chain."""

    candidates: list[ModelOffer]
    redact_before_dispatch: bool = False


def apply_sensitivity(
    candidates: list[ModelOffer],
    pii_detected: bool,
    org_sensitivity_routing: str,
    assignment_override: str | None = None,
) -> SensitivityResult:
    """Apply sensitivity-aware clamping to a capability/policy-qualified chain.

    Args:
        candidates: The ordered fallback chain (already capability + policy
            filtered/sorted).
        pii_detected: True when SecurityInStage flagged PII/sensitive content
            on this request.
        org_sensitivity_routing: The org's routing_policies.sensitivity_routing.
        assignment_override: A per-tool-type assignment override, taking
            precedence over the org policy when set.

    Returns:
        SensitivityResult: for ``local_only`` with PII present, every
        commercial candidate is dropped (a PII-flagged request can never
        dispatch commercial -- this is a security invariant, not a
        preference); ``redact_then_any`` keeps the full chain but flags
        redact_before_dispatch; ``ignore`` (or no PII) is a no-op.

    """
    mode = assignment_override or org_sensitivity_routing
    if mode not in _VALID_MODES:
        mode = "local_only"

    if not pii_detected or mode == "ignore":
        return SensitivityResult(candidates=list(candidates))

    if mode == "local_only":
        clamped = [c for c in candidates if c.location == "local"]
        return SensitivityResult(candidates=clamped)

    # redact_then_any: chain unchanged, but caller must redact before dispatch.
    return SensitivityResult(candidates=list(candidates), redact_before_dispatch=True)
