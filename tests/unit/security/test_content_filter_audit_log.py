"""Tests for `ContentFilter._log_filter_event`'s audit-log write path.

Runs against the real sqlite-backed `content_filter_audit_log` table (see
`conftest.content_filter_db`) and asserts the actually-persisted row, not
just that `.insert()` was called -- an insert-call assertion on a spec-less
mock would not catch a schema-drifted kwarg (this method's own source
comment documents exactly that regression: a `timestamp=time.time()` float
silently failed every insert under real penguin_dal).
"""

from __future__ import annotations

import json
import logging

import pytest
from penguin_dal import DAL

from shared.security.content_filter import ContentFilter, FilterResult, FilterViolation


@pytest.fixture
def filter_instance(content_filter_db: DAL) -> ContentFilter:
    """A content filter backed by the real sqlite content-filter tables."""
    return ContentFilter(db=content_filter_db)


class TestAuditLogWritePath:
    """A finalized `FilterResult` is persisted to `content_filter_audit_log`."""

    def test_block_result_is_persisted_with_expected_fields(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """A block decision writes a row with the violations JSON and text sample intact."""
        result = FilterResult(
            allowed=False,
            action="block",
            violations=[
                FilterViolation(
                    rule_name="ssn",
                    rule_type="builtin_pii",
                    matched_text="123-45-6789",
                    action="block",
                    confidence=0.95,
                )
            ],
            filtered_text="my ssn is 123-45-6789",
            auditor_used=True,
        )

        filter_instance._log_filter_event(
            phase="input", result=result, user_id=42, org_id=7, ip="10.0.0.1"
        )

        row = (
            content_filter_db(content_filter_db.content_filter_audit_log.user_id == 42)
            .select()
            .first()
        )
        assert row is not None
        assert row.phase == "input"
        assert row.organization_id == 7
        assert row.ip_address == "10.0.0.1"
        assert row.action_taken == "block"
        assert row.auditor_used is True
        assert row.text_sample == "my ssn is 123-45-6789"
        violations = json.loads(row.violations_json)
        assert violations == [
            {"rule_name": "ssn", "rule_type": "builtin_pii", "action": "block", "confidence": 0.95}
        ]

    def test_text_sample_is_truncated_to_200_chars(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """`filtered_text` longer than 200 chars is truncated before being persisted."""
        long_text = "x" * 500
        result = FilterResult(
            allowed=True, action="allow", violations=[], filtered_text=long_text, auditor_used=False
        )

        filter_instance._log_filter_event(
            phase="output", result=result, user_id=1, org_id=None, ip=None
        )

        row = (
            content_filter_db(content_filter_db.content_filter_audit_log.user_id == 1)
            .select()
            .first()
        )
        assert row is not None
        assert len(row.text_sample) == 200

    def test_timestamp_is_populated(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """`timestamp` is populated on every insert.

        Regression history: this method previously passed
        `timestamp=time.time()` (a float epoch) against a `datetime`
        column, which penguin-dal rejects -- every insert silently failed
        under the method's own broad exception handler. That was "fixed" by
        omitting the kwarg entirely and relying on the column's own
        default -- which only ever worked against a self-migrated sqlite
        schema (this fixture) and never against the real, reflected
        Postgres table in production (gh-207 defects 2/3): `get_db()`
        reflects the table before `define_tables()` runs, so the
        Python-side default declared in `shared/database/models.py` was
        never actually registered on it. `_log_filter_event` now passes
        `timestamp=` explicitly (backed by a `server_default` in migration
        `020_token_usage_api_key_id` for whichever layer inserts the row).
        """
        result = FilterResult(
            allowed=True, action="allow", violations=[], filtered_text="hi", auditor_used=False
        )

        filter_instance._log_filter_event(
            phase="input", result=result, user_id=2, org_id=None, ip=None
        )

        row = (
            content_filter_db(content_filter_db.content_filter_audit_log.user_id == 2)
            .select()
            .first()
        )
        assert row is not None
        assert row.timestamp is not None

    def test_degraded_is_set_explicitly(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """`degraded` is populated on every insert, mirroring `FilterResult.degraded`.

        # regression: gh-207 -- `content_filter_audit_log.degraded` was
        # NOT NULL with no default at all and no insert ever set it,
        # a second, independent NOT NULL violation stacked on the
        # `timestamp` bug above.
        """
        allow_result = FilterResult(
            allowed=True, action="allow", violations=[], filtered_text="hi", auditor_used=False
        )
        filter_instance._log_filter_event(
            phase="input", result=allow_result, user_id=5, org_id=None, ip=None
        )
        allow_row = (
            content_filter_db(content_filter_db.content_filter_audit_log.user_id == 5)
            .select()
            .first()
        )
        assert allow_row is not None
        assert allow_row.degraded is False

        degraded_result = FilterResult(
            allowed=True,
            action="allow",
            violations=[],
            filtered_text="hi",
            auditor_used=False,
            degraded=True,
        )
        filter_instance._log_filter_event(
            phase="input", result=degraded_result, user_id=6, org_id=None, ip=None
        )
        degraded_row = (
            content_filter_db(content_filter_db.content_filter_audit_log.user_id == 6)
            .select()
            .first()
        )
        assert degraded_row is not None
        assert degraded_row.degraded is True

    def test_redact_action_logs_at_info_not_warning(
        self, filter_instance: ContentFilter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A redact decision is logged at INFO, distinct from BLOCK's WARNING."""
        result = FilterResult(
            allowed=True,
            action="redact",
            violations=[],
            filtered_text="hi [REDACTED]",
            auditor_used=False,
        )

        with caplog.at_level(logging.INFO):
            filter_instance._log_filter_event(
                phase="input", result=result, user_id=3, org_id=None, ip=None
            )

        assert "Content filter REDACT" in caplog.text
        assert "Content filter BLOCK" not in caplog.text

    def test_allow_action_emits_no_block_or_redact_log_line(
        self, filter_instance: ContentFilter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An allow decision writes the audit row but emits neither the BLOCK nor REDACT log."""
        result = FilterResult(
            allowed=True, action="allow", violations=[], filtered_text="hi", auditor_used=False
        )

        with caplog.at_level(logging.INFO):
            filter_instance._log_filter_event(
                phase="input", result=result, user_id=4, org_id=None, ip=None
            )

        assert "Content filter BLOCK" not in caplog.text
        assert "Content filter REDACT" not in caplog.text


class TestDegradedAuditorEndToEndAuditTrail:
    """A degraded auditor call reaches the real audit-log row via the full `_filter()` path.

    Distinct from `test_degraded_is_set_explicitly` above (which calls
    `_log_filter_event` directly with a hand-built `FilterResult`): this
    proves `_filter()` itself actually sets `degraded=True` when
    `_invoke_llm_auditor` returns a degraded `AuditorResult`, not just that
    the write path persists the flag once set -- the end-to-end path the
    silent-fail-open finding broke.
    """

    @pytest.mark.asyncio
    async def test_degraded_auditor_call_persists_degraded_true(
        self,
        filter_instance: ContentFilter,
        content_filter_db: DAL,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A dead-endpoint auditor call writes `degraded=True` to the real audit-log row."""
        from shared.security.content_filter import AuditorResult

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

        async def _degraded(*args: object, **kwargs: object) -> AuditorResult:
            return AuditorResult(
                should_block=False,
                reason="auditor unavailable",
                degraded=True,
                error_class="ConnectionError",
            )

        monkeypatch.setattr(filter_instance, "_run_builtin_patterns", _log_only_violation)
        monkeypatch.setattr(filter_instance, "_invoke_llm_auditor", _degraded)

        result = await filter_instance.filter_input("some text", user_id=99, org_id=None)

        assert result.degraded is True
        row = (
            content_filter_db(content_filter_db.content_filter_audit_log.user_id == 99)
            .select()
            .first()
        )
        assert row is not None
        assert row.degraded is True
        assert row.auditor_used is True


class TestAuditLogInsertFailureClassification:
    """`_log_filter_event`'s own failures never raise -- classified and swallowed locally."""

    def test_unclassified_exception_is_logged_generically_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A ValueError (not one of the programming-defect types) hits the generic except branch."""

        class _WeirdInsertError(ValueError):
            pass

        class _BrokenAuditLog:
            def insert(self, **kwargs: object) -> None:
                raise _WeirdInsertError("unexpected DB constraint")

        class _BrokenDB:
            content_filter_audit_log = _BrokenAuditLog()

        cf = ContentFilter(db=_BrokenDB())
        result = FilterResult(
            allowed=True, action="allow", violations=[], filtered_text="hi", auditor_used=False
        )

        with caplog.at_level(logging.ERROR):
            cf._log_filter_event(phase="input", result=result, user_id=1, org_id=None, ip=None)

        assert "Failed to log filter event" in caplog.text
        # This is the generic branch -- must not be mistaken for the
        # classified "code defect" branch (see test_content_filter_fail_mode.py).
        assert "code defect" not in caplog.text
