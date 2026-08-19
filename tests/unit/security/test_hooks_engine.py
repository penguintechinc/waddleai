"""Tests for HooksPolicyEngine: the tier1 -> hook_rules -> tier2 -> default chain (§18)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from shared.security.hooks_config import HookConfig
from shared.security.hooks_denylist import DenylistEntry
from shared.security.hooks_engine import HooksPolicyEngine
from shared.security.hooks_rules import HookRule


class _StaticDenylistResolver:
    """A HookDenylistResolver stand-in returning a fixed entry list."""

    def __init__(self, entries: list[DenylistEntry] | None = None) -> None:
        self.entries = entries or []

    async def resolve(self, org_id: Any) -> list[DenylistEntry]:
        return self.entries


class _StaticRulesResolver:
    """A HookRulesResolver stand-in returning a fixed rule list."""

    def __init__(self, rules: list[HookRule] | None = None) -> None:
        self.rules = rules or []

    async def resolve(self, org_id: Any) -> list[HookRule]:
        return self.rules


class _StaticConfigResolver:
    """A HookConfigResolver stand-in returning a fixed HookConfig."""

    def __init__(self, config: HookConfig | None = None) -> None:
        self.config = config or HookConfig()

    async def resolve(self, org_id: Any) -> HookConfig:
        return self.config


@dataclass(slots=True)
class _StubVerdict:
    """Minimal stand-in for policy_engine.SecurityVerdict."""

    action: str
    violations: list[Any] = field(default_factory=list)
    degraded: bool = False


class _StubSecurityPolicyEngine:
    """A SecurityPolicyEngine stand-in: scriptable action, optional artificial delay."""

    def __init__(self, action: str = "allow", delay_s: float = 0.0, raises: bool = False) -> None:
        self.action = action
        self.delay_s = delay_s
        self.raises = raises
        self.calls: list[tuple[str, str, Any]] = []

    async def evaluate(
        self, text: str, direction: str, resolved: Any, ctx: Any = None
    ) -> _StubVerdict:
        self.calls.append((text, direction, ctx))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.raises:
            raise RuntimeError("boom")
        return _StubVerdict(action=self.action)


class _StubPolicyResolver:
    """A PolicyResolver stand-in -- returns a sentinel `resolved` object."""

    async def resolve(self, org_id: Any, model: Any, tool_name: Any, direction: str) -> str:
        return "resolved-policy-sentinel"


class _StubMetrics:
    """Records every hook-metric call for assertion."""

    def __init__(self) -> None:
        self.fail_modes: list[str] = []
        self.timeouts: list[str] = []
        self.rule_evaluations: list[tuple[str, str]] = []
        self.rule_decisions: list[tuple[str, str, str]] = []

    def record_hook_fail_mode(self, mode: str) -> None:
        self.fail_modes.append(mode)

    def record_hook_timeout(self, tier: str) -> None:
        self.timeouts.append(tier)

    def record_hook_rule_evaluation(self, rule_id: str, scope: str) -> None:
        self.rule_evaluations.append((rule_id, scope))

    def record_hook_rule_decision(self, rule_id: str, scope: str, decision: str) -> None:
        self.rule_decisions.append((rule_id, scope, decision))


def _engine(
    denylist_entries: list[DenylistEntry] | None = None,
    rules: list[HookRule] | None = None,
    config: HookConfig | None = None,
    security_policy_engine: Any = None,
    metrics: Any = None,
) -> HooksPolicyEngine:
    return HooksPolicyEngine(
        denylist_resolver=_StaticDenylistResolver(denylist_entries),
        rules_resolver=_StaticRulesResolver(rules),
        config_resolver=_StaticConfigResolver(config),
        security_policy_resolver=_StubPolicyResolver(),
        security_policy_engine=security_policy_engine,
        metrics=metrics,
    )


async def _evaluate(
    engine: HooksPolicyEngine, tool_input: dict[str, Any] | None = None, tool_name: str = "Bash"
) -> Any:
    """Shorthand for the common evaluate() call shape used throughout this file."""
    return await engine.evaluate(
        "claude-code", "pre_tool_use", tool_name, tool_input or {}, "org-1"
    )


class TestTier1Denylist:
    """Tier 1 is absolute: a match denies immediately, before hook_rules even load."""

    @pytest.mark.asyncio
    async def test_denylist_hit_denies(self) -> None:
        """A matched denylist entry returns deny with tier='tier1'."""
        engine = _engine(denylist_entries=[DenylistEntry(pattern=".env*", source="builtin")])

        result = await _evaluate(engine, {"command": "cat .env"})

        assert result.decision == "deny"
        assert result.tier == "tier1"

    @pytest.mark.asyncio
    async def test_admin_allow_rule_cannot_weaken_denylist(self) -> None:
        """An admin hook_rule explicitly allowing `.env` does NOT override the Tier-1 deny.

        The rule is never even reached: Tier 1 returns before hook_rules are
        loaded, which is the structural (not runtime-special-cased)
        enforcement of "an admin rule cannot weaken the Tier-1 denylist".
        """
        allow_env_rule = HookRule(
            id=99,
            scope_type="global",
            scope_ref=None,
            ecosystem=None,
            event=None,
            tool_name_pattern=None,
            match_pattern="*.env*",
            decision="allow",
            reason="admin says .env is fine, actually",
            priority=1,
        )
        engine = _engine(
            denylist_entries=[DenylistEntry(pattern=".env*", source="builtin")],
            rules=[allow_env_rule],
        )

        result = await _evaluate(engine, {"command": "cat .env"})

        assert result.decision == "deny"
        assert result.tier == "tier1"


class TestHookRulesTier:
    """A matched admin rule is authoritative and skips Tier 2 entirely."""

    @pytest.mark.asyncio
    async def test_matched_rule_wins_and_skips_tier2(self) -> None:
        """A matching rule's decision is returned; Tier 2 is never invoked."""
        rule = HookRule(
            id=5,
            scope_type="org",
            scope_ref="org-1",
            ecosystem=None,
            event=None,
            tool_name_pattern="Bash",
            match_pattern=None,
            decision="ask",
            reason="confirm shell use",
        )
        tier2 = _StubSecurityPolicyEngine(action="allow")
        engine = _engine(
            rules=[rule],
            config=HookConfig(remote_eval_enabled=True),
            security_policy_engine=tier2,
        )

        result = await _evaluate(engine, {"command": "ls"})

        assert result.decision == "ask"
        assert result.tier == "hook_rule"
        assert result.rule_id == "5"
        assert tier2.calls == []

    @pytest.mark.asyncio
    async def test_records_evaluation_and_decision_metrics(self) -> None:
        """Every matched rule is counted as an evaluation; only the winner as a decision."""
        loser = HookRule(
            id=1,
            scope_type="global",
            scope_ref=None,
            ecosystem=None,
            event=None,
            tool_name_pattern=None,
            match_pattern=None,
            decision="allow",
            reason="floor",
            priority=200,
        )
        winner = HookRule(
            id=2,
            scope_type="org",
            scope_ref="org-1",
            ecosystem=None,
            event=None,
            tool_name_pattern=None,
            match_pattern=None,
            decision="deny",
            reason="blocked",
            priority=1,
        )
        metrics = _StubMetrics()
        engine = _engine(rules=[loser, winner], metrics=metrics)

        await _evaluate(engine)

        assert ("1", "global") in metrics.rule_evaluations
        assert ("2", "org") in metrics.rule_evaluations
        assert metrics.rule_decisions == [("2", "org", "deny")]


class TestTier2:
    """Opt-in remote policy evaluation, action->decision mapping, timeout/fail-mode."""

    @pytest.mark.asyncio
    async def test_disabled_by_default_falls_through_to_allow(self) -> None:
        """No rule matched, Tier 2 not enabled -> default allow."""
        tier2 = _StubSecurityPolicyEngine(action="block")
        config = HookConfig(remote_eval_enabled=False)
        engine = _engine(config=config, security_policy_engine=tier2)

        result = await _evaluate(engine)

        assert result.decision == "allow"
        assert result.tier == "default"
        assert tier2.calls == []

    @pytest.mark.parametrize(
        "action,expected_decision",
        [("allow", "allow"), ("flag", "ask"), ("redact", "ask"), ("block", "deny")],
    )
    @pytest.mark.asyncio
    async def test_action_to_decision_mapping(self, action: str, expected_decision: str) -> None:
        """§8 content-filter actions map onto hook decisions per the documented table."""
        tier2 = _StubSecurityPolicyEngine(action=action)
        config = HookConfig(remote_eval_enabled=True)
        engine = _engine(config=config, security_policy_engine=tier2)

        result = await _evaluate(engine, {"command": "x"})

        assert result.decision == expected_decision
        assert result.tier == "tier2"

    @pytest.mark.asyncio
    async def test_timeout_fail_open_allows(self) -> None:
        """remote_eval_fail_mode='open': a Tier-2 timeout allows, logged as fail_open."""
        tier2 = _StubSecurityPolicyEngine(action="block", delay_s=0.05)
        metrics = _StubMetrics()
        config = HookConfig(
            remote_eval_enabled=True, remote_eval_timeout_ms=1, remote_eval_fail_mode="open"
        )
        engine = _engine(config=config, security_policy_engine=tier2, metrics=metrics)

        result = await _evaluate(engine, {"command": "x"})

        assert result.decision == "allow"
        assert result.degraded is True
        assert metrics.fail_modes == ["fail_open"]
        assert metrics.timeouts == ["tier2"]

    @pytest.mark.asyncio
    async def test_timeout_fail_closed_denies(self) -> None:
        """remote_eval_fail_mode='closed': a Tier-2 timeout denies, logged as fail_closed."""
        tier2 = _StubSecurityPolicyEngine(action="allow", delay_s=0.05)
        metrics = _StubMetrics()
        config = HookConfig(
            remote_eval_enabled=True, remote_eval_timeout_ms=1, remote_eval_fail_mode="closed"
        )
        engine = _engine(config=config, security_policy_engine=tier2, metrics=metrics)

        result = await _evaluate(engine, {"command": "x"})

        assert result.decision == "deny"
        assert result.degraded is True
        assert metrics.fail_modes == ["fail_closed"]

    @pytest.mark.asyncio
    async def test_exception_applies_fail_mode_too(self) -> None:
        """A Tier-2 exception (not just a timeout) also goes through fail_mode."""
        tier2 = _StubSecurityPolicyEngine(action="allow", raises=True)
        config = HookConfig(remote_eval_enabled=True, remote_eval_fail_mode="open")
        engine = _engine(config=config, security_policy_engine=tier2)

        result = await _evaluate(engine, {"command": "x"})

        assert result.decision == "allow"
        assert result.degraded is True
