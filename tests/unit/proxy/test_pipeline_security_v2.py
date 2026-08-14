"""Tests for security-v2 wiring in SecurityInStage/SecurityOutStage/DispatchStage.

Uses real PolicyResolver/SecurityPolicyEngine/BypassResolver against stub
stores/content-filters (same pattern as the shared/security/ unit tests) so
these tests exercise the actual stage-level plumbing, not just mocks.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from proxy.apps.proxy_server.pipeline.stages import (
    DispatchStage,
    PipelineContext,
    SecurityInStage,
    SecurityOutStage,
)
from shared.security.bypass import BYPASS_SCOPE, BypassGrant, BypassResolver, BypassStore
from shared.security.output_guardrails import OutputGuardrails
from shared.security.policy_engine import SecurityPolicyEngine
from shared.security.policy_resolver import PolicyResolver, _CandidateRow


class _StubCF:
    """Minimal ContentFilter stand-in shared by all v2 wiring tests here."""

    def __init__(self, secret: str = "123-45-6789") -> None:  # noqa: S107 -- test SSN fixture, not a credential
        """Track calls; redact `secret` wherever it appears."""
        self.secret = secret
        self.calls: list[str] = []

    async def _run_builtin_patterns(self, text: str, direction: str, org_id: Any) -> list[Any]:
        self.calls.append("tier1")
        if self.secret in text:
            return [
                SimpleNamespace(
                    action="redact",
                    rule_name="ssn",
                    full_matched_text=self.secret,
                    matched_text=self.secret,
                )
            ]
        return []

    async def _run_custom_rules(self, text: str, direction: str, org_id: Any) -> list[Any]:
        self.calls.append("tier2")
        return []

    async def _run_ner_patterns(self, text: str, direction: str, org_id: Any) -> list[Any]:
        self.calls.append("tier3")
        return []

    async def _invoke_llm_auditor(self, *args: Any, **kwargs: Any) -> tuple[bool, str]:
        self.calls.append("tier4")
        return False, "allow"

    def _determine_action(self, text: str, violations: list[Any]) -> tuple[str, str]:
        if not violations:
            return "allow", text
        redacted = text
        for v in violations:
            redacted = redacted.replace(v.full_matched_text, "[REDACTED]")
        return "redact", redacted


class _AllowAllStore:
    """PolicyStore stand-in returning a single permissive global row."""

    async def fetch_scope_rows(self, scope_type: str, scope_ref):
        if scope_type == "global":
            fields = {"tier1_enabled": True, "tier4_enabled": False}
            return [_CandidateRow("global", None, "both", fields)]
        return []


class _NoGrantStore(BypassStore):
    async def find_active_grant(self, subject_type, subject_ref, now):
        return None


class _AlwaysFeaturesOn:
    """features helper stand-in: always reports the flag enabled."""

    def is_feature_enabled(self, flag_key: str, distinct_id: str | None = None) -> bool:
        return True


class _AlwaysFeaturesOff:
    """features helper stand-in: always reports the flag disabled."""

    def is_feature_enabled(self, flag_key: str, distinct_id: str | None = None) -> bool:
        return False


def _ctx(**overrides: Any) -> PipelineContext:
    user = Mock(id=1, tenant_id="org1", user_id=1, organization_id="org1")
    base = dict(user=user, body={}, model="gpt-4", messages=[{"role": "user", "content": "hi"}])
    base.update(overrides)
    return PipelineContext(**base)


class TestSecurityInStageV2Wiring:
    """(a): flag ON resolves + runs engine; flag OFF falls through to v1 unchanged."""

    @pytest.mark.asyncio
    async def test_flag_off_uses_v1_path_even_with_v2_collaborators_wired(self) -> None:
        """Even with v2 collaborators present, flag-off never touches them."""
        cf = _StubCF()
        scanner = Mock(
            scan_messages=Mock(return_value=([], None)), should_block=Mock(return_value=False)
        )
        real_content_filter = Mock()
        real_content_filter.filter_input = AsyncMock(
            return_value=SimpleNamespace(allowed=True, filtered_text="hi")
        )
        engine = SecurityPolicyEngine(cf)
        resolver = PolicyResolver(_AllowAllStore())

        stage = SecurityInStage(
            "security_in",
            scanner,
            real_content_filter,
            policy_resolver=resolver,
            policy_engine=engine,
            features=_AlwaysFeaturesOff(),
        )

        await stage(_ctx())

        assert cf.calls == []  # v2 engine never invoked
        real_content_filter.filter_input.assert_awaited()  # v1 path ran instead

    @pytest.mark.asyncio
    async def test_flag_on_resolves_and_runs_engine(self) -> None:
        """Flag-on with v2 collaborators wired redacts via the policy engine, not v1."""
        cf = _StubCF()
        scanner = Mock(
            scan_messages=Mock(return_value=([], None)), should_block=Mock(return_value=False)
        )
        real_content_filter = Mock()
        real_content_filter.filter_input = AsyncMock()
        engine = SecurityPolicyEngine(cf)
        resolver = PolicyResolver(_AllowAllStore())

        stage = SecurityInStage(
            "security_in",
            scanner,
            real_content_filter,
            policy_resolver=resolver,
            policy_engine=engine,
            features=_AlwaysFeaturesOn(),
        )
        ctx = _ctx(messages=[{"role": "user", "content": "ssn 123-45-6789"}])

        result = await stage(ctx)

        assert "tier1" in cf.calls
        real_content_filter.filter_input.assert_not_awaited()  # v1 path never ran
        assert "[REDACTED]" in result.messages[0]["content"]


class TestSecurityInStageBypass:
    """(e): bypass shadow/skip honored end-to-end in the stage."""

    @pytest.mark.asyncio
    async def test_skip_grant_short_circuits_enforcement(self) -> None:
        """A skip-mode bypass grant means the engine never redacts the message."""
        cf = _StubCF()
        scanner = Mock(
            scan_messages=Mock(return_value=([], None)), should_block=Mock(return_value=False)
        )
        engine = SecurityPolicyEngine(cf)
        resolver = PolicyResolver(_AllowAllStore())

        class _SkipGrantStore(BypassStore):
            async def find_active_grant(self, subject_type, subject_ref, now):
                return BypassGrant(id=1, subject_type="user", subject_ref="1", mode="skip")

        bypass_resolver = BypassResolver(_SkipGrantStore())
        user = Mock(id=1, tenant_id="org1", token_scopes=(BYPASS_SCOPE,))

        stage = SecurityInStage(
            "security_in",
            scanner,
            Mock(),
            policy_resolver=resolver,
            policy_engine=engine,
            bypass_resolver=bypass_resolver,
            features=_AlwaysFeaturesOn(),
        )
        ctx = _ctx(user=user, messages=[{"role": "user", "content": "ssn 123-45-6789"}])

        result = await stage(ctx)

        assert cf.calls == []  # tiers never ran
        assert result.messages[0]["content"] == "ssn 123-45-6789"  # untouched


class TestSecurityOutStageV2Wiring:
    """(b): SecurityOutStage runs output guardrails under the resolved policy."""

    @pytest.mark.asyncio
    async def test_flag_on_redacts_via_output_guardrails(self) -> None:
        """Flag-on output redaction runs via OutputGuardrails, not the v1 content_filter."""
        cf = _StubCF()
        engine = SecurityPolicyEngine(cf)
        guardrails = OutputGuardrails(engine)
        resolver = PolicyResolver(_AllowAllStore())
        real_content_filter = Mock()
        real_content_filter.filter_output = AsyncMock()

        stage = SecurityOutStage(
            "security_out",
            real_content_filter,
            output_guardrails=guardrails,
            policy_resolver=resolver,
            features=_AlwaysFeaturesOn(),
        )
        ctx = _ctx(response_text="your ssn is 123-45-6789")

        result = await stage(ctx)

        real_content_filter.filter_output.assert_not_awaited()
        assert "[REDACTED]" in result.response_text


class TestDispatchStageUpstreamFilter:
    """(c): DispatchStage applies upstream filters only for commercial destinations."""

    @pytest.mark.asyncio
    async def test_upstream_filter_applies_for_commercial_not_local(self) -> None:
        """A commercial destination gets filtered messages; the resolved policy governs mode."""
        resolver = PolicyResolver(_AllowAllStore())

        calls: list[str] = []

        class _StubUpstreamFilter:
            async def apply(self, text, resolved, destination_kind, ctx=None):
                calls.append(destination_kind)
                redacted = text.replace("123-45-6789", "[REDACTED]")
                return SimpleNamespace(text=redacted, mapping_id=None, counts={})

            async def depseudonymize(self, text, mapping_id):
                return text

            async def cleanup(self, mapping_id):
                pass

        router = Mock()
        router.select_provider = Mock(return_value=("openai", "gpt-4"))
        connector = Mock()
        connector.chat_completion = AsyncMock(
            return_value=("ok", {"input_tokens": 1, "output_tokens": 1, "finish_reason": "stop"})
        )

        stage = DispatchStage(
            "dispatch",
            router,
            {"openai": connector},
            upstream_filter=_StubUpstreamFilter(),
            policy_resolver=resolver,
            features=_AlwaysFeaturesOn(),
        )
        ctx = _ctx(messages=[{"role": "user", "content": "ssn 123-45-6789"}])

        result = await stage(ctx)

        assert calls == ["commercial"]
        assert "[REDACTED]" in result.messages[0]["content"]
