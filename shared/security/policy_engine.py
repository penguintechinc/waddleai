"""SecurityPolicyEngine: resolved-policy tiered evaluation (§8.2, §8.5).

Runs the resolved policy's enabled tiers cheapest-first (1 builtin regex ->
2 org custom rules -> 3 NER -> 4 LLM auditor), enforcing `fail_mode`, the
auditor timeout, and a per-request latency budget. Deterministic tier-1/2/3
findings are locked before tier 4 runs; tier 4's verdict is folded in via a
monotonic `combine()` that can only raise severity, never lower it (§8.5.1)
-- this property holds for genuine LLM verdicts. `fail_mode` is a distinct
concern: it governs what happens when tier 4 could not produce a verdict at
all (timeout/exception/budget-skip), and is an explicit admin trade-off
(`degrade` keeps the tiers-1-3 verdict, `closed` fails safe, `open` fails
available) -- not a downgrade of a real verdict.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from shared.security.policy_resolver import ResolvedPolicy

logger = logging.getLogger(__name__)

# Severity order for monotonic composition -- an LLM verdict can only move
# the action to a *higher* index, never lower.
_SEVERITY = {"allow": 0, "flag": 1, "redact": 2, "block": 3}


@dataclass(slots=True)
class SecurityVerdict:
    """Result of one `SecurityPolicyEngine.evaluate()` call."""

    action: str  # "allow" | "flag" | "redact" | "block"
    violations: list[Any] = field(default_factory=list)
    filtered_text: str = ""
    degraded: bool = False
    auditor_used: bool = False
    tiers_run: tuple[str, ...] = ()
    redactions: int = 0


def combine(deterministic_action: str, llm_verdict: str) -> str:
    """Fold a real tier-4/intent LLM verdict into a deterministic action.

    Monotonic: the result's severity is `max(deterministic, llm)` -- an LLM
    "allow" can never undo a deterministic block/redact/flag, and an LLM
    "block" can escalate a clean deterministic pass.
    """
    llm_action = "block" if llm_verdict == "block" else "allow"
    if _SEVERITY.get(llm_action, 0) > _SEVERITY.get(deterministic_action, 0):
        return llm_action
    return deterministic_action


class SecurityPolicyEngine:
    """Executes a `ResolvedPolicy` against text via the underlying `ContentFilter`."""

    def __init__(self, content_filter: Any, resolver: Any = None, features: Any = None) -> None:
        """Wire the tier-1..4 implementation (`ContentFilter`) and optional collaborators."""
        self.content_filter = content_filter
        self.resolver = resolver
        self.features = features

    async def evaluate(
        self,
        text: str,
        direction: str,
        resolved: ResolvedPolicy,
        ctx: Any = None,
    ) -> SecurityVerdict:
        """Run the resolved policy's enabled tiers and return a `SecurityVerdict`.

        `direction` is "input" or "output" (the phase passed through to the
        tier implementations); `ctx` optionally carries `org_id` for
        org-scoped tier 2/4 lookups.
        """
        org_id = getattr(ctx, "org_id", None) if ctx is not None else None
        start = time.monotonic()
        tiers_run: list[str] = []
        violations: list[Any] = []

        if resolved.tier1_enabled:
            violations.extend(
                await self.content_filter._run_builtin_patterns(text, direction, org_id)
            )
            tiers_run.append("tier1")

        if resolved.tier2_enabled:
            violations.extend(
                await self.content_filter._run_custom_rules(text, direction, org_id)
            )
            tiers_run.append("tier2")

        if resolved.tier3_enabled and not self._budget_exceeded(resolved, start):
            violations.extend(
                await self.content_filter._run_ner_patterns(text, direction, org_id)
            )
            tiers_run.append("tier3")

        deterministic_action, filtered_text = self.content_filter._determine_action(
            text, violations
        )
        redactions = sum(1 for v in violations if getattr(v, "action", None) == "redact")

        final_action = deterministic_action
        degraded = False
        auditor_used = False

        if resolved.tier4_enabled:
            if self._budget_exceeded(resolved, start):
                final_action, degraded = self._apply_fail_mode(
                    resolved.fail_mode, deterministic_action, reason="latency_budget_exceeded"
                )
            else:
                try:
                    timeout_s = resolved.auditor_timeout_ms / 1000
                    auditor_call = self.content_filter._invoke_llm_auditor(
                        text, direction, violations, org_id
                    )
                    should_block, _explanation = await asyncio.wait_for(
                        auditor_call,
                        timeout=timeout_s,
                    )
                    auditor_used = True
                    tiers_run.append("tier4")
                    llm_verdict = "block" if should_block else "allow"
                    final_action = combine(deterministic_action, llm_verdict)
                except TimeoutError:
                    final_action, degraded = self._apply_fail_mode(
                        resolved.fail_mode, deterministic_action, reason="auditor_timeout"
                    )
                except Exception as e:
                    logger.warning("SecurityPolicyEngine: tier-4 auditor error: %s", e)
                    final_action, degraded = self._apply_fail_mode(
                        resolved.fail_mode, deterministic_action, reason="auditor_error"
                    )

        return SecurityVerdict(
            action=final_action,
            violations=violations,
            filtered_text=filtered_text,
            degraded=degraded,
            auditor_used=auditor_used,
            tiers_run=tuple(tiers_run),
            redactions=redactions,
        )

    @staticmethod
    def _budget_exceeded(resolved: ResolvedPolicy, start: float) -> bool:
        if resolved.latency_budget_ms is None:
            return False
        elapsed_ms = (time.monotonic() - start) * 1000
        return elapsed_ms >= resolved.latency_budget_ms

    @staticmethod
    def _apply_fail_mode(
        fail_mode: str, deterministic_action: str, reason: str
    ) -> tuple[str, bool]:
        """Resolve the final action when tier 4 could not produce a verdict.

        Returns (action, degraded). `degrade` keeps the tiers-1-3 verdict
        and marks the result degraded; `closed`/`open` are explicit
        admin-selected trade-offs (fail safe / fail available) and are
        logged but not flagged "degraded" -- they are policy working as
        configured, not a fallback.
        """
        if fail_mode == "closed":
            logger.warning("SecurityPolicyEngine: fail_mode=closed (%s) -- blocking", reason)
            return "block", False
        if fail_mode == "open":
            logger.warning("SecurityPolicyEngine: fail_mode=open (%s) -- allowing", reason)
            return "allow", False
        # default: degrade
        logger.info(
            "SecurityPolicyEngine: fail_mode=degrade (%s) -- enforcing tiers-1-3 verdict (%s)",
            reason,
            deterministic_action,
        )
        return deterministic_action, True


def create_policy_engine(
    content_filter: Any, resolver: Any = None, features: Any = None
) -> SecurityPolicyEngine:
    """Factory for `SecurityPolicyEngine`."""
    return SecurityPolicyEngine(content_filter, resolver, features)
