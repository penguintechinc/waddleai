"""Tests for `ContentFilter`'s auditor prompt/policy loading and message building.

Covers `_load_system_prompt`, `_load_shieldgemma_policy`,
`_build_shieldgemma_messages`, and `_build_granite_guardian_messages`'s
pre-scan context-line rendering, against the real sqlite-backed
`content_filter_config` table (see `conftest.content_filter_db`).

`_load_system_prompt` and `_load_shieldgemma_policy` both read the same
`auditor_system_prompt` config key. `_load_shieldgemma_policy` combines its
filter conditions with `&` (a single `db(query).select().first()` call,
matching real penguin_dal's `QuerySet`, which has no `__call__`).
`_load_system_prompt` previously did `query = db(cond1); query =
query(cond2)` -- chaining a second call directly onto the returned
`QuerySet` -- which always raised `TypeError` (real penguin_dal's
`QuerySet` has no `__call__`), caught by `_load_system_prompt`'s own broad
`except Exception`, so a configured custom system prompt was silently
never applied in production. This was the same regression class already
fixed once in `_load_custom_rules` (see that method's source comment);
`_load_system_prompt` now mirrors that fix -- conditions are combined with
`&` before a single `db()` call.
"""

from __future__ import annotations

import logging

import pytest
from penguin_dal import DAL

from shared.security.content_filter import ContentFilter, FilterViolation


@pytest.fixture
def filter_instance(content_filter_db: DAL) -> ContentFilter:
    """A content filter backed by the real sqlite content-filter tables."""
    return ContentFilter(db=content_filter_db)


class TestLoadSystemPromptNoDatabase:
    """`db=None` skips the DB lookup entirely and always uses the default body."""

    def test_no_db_uses_default_body(self) -> None:
        """With no db configured, the returned prompt is the preamble + default + suffix."""
        cf = ContentFilter(db=None)

        prompt = cf._load_system_prompt(org_id=None)

        assert "SECURITY AUDITOR INSTRUCTIONS" in prompt
        assert "You are a security auditor for an AI proxy" in prompt
        assert prompt.endswith("Respond with exactly one word: BLOCK or ALLOW")


class TestLoadSystemPromptWithDatabase:
    """A real DB is present -- exercises the org_id branch and the query path."""

    def test_org_scoped_lookup_falls_back_to_default(
        self,
        filter_instance: ContentFilter,
        content_filter_db: DAL,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An org_id is supplied but no matching row exists -- default, no warning."""
        with caplog.at_level(logging.WARNING):
            prompt = filter_instance._load_system_prompt(org_id=7)

        assert "You are a security auditor for an AI proxy" in prompt
        assert "Failed to load custom auditor prompt" not in caplog.text

    def test_custom_prompt_configured_in_db_is_applied(
        self,
        filter_instance: ContentFilter,
        content_filter_db: DAL,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A configured global custom prompt is used instead of the default body.

        `content_filter_config` has a row for the 'auditor_system_prompt'
        key, exactly as an admin would configure via the API -- the fixed
        method's single combined-condition `db()` call finds it and no
        exception is raised or logged.
        """
        content_filter_db.content_filter_config.insert(
            key="auditor_system_prompt",
            value="CUSTOM ADMIN-CONFIGURED PROMPT",
            organization_id=None,
        )

        with caplog.at_level(logging.WARNING):
            prompt = filter_instance._load_system_prompt(org_id=None)

        assert "CUSTOM ADMIN-CONFIGURED PROMPT" in prompt
        assert "You are a security auditor for an AI proxy" not in prompt
        assert "'QuerySet' object is not callable" not in caplog.text
        assert caplog.text == ""

    def test_org_specific_prompt_takes_precedence_over_global(
        self,
        filter_instance: ContentFilter,
        content_filter_db: DAL,
    ) -> None:
        """An org-specific override outranks a global fallback row for the same org."""
        content_filter_db.content_filter_config.insert(
            key="auditor_system_prompt", value="GLOBAL PROMPT", organization_id=None
        )
        content_filter_db.content_filter_config.insert(
            key="auditor_system_prompt", value="ORG 5 PROMPT", organization_id=5
        )

        prompt = filter_instance._load_system_prompt(org_id=5)

        assert "ORG 5 PROMPT" in prompt
        assert "GLOBAL PROMPT" not in prompt

    def test_org_with_no_override_falls_back_to_global(
        self,
        filter_instance: ContentFilter,
        content_filter_db: DAL,
    ) -> None:
        """An org with no row of its own still gets the global prompt, not the default."""
        content_filter_db.content_filter_config.insert(
            key="auditor_system_prompt", value="GLOBAL PROMPT", organization_id=None
        )

        prompt = filter_instance._load_system_prompt(org_id=99)

        assert "GLOBAL PROMPT" in prompt
        assert "You are a security auditor for an AI proxy" not in prompt

    def test_malformed_row_with_empty_value_falls_back_to_default_and_warns(
        self,
        filter_instance: ContentFilter,
        content_filter_db: DAL,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A row exists for the key but its value is empty -- default, with a warning."""
        content_filter_db.content_filter_config.insert(
            key="auditor_system_prompt",
            value=None,
            organization_id=None,
        )

        with caplog.at_level(logging.WARNING):
            prompt = filter_instance._load_system_prompt(org_id=None)

        assert "You are a security auditor for an AI proxy" in prompt
        assert "empty" in caplog.text.lower()

    def test_db_error_is_caught_and_falls_back_to_default(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A broken db object degrades to the default body rather than raising.

        This is the prompt-loading fail-open path only -- it must never be
        confused with the filter's own block/allow decision, which has its
        own separate fail-open/fail-closed split (see the module-level
        comment in `content_filter.py`).
        """

        class _BrokenDB:
            def __getattr__(self, name: str) -> None:
                raise RuntimeError("db down")

        cf = ContentFilter(db=_BrokenDB())

        with caplog.at_level(logging.WARNING):
            prompt = cf._load_system_prompt(org_id=None)

        assert "You are a security auditor for an AI proxy" in prompt
        assert "Failed to load custom auditor prompt" in caplog.text

    def test_not_cached_second_call_reflects_updated_db_row(
        self,
        filter_instance: ContentFilter,
        content_filter_db: DAL,
    ) -> None:
        """Unlike `_load_custom_rules`, this method has no TTL cache -- every call re-queries."""
        prompt_before = filter_instance._load_system_prompt(org_id=None)
        assert "You are a security auditor for an AI proxy" in prompt_before

        content_filter_db.content_filter_config.insert(
            key="auditor_system_prompt",
            value="FRESHLY CONFIGURED PROMPT",
            organization_id=None,
        )

        prompt_after = filter_instance._load_system_prompt(org_id=None)
        assert "FRESHLY CONFIGURED PROMPT" in prompt_after


class TestLoadShieldgemmaPolicyNoDatabase:
    """`db=None` skips the lookup and returns the default policy."""

    def test_no_db_uses_default_policy(self) -> None:
        """With no db configured, the default PII/security policy text is returned."""
        cf = ContentFilter(db=None)

        policy = cf._load_shieldgemma_policy(org_id=None)

        assert "Personally identifiable information" in policy


class TestLoadShieldgemmaPolicyWithDatabase:
    """Unlike `_load_system_prompt`, this method's query is not chained -- happy path works."""

    def test_global_custom_policy_is_applied(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """A configured global policy row is used verbatim instead of the default."""
        content_filter_db.content_filter_config.insert(
            key="auditor_system_prompt",
            value="CUSTOM POLICY: never allow mentions of Project Phoenix.",
            organization_id=None,
        )

        policy = filter_instance._load_shieldgemma_policy(org_id=None)

        assert policy == "CUSTOM POLICY: never allow mentions of Project Phoenix."

    def test_org_specific_policy_takes_precedence_over_global(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """An org-specific override is preferred over a global fallback row."""
        content_filter_db.content_filter_config.insert(
            key="auditor_system_prompt", value="GLOBAL POLICY", organization_id=None
        )
        content_filter_db.content_filter_config.insert(
            key="auditor_system_prompt", value="ORG 5 POLICY", organization_id=5
        )

        policy = filter_instance._load_shieldgemma_policy(org_id=5)

        assert policy == "ORG 5 POLICY"

    def test_org_with_no_override_falls_back_to_global(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """An org with no row of its own still gets the global policy, not the default."""
        content_filter_db.content_filter_config.insert(
            key="auditor_system_prompt", value="GLOBAL POLICY", organization_id=None
        )

        policy = filter_instance._load_shieldgemma_policy(org_id=99)

        assert policy == "GLOBAL POLICY"

    def test_no_matching_row_uses_default_policy(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """An empty content_filter_config table falls back to the built-in default."""
        policy = filter_instance._load_shieldgemma_policy(org_id=None)

        assert "Personally identifiable information" in policy

    def test_db_error_is_caught_and_falls_back_to_default(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A broken db object degrades to the default policy rather than raising."""

        class _BrokenDB:
            def __getattr__(self, name: str) -> None:
                raise RuntimeError("db down")

        cf = ContentFilter(db=_BrokenDB())

        with caplog.at_level(logging.WARNING):
            policy = cf._load_shieldgemma_policy(org_id=None)

        assert "Personally identifiable information" in policy
        assert "Failed to load ShieldGemma policy" in caplog.text


class TestShieldgemmaMessageContextLines:
    """`_build_shieldgemma_messages` renders pre-scan pattern/NER context lines."""

    def test_pattern_and_ner_violations_appear_as_pre_scan_context(self) -> None:
        """Both a pattern-match and an NER-detection line are rendered before the content."""
        cf = ContentFilter(db=None)
        violations = [
            FilterViolation(
                rule_name="email",
                rule_type="builtin_pii",
                matched_text="a@b.com",
                action="log",
                confidence=0.85,
            ),
            FilterViolation(
                rule_name="ner:PERSON",
                rule_type="ner_entity",
                matched_text="Jane Doe",
                action="log",
                confidence=0.9,
            ),
        ]

        messages = cf._build_shieldgemma_messages("hello world", violations, org_id=None)

        content = messages[0]["content"]
        assert "Pre-scan findings" in content
        assert "Pattern match: email found 'a@b.com'" in content
        assert "NER detection: PERSON entity 'Jane Doe'" in content

    def test_no_violations_omits_context_section(self) -> None:
        """With no pre-scan violations, no 'Pre-scan findings' header is rendered."""
        cf = ContentFilter(db=None)

        messages = cf._build_shieldgemma_messages("hello world", [], org_id=None)

        assert "Pre-scan findings" not in messages[0]["content"]


class TestGraniteGuardianMessageContextLines:
    """`_build_granite_guardian_messages` renders the same pre-scan context lines."""

    def test_pattern_and_ner_violations_appear_as_pre_scan_context(self) -> None:
        """Both a pattern-match and an NER-detection line are rendered in the user turn."""
        cf = ContentFilter(db=None)
        violations = [
            FilterViolation(
                rule_name="ssn",
                rule_type="builtin_pii",
                matched_text="123-45-6789",
                action="redact",
                confidence=0.95,
            ),
            FilterViolation(
                rule_name="ner:LOCATION",
                rule_type="ner_entity",
                matched_text="Paris",
                action="log",
                confidence=0.7,
            ),
        ]

        messages = cf._build_granite_guardian_messages("hello world", violations, org_id=None)

        user_content = messages[1]["content"]
        assert "Pre-scan findings" in user_content
        assert "Pattern match: ssn found '123-45-6789'" in user_content
        assert "NER detection: LOCATION entity 'Paris'" in user_content

    def test_no_violations_omits_context_section(self) -> None:
        """With no pre-scan violations, no 'Pre-scan findings' header is rendered."""
        cf = ContentFilter(db=None)

        messages = cf._build_granite_guardian_messages("hello world", [], org_id=None)

        assert "Pre-scan findings" not in messages[1]["content"]
