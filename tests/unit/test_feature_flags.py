"""Unit tests for the PostHog-backed feature flag helper.

House rule: every feature ships behind a flag, default OFF, with graceful
degradation -- flag-server failure falls back to the last-known cached value,
or the default if nothing was ever cached. Never raises. The env override
(WADDLEAI_FLAG_*) is the test/alpha mechanism and always wins.
"""

from unittest.mock import Mock, patch

import shared.utils.feature_flags as ff
from shared.utils.feature_flags import is_feature_enabled


def setup_function() -> None:
    """Reset all module-level state so each test starts clean."""
    ff.reset_for_testing()


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


def test_env_override_wins_over_a_fresh_cache_entry(monkeypatch) -> None:
    """Env override always wins, even if a TTL-fresh cached PostHog value exists."""
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    fake = Mock()
    fake.feature_enabled.return_value = True
    with patch.object(ff, "_get_posthog_client", return_value=fake):
        assert is_feature_enabled("waddleai.memory-org-scope") is True
    monkeypatch.setenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", "false")
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


def test_posthog_undefined_flag_uses_default(monkeypatch) -> None:
    """A defined PostHog client answering None (flag undefined) is a deliberate default."""
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    fake = Mock()
    fake.feature_enabled.return_value = None
    with patch.object(ff, "_get_posthog_client", return_value=fake):
        assert is_feature_enabled("waddleai.memory-org-scope", default=True) is True


def test_posthog_failure_falls_back_to_default_when_never_cached(monkeypatch) -> None:
    """A PostHog exception with no prior successful resolution falls back to the default.

    Never raises.
    """
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    fake = Mock()
    fake.feature_enabled.side_effect = RuntimeError("posthog down")
    with patch.object(ff, "_get_posthog_client", return_value=fake):
        assert is_feature_enabled("waddleai.memory-org-scope") is False
        assert is_feature_enabled("waddleai.memory-org-scope", default=True) is True


def test_posthog_outage_serves_last_known_value(monkeypatch) -> None:
    """A PostHog outage after a prior successful resolution serves that last-known value.

    This is the graceful-degradation fix: an outage must not snap straight to
    the caller's hardcoded default when a real value was previously resolved.
    """
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    monkeypatch.setenv("FEATURE_FLAG_TTL_SECONDS", "0")  # force every call to re-resolve
    fake = Mock()
    with patch.object(ff, "_get_posthog_client", return_value=fake):
        fake.feature_enabled.return_value = True
        assert is_feature_enabled("waddleai.memory-org-scope", default=False) is True

        fake.feature_enabled.side_effect = RuntimeError("posthog outage")
        # Default is False, but the last-known value (True) must win.
        assert is_feature_enabled("waddleai.memory-org-scope", default=False) is True


def test_ttl_fresh_cache_skips_the_posthog_call(monkeypatch) -> None:
    """A TTL-fresh cached value is served without a second PostHog round trip."""
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    monkeypatch.setenv("FEATURE_FLAG_TTL_SECONDS", "60")
    fake = Mock()
    fake.feature_enabled.return_value = True
    with patch.object(ff, "_get_posthog_client", return_value=fake):
        assert is_feature_enabled("waddleai.memory-org-scope") is True
        assert is_feature_enabled("waddleai.memory-org-scope") is True
    fake.feature_enabled.assert_called_once()


def test_outage_then_recovery_refreshes_the_cache(monkeypatch) -> None:
    """After an outage degrades to last-known, a later successful call updates the cache."""
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    monkeypatch.setenv("FEATURE_FLAG_TTL_SECONDS", "0")
    fake = Mock()
    with patch.object(ff, "_get_posthog_client", return_value=fake):
        fake.feature_enabled.return_value = True
        assert is_feature_enabled("waddleai.memory-org-scope") is True

        fake.feature_enabled.side_effect = RuntimeError("outage")
        assert is_feature_enabled("waddleai.memory-org-scope") is True  # degraded, last-known

        fake.feature_enabled.side_effect = None
        fake.feature_enabled.return_value = False
        assert is_feature_enabled("waddleai.memory-org-scope") is False  # recovered

        fake.feature_enabled.side_effect = RuntimeError("outage again")
        assert is_feature_enabled("waddleai.memory-org-scope") is False  # last-known is now False


def test_warn_once_per_outage_window_not_per_call(monkeypatch) -> None:
    """Repeated outage calls within FEATURE_FLAG_WARN_INTERVAL_SECONDS log once, not per call."""
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    monkeypatch.setenv("FEATURE_FLAG_TTL_SECONDS", "0")
    monkeypatch.setenv("FEATURE_FLAG_WARN_INTERVAL_SECONDS", "3600")
    fake = Mock()
    fake.feature_enabled.side_effect = RuntimeError("posthog down")
    with (
        patch.object(ff, "_get_posthog_client", return_value=fake),
        patch.object(ff.logger, "warning") as warn,
    ):
        for _ in range(5):
            is_feature_enabled("waddleai.memory-org-scope")
    assert warn.call_count == 1


def test_cache_bound_evicts_least_recently_used(monkeypatch) -> None:
    """The cache never grows past FEATURE_FLAG_CACHE_MAX_ENTRIES."""
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    monkeypatch.setenv("FEATURE_FLAG_CACHE_MAX_ENTRIES", "3")
    fake = Mock()
    fake.feature_enabled.return_value = True
    with patch.object(ff, "_get_posthog_client", return_value=fake):
        for distinct_id in ("a", "b", "c", "d", "e"):
            is_feature_enabled("waddleai.memory-org-scope", distinct_id=distinct_id)
    assert len(ff._cache) == 3
    # The most recently used entries survive; the earliest ("a", "b") are evicted.
    assert ("waddleai.memory-org-scope", "a") not in ff._cache
    assert ("waddleai.memory-org-scope", "e") in ff._cache


def test_kill_switch_reverts_to_legacy_behaviour(monkeypatch) -> None:
    """WADDLEAI_FLAG_DISABLE_FLAG_DEGRADATION_CACHE=1 disables the cache/last-known mechanism."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_FLAG_DEGRADATION_CACHE", "1")
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    fake = Mock()
    with patch.object(ff, "_get_posthog_client", return_value=fake):
        fake.feature_enabled.return_value = True
        assert is_feature_enabled("waddleai.memory-org-scope") is True

        fake.feature_enabled.side_effect = RuntimeError("outage")
        # Kill switch ON -- legacy behaviour: no last-known fallback, straight to default.
        assert is_feature_enabled("waddleai.memory-org-scope", default=False) is False


def test_evaluation_metric_records_each_result(monkeypatch) -> None:
    """The feature_flag_evaluations_total counter is incremented for live/cached/default/error."""
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    monkeypatch.setenv("FEATURE_FLAG_TTL_SECONDS", "60")
    fake_counter = Mock()
    with patch("shared.observability.metrics.get_meter") as get_meter:
        get_meter.return_value.create_counter.return_value = fake_counter
        fake = Mock()
        with patch.object(ff, "_get_posthog_client", return_value=fake):
            fake.feature_enabled.return_value = True
            is_feature_enabled("waddleai.memory-org-scope")  # live
            is_feature_enabled("waddleai.memory-org-scope")  # cached (TTL-fresh)

            fake.feature_enabled.return_value = None
            is_feature_enabled("waddleai.other-flag")  # default (undefined)

            fake.feature_enabled.side_effect = RuntimeError("down")
            is_feature_enabled("waddleai.never-cached-flag")  # error (never cached)

    results = [call.args[1]["result"] for call in fake_counter.add.call_args_list]
    assert results == ["live", "cached", "default", "error"]


def test_env_float_invalid_falls_back_to_default(monkeypatch) -> None:
    """An unparseable float env var logs a warning and falls back to the default."""
    monkeypatch.setenv("FEATURE_FLAG_TTL_SECONDS", "not-a-number")
    assert ff._env_float("FEATURE_FLAG_TTL_SECONDS", 30.0) == 30.0


def test_env_int_invalid_falls_back_to_default(monkeypatch) -> None:
    """An unparseable int env var logs a warning and falls back to the default."""
    monkeypatch.setenv("FEATURE_FLAG_CACHE_MAX_ENTRIES", "not-a-number")
    assert ff._env_int("FEATURE_FLAG_CACHE_MAX_ENTRIES", 2048) == 2048


def test_get_posthog_client_constructs_with_env_config(monkeypatch) -> None:
    """_get_posthog_client builds a real Posthog client using the configured host/timeout."""
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    monkeypatch.setenv("POSTHOG_HOST", "https://posthog.example.com")
    monkeypatch.setenv("FEATURE_FLAG_TIMEOUT_SECONDS", "7")
    fake_posthog_cls = Mock()
    with patch("posthog.Posthog", fake_posthog_cls):
        client = ff._get_posthog_client()
    assert client is fake_posthog_cls.return_value
    fake_posthog_cls.assert_called_once_with(
        "phc_test",
        host="https://posthog.example.com",
        feature_flags_request_timeout_seconds=7.0,
    )
    # Second call reuses the cached instance -- no second construction.
    with patch("posthog.Posthog", fake_posthog_cls):
        ff._get_posthog_client()
    fake_posthog_cls.assert_called_once()


def test_evaluation_metric_emission_failure_is_swallowed(monkeypatch) -> None:
    """A broken metrics backend must never break flag evaluation."""
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.delenv("POSTHOG_KEY", raising=False)
    with patch("shared.observability.metrics.get_meter", side_effect=RuntimeError("no meter")):
        assert is_feature_enabled("waddleai.memory-org-scope") is False


def test_legacy_mode_env_override(monkeypatch) -> None:
    """Legacy (kill-switch-on) path still honours the env override first."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_FLAG_DEGRADATION_CACHE", "1")
    monkeypatch.setenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", "true")
    assert is_feature_enabled("waddleai.memory-org-scope") is True


def test_legacy_mode_no_posthog_client_uses_default(monkeypatch) -> None:
    """Legacy path with no PostHog configured falls back to the caller's default."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_FLAG_DEGRADATION_CACHE", "1")
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.delenv("POSTHOG_KEY", raising=False)
    assert is_feature_enabled("waddleai.memory-org-scope", default=True) is True


def test_legacy_mode_posthog_exception_falls_back_to_default(monkeypatch) -> None:
    """Legacy path swallows a PostHog exception and uses the caller's default (no cache)."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_FLAG_DEGRADATION_CACHE", "1")
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    fake = Mock()
    fake.feature_enabled.side_effect = RuntimeError("down")
    with patch.object(ff, "_get_posthog_client", return_value=fake):
        assert is_feature_enabled("waddleai.memory-org-scope", default=True) is True


def test_env_name_derivation() -> None:
    """Flag keys map to env var names by uppercasing and replacing '.'/'-' with '_'."""
    assert ff._env_var_name("waddleai.memory-org-scope") == "WADDLEAI_FLAG_MEMORY_ORG_SCOPE"
    assert ff._env_var_name("waddleai.security-v2") == "WADDLEAI_FLAG_SECURITY_V2"
