"""Unit tests for the per-account login throttle.

regression: audit-2026-09-14 (HIGH -- no brute-force protection on login).
Covers the shared-cache path, the degraded in-process fallback, and the
progressive backoff schedule.
"""

import logging

import pytest

from services.management.app.services.login_throttle import (
    LoginThrottle,
    ThrottleConfig,
    _LocalCounter,
    _lockout_seconds,
    account_key,
    get_login_throttle,
    reset_login_throttle,
)


class _FakeCache:
    """Minimal in-memory stand-in for the Valkey/Redis client surface used here."""

    def __init__(self) -> None:
        """Start empty, with no simulated failures."""
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.fail = False

    def _maybe_fail(self) -> None:
        if self.fail:
            raise ConnectionError("cache down")

    def incr(self, name: str) -> int:
        """Increment and return the counter at *name*."""
        self._maybe_fail()
        self.values[name] = self.values.get(name, 0) + 1
        return self.values[name]

    def expire(self, name: str, time: int) -> bool:
        """Record a TTL for *name*."""
        self._maybe_fail()
        self.ttls[name] = time
        return True

    def setex(self, name: str, time: int, value: str) -> bool:
        """Set *name* with a TTL."""
        self._maybe_fail()
        self.values[name] = int(value)
        self.ttls[name] = time
        return True

    def ttl(self, name: str) -> int:
        """Return the remaining TTL of *name*, or -2 when absent."""
        self._maybe_fail()
        return self.ttls.get(name, -2)

    def delete(self, *names: str) -> int:
        """Delete the given keys."""
        self._maybe_fail()
        removed = 0
        for name in names:
            removed += self.values.pop(name, None) is not None
            self.ttls.pop(name, None)
        return removed


@pytest.fixture
def cache() -> _FakeCache:
    """Return a fresh fake cache."""
    return _FakeCache()


@pytest.fixture
def throttle(cache: _FakeCache) -> LoginThrottle:
    """Return a throttle backed by the fake cache, tripping after 3 failures."""
    return LoginThrottle(
        config=ThrottleConfig(
            max_failures=3, window_seconds=900, base_lockout_seconds=60, max_lockout_seconds=600
        ),
        client_provider=lambda: cache,
    )


class TestAccountKey:
    """account_key() normalisation."""

    def test_is_case_and_whitespace_insensitive(self) -> None:
        """The same account submitted differently maps to one counter."""
        assert account_key("Admin") == account_key("  admin ")

    def test_carries_no_plaintext_username(self) -> None:
        """The key is a hash, so cache keys and logs hold no PII."""
        key = account_key("alice@example.com")
        assert "alice" not in key
        assert len(key) == 64


class TestSharedCacheBackend:
    """Behaviour when a shared cache is reachable."""

    def test_allows_until_the_threshold(self, throttle: LoginThrottle) -> None:
        """Failures below the threshold leave the account usable."""
        first = throttle.register_failure("admin")
        second = throttle.register_failure("admin")
        assert (first.allowed, first.failure_count) == (True, 1)
        assert (second.allowed, second.failure_count) == (True, 2)
        assert throttle.check("admin").allowed is True

    def test_locks_on_the_nth_failure(self, throttle: LoginThrottle) -> None:
        """The Nth failure locks and reports a positive retry_after."""
        for _ in range(2):
            assert throttle.register_failure("admin").allowed is True
        decision = throttle.register_failure("admin")
        assert decision.allowed is False
        assert decision.retry_after_seconds == 60
        assert throttle.check("admin").allowed is False

    def test_reset_clears_the_lock(self, throttle: LoginThrottle) -> None:
        """A successful login wipes both the counter and the lock."""
        for _ in range(3):
            throttle.register_failure("admin")
        throttle.reset("admin")
        assert throttle.check("admin").allowed is True

    def test_counters_are_isolated_per_account(self, throttle: LoginThrottle) -> None:
        """Locking one account never touches another."""
        for _ in range(3):
            throttle.register_failure("victim")
        assert throttle.check("victim").allowed is False
        assert throttle.check("bystander").allowed is True

    def test_window_ttl_set_once(self, throttle: LoginThrottle, cache: _FakeCache) -> None:
        """The failure counter gets its window TTL on the first failure only."""
        throttle.register_failure("admin")
        counter_key = f"waddleai:auth:login_fail:{account_key('admin')}"
        assert cache.ttls[counter_key] == 900


class TestDegradedMode:
    """A cache outage must never lock the whole user base out."""

    def test_check_degrades_open(
        self, throttle: LoginThrottle, cache: _FakeCache, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failing cache lookup falls back to the in-process counter, not a lockout."""
        cache.fail = True
        with caplog.at_level(logging.WARNING):
            assert throttle.check("admin").allowed is True
        assert "degrading to a per-process limiter" in caplog.text

    def test_failures_still_counted_locally(
        self, throttle: LoginThrottle, cache: _FakeCache
    ) -> None:
        """The in-process fallback still enforces the same threshold."""
        cache.fail = True
        assert throttle.register_failure("admin").allowed is True
        assert throttle.register_failure("admin").allowed is True
        assert throttle.register_failure("admin").allowed is False
        assert throttle.check("admin").allowed is False

    def test_reset_tolerates_a_dead_cache(self, throttle: LoginThrottle, cache: _FakeCache) -> None:
        """Reset must not raise when the cache is unreachable."""
        throttle.register_failure("admin")
        cache.fail = True
        throttle.reset("admin")

    def test_no_client_configured_uses_local_counter(self) -> None:
        """With no cache at all the throttle still enforces the threshold."""
        local_only = LoginThrottle(
            config=ThrottleConfig(max_failures=2, base_lockout_seconds=30),
            client_provider=lambda: None,
        )
        assert local_only.register_failure("admin").allowed is True
        assert local_only.register_failure("admin").allowed is False
        assert local_only.check("admin").retry_after_seconds > 0

    def test_degrade_warning_logged_once(
        self, throttle: LoginThrottle, cache: _FakeCache, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Repeated degraded calls do not flood the log."""
        cache.fail = True
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                throttle.check("admin")
        assert caplog.text.count("degrading to a per-process limiter") == 1


class TestProgressiveBackoff:
    """_lockout_seconds() schedule."""

    @pytest.mark.parametrize(
        ("failures", "expected"),
        [(5, 60), (6, 120), (7, 240), (8, 480), (9, 600), (50, 600)],
    )
    def test_doubles_then_caps(self, failures: int, expected: int) -> None:
        """Lockout doubles per failure past the threshold, capped at the maximum."""
        config = ThrottleConfig(max_failures=5, base_lockout_seconds=60, max_lockout_seconds=600)
        assert _lockout_seconds(failures, config) == expected

    def test_huge_failure_counts_do_not_explode(self) -> None:
        """A very large counter must not build an enormous intermediate int."""
        config = ThrottleConfig(max_failures=1, base_lockout_seconds=1, max_lockout_seconds=3600)
        assert _lockout_seconds(10_000_000, config) == 3600


class TestConfig:
    """ThrottleConfig.from_env()."""

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unconfigured environment yields the documented safe defaults."""
        for name in (
            "LOGIN_THROTTLE_MAX_FAILURES",
            "LOGIN_THROTTLE_WINDOW_SECONDS",
            "LOGIN_THROTTLE_LOCKOUT_SECONDS",
            "LOGIN_THROTTLE_MAX_LOCKOUT_SECONDS",
            "LOGIN_THROTTLE_ENABLED",
        ):
            monkeypatch.delenv(name, raising=False)
        config = ThrottleConfig.from_env()
        assert (config.max_failures, config.window_seconds, config.enabled) == (5, 900, True)

    @pytest.mark.parametrize("raw", ["nonsense", "0", "-4"])
    def test_invalid_values_fall_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """A bad value must not silently disable or weaken the control."""
        monkeypatch.setenv("LOGIN_THROTTLE_MAX_FAILURES", raw)
        assert ThrottleConfig.from_env().max_failures == 5

    def test_can_be_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Operators can turn the throttle off explicitly."""
        monkeypatch.setenv("LOGIN_THROTTLE_ENABLED", "false")
        config = ThrottleConfig.from_env()
        assert config.enabled is False
        throttle = LoginThrottle(config=config, client_provider=lambda: None)
        for _ in range(50):
            assert throttle.register_failure("admin").allowed is True
        assert throttle.check("admin").allowed is True


class TestLocalCounterBounds:
    """The in-process counter is attacker-facing and must stay bounded."""

    def test_entries_are_capped(self) -> None:
        """Submitting many distinct usernames cannot grow the map without limit."""
        counter = _LocalCounter()
        config = ThrottleConfig(max_failures=99, window_seconds=900)
        for i in range(_LocalCounter._MAX_ENTRIES + 500):
            counter.register_failure(f"user-{i}", 1000.0, config)
        assert len(counter._entries) <= _LocalCounter._MAX_ENTRIES

    def test_expired_window_restarts_the_count(self) -> None:
        """Failures outside the window do not accumulate toward a lockout."""
        counter = _LocalCounter()
        config = ThrottleConfig(max_failures=2, window_seconds=10, base_lockout_seconds=30)
        assert counter.register_failure("a", 0.0, config).allowed is True
        assert counter.register_failure("a", 100.0, config).allowed is True

    def test_check_of_unknown_key_is_allowed(self) -> None:
        """An account with no history is never throttled."""
        assert _LocalCounter().check("never-seen", 1.0).allowed is True


class TestSingleton:
    """get_login_throttle()/reset_login_throttle()."""

    def test_returns_a_stable_instance(self) -> None:
        """Repeated calls hand back the same throttle."""
        reset_login_throttle()
        try:
            assert get_login_throttle() is get_login_throttle()
        finally:
            reset_login_throttle()

    def test_reset_installs_a_replacement(self) -> None:
        """Tests can swap in a throttle with known thresholds."""
        replacement = LoginThrottle(client_provider=lambda: None)
        reset_login_throttle(replacement)
        try:
            assert get_login_throttle() is replacement
        finally:
            reset_login_throttle()
