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

import json
import logging

import pytest

from shared.security.content_filter import (
    ContentFilter,
    FilterViolation,
    _content_filter_fail_total,
)


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
        """An auditor timeout (existing, expected operational path) is unaffected by the split.

        Regression (silent-fail-open finding): a timed-out/unreachable
        auditor must never be indistinguishable from a real ALLOW verdict
        -- `result.degraded` and the fail-mode counter must both reflect it,
        even though (with the default fail_mode="open") the rule-based
        action itself is unchanged.
        """
        from shared.security.content_filter import AuditorResult, FilterViolation

        async def _timeout(*args: object, **kwargs: object) -> AuditorResult:
            return AuditorResult(
                should_block=False,
                reason="auditor timeout",
                degraded=True,
                error_class="TimeoutError",
            )

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
        before = _counter_value("input", "fail_open")

        result = await filter_instance.filter_input("some text")

        # Auditor merely timed out (returned a degraded AuditorResult, did
        # not raise) -- with fail_mode="open" the rule-based action
        # (log-only -> allowed) stands, but the degradation is now visible.
        assert result.allowed is True
        assert result.degraded is True
        assert _counter_value("input", "fail_open") == before + 1
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

        from shared.security.content_filter import AuditorResult

        async def _blocking_auditor(*args: object, **kwargs: object) -> AuditorResult:
            return AuditorResult(should_block=True, reason="BLOCK - contains a credential")

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


class _CapturingAuditTable:
    """Records content_filter_audit_log.insert() kwargs."""

    def __init__(self, rows: list) -> None:
        self.rows = rows

    def insert(self, **kwargs: object) -> int:
        self.rows.append(kwargs)
        return len(self.rows)


class _CapturingAuditDB:
    """Minimal db exposing only content_filter_audit_log for audit-trail assertions."""

    def __init__(self) -> None:
        self.rows: list = []
        self.content_filter_audit_log = _CapturingAuditTable(self.rows)


_SSN = "123-45-6789"  # noqa: S105 -- test SSN fixture, not a credential


class TestFailModeAuditTrail:
    """release-audit-2026-09-23: both fail exits must audit-log PII types, never values."""

    @pytest.mark.asyncio
    # regression: release-audit-2026-09-23
    async def test_fail_open_writes_audit_row_with_types_not_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operational failure (fail-open) records the PII type, never the raw text."""
        db = _CapturingAuditDB()
        cf = ContentFilter(db=db, license_client=_LicensedForNER())

        async def _one_violation(text: str, target: str, org_id: int | None = None) -> list:
            return [
                FilterViolation(
                    rule_name="ssn",
                    rule_type="builtin_pii",
                    matched_text=_SSN,
                    action="redact",
                    confidence=0.99,
                    full_matched_text=_SSN,
                )
            ]

        async def _boom(text: str, target: str, org_id: int | None = None) -> list:
            raise RuntimeError("DB unreachable mid-filter")

        monkeypatch.setattr(cf, "_run_builtin_patterns", _one_violation)
        monkeypatch.setattr(cf, "_run_custom_rules", _boom)

        result = await cf.filter_input(f"my ssn is {_SSN}", user_id=7, org_id=3)

        # Fail-open behaviour preserved (availability choice) ...
        assert result.allowed is True
        assert result.action == "allow"
        # ... but now audit-logged.
        assert len(db.rows) == 1
        row = db.rows[0]
        assert row["action_taken"] == "fail_open"
        assert row["degraded"] is True
        assert row["text_sample"] == ""  # never persist raw text on an error path
        blob = json.dumps(row, default=str)
        assert _SSN not in blob  # the raw value never reaches the audit trail
        assert "builtin_pii" in row["violations_json"]  # the TYPE is recorded

    @pytest.mark.asyncio
    # regression: release-audit-2026-09-23
    async def test_fail_closed_writes_audit_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A programming defect (fail-closed) is also audit-logged."""
        db = _CapturingAuditDB()
        cf = ContentFilter(db=db, license_client=_LicensedForNER())

        async def _boom(text: str, target: str, org_id: int | None = None) -> list:
            raise TypeError("bad call signature")

        monkeypatch.setattr(cf, "_run_builtin_patterns", _boom)

        result = await cf.filter_input(f"my ssn is {_SSN}", user_id=7, org_id=3)

        assert result.allowed is False
        assert result.action == "block"
        assert len(db.rows) == 1
        assert db.rows[0]["action_taken"] == "fail_closed"
        assert db.rows[0]["text_sample"] == ""
        assert _SSN not in json.dumps(db.rows[0], default=str)


def _log_only_violation_factory() -> object:
    """Build a `_run_builtin_patterns` replacement returning one log-only violation.

    Shared by the fail-mode-policy tests below: a log-only violation is what
    makes `_should_invoke_auditor` return True without otherwise forcing a
    block/redact decision, isolating the auditor's own contribution to the
    final action.
    """

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

    return _log_only_violation


class _KillSwitchFeatures:
    """Minimal `features` stub for `_auditor_fail_mode_policy_disabled`."""

    def __init__(self, disabled: bool) -> None:
        self.disabled = disabled
        self.calls: list[str] = []

    def is_feature_enabled(self, flag_key: str, distinct_id: str = "server") -> bool:
        self.calls.append(flag_key)
        return self.disabled


class _PerOrgKillSwitchFeatures:
    """Flag stub whose answer depends on `distinct_id`, modeling an org-targeted PostHog flag."""

    def __init__(self, disabled_distinct_ids: set[str]) -> None:
        self.disabled_distinct_ids = disabled_distinct_ids
        self.calls: list[tuple[str, str]] = []

    def is_feature_enabled(self, flag_key: str, distinct_id: str = "server") -> bool:
        self.calls.append((flag_key, distinct_id))
        return distinct_id in self.disabled_distinct_ids


class TestAuditorFailModePolicy:
    """`SECURITY_AUDITOR_FAIL_MODE` (`ContentFilter.auditor_fail_mode`) governs degraded calls.

    Regression (silent-fail-open finding): a degraded auditor call used to
    be indistinguishable from a real ALLOW in both logs and metrics. These
    tests cover the full matrix this fix introduces: `fail_mode="open"`
    (preserves the historical allow-through behaviour, but now visibly),
    `fail_mode="closed"` (blocks instead), an invalid configured value
    (defaults to "open" with a warning), and the opt-out kill switch
    (reverts to the pre-fix silent behaviour).
    """

    @pytest.mark.asyncio
    async def test_fail_mode_closed_blocks_on_degraded_auditor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`auditor_fail_mode="closed"` blocks when the auditor is degraded."""
        from shared.security.content_filter import AuditorResult

        async def _degraded(*args: object, **kwargs: object) -> AuditorResult:
            return AuditorResult(
                should_block=False,
                reason="auditor unavailable",
                degraded=True,
                error_class="ConnectionError",
            )

        cf = ContentFilter(db=None, license_client=_LicensedForNER(), auditor_fail_mode="closed")
        monkeypatch.setattr(cf, "_run_builtin_patterns", _log_only_violation_factory())
        monkeypatch.setattr(cf, "_invoke_llm_auditor", _degraded)
        before = _counter_value("input", "fail_closed")

        result = await cf.filter_input("some text")

        assert result.allowed is False
        assert result.action == "block"
        assert result.degraded is True
        assert _counter_value("input", "fail_closed") == before + 1

    @pytest.mark.asyncio
    async def test_fail_mode_open_keeps_rule_based_action_on_degraded_auditor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`auditor_fail_mode="open"` (the default) keeps the rule-based action, but visibly."""
        from shared.security.content_filter import AuditorResult

        async def _degraded(*args: object, **kwargs: object) -> AuditorResult:
            return AuditorResult(
                should_block=False,
                reason="auditor unavailable",
                degraded=True,
                error_class="ConnectionError",
            )

        cf = ContentFilter(db=None, license_client=_LicensedForNER(), auditor_fail_mode="open")
        monkeypatch.setattr(cf, "_run_builtin_patterns", _log_only_violation_factory())
        monkeypatch.setattr(cf, "_invoke_llm_auditor", _degraded)
        before = _counter_value("input", "fail_open")

        result = await cf.filter_input("some text")

        # log-only rule-based action -> allowed, but now traceable as degraded.
        assert result.allowed is True
        assert result.action == "log"
        assert result.degraded is True
        assert _counter_value("input", "fail_open") == before + 1

    def test_invalid_fail_mode_defaults_to_open_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unrecognised `auditor_fail_mode` value falls back to 'open', logged loudly."""
        with caplog.at_level(logging.WARNING):
            cf = ContentFilter(db=None, auditor_fail_mode="bogus")

        assert cf.auditor_fail_mode == "open"
        assert "Invalid auditor_fail_mode" in caplog.text

    def test_fail_mode_value_is_normalized_case_and_whitespace(self) -> None:
        """`" CLOSED "` normalizes to `"closed"` rather than falling back to the default."""
        cf = ContentFilter(db=None, auditor_fail_mode=" CLOSED ")
        assert cf.auditor_fail_mode == "closed"

    @pytest.mark.asyncio
    async def test_degraded_warn_log_is_rate_limited_per_org_and_phase(
        self, filter_instance: ContentFilter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A second degraded call for the same (org, phase) in the same window logs nothing more.

        The Prometheus counter and `FilterResult.degraded` are never
        throttled -- only this specific log statement, so an extended
        outage doesn't flood logs at full request volume.
        """
        with caplog.at_level(logging.WARNING):
            filter_instance._warn_auditor_degraded("input", "TimeoutError", org_id=1)
            filter_instance._warn_auditor_degraded("input", "TimeoutError", org_id=1)

        assert caplog.text.count("LLM auditor degraded") == 1

    @pytest.mark.asyncio
    async def test_degraded_warn_rate_limit_does_not_cross_orgs(
        self, filter_instance: ContentFilter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Org B's first degraded WARN is never suppressed by org A's rate limit."""
        with caplog.at_level(logging.WARNING):
            filter_instance._warn_auditor_degraded("input", "TimeoutError", org_id=1)
            filter_instance._warn_auditor_degraded("input", "TimeoutError", org_id=2)

        assert caplog.text.count("LLM auditor degraded") == 2

    @pytest.mark.asyncio
    async def test_kill_switch_reverts_to_legacy_silent_behaviour(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`waddleai.disable-auditor-fail-mode-policy` ON reproduces the pre-fix bug on purpose.

        This is the kill switch's documented escape hatch, not a residual
        bug: with it ON, a degraded auditor call is once again silently
        treated as an ordinary ALLOW -- no counter increment, no
        `FilterResult.degraded`.
        """
        from shared.security.content_filter import AuditorResult

        async def _degraded(*args: object, **kwargs: object) -> AuditorResult:
            return AuditorResult(
                should_block=False,
                reason="auditor unavailable",
                degraded=True,
                error_class="ConnectionError",
            )

        features = _KillSwitchFeatures(disabled=True)
        cf = ContentFilter(db=None, license_client=_LicensedForNER(), features=features)
        monkeypatch.setattr(cf, "_run_builtin_patterns", _log_only_violation_factory())
        monkeypatch.setattr(cf, "_invoke_llm_auditor", _degraded)
        before = _counter_value("input", "fail_open")

        result = await cf.filter_input("some text")

        assert result.allowed is True
        assert result.action == "log"
        assert result.degraded is False
        assert result.auditor_used is True
        assert _counter_value("input", "fail_open") == before
        assert "waddleai.disable-auditor-fail-mode-policy" in features.calls

    @pytest.mark.asyncio
    async def test_kill_switch_off_keeps_new_mechanism_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The kill switch OFF (the default) is a no-op -- the new mechanism stays active."""
        from shared.security.content_filter import AuditorResult

        async def _degraded(*args: object, **kwargs: object) -> AuditorResult:
            return AuditorResult(
                should_block=False,
                reason="auditor timeout",
                degraded=True,
                error_class="TimeoutError",
            )

        features = _KillSwitchFeatures(disabled=False)
        cf = ContentFilter(db=None, license_client=_LicensedForNER(), features=features)
        monkeypatch.setattr(cf, "_run_builtin_patterns", _log_only_violation_factory())
        monkeypatch.setattr(cf, "_invoke_llm_auditor", _degraded)

        result = await cf.filter_input("some text")

        assert result.degraded is True

    @pytest.mark.asyncio
    async def test_kill_switch_check_is_cached_within_the_ttl(self) -> None:
        """A second check for the same org inside the TTL window reuses the cached value."""
        features = _KillSwitchFeatures(disabled=True)
        cf = ContentFilter(db=None, features=features)

        first = await cf._auditor_fail_mode_policy_disabled(org_id=None)
        second = await cf._auditor_fail_mode_policy_disabled(org_id=None)

        assert first is True
        assert second is True
        # Only one real flag check -- the second call hit the TTL cache.
        assert features.calls == ["waddleai.disable-auditor-fail-mode-policy"]

    @pytest.mark.asyncio
    async def test_kill_switch_check_failure_fails_toward_visibility(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A raising flag client keeps the new mechanism ON rather than crashing the filter."""

        class _RaisingFeatures:
            def is_feature_enabled(self, flag_key: str, distinct_id: str = "server") -> bool:
                raise RuntimeError("PostHog unreachable")

        cf = ContentFilter(db=None, features=_RaisingFeatures())

        with caplog.at_level(logging.WARNING):
            disabled = await cf._auditor_fail_mode_policy_disabled(org_id=None)

        assert disabled is False
        assert "kill-switch check failed" in caplog.text

    @pytest.mark.asyncio
    async def test_fail_mode_flag_cache_does_not_bleed_across_orgs(self) -> None:
        """Org A's resolved kill-switch state is never served to org B.

        Regression: `_auditor_fail_mode_flag_cache` was originally a single
        process-wide `(checked_at, disabled)` tuple -- the FIRST org to
        trigger the check cached its answer for every org for
        `_AUDITOR_FAIL_MODE_FLAG_CACHE_TTL` seconds. The flag
        (`waddleai.disable-auditor-fail-mode-policy`) can be org-targeted in
        PostHog, so that was a cross-tenant leak: org A's kill switch being
        ON would silently disable org B's fail-mode-policy telemetry too.
        """
        features = _PerOrgKillSwitchFeatures(disabled_distinct_ids={"1"})
        cf = ContentFilter(db=None, features=features)

        org_a_disabled = await cf._auditor_fail_mode_policy_disabled(org_id=1)
        org_b_disabled = await cf._auditor_fail_mode_policy_disabled(org_id=2)
        # Re-check org A again (within the TTL) -- must still come back True,
        # not be overwritten/shadowed by org B's cache entry.
        org_a_disabled_again = await cf._auditor_fail_mode_policy_disabled(org_id=1)

        assert org_a_disabled is True
        assert org_b_disabled is False
        assert org_a_disabled_again is True
        # One real flag check per org -- org A's second call hit its own
        # cache entry, not org B's.
        assert features.calls == [
            ("waddleai.disable-auditor-fail-mode-policy", "1"),
            ("waddleai.disable-auditor-fail-mode-policy", "2"),
        ]
        # Two independent cache entries, keyed by org_id.
        assert set(cf._auditor_fail_mode_flag_cache.keys()) == {1, 2}
