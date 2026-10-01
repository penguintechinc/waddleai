"""Unit tests for ``services.management.app.services.rate_limiter``.

Covers the pieces ``test_auth_routes.py``'s `TestAuthRateLimit` exercises only
indirectly through the HTTP layer: config validation, `RateLimiterConfig.
from_env()`'s `_positive_int` fallbacks, the disabled-limiter short-circuit,
bucket-cap eviction, and the module-level singleton's double-checked-locking
branches.
"""

from __future__ import annotations

import threading

import pytest

import services.management.app.services.rate_limiter as rate_limiter_mod
from services.management.app.services.rate_limiter import (
    RateLimiterConfig,
    RequestRateLimiter,
    get_auth_rate_limiter,
    reset_auth_rate_limiter,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the module-level limiter singleton around every test."""
    reset_auth_rate_limiter()
    yield
    reset_auth_rate_limiter()


class TestRateLimiterConfigValidation:
    """`RateLimiterConfig.__post_init__` rejects a non-positive rate or window."""

    def test_rejects_non_positive_max_requests(self) -> None:
        """A zero or negative `max_requests` is rejected -- it could never allow a request."""
        with pytest.raises(ValueError, match="max_requests"):
            RateLimiterConfig(max_requests=0)

    def test_rejects_non_positive_window_seconds(self) -> None:
        """A zero or negative `window_seconds` is rejected."""
        with pytest.raises(ValueError, match="window_seconds"):
            RateLimiterConfig(window_seconds=0)


class TestRateLimiterConfigFromEnv:
    """`RateLimiterConfig.from_env()` / `_positive_int`'s fallback behaviour."""

    def test_valid_positive_int_env_vars_are_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A valid positive integer env var is parsed and used as-is."""
        monkeypatch.setenv("AUTH_RATE_LIMIT_MAX_REQUESTS", "42")
        monkeypatch.setenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "120")

        config = RateLimiterConfig.from_env()

        assert config.max_requests == 42
        assert config.window_seconds == 120

    def test_non_integer_env_var_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-numeric value warns and falls back to the safe default, not a crash."""
        monkeypatch.setenv("AUTH_RATE_LIMIT_MAX_REQUESTS", "not-a-number")

        with caplog.at_level("WARNING"):
            config = RateLimiterConfig.from_env()

        assert config.max_requests == 10
        assert any("not an integer" in r.message for r in caplog.records)

    def test_non_positive_env_var_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A parseable but non-positive value warns and falls back, not disabling the limiter."""
        monkeypatch.setenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "-5")

        with caplog.at_level("WARNING"):
            config = RateLimiterConfig.from_env()

        assert config.window_seconds == 60
        assert any("not positive" in r.message for r in caplog.records)

    def test_unset_env_var_falls_back_to_default_without_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unset env var silently uses the default -- no warning for the common case."""
        monkeypatch.delenv("AUTH_RATE_LIMIT_MAX_REQUESTS", raising=False)

        config = RateLimiterConfig.from_env()

        assert config.max_requests == 10

    def test_disabled_flag_variants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AUTH_RATE_LIMIT_ENABLED recognises each falsy spelling."""
        for value in ("0", "false", "no", "off", "False", "OFF"):
            monkeypatch.setenv("AUTH_RATE_LIMIT_ENABLED", value)
            assert RateLimiterConfig.from_env().enabled is False, value

        monkeypatch.setenv("AUTH_RATE_LIMIT_ENABLED", "true")
        assert RateLimiterConfig.from_env().enabled is True


class TestRequestRateLimiterDisabled:
    """`enabled=False` short-circuits every check to an unconditional allow."""

    def test_disabled_limiter_always_allows(self) -> None:
        """A disabled limiter never consumes a token or reports a retry wait."""
        limiter = RequestRateLimiter(RateLimiterConfig(max_requests=1, enabled=False))

        for _ in range(5):
            decision = limiter.check("same-key")
            assert decision.allowed is True
            assert decision.retry_after_seconds == 0


class TestRequestRateLimiterBucketCapEviction:
    """`_prune` evicts the least-recently-refilled buckets once over `_MAX_ENTRIES`."""

    def test_oldest_bucket_is_evicted_once_over_capacity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Filling past `_MAX_ENTRIES` evicts the bucket untouched the longest.

        `_MAX_ENTRIES` is lowered via monkeypatch so the test does not need
        to create thousands of real buckets to exercise the eviction branch.
        """
        monkeypatch.setattr(RequestRateLimiter, "_MAX_ENTRIES", 3)
        limiter = RequestRateLimiter(RateLimiterConfig(max_requests=5, window_seconds=60))

        limiter.check("key-a")
        limiter.check("key-b")
        limiter.check("key-c")
        assert len(limiter._buckets) == 3

        # A 4th distinct key pushes the bucket count over the cap -- key-a
        # (the oldest `last_refill`) must be evicted to make room.
        limiter.check("key-d")

        assert len(limiter._buckets) == 3
        assert "key-a" not in limiter._buckets
        assert "key-d" in limiter._buckets


class TestGetAuthRateLimiterSingleton:
    """Double-checked-locking branches around the process-wide singleton."""

    def test_builds_from_env_on_first_use(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The first call with no limiter installed builds one from the environment."""
        monkeypatch.setenv("AUTH_RATE_LIMIT_MAX_REQUESTS", "7")

        limiter = get_auth_rate_limiter()

        assert isinstance(limiter, RequestRateLimiter)
        assert limiter._config.max_requests == 7
        # A second call returns the exact same instance, not a fresh build.
        assert get_auth_rate_limiter() is limiter

    def test_inner_null_check_skips_rebuild_when_already_set_inside_the_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The inner (lock-held) null check must re-read state, not blindly rebuild.

        Simulates the race the double-checked-locking pattern defends
        against: another "thread" (here, the lock's own `__enter__`) installs
        the singleton while this caller was waiting on the lock, so the inner
        `if _limiter is None:` must observe it and skip construction.
        """
        sentinel = RequestRateLimiter(RateLimiterConfig())
        real_lock = threading.Lock()

        class _RaceSimulatingLock:
            def __enter__(self) -> _RaceSimulatingLock:
                real_lock.acquire()
                rate_limiter_mod._limiter = sentinel
                return self

            def __exit__(self, *exc_info: object) -> None:
                real_lock.release()

        monkeypatch.setattr(rate_limiter_mod, "_limiter_lock", _RaceSimulatingLock())

        result = get_auth_rate_limiter()

        assert result is sentinel
