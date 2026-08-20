"""Unit tests for the PostHog-backed feature flag helper.

House rule: every feature ships behind a flag, default OFF, with graceful
degradation — flag-server failure falls back to the default, never raises.
The env override (WADDLEAI_FLAG_*) is the test/alpha mechanism.
"""

from unittest.mock import Mock, patch

import shared.utils.feature_flags as ff
from shared.utils.feature_flags import is_feature_enabled


def setup_function() -> None:
    """Clear the module-level cached PostHog client so each test starts unconfigured."""
    # Reset the cached client between tests
    ff._posthog_client = None


def test_default_off_when_no_env_and_no_posthog(monkeypatch) -> None:
    """With no env override and no PostHog configured, an unseen flag defaults OFF."""
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.delenv("POSTHOG_KEY", raising=False)
    assert is_feature_enabled("waddleai.memory-org-scope") is False


def test_env_override_on(monkeypatch) -> None:
    """WADDLEAI_FLAG_* env var set to a truthy value forces the flag on."""
    monkeypatch.setenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", "1")
    assert is_feature_enabled("waddleai.memory-org-scope") is True


def test_env_override_off_beats_posthog(monkeypatch) -> None:
    """An explicit env override of 'false' wins even when PostHog is configured."""
    monkeypatch.setenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", "false")
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    assert is_feature_enabled("waddleai.memory-org-scope") is False


def test_posthog_result_used_when_configured(monkeypatch) -> None:
    """With no env override, the PostHog client's feature_enabled result is used verbatim."""
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    fake = Mock()
    fake.feature_enabled.return_value = True
    with patch.object(ff, "_get_posthog_client", return_value=fake):
        assert is_feature_enabled("waddleai.memory-org-scope", distinct_id="3") is True
    fake.feature_enabled.assert_called_once_with("waddleai.memory-org-scope", "3")


def test_posthog_failure_falls_back_to_default(monkeypatch) -> None:
    """A PostHog client exception is swallowed and falls back to the caller's default.

    Never raises.
    """
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    fake = Mock()
    fake.feature_enabled.side_effect = RuntimeError("posthog down")
    with patch.object(ff, "_get_posthog_client", return_value=fake):
        assert is_feature_enabled("waddleai.memory-org-scope") is False
        assert is_feature_enabled("waddleai.memory-org-scope", default=True) is True


def test_env_name_derivation() -> None:
    """Flag keys map to env var names by uppercasing and replacing '.'/'-' with '_'."""
    assert ff._env_var_name("waddleai.memory-org-scope") == "WADDLEAI_FLAG_MEMORY_ORG_SCOPE"
    assert ff._env_var_name("waddleai.security-v2") == "WADDLEAI_FLAG_SECURITY_V2"
