"""HooksPolicyEngine: server-side evaluation for developer-agent hooks (§18).

Hooks fire synchronously inside an agent's loop -- a `PreToolUse` hook runs
before *every* tool call, so latency is a correctness concern here, not a
nicety. This engine implements the evaluation chain `POST
/api/v1/hooks/evaluate` calls, in strict order, short-circuiting as soon as
a tier produces a decision:

  1. **Tier 1 -- canonical denylist** (`hooks_denylist`): unconditional and
     absolute. A match returns `deny` immediately, before Tier 2 admin
     `hook_rules` are even loaded from the resolver. This is what makes an
     admin `hook_rules` allow-rule structurally unable to weaken the
     denylist (§18.4 coordinator directive) -- the check that would be
     weakened never runs once Tier 1 has already returned.
  2. **Admin-authored declarative `hook_rules`** (`hooks_rules`): matched
     rules combine by max severity (deny > ask > allow) across both global
     and tenant-scoped matches -- see `hooks_rules.combine_hook_rule_matches`
     for why this makes a global `deny` unconditionally outrank a tenant
     `allow`. A match here is authoritative and skips Tier 2 entirely (both
     for latency and because an explicit admin decision shouldn't be
     second-guessed by a heuristic content scan).
  3. **Tier 2 -- opt-in, org-scoped remote policy evaluation**: calls the
     *existing* §8 `SecurityPolicyEngine`/`ContentFilter` against the
     flattened `tool_input` text -- never a bespoke reimplementation, per
     house rule ("hook decisions and proxy decisions cannot drift"). Bounded
     by its own `remote_eval_timeout_ms` (default 200ms), independent of the
     resolved security policy's own `auditor_timeout_ms` (tuned for the
     proxy's chat-completion budget, not this interactive path).

  4. **Default: `allow`** -- no rule matched, Tier 2 disabled, or Tier 2
     itself resolved to "allow".

## Tier-2 fail-mode default: `open` (fail available) -- and why

`remote_eval_fail_mode` governs what happens when Tier 2 cannot produce a
verdict (timeout or exception), and it defaults to **"open"** (deny becomes
the exception, not the rule):

- Tier 2 is opt-in and layered *on top of* a Tier-1 floor that already
  fails closed independently of any network round trip. The genuinely
  catastrophic paths (credential leaks, unrecoverable edits) are covered
  before Tier 2 is ever reached.
- Hooks fire on *every* tool call in an interactive dev loop -- far more
  often, and far more latency-sensitively, than the proxy's own §8 engine
  (which defaults its own tier-4-auditor-unavailable case to `degrade`, not
  `closed`, for the identical reason: blocking every request on a security
  service hiccup is worse than degrading). A management-service blip
  failing closed here means every tool call in an entire org grinds to a
  halt simultaneously.
- This is a *default*, not a mandate: `remote_eval_fail_mode` is a
  per-org config field precisely so a regulated-environment org can flip
  it to `closed` and accept the availability cost.

Because a silent fail-open is a silent security degradation, fail-open and
fail-closed events are counted on **separate** metrics
(`record_hook_fail_mode("fail_open"|"fail_closed")`) and always logged at
WARNING, so a spike is immediately visible and alertable -- see
`_apply_fail_mode`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from shared.security.hooks_config import HookConfig, HookConfigResolver
from shared.security.hooks_denylist import (
    HookDenylistResolver,
    flatten_tool_input_text,
    match_denylist,
)
from shared.security.hooks_rules import (
    HookRule,
    HookRulesResolver,
    combine_hook_rule_matches,
    matches_rule,
)
from shared.security.policy_engine import SecurityPolicyEngine
from shared.security.policy_resolver import PolicyResolver

logger = logging.getLogger(__name__)

# §8 ContentFilter/SecurityPolicyEngine actions -> hook decisions. "flag"
# (uncertain) and "redact" (sensitive content found, but a tool call can't
# be partially redacted the way a chat message can) both degrade to "ask"
# rather than a hard "deny" -- only a confident "block" verdict denies.
_ACTION_TO_DECISION: dict[str, str] = {
    "allow": "allow",
    "flag": "ask",
    "redact": "ask",
    "block": "deny",
}


@dataclass(slots=True)
class _HookEvalCtx:
    """Minimal ctx carrying org_id for `SecurityPolicyEngine.evaluate(..., ctx)`."""

    org_id: Any = None


@dataclass(slots=True, frozen=True)
class HookEvaluation:
    """Result of one `HooksPolicyEngine.evaluate()` call -- maps onto the wire contract.

    `tier` ("tier1" | "hook_rule" | "tier2" | "default") is internal, used
    for metrics/logging; it is not part of the `POST /hooks/evaluate`
    response contract.
    """

    decision: str  # "allow" | "deny" | "ask"
    reason: str
    rule_id: str | None
    tier: str
    degraded: bool = False


class HooksPolicyEngine:
    """Orchestrates the tier1 -> hook_rules -> tier2 -> default evaluation chain."""

    def __init__(
        self,
        denylist_resolver: HookDenylistResolver,
        rules_resolver: HookRulesResolver,
        config_resolver: HookConfigResolver,
        security_policy_resolver: PolicyResolver | None = None,
        security_policy_engine: SecurityPolicyEngine | None = None,
        metrics: Any = None,
    ) -> None:
        """Wire the resolvers, the optional §8 collaborators for Tier 2, and metrics."""
        self.denylist_resolver = denylist_resolver
        self.rules_resolver = rules_resolver
        self.config_resolver = config_resolver
        self.security_policy_resolver = security_policy_resolver
        self.security_policy_engine = security_policy_engine
        self.metrics = metrics

    async def evaluate(
        self,
        ecosystem: str,
        event: str,
        tool_name: str,
        tool_input: dict[str, Any] | None,
        org_id: Any,
    ) -> HookEvaluation:
        """Run the tier1 -> hook_rules -> tier2 -> default chain for one hook event."""
        matchable_text = flatten_tool_input_text(tool_input)

        # Tier 1 -- absolute, unconditional, evaluated first and alone.
        denylist = await self.denylist_resolver.resolve(org_id)
        hit = match_denylist(denylist, matchable_text)
        if hit is not None:
            logger.info("HooksPolicyEngine: tier1 denylist hit (pattern=%s)", hit.pattern)
            return HookEvaluation(
                decision="deny",
                reason=f"Blocked by protected-path policy: matches '{hit.pattern}'",
                rule_id=None,
                tier="tier1",
            )

        # Admin-authored declarative rules -- authoritative when matched.
        rules = await self.rules_resolver.resolve(org_id)
        matched: list[HookRule] = [
            r for r in rules if matches_rule(r, ecosystem, event, tool_name, matchable_text)
        ]
        for r in matched:
            self._record_matched_rule(r)
        winner = combine_hook_rule_matches(matched)
        if winner is not None:
            self._record_winning_rule(winner)
            return HookEvaluation(
                decision=winner.decision,
                reason=winner.reason,
                rule_id=str(winner.id),
                tier="hook_rule",
            )

        # Tier 2 -- opt-in, org-scoped remote policy evaluation.
        config = await self.config_resolver.resolve(org_id)
        if config.remote_eval_enabled and self.security_policy_engine is not None:
            return await self._evaluate_tier2(tool_name, matchable_text, org_id, config)

        return HookEvaluation(
            decision="allow",
            reason="No matching rule; Tier 2 not enabled for this organization",
            rule_id=None,
            tier="default",
        )

    async def _evaluate_tier2(
        self, tool_name: str, matchable_text: str, org_id: Any, config: HookConfig
    ) -> HookEvaluation:
        """Call the existing §8 engine against `matchable_text`, bounded by its own timeout."""
        if self.security_policy_resolver is None:
            return HookEvaluation(
                decision="allow",
                reason="Tier 2 misconfigured (no policy resolver)",
                rule_id=None,
                tier="default",
            )
        try:
            resolved = await self.security_policy_resolver.resolve(
                org_id, model=None, tool_name=tool_name, direction="input"
            )
            ctx = _HookEvalCtx(org_id=org_id)
            timeout_s = config.remote_eval_timeout_ms / 1000
            verdict = await asyncio.wait_for(
                self.security_policy_engine.evaluate(matchable_text, "input", resolved, ctx),
                timeout=timeout_s,
            )
        except TimeoutError:
            if self.metrics is not None:
                self.metrics.record_hook_timeout("tier2")
            return self._apply_fail_mode(config, "tier2_timeout")
        except Exception as e:
            logger.warning("HooksPolicyEngine: tier2 evaluation error: %s", e)
            return self._apply_fail_mode(config, "tier2_error")

        decision = _ACTION_TO_DECISION.get(verdict.action, "allow")
        detail = f" ({len(verdict.violations)} violation(s))" if verdict.violations else ""
        return HookEvaluation(
            decision=decision,
            reason=f"Tier-2 policy evaluation: {verdict.action}{detail}",
            rule_id=None,
            tier="tier2",
            degraded=verdict.degraded,
        )

    def _apply_fail_mode(self, config: HookConfig, reason: str) -> HookEvaluation:
        """Resolve the Tier-2 verdict when it could not be produced (timeout/error).

        See module docstring for the default's rationale. `closed` and
        `open` are both explicit, admin-selected trade-offs -- logged at
        WARNING and counted on separate metrics either way, so a shift in
        the fail_open/fail_closed ratio over time is directly observable.
        """
        if config.remote_eval_fail_mode == "closed":
            if self.metrics is not None:
                self.metrics.record_hook_fail_mode("fail_closed")
            logger.warning(
                "HooksPolicyEngine: remote_eval_fail_mode=closed (%s) -- denying", reason
            )
            return HookEvaluation(
                decision="deny",
                reason=f"Tier-2 evaluation unavailable ({reason}); fail_mode=closed",
                rule_id=None,
                tier="tier2",
                degraded=True,
            )
        if self.metrics is not None:
            self.metrics.record_hook_fail_mode("fail_open")
        logger.warning("HooksPolicyEngine: remote_eval_fail_mode=open (%s) -- allowing", reason)
        return HookEvaluation(
            decision="allow",
            reason=f"Tier-2 evaluation unavailable ({reason}); fail_mode=open",
            rule_id=None,
            tier="tier2",
            degraded=True,
        )

    def _record_matched_rule(self, rule: HookRule) -> None:
        if self.metrics is not None:
            self.metrics.record_hook_rule_evaluation(str(rule.id), rule.scope_type)

    def _record_winning_rule(self, rule: HookRule) -> None:
        if self.metrics is not None:
            self.metrics.record_hook_rule_decision(str(rule.id), rule.scope_type, rule.decision)
