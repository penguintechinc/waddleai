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
from shared.security.intent_classifier import IntentResult
from shared.security.output_guardrails import OutputGuardrails
from shared.security.policy_engine import SecurityPolicyEngine, SecurityVerdict
from shared.security.policy_resolver import PolicyResolver, ResolvedPolicy, _CandidateRow


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
            # `violations` is required: the §7 routing branch made SecurityInStage's
            # v1 path read filter_result.violations to set ctx.pii_detected for
            # RoutingStage's sensitivity clamp. A double omitting it raises
            # AttributeError once both features are wired together.
            return_value=SimpleNamespace(allowed=True, filtered_text="hi", violations=[])
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


class TestSecurityInStageIntentClassifier:
    """(§8.3) intent_classifier escalates/overrides the policy-engine verdict."""

    @pytest.mark.asyncio
    async def test_intent_flag_upgrades_allow_but_does_not_block(self) -> None:
        """An intent 'flag' verdict upgrades a policy-engine 'allow' but never blocks."""
        scanner = Mock(
            scan_messages=Mock(return_value=([], None)), should_block=Mock(return_value=False)
        )
        policy_resolver = Mock()
        policy_resolver.resolve = AsyncMock(
            return_value=ResolvedPolicy(intent_classifier_enabled=True)
        )
        policy_engine = Mock()
        policy_engine.evaluate = AsyncMock(
            return_value=SecurityVerdict(action="allow", filtered_text="hi")
        )
        intent_classifier = Mock()
        intent_classifier.classify = AsyncMock(return_value=IntentResult(action="flag"))

        stage = SecurityInStage(
            "security_in",
            scanner,
            Mock(),
            policy_resolver=policy_resolver,
            policy_engine=policy_engine,
            intent_classifier=intent_classifier,
            features=_AlwaysFeaturesOn(),
        )
        ctx = _ctx(messages=[{"role": "user", "content": "hi"}])

        result = await stage(ctx)

        intent_classifier.classify.assert_awaited_once()
        assert result.blocked is False
        assert result.messages[0]["content"] == "hi"

    @pytest.mark.asyncio
    async def test_intent_block_overrides_policy_engine_allow(self) -> None:
        """An intent 'block' verdict overrides a policy-engine 'allow' and blocks."""
        scanner = Mock(
            scan_messages=Mock(return_value=([], None)), should_block=Mock(return_value=False)
        )
        policy_resolver = Mock()
        policy_resolver.resolve = AsyncMock(
            return_value=ResolvedPolicy(intent_classifier_enabled=True)
        )
        policy_engine = Mock()
        policy_engine.evaluate = AsyncMock(
            return_value=SecurityVerdict(action="allow", filtered_text="hi")
        )
        intent_classifier = Mock()
        intent_classifier.classify = AsyncMock(return_value=IntentResult(action="block"))

        stage = SecurityInStage(
            "security_in",
            scanner,
            Mock(),
            policy_resolver=policy_resolver,
            policy_engine=policy_engine,
            intent_classifier=intent_classifier,
            features=_AlwaysFeaturesOn(),
        )
        ctx = _ctx(messages=[{"role": "user", "content": "hi"}])

        result = await stage(ctx)

        assert result.blocked is True
        assert result.status_code == 400
        assert result.block_reason == "security_v2_blocked"

    @pytest.mark.asyncio
    async def test_degraded_verdict_sets_ctx_security_degraded(self) -> None:
        """A degraded SecurityVerdict propagates to ctx.security_degraded (no intent_classifier)."""
        scanner = Mock(
            scan_messages=Mock(return_value=([], None)), should_block=Mock(return_value=False)
        )
        policy_resolver = Mock()
        policy_resolver.resolve = AsyncMock(return_value=ResolvedPolicy())
        policy_engine = Mock()
        policy_engine.evaluate = AsyncMock(
            return_value=SecurityVerdict(action="allow", filtered_text="hi", degraded=True)
        )

        stage = SecurityInStage(
            "security_in",
            scanner,
            Mock(),
            policy_resolver=policy_resolver,
            policy_engine=policy_engine,
            features=_AlwaysFeaturesOn(),
        )
        ctx = _ctx(messages=[{"role": "user", "content": "hi"}])

        result = await stage(ctx)

        assert result.security_degraded is True
        assert result.blocked is False


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

    @pytest.mark.asyncio
    async def test_flag_off_uses_v1_path_even_with_guardrails_wired(self) -> None:
        """Flag-off never touches OutputGuardrails, even wired -- v1 content_filter runs instead."""
        resolver = PolicyResolver(_AllowAllStore())
        guardrails = Mock()
        guardrails.scan_output = AsyncMock()
        real_content_filter = Mock()
        real_content_filter.filter_output = AsyncMock(
            return_value=SimpleNamespace(allowed=True, filtered_text="clean", violations=[])
        )

        stage = SecurityOutStage(
            "security_out",
            real_content_filter,
            output_guardrails=guardrails,
            policy_resolver=resolver,
            features=_AlwaysFeaturesOff(),
        )
        ctx = _ctx(response_text="ssn 123-45-6789")

        result = await stage(ctx)

        guardrails.scan_output.assert_not_awaited()
        real_content_filter.filter_output.assert_awaited()
        assert result.response_text == "clean"

    @pytest.mark.asyncio
    async def test_v2_scan_output_type_error_fails_closed(self) -> None:
        """A programming-error TypeError from scan_output blocks the response (fail CLOSED)."""
        resolver = PolicyResolver(_AllowAllStore())
        guardrails = Mock()
        guardrails.scan_output = AsyncMock(side_effect=TypeError("bad kwargs"))

        stage = SecurityOutStage(
            "security_out",
            Mock(),
            output_guardrails=guardrails,
            policy_resolver=resolver,
            features=_AlwaysFeaturesOn(),
        )
        ctx = _ctx(response_text="hello there")

        result = await stage(ctx)

        assert result.blocked is True
        assert result.status_code == 500
        assert result.block_reason == "output_filter_defect"

    @pytest.mark.asyncio
    async def test_v2_degraded_verdict_sets_ctx_security_degraded(self) -> None:
        """A degraded verdict from OutputGuardrails propagates to ctx.security_degraded."""
        resolver = PolicyResolver(_AllowAllStore())
        verdict = SecurityVerdict(action="allow", filtered_text="clean text", degraded=True)
        guardrails = Mock()
        guardrails.scan_output = AsyncMock(return_value=verdict)

        stage = SecurityOutStage(
            "security_out",
            Mock(),
            output_guardrails=guardrails,
            policy_resolver=resolver,
            features=_AlwaysFeaturesOn(),
        )
        ctx = _ctx(response_text="hello")

        result = await stage(ctx)

        assert result.security_degraded is True
        assert result.blocked is False
        assert result.response_text == "clean text"

    @pytest.mark.asyncio
    async def test_v2_block_verdict_blocks_response(self) -> None:
        """A 'block' verdict from OutputGuardrails blocks the response."""
        resolver = PolicyResolver(_AllowAllStore())
        verdict = SecurityVerdict(action="block", filtered_text="")
        guardrails = Mock()
        guardrails.scan_output = AsyncMock(return_value=verdict)

        stage = SecurityOutStage(
            "security_out",
            Mock(),
            output_guardrails=guardrails,
            policy_resolver=resolver,
            features=_AlwaysFeaturesOn(),
        )
        ctx = _ctx(response_text="sensitive stuff")

        result = await stage(ctx)

        assert result.blocked is True
        assert result.status_code == 400
        assert result.block_reason == "security_v2_output_blocked"


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

    @pytest.mark.asyncio
    async def test_upstream_filter_skipped_when_flag_off(self) -> None:
        """Flag-off never invokes the security_v2 upstream filter, even when wired."""
        resolver = PolicyResolver(_AllowAllStore())
        calls: list[str] = []

        class _StubUpstreamFilter:
            async def apply(self, text, resolved, destination_kind, ctx=None):
                calls.append(destination_kind)
                return SimpleNamespace(text=text, mapping_id=None, counts={})

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
            features=_AlwaysFeaturesOff(),
        )
        ctx = _ctx(messages=[{"role": "user", "content": "ssn 123-45-6789"}])

        result = await stage(ctx)

        assert calls == []  # upstream filter never invoked
        assert result.messages[0]["content"] == "ssn 123-45-6789"  # untouched

    @pytest.mark.asyncio
    async def test_upstream_mapping_id_depseudonymized_and_cleaned_up_after_dispatch(self) -> None:
        """A mapping_id stashed during pre-dispatch filtering is depseudonymized, then dropped."""
        resolver = PolicyResolver(_AllowAllStore())
        cleanup_calls: list[str] = []

        class _StubUpstreamFilter:
            async def apply(self, text, resolved, destination_kind, ctx=None):
                return SimpleNamespace(text=text, mapping_id="map-123", counts={})

            async def depseudonymize(self, text, mapping_id):
                return f"{text}[depseudo:{mapping_id}]"

            async def cleanup(self, mapping_id):
                cleanup_calls.append(mapping_id)

        router = Mock()
        router.select_provider = Mock(return_value=("openai", "gpt-4"))
        connector = Mock()
        connector.chat_completion = AsyncMock(
            return_value=(
                "raw response",
                {"input_tokens": 1, "output_tokens": 1, "finish_reason": "stop"},
            )
        )

        stage = DispatchStage(
            "dispatch",
            router,
            {"openai": connector},
            upstream_filter=_StubUpstreamFilter(),
            policy_resolver=resolver,
            features=_AlwaysFeaturesOn(),
        )
        ctx = _ctx(messages=[{"role": "user", "content": "hi"}])

        result = await stage(ctx)

        assert result.response_text == "raw response[depseudo:map-123]"
        assert result.upstream_mapping_id is None
        assert cleanup_calls == ["map-123"]
