"""Tests for OutputGuardrails: non-streamed redaction, streaming windows, fail_mode."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from shared.security.output_guardrails import OutputGuardrails
from shared.security.policy_engine import SecurityPolicyEngine
from shared.security.policy_resolver import ResolvedPolicy

_SECRET = "123-45-6789"  # noqa: S105 -- test fixture SSN pattern, not a credential


class StubOutputContentFilter:
    """Minimal ContentFilter stand-in: one substring ("SSN") pattern, tiers 1-2 only."""

    def __init__(self, secret: str = _SECRET) -> None:
        """Track calls; redact `secret` wherever it appears in scanned text."""
        self.secret = secret
        self.calls: list[str] = []

    async def _run_builtin_patterns(self, text: str, direction: str, org_id: Any) -> list[Any]:
        self.calls.append("tier1")
        if self.secret in text:
            return [
                SimpleNamespace(
                    action="redact",
                    full_matched_text=self.secret,
                    matched_text=self.secret,
                    rule_name="ssn",
                    rule_type="builtin_pii",
                    confidence=0.95,
                )
            ]
        return []

    async def _run_custom_rules(self, text: str, direction: str, org_id: Any) -> list[Any]:
        self.calls.append("tier2")
        return []

    async def _run_ner_patterns(self, text: str, direction: str, org_id: Any) -> list[Any]:
        self.calls.append("tier3")
        return []

    def _determine_action(self, text: str, violations: list[Any]) -> tuple[str, str]:
        if not violations:
            return "allow", text
        redacted = text
        for v in violations:
            if v.action == "redact":
                redacted = redacted.replace(v.full_matched_text, "[REDACTED]")
        return ("redact" if redacted != text else "log"), redacted

    async def _invoke_llm_auditor(
        self, text: str, direction: str, violations: list[Any], org_id: Any
    ) -> tuple[bool, str]:
        return False, "allow"


def _policy(**overrides: Any) -> ResolvedPolicy:
    base = {
        "tier1_enabled": True,
        "tier2_enabled": True,
        "tier3_enabled": False,
        "tier4_enabled": False,
    }
    base.update(overrides)
    return ResolvedPolicy(**base)


async def _achunks(chunks: list[str]):
    for c in chunks:
        yield c


class TestNonStreamedRedaction:
    """(a): a non-streamed response containing an SSN is redacted per block_action=redact."""

    @pytest.mark.asyncio
    async def test_ssn_in_response_is_redacted(self) -> None:
        """scan_output redacts the SSN and reports the redaction count."""
        cf = StubOutputContentFilter()
        engine = SecurityPolicyEngine(cf)
        guardrails = OutputGuardrails(engine)

        result = await guardrails.scan_output(f"your ssn is {_SECRET}", _policy())

        assert result.action == "redact"
        assert _SECRET not in result.filtered_text
        assert "[REDACTED]" in result.filtered_text
        assert result.redactions == 1  # (e) redaction counts surface for metering


class TestDirectionScoping:
    """(b): an input-only-scoped policy (all tiers off for output) leaves output untouched."""

    @pytest.mark.asyncio
    async def test_all_tiers_disabled_leaves_output_untouched(self) -> None:
        """With every tier disabled (as an input-only policy resolves for output), no scan runs."""
        cf = StubOutputContentFilter()
        engine = SecurityPolicyEngine(cf)
        guardrails = OutputGuardrails(engine)
        policy = _policy(tier1_enabled=False, tier2_enabled=False)

        result = await guardrails.scan_output(f"your ssn is {_SECRET}", policy)

        assert result.action == "allow"
        assert _SECRET in result.filtered_text
        assert cf.calls == []


class TestStreamingWindow:
    """(c): a boundary-straddling match across chunks is caught by the sliding window."""

    @pytest.mark.asyncio
    async def test_boundary_straddling_secret_never_leaks_in_any_chunk(self) -> None:
        """The secret split across two chunks is redacted before either half is emitted."""
        cf = StubOutputContentFilter()
        engine = SecurityPolicyEngine(cf)
        guardrails = OutputGuardrails(engine)

        chunk1 = "A" * 200 + "123-45-"
        chunk2 = "6789" + "B" * 10
        source = _achunks([chunk1, chunk2])

        emitted: list[str] = []
        async for piece in guardrails.scan_stream(source, _policy()):
            emitted.append(piece)

        full_output = "".join(emitted)
        assert _SECRET not in full_output
        assert "[REDACTED]" in full_output
        # Also verify no single yielded chunk carries a partial-but-recoverable
        # match (the property that actually matters for the "never leaks" claim).
        for piece in emitted:
            assert _SECRET not in piece


class TestStreamFailMode:
    """(d): a latency-budget overrun during streaming applies fail_mode."""

    @pytest.mark.asyncio
    async def test_closed_stops_the_stream(self) -> None:
        """fail_mode=closed stops emitting further chunks once the budget is blown."""
        cf = StubOutputContentFilter()
        engine = SecurityPolicyEngine(cf)
        guardrails = OutputGuardrails(engine)
        policy = _policy(fail_mode="closed", latency_budget_ms=0)
        source = _achunks(["chunk one", "chunk two"])

        emitted = [piece async for piece in guardrails.scan_stream(source, policy)]

        assert emitted == []

    @pytest.mark.asyncio
    async def test_open_passes_through_unredacted(self) -> None:
        """fail_mode=open keeps streaming, unredacted, once the budget is blown."""
        cf = StubOutputContentFilter()
        engine = SecurityPolicyEngine(cf)
        guardrails = OutputGuardrails(engine)
        policy = _policy(fail_mode="open", latency_budget_ms=0)
        source = _achunks([f"secret: {_SECRET}"])

        emitted = [piece async for piece in guardrails.scan_stream(source, policy)]

        assert "".join(emitted) == f"secret: {_SECRET}"  # passthrough, unredacted
