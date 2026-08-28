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

    def test_timestamp_defaults_rather_than_being_passed_explicitly(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """No `timestamp=` kwarg is passed -- the column's own default populates it.

        Regression: this method previously passed `timestamp=time.time()`
        (a float epoch) against a `datetime` column, which penguin-dal
        rejects -- every insert silently failed under the method's own
        broad exception handler. Omitting the kwarg and asserting the row
        was actually written (with a non-null timestamp) is the regression
        guard.
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
