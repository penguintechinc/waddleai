"""Tests for SecurityPolicyEngine: tier gating, fail-mode matrix, monotonic composition."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from shared.security.policy_engine import SecurityPolicyEngine, combine
from shared.security.policy_resolver import ResolvedPolicy


@dataclass(slots=True)
class _Violation:
    """Minimal stand-in for content_filter.FilterViolation."""

    action: str = "log"


class StubContentFilter:
    """Records which tier methods were called; fully scriptable per test."""

    def __init__(self) -> None:
        """Track calls and let tests configure return values / side effects."""
        self.calls: list[str] = []
        self.tier1_violations: list[_Violation] = []
        self.tier2_violations: list[_Violation] = []
        self.tier3_violations: list[_Violation] = []
        self.determine_action_result: tuple[str, str] = ("allow", "text")
        self.auditor_result: tuple[bool, str] = (False, "allow")
        self.auditor_side_effect: BaseException | None = None
        self.auditor_delay_s: float = 0.0

    async def _run_builtin_patterns(
        self, text: str, direction: str, org_id: Any
    ) -> list[_Violation]:
        self.calls.append("tier1")
        return self.tier1_violations

    async def _run_custom_rules(self, text: str, direction: str, org_id: Any) -> list[_Violation]:
        self.calls.append("tier2")
        return self.tier2_violations

    async def _run_ner_patterns(self, text: str, direction: str, org_id: Any) -> list[_Violation]:
        self.calls.append("tier3")
        return self.tier3_violations

    def _determine_action(self, text: str, violations: list[_Violation]) -> tuple[str, str]:
        return self.determine_action_result

    async def _invoke_llm_auditor(
        self, text: str, direction: str, violations: list[_Violation], org_id: Any
    ) -> tuple[bool, str]:
        self.calls.append("tier4")
        if self.auditor_delay_s:
            await asyncio.sleep(self.auditor_delay_s)
        if self.auditor_side_effect is not None:
            raise self.auditor_side_effect
        return self.auditor_result


def _policy(**overrides: Any) -> ResolvedPolicy:
    base = {
        "tier1_enabled": True,
        "tier2_enabled": True,
        "tier3_enabled": True,
        "tier4_enabled": True,
        "fail_mode": "degrade",
        "auditor_timeout_ms": 5000,
        "latency_budget_ms": None,
    }
    base.update(overrides)
    return ResolvedPolicy(**base)


class TestTierGating:
    """(a): only tiers enabled by the resolved policy run."""

    @pytest.mark.asyncio
    async def test_only_enabled_tiers_run(self) -> None:
        """Tiers disabled on the resolved policy are never invoked."""
        cf = StubContentFilter()
        engine = SecurityPolicyEngine(cf)
        policy = _policy(tier2_enabled=False, tier3_enabled=False, tier4_enabled=False)

        result = await engine.evaluate("hello", "input", policy)

        assert cf.calls == ["tier1"]
        assert result.tiers_run == ("tier1",)


class TestFailModeMatrix:
    """(b)-(e): degrade/closed/open under tier-4 timeout/error + latency budget."""

    @pytest.mark.asyncio
    async def test_degrade_on_timeout_enforces_tiers_1_to_3(self) -> None:
        """fail_mode=degrade + tier-4 timeout enforces the tiers-1-3 verdict, degraded=True."""
        cf = StubContentFilter()
        cf.determine_action_result = ("redact", "redacted-text")
        cf.auditor_delay_s = 0.05
        engine = SecurityPolicyEngine(cf)
        policy = _policy(fail_mode="degrade", auditor_timeout_ms=1)

        result = await engine.evaluate("hello", "input", policy)

        assert result.action == "redact"
        assert result.degraded is True

    @pytest.mark.asyncio
    async def test_closed_on_error_blocks(self) -> None:
        """fail_mode=closed + tier-4 error blocks regardless of the deterministic verdict."""
        cf = StubContentFilter()
        cf.determine_action_result = ("allow", "hello")
        cf.auditor_side_effect = RuntimeError("auditor down")
        engine = SecurityPolicyEngine(cf)
        policy = _policy(fail_mode="closed")

        result = await engine.evaluate("hello", "input", policy)

        assert result.action == "block"

    @pytest.mark.asyncio
    async def test_open_on_error_allows(self) -> None:
        """fail_mode=open + tier-4 error allows (a deliberate availability trade-off)."""
        cf = StubContentFilter()
        cf.determine_action_result = ("allow", "hello")
        cf.auditor_side_effect = RuntimeError("auditor down")
        engine = SecurityPolicyEngine(cf)
        policy = _policy(fail_mode="open")

        result = await engine.evaluate("hello", "input", policy)

        assert result.action == "allow"

    @pytest.mark.asyncio
    async def test_latency_budget_exceeded_skips_tier4(self) -> None:
        """Latency budget exceeded before tier 4 skips it; fail_mode governs the result."""
        cf = StubContentFilter()
        cf.determine_action_result = ("allow", "hello")
        engine = SecurityPolicyEngine(cf)
        # A budget of 0ms is exceeded immediately after tiers 1-3 run.
        policy = _policy(fail_mode="closed", latency_budget_ms=0)

        result = await engine.evaluate("hello", "input", policy)

        assert "tier4" not in cf.calls
        assert result.action == "block"  # fail_mode=closed governs the skip

    @pytest.mark.asyncio
    async def test_default_auditor_timeout_is_5000ms(self) -> None:
        """(g): ResolvedPolicy's default auditor_timeout_ms is 5000."""
        assert ResolvedPolicy().auditor_timeout_ms == 5000


class TestMonotonicComposition:
    """(f): an LLM verdict can only raise severity, never lower it."""

    def test_combine_llm_allow_never_downgrades_a_block(self) -> None:
        """An LLM 'allow' can never override a deterministic tier-1 block."""
        assert combine("block", "allow") == "block"

    def test_combine_llm_block_escalates_a_clean_pass(self) -> None:
        """An LLM 'block' over a clean deterministic pass escalates to block."""
        assert combine("allow", "block") == "block"

    def test_combine_llm_allow_over_clean_pass_stays_allow(self) -> None:
        """An LLM 'allow' over an already-clean pass stays allow."""
        assert combine("allow", "allow") == "allow"

    def test_combine_never_downgrades_redact(self) -> None:
        """An LLM 'allow' cannot downgrade a deterministic redact to allow."""
        assert combine("redact", "allow") == "redact"

    @pytest.mark.asyncio
    async def test_tier1_block_survives_a_real_llm_allow_verdict(self) -> None:
        """End-to-end: a tier-1 SSN block is never downgraded by tier 4."""
        cf = StubContentFilter()
        cf.tier1_violations = [_Violation(action="block")]
        cf.determine_action_result = ("block", "hello")
        cf.auditor_result = (False, "allow")  # LLM says allow
        engine = SecurityPolicyEngine(cf)
        policy = _policy()

        result = await engine.evaluate("hello with an SSN", "input", policy)

        assert result.action == "block"


class TestValkeyMockPattern:
    """Establishes AsyncMock-based collaborator wiring matches repo convention."""

    @pytest.mark.asyncio
    async def test_engine_works_with_resolver_and_features_collaborators(self) -> None:
        """Optional resolver/features collaborators are accepted but not required."""
        cf = StubContentFilter()
        resolver = AsyncMock()
        features = AsyncMock()
        engine = SecurityPolicyEngine(cf, resolver=resolver, features=features)

        result = await engine.evaluate("hello", "input", _policy())

        assert result.action == "allow"
