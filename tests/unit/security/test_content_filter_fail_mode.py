"""Tests for ContentFilter's fail-open vs fail-closed classification.

Regression: `ContentFilter._filter()` used a single blanket `except
Exception` that failed OPEN (allowed content through) for any internal
error, including future logic bugs -- the same defect class already fixed
one layer up in `SecurityOutStage` (a stale `ip=` kwarg made every
`filter_output()` call raise `TypeError`, silently disabling output PII
filtering in production because the blanket handler treated it identically
to an ordinary auditor timeout).

Programming errors (TypeError/AttributeError/KeyError/NameError/ImportError)
must now fail CLOSED and be logged loudly; genuine operational errors (DB
timeout, unreachable auditor, network blip) must keep failing open, exactly
as before.
"""

from __future__ import annotations

import logging

import pytest

from shared.security.content_filter import ContentFilter, _content_filter_fail_total


def _counter_value(phase: str, mode: str) -> float:
    """Read the current value of the fail-mode counter for a label set."""
    return _content_filter_fail_total.labels(phase=phase, mode=mode)._value.get()


class _LicensedForNER:
    """Licence stub entitling the NER tier.

    Needed because the tier is licence-gated: without an entitlement the
    filter skips _run_ner_patterns entirely, and a test that patches that
    method to raise would assert fail-closed behaviour against code that
    never runs.
    """

    def check_feature(self, _feature: str) -> bool:
        return True


@pytest.fixture
def filter_instance() -> ContentFilter:
    """Create a content filter with no database backend, NER tier entitled."""
    return ContentFilter(db=None, license_client=_LicensedForNER())


class TestProgrammingErrorsFailClosed:
    """A programming defect anywhere in the pipeline must block, not allow."""

    @pytest.mark.asyncio
    async def test_type_error_in_builtin_patterns_fails_closed(
        self, filter_instance: ContentFilter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A TypeError raised inside tier-1 pattern matching blocks the request."""

        async def _boom(text: str, target: str, org_id: int | None = None) -> list:
            raise TypeError("bad call signature")

        monkeypatch.setattr(filter_instance, "_run_builtin_patterns", _boom)
        before = _counter_value("input", "fail_closed")

        result = await filter_instance.filter_input("some text")

        assert result.allowed is False
        assert result.action == "block"
        assert result.violations == []
        assert _counter_value("input", "fail_closed") == before + 1

    @pytest.mark.asyncio
    async def test_attribute_error_in_custom_rules_loop_fails_closed(
        self, filter_instance: ContentFilter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An AttributeError from applying a malformed cached rule blocks, not degrades silently."""

        async def _boom(text: str, target: str, org_id: int | None) -> list:
            raise AttributeError("'NoneType' object has no attribute 'lower'")

        monkeypatch.setattr(filter_instance, "_run_custom_rules", _boom)

        result = await filter_instance.filter_output("some response text")

        assert result.allowed is False
        assert result.action == "block"

    @pytest.mark.asyncio
    async def test_key_error_in_ner_processing_fails_closed(
        self, filter_instance: ContentFilter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A KeyError from a schema-drifted NER entity dict blocks, not silently drops the tier."""

        async def _boom(text: str, target: str, org_id: int | None = None) -> list:
            raise KeyError("entity_type")

        monkeypatch.setattr(filter_instance, "_run_ner_patterns", _boom)

        result = await filter_instance.filter_input("some text")

        assert result.allowed is False
        assert result.action == "block"

    @pytest.mark.asyncio
    async def test_type_error_in_llm_auditor_call_fails_closed_not_rule_based(
        self, filter_instance: ContentFilter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A defect in the auditor call path overrides the rule-based action with block."""
        # Force the auditor to be invoked: a single log-only violation.
        from shared.security.content_filter import FilterViolation

        async def _log_only_violation(text: str, target: str, org_id: int | None = None) -> list:
            return [
                FilterViolation(
                    rule_name="custom_log_rule",
                    rule_type="custom_string",
                    matched_text="x",
                    action="log",
                    confidence=0.5,
                )
            ]

        async def _boom(*args: object, **kwargs: object) -> tuple[bool, str]:
            raise TypeError("bad message-builder call")

        monkeypatch.setattr(filter_instance, "_run_builtin_patterns", _log_only_violation)
        monkeypatch.setattr(filter_instance, "_invoke_llm_auditor", _boom)
        before = _counter_value("input", "fail_closed")

        result = await filter_instance.filter_input("some text")

        # A code defect in the auditor tier must override the (otherwise
        # log-only) rule-based action with block -- never silently fall
        # back to whatever the pattern tiers alone decided.
        assert result.action == "block"
        assert result.allowed is False
        assert _counter_value("input", "fail_closed") == before + 1


class TestOperationalErrorsFailOpen:
    """Genuine operational failures keep the existing, deliberate fail-open behaviour."""

    @pytest.mark.asyncio
    async def test_connection_error_fails_open(
        self, filter_instance: ContentFilter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ConnectionError (DB/network unreachable) still allows content through."""

        async def _boom(text: str, target: str, org_id: int | None = None) -> list:
            raise ConnectionError("db unreachable")

        monkeypatch.setattr(filter_instance, "_run_builtin_patterns", _boom)
        before = _counter_value("input", "fail_open")

        result = await filter_instance.filter_input("some text")

        assert result.allowed is True
        assert result.action == "allow"
        assert _counter_value("input", "fail_open") == before + 1

    @pytest.mark.asyncio
    async def test_llm_auditor_timeout_still_uses_rule_based_decision(
        self, filter_instance: ContentFilter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An auditor timeout (existing, expected operational path) is unaffected by the split."""

        async def _timeout(*args: object, **kwargs: object) -> tuple[bool, str]:
            return False, "auditor timeout"

        from shared.security.content_filter import FilterViolation

        async def _log_only_violation(text: str, target: str, org_id: int | None = None) -> list:
            return [
                FilterViolation(
                    rule_name="custom_log_rule",
                    rule_type="custom_string",
                    matched_text="x",
                    action="log",
                    confidence=0.5,
                )
            ]

        monkeypatch.setattr(filter_instance, "_run_builtin_patterns", _log_only_violation)
        monkeypatch.setattr(filter_instance, "_invoke_llm_auditor", _timeout)

        result = await filter_instance.filter_input("some text")

        # Auditor merely timed out (returned its own "no block" tuple, did
        # not raise) -- rule-based action (log-only -> allowed) stands.
        assert result.allowed is True
        assert result.action == "log"


class TestKeyboardInterruptNotSwallowed:
    """KeyboardInterrupt/SystemExit must never be caught by the fail-open/fail-closed split."""

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_propagates(
        self, filter_instance: ContentFilter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A KeyboardInterrupt raised mid-pipeline is never converted into a FilterResult."""

        async def _boom(text: str, target: str, org_id: int | None = None) -> list:
            raise KeyboardInterrupt

        monkeypatch.setattr(filter_instance, "_run_builtin_patterns", _boom)

        with pytest.raises(KeyboardInterrupt):
            await filter_instance.filter_input("some text")


class TestAuditorOverridesRuleBasedAction:
    """A blocking auditor verdict escalates an otherwise non-block rule-based action."""

    @pytest.mark.asyncio
    async def test_auditor_should_block_true_escalates_log_only_action_to_block(
        self, filter_instance: ContentFilter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A log-only rule-based action is overridden to 'block' when the auditor says so."""
        from shared.security.content_filter import FilterViolation

        async def _log_only_violation(text: str, target: str, org_id: int | None = None) -> list:
            return [
                FilterViolation(
                    rule_name="custom_log_rule",
                    rule_type="custom_string",
                    matched_text="x",
                    action="log",
                    confidence=0.5,
                )
            ]

        async def _blocking_auditor(*args: object, **kwargs: object) -> tuple[bool, str]:
            return True, "BLOCK - contains a credential"

        monkeypatch.setattr(filter_instance, "_run_builtin_patterns", _log_only_violation)
        monkeypatch.setattr(filter_instance, "_invoke_llm_auditor", _blocking_auditor)

        result = await filter_instance.filter_input("some text")

        assert result.action == "block"
        assert result.allowed is False
        assert result.auditor_used is True


class TestAuditorUnclassifiedExceptionFailsOpen:
    """An auditor exception outside the classified programming-defect list fails open."""

    @pytest.mark.asyncio
    async def test_runtime_error_from_auditor_keeps_rule_based_action(
        self, filter_instance: ContentFilter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A RuntimeError (not a classified defect type) logs and falls open; action unchanged."""
        from shared.security.content_filter import FilterViolation

        async def _log_only_violation(text: str, target: str, org_id: int | None = None) -> list:
            return [
                FilterViolation(
                    rule_name="custom_log_rule",
                    rule_type="custom_string",
                    matched_text="x",
                    action="log",
                    confidence=0.5,
                )
            ]

        async def _boom(*args: object, **kwargs: object) -> tuple[bool, str]:
            raise RuntimeError("unexpected auditor failure")

        monkeypatch.setattr(filter_instance, "_run_builtin_patterns", _log_only_violation)
        monkeypatch.setattr(filter_instance, "_invoke_llm_auditor", _boom)
        before = _counter_value("input", "fail_open")

        result = await filter_instance.filter_input("some text")

        # The rule-based action (log-only) stands -- a defect in the auditor
        # call itself must not silently turn into a block, distinct from
        # the classified-defect case above which deliberately fails closed.
        assert result.action == "log"
        assert result.allowed is True
        assert result.auditor_used is False
        assert _counter_value("input", "fail_open") == before + 1


class TestNerTierDisabledSkipsTier3:
    """When the NER tier gate is closed, `_filter()` never calls `_run_ner_patterns`."""

    @pytest.mark.asyncio
    async def test_unlicensed_filter_never_invokes_ner_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A filter with no license_client (NER tier off) skips `_run_ner_patterns` entirely."""
        monkeypatch.setenv(
            "WADDLEAI_STUB_UPSTREAM", "1"
        )  # skip real NER model init; irrelevant here
        cf = ContentFilter(db=None)  # no license_client -> _ner_tier_enabled() is False

        async def _boom(text: str, target: str, org_id: int | None = None) -> list:
            raise AssertionError(
                "_run_ner_patterns must not be called when the NER tier is disabled"
            )

        monkeypatch.setattr(cf, "_run_ner_patterns", _boom)

        result = await cf.filter_input("plain text, no PII")

        assert result.action == "allow"
        assert result.ner_backend == "none"


class TestLogFilterEventNeverOverridesDecision:
    """`_log_filter_event`'s own failures must never change the already-finalized result."""

    def test_broken_audit_insert_does_not_raise(
        self,
        filter_instance: ContentFilter,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A TypeError from a broken audit-log insert is swallowed and logged loudly, not raised."""
        from shared.security.content_filter import FilterResult

        class _BrokenAuditLog:
            def insert(self, **kwargs: object) -> None:
                raise TypeError("unexpected keyword argument 'ip_address'")

        class _BrokenDB:
            content_filter_audit_log = _BrokenAuditLog()

        filter_instance.db = _BrokenDB()
        result = FilterResult(
            allowed=False,
            action="block",
            violations=[],
            filtered_text="text",
            auditor_used=False,
        )

        with caplog.at_level(logging.ERROR):
            filter_instance._log_filter_event(phase="input", result=result, user_id=1, org_id=1)

        assert "code defect" in caplog.text
        assert "audit trail is silently not being written" in caplog.text
