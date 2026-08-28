"""Tests for `ContentFilter._run_custom_rules` and `_load_custom_rules`.

Covers the per-rule-type application logic (custom_string / custom_regex /
target mismatch / invalid regex) and the DB-backed rule loader's TTL cache,
query, and fail-open error path -- all against a real sqlite-backed
penguin_dal DAL (see `conftest.content_filter_db`), so a regression back to
the PyDAL-style `db(q1)(q2)` chaining bug documented in `_load_custom_rules`
would raise `TypeError` here exactly as it does in production, rather than
being invisible behind a spec-less mock.
"""

from __future__ import annotations

import time

import pytest
from penguin_dal import DAL

from shared.security.content_filter import ContentFilter, FilterRule


@pytest.fixture
def filter_instance(content_filter_db: DAL) -> ContentFilter:
    """A content filter backed by the real sqlite content-filter tables."""
    return ContentFilter(db=content_filter_db)


class TestRunCustomRulesPerType:
    """`_run_custom_rules` applies rule_type-specific matching logic."""

    async def test_custom_string_no_match_continues_to_next_rule(
        self, filter_instance: ContentFilter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-matching custom_string rule is skipped; a later matching rule still fires."""

        async def _rules(_org_id: int | None) -> list[FilterRule]:
            return [
                FilterRule(
                    id=10,
                    name="absent-word",
                    description="",
                    rule_type="custom_string",
                    target="both",
                    pattern="not-in-the-text",
                    action="block",
                    redact_with="[REDACTED]",
                    enabled=True,
                    organization_id=None,
                ),
                FilterRule(
                    id=11,
                    name="present-word",
                    description="",
                    rule_type="custom_string",
                    target="both",
                    pattern="foo",
                    action="log",
                    redact_with="[REDACTED]",
                    enabled=True,
                    organization_id=None,
                ),
            ]

        monkeypatch.setattr(filter_instance, "_load_custom_rules", _rules)

        violations = await filter_instance._run_custom_rules(
            "this text has foo in it", "input", None
        )

        assert [v.rule_name for v in violations] == ["present-word"]

    async def test_custom_regex_no_match_continues_to_next_rule(
        self, filter_instance: ContentFilter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-matching custom_regex rule is skipped; a later matching rule still fires."""

        async def _rules(_org_id: int | None) -> list[FilterRule]:
            return [
                FilterRule(
                    id=12,
                    name="absent-pattern",
                    description="",
                    rule_type="custom_regex",
                    target="both",
                    pattern=r"NOPE-\d{4}",
                    action="block",
                    redact_with="[REDACTED]",
                    enabled=True,
                    organization_id=None,
                ),
                FilterRule(
                    id=13,
                    name="present-pattern",
                    description="",
                    rule_type="custom_regex",
                    target="both",
                    pattern=r"TICKET-\d{4}",
                    action="log",
                    redact_with="[REDACTED]",
                    enabled=True,
                    organization_id=None,
                ),
            ]

        monkeypatch.setattr(filter_instance, "_load_custom_rules", _rules)

        violations = await filter_instance._run_custom_rules(
            "see TICKET-9999 please", "input", None
        )

        assert [v.rule_name for v in violations] == ["present-pattern"]

    async def test_unrecognized_rule_type_is_silently_skipped(
        self, filter_instance: ContentFilter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rule row whose rule_type matches neither known branch produces no violation.

        A `content_filter_rules` row's `rule_type` is a free-text DB column
        (e.g. a stale/misconfigured 'builtin_pii' value written directly
        rather than through the two supported custom types) -- the loop
        must not error out on it, and a later valid rule must still run.
        """

        async def _rules(_org_id: int | None) -> list[FilterRule]:
            return [
                FilterRule(
                    id=20,
                    name="misconfigured-rule",
                    description="",
                    rule_type="builtin_pii",  # neither custom_string nor custom_regex
                    target="both",
                    pattern="foo",
                    action="block",
                    redact_with="[REDACTED]",
                    enabled=True,
                    organization_id=None,
                ),
                FilterRule(
                    id=21,
                    name="valid-rule",
                    description="",
                    rule_type="custom_string",
                    target="both",
                    pattern="bar",
                    action="log",
                    redact_with="[REDACTED]",
                    enabled=True,
                    organization_id=None,
                ),
            ]

        monkeypatch.setattr(filter_instance, "_load_custom_rules", _rules)

        violations = await filter_instance._run_custom_rules(
            "this text has foo and bar", "input", None
        )

        assert [v.rule_name for v in violations] == ["valid-rule"]

    async def test_target_mismatch_is_skipped(
        self, filter_instance: ContentFilter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rule scoped to 'output' produces no violation when target is 'input'."""

        async def _rules(_org_id: int | None) -> list[FilterRule]:
            return [
                FilterRule(
                    id=1,
                    name="output-only",
                    description="",
                    rule_type="custom_string",
                    target="output",
                    pattern="secret",
                    action="block",
                    redact_with="[REDACTED]",
                    enabled=True,
                    organization_id=None,
                )
            ]

        monkeypatch.setattr(filter_instance, "_load_custom_rules", _rules)

        violations = await filter_instance._run_custom_rules("this has secret in it", "input", None)

        assert violations == []

    async def test_custom_string_match_is_case_insensitive_and_captures_full_text(
        self, filter_instance: ContentFilter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A custom_string rule matches case-insensitively and preserves the exact substring."""

        async def _rules(_org_id: int | None) -> list[FilterRule]:
            return [
                FilterRule(
                    id=2,
                    name="banned-word",
                    description="",
                    rule_type="custom_string",
                    target="both",
                    pattern="ProjectPhoenix",
                    action="redact",
                    redact_with="[REDACTED]",
                    enabled=True,
                    organization_id=None,
                )
            ]

        monkeypatch.setattr(filter_instance, "_load_custom_rules", _rules)

        violations = await filter_instance._run_custom_rules(
            "the codename is projectphoenix, keep it quiet", "input", None
        )

        assert len(violations) == 1
        v = violations[0]
        assert v.rule_name == "banned-word"
        assert v.rule_type == "custom_string"
        assert v.action == "redact"
        assert v.confidence == 0.95
        assert v.full_matched_text == "projectphoenix"  # exact case as it appeared in text

    async def test_custom_regex_match_produces_violation_with_full_text(
        self, filter_instance: ContentFilter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A custom_regex rule matches and records the full match for redaction."""

        async def _rules(_org_id: int | None) -> list[FilterRule]:
            return [
                FilterRule(
                    id=3,
                    name="internal-ticket-id",
                    description="",
                    rule_type="custom_regex",
                    target="both",
                    pattern=r"TICKET-\d{4}",
                    action="log",
                    redact_with="[REDACTED]",
                    enabled=True,
                    organization_id=None,
                )
            ]

        monkeypatch.setattr(filter_instance, "_load_custom_rules", _rules)

        violations = await filter_instance._run_custom_rules(
            "see TICKET-1234 for details", "output", None
        )

        assert len(violations) == 1
        v = violations[0]
        assert v.rule_name == "internal-ticket-id"
        assert v.rule_type == "custom_regex"
        assert v.confidence == 0.90
        assert v.full_matched_text == "TICKET-1234"

    async def test_invalid_regex_is_skipped_with_a_warning_not_raised(
        self,
        filter_instance: ContentFilter,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A malformed regex pattern in a custom rule logs a warning and yields no violation."""

        async def _rules(_org_id: int | None) -> list[FilterRule]:
            return [
                FilterRule(
                    id=4,
                    name="broken-pattern",
                    description="",
                    rule_type="custom_regex",
                    target="both",
                    pattern="(unclosed[",
                    action="block",
                    redact_with="[REDACTED]",
                    enabled=True,
                    organization_id=None,
                )
            ]

        monkeypatch.setattr(filter_instance, "_load_custom_rules", _rules)

        import logging

        with caplog.at_level(logging.WARNING):
            violations = await filter_instance._run_custom_rules("anything", "input", None)

        assert violations == []
        assert "Invalid regex" in caplog.text
        assert "broken-pattern" in caplog.text


class TestLoadCustomRulesCacheAndQuery:
    """`_load_custom_rules` TTL cache, DB query, and fail-open behaviour."""

    async def test_global_and_org_scoped_rules_are_both_returned(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """An org-scoped call returns both its own rules and global (org_id=None) rules."""
        content_filter_db.content_filter_rules.insert(
            name="global-rule",
            description="",
            rule_type="custom_string",
            target="both",
            pattern="foo",
            action="log",
            redact_with="[REDACTED]",
            enabled=True,
            organization_id=None,
        )
        content_filter_db.content_filter_rules.insert(
            name="org-rule",
            description="",
            rule_type="custom_string",
            target="both",
            pattern="bar",
            action="log",
            redact_with="[REDACTED]",
            enabled=True,
            organization_id=42,
        )
        content_filter_db.content_filter_rules.insert(
            name="other-org-rule",
            description="",
            rule_type="custom_string",
            target="both",
            pattern="baz",
            action="log",
            redact_with="[REDACTED]",
            enabled=True,
            organization_id=99,
        )

        rules = await filter_instance._load_custom_rules(org_id=42)

        names = {r.name for r in rules}
        assert names == {"global-rule", "org-rule"}

    async def test_global_only_scope_excludes_all_org_rules(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """A global call (org_id=None) never returns org-scoped rules."""
        content_filter_db.content_filter_rules.insert(
            name="global-rule",
            description="",
            rule_type="custom_string",
            target="both",
            pattern="foo",
            action="log",
            redact_with="[REDACTED]",
            enabled=True,
            organization_id=None,
        )
        content_filter_db.content_filter_rules.insert(
            name="org-rule",
            description="",
            rule_type="custom_string",
            target="both",
            pattern="bar",
            action="log",
            redact_with="[REDACTED]",
            enabled=True,
            organization_id=7,
        )

        rules = await filter_instance._load_custom_rules(org_id=None)

        assert [r.name for r in rules] == ["global-rule"]

    async def test_disabled_rules_are_never_returned(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """A disabled rule row is filtered out by the enabled == True query condition."""
        content_filter_db.content_filter_rules.insert(
            name="disabled-rule",
            description="",
            rule_type="custom_string",
            target="both",
            pattern="foo",
            action="block",
            redact_with="[REDACTED]",
            enabled=False,
            organization_id=None,
        )

        rules = await filter_instance._load_custom_rules(org_id=None)

        assert rules == []

    async def test_repeated_call_within_ttl_hits_cache_not_db(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """A second call inside the TTL window returns the cached list without re-querying."""
        content_filter_db.content_filter_rules.insert(
            name="cached-rule",
            description="",
            rule_type="custom_string",
            target="both",
            pattern="foo",
            action="log",
            redact_with="[REDACTED]",
            enabled=True,
            organization_id=None,
        )

        first = await filter_instance._load_custom_rules(org_id=None)
        assert [r.name for r in first] == ["cached-rule"]

        # Insert a second row directly -- if the cache is bypassed, the next
        # call would see it; it must not, within the TTL window.
        content_filter_db.content_filter_rules.insert(
            name="not-yet-visible",
            description="",
            rule_type="custom_string",
            target="both",
            pattern="bar",
            action="log",
            redact_with="[REDACTED]",
            enabled=True,
            organization_id=None,
        )

        second = await filter_instance._load_custom_rules(org_id=None)
        assert [r.name for r in second] == ["cached-rule"]

    async def test_cache_expiry_after_ttl_re_queries_the_db(
        self,
        filter_instance: ContentFilter,
        content_filter_db: DAL,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Once the TTL window elapses, the next call re-queries and picks up new rows."""
        content_filter_db.content_filter_rules.insert(
            name="first-rule",
            description="",
            rule_type="custom_string",
            target="both",
            pattern="foo",
            action="log",
            redact_with="[REDACTED]",
            enabled=True,
            organization_id=None,
        )
        first = await filter_instance._load_custom_rules(org_id=None)
        assert [r.name for r in first] == ["first-rule"]

        content_filter_db.content_filter_rules.insert(
            name="second-rule",
            description="",
            rule_type="custom_string",
            target="both",
            pattern="bar",
            action="log",
            redact_with="[REDACTED]",
            enabled=True,
            organization_id=None,
        )

        # Advance the monotonic clock past the TTL instead of sleeping.
        real_monotonic = time.monotonic
        monkeypatch.setattr(
            time, "monotonic", lambda: real_monotonic() + filter_instance.rule_cache_ttl + 1
        )

        second = await filter_instance._load_custom_rules(org_id=None)
        assert {r.name for r in second} == {"first-rule", "second-rule"}

    async def test_db_error_fails_open_to_empty_list(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A DB failure while loading custom rules degrades to no rules, not a raised exception."""
        import logging

        class _BrokenTable:
            @property
            def enabled(self) -> None:
                raise RuntimeError("db down")

        class _BrokenDB:
            content_filter_rules = _BrokenTable()

        cf = ContentFilter(db=_BrokenDB())

        with caplog.at_level(logging.ERROR):
            rules = await cf._load_custom_rules(org_id=None)

        assert rules == []
        assert "Failed to load custom rules" in caplog.text
