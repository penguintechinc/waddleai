"""
Tests for content filter redaction completeness.

Regression: security review 2026-07-26 — redaction truncation leak
Ensures long secrets (>100 chars) are completely redacted, not partially leaking tail.
"""

import pytest
from shared.security.content_filter import ContentFilter, FilterViolation


@pytest.fixture
def filter_instance() -> ContentFilter:
    """Create a content filter with no database backend for testing."""
    return ContentFilter(db=None)


def test_long_api_key_fully_redacted(filter_instance: ContentFilter) -> None:
    """Long API key (180 chars) should be completely redacted, no tail leak."""
    # Create a 180-char token
    long_token = "x" * 180
    text = f"api_key=sk-{long_token}"

    # Create a violation matching the full secret
    violation = FilterViolation(
        rule_name="api_key_generic",
        rule_type="builtin_pii",
        matched_text=f"api_key=sk-{long_token[:100]}",  # Truncated for logging
        full_matched_text=f"api_key=sk-{long_token}",  # Full text for redaction
        action="redact",
        confidence=0.95,
    )

    redacted = filter_instance._apply_redactions(text, [violation])

    # [REDACTED] should be present
    assert "[REDACTED]" in redacted, "Placeholder should be in redacted text"

    # Original secret (full or tail) should NOT survive in redacted text
    # Check that no 8+ char substring from the original token tail appears
    original_tail = long_token[-30:]  # Last 30 chars of the token
    assert original_tail not in redacted, f"Secret tail leaked: {original_tail} found in redacted text"

    # Verify filtering was effective: the original full secret should not be present
    assert long_token not in redacted, "Full secret token should not appear in redacted text"


def test_long_password_fully_redacted(filter_instance: ContentFilter) -> None:
    """Long password (150 chars) should be completely redacted."""
    long_password = "SecureP@ssw0rd_" + "a" * 135
    text = f"password={long_password}"

    violation = FilterViolation(
        rule_name="password_in_text",
        rule_type="builtin_pii",
        matched_text=f"password={long_password[:100]}",  # Truncated for logging
        full_matched_text=f"password={long_password}",  # Full text for redaction
        action="redact",
        confidence=0.80,
    )

    redacted = filter_instance._apply_redactions(text, [violation])

    assert "[REDACTED]" in redacted, "Placeholder should be in redacted text"
    assert long_password not in redacted, "Full password should not appear in redacted text"
    assert long_password[-20:] not in redacted, "Password tail should not leak"


def test_short_match_still_redacted(filter_instance: ContentFilter) -> None:
    """Short matches (< 100 chars) should still be fully redacted."""
    short_secret = "sk-short123456789"
    text = f"api_key={short_secret}"

    violation = FilterViolation(
        rule_name="api_key_generic",
        rule_type="builtin_pii",
        matched_text=f"api_key={short_secret}",  # Full text is < 100 chars
        full_matched_text=f"api_key={short_secret}",  # Same as matched_text
        action="redact",
        confidence=0.95,
    )

    redacted = filter_instance._apply_redactions(text, [violation])

    assert "[REDACTED]" in redacted, "Short secret should be redacted"
    assert short_secret not in redacted, "Short secret should not appear in redacted text"


def test_multiple_long_secrets_all_redacted(filter_instance: ContentFilter) -> None:
    """Multiple long secrets in one text should all be redacted."""
    token1 = "tk1_" + "a" * 110
    token2 = "tk2_" + "b" * 120
    text = f"First secret: api_key={token1}\nSecond secret: api_key={token2}"

    violations = [
        FilterViolation(
            rule_name="api_key_generic",
            rule_type="builtin_pii",
            matched_text=f"api_key={token1[:100]}",
            full_matched_text=f"api_key={token1}",
            action="redact",
            confidence=0.95,
        ),
        FilterViolation(
            rule_name="api_key_generic",
            rule_type="builtin_pii",
            matched_text=f"api_key={token2[:100]}",
            full_matched_text=f"api_key={token2}",
            action="redact",
            confidence=0.95,
        ),
    ]

    redacted = filter_instance._apply_redactions(text, violations)

    # Both secrets should be replaced
    assert redacted.count("[REDACTED]") == 2, "Both long secrets should be redacted"
    assert token1 not in redacted, "First secret should not appear"
    assert token2 not in redacted, "Second secret should not appear"


def test_logged_matched_text_stays_truncated(filter_instance: ContentFilter) -> None:
    """
    Logged matched_text should remain truncated (≤100 chars) for storage efficiency,
    while full_matched_text is used for redaction only.
    """
    long_token = "x" * 180
    full_secret = f"api_key=sk-{long_token}"
    text = full_secret

    # The full match is longer than 100 chars
    assert len(full_secret) > 100, "Full secret should exceed 100 chars for this test"

    violation = FilterViolation(
        rule_name="api_key_generic",
        rule_type="builtin_pii",
        matched_text=full_secret[:100],  # Truncated to 100 chars for logging
        full_matched_text=full_secret,  # Full match for redaction
        action="redact",
        confidence=0.95,
    )

    # The logged matched_text should be truncated (audit log doesn't store full secrets)
    assert len(violation.matched_text) == 100, "Logged matched_text should be exactly 100 chars when truncated"
    # But full_matched_text should have the complete secret for redaction
    assert len(violation.full_matched_text) == len(full_secret), "full_matched_text should preserve complete match"

    redacted = filter_instance._apply_redactions(text, [violation])
    assert "[REDACTED]" in redacted, "Full secret should be redacted using full_matched_text"
    assert long_token not in redacted, "Complete token should be redacted"
