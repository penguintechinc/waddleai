"""The pooled-quota flag is default-OFF and fails to OFF."""

import pytest

from shared.quota import flags


def test_default_off_without_env(monkeypatch):
    """Unseen flag -> OFF (house rule: new flags default OFF)."""
    monkeypatch.delenv("WADDLEAI_FLAG_POOLED_QUOTA", raising=False)
    assert flags.pooled_quota_enabled("1") is False


@pytest.mark.parametrize(
    ("raw", "expected"), [("1", True), ("true", True), ("0", False), ("no", False)]
)
def test_env_override_wins(monkeypatch, raw, expected):
    """WADDLEAI_FLAG_POOLED_QUOTA is the test/alpha override."""
    monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", raw)
    assert flags.pooled_quota_enabled("7") is expected


def test_lookup_failure_fails_to_off(monkeypatch):
    """A raising flag layer must read as OFF, never as ON and never propagate."""

    def boom(*a, **k):
        raise RuntimeError("posthog down")

    monkeypatch.setattr(flags, "is_feature_enabled", boom)
    assert flags.pooled_quota_enabled("7") is False
