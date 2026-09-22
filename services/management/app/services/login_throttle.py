"""Per-account failed-login throttling for the management API.

Implements the account-scoped brute-force control required by the
2026-09-14 security audit (HIGH: no brute-force protection on login).
WaddleAI deliberately has no in-app rate limiters -- network rate limiting
lives at the Cilium layer -- but that layer is IP-scoped and cannot express
"lock this *account* after N failures", so credential stuffing spread across
many source IPs was previously unimpeded.

Counters are keyed on the submitted account name (never the client IP) so a
distributed attack against one account is still caught, and they are applied
to *every* failed attempt including ones for accounts that do not exist --
otherwise "throttled vs not throttled" would itself be a user-enumeration
oracle.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_KEY_PREFIX = "waddleai:auth:"


class _CounterClient(Protocol):
    """Minimal Redis surface this module needs from a cache client."""

    def incr(self, name: str) -> Any:
        """Atomically increment the integer stored at *name* and return it."""
        ...

    def expire(self, name: str, time: int) -> Any:
        """Set a TTL of *time* seconds on *name*."""
        ...

    def setex(self, name: str, time: int, value: str) -> Any:
        """Set *name* to *value* with a TTL of *time* seconds."""
        ...

    def ttl(self, name: str) -> Any:
        """Return the remaining TTL of *name* in seconds."""
        ...

    def delete(self, *names: str) -> Any:
        """Delete the given keys."""
        ...


@dataclass(slots=True, frozen=True)
class ThrottleConfig:
    """Tunables for the login throttle, all overridable from the environment.

    Defaults are deliberately conservative: five failures inside a fifteen
    minute window trips a fifteen minute lockout that doubles per subsequent
    trip, capped at one hour.
    """

    max_failures: int = 5
    window_seconds: int = 900
    base_lockout_seconds: int = 900
    max_lockout_seconds: int = 3600
    enabled: bool = True

    @classmethod
    def from_env(cls) -> ThrottleConfig:
        """Build a config from LOGIN_THROTTLE_* environment variables.

        Any variable that is unset, non-numeric, or non-positive falls back to
        the safe default rather than disabling the control.
        """
        return cls(
            max_failures=_positive_int("LOGIN_THROTTLE_MAX_FAILURES", 5),
            window_seconds=_positive_int("LOGIN_THROTTLE_WINDOW_SECONDS", 900),
            base_lockout_seconds=_positive_int("LOGIN_THROTTLE_LOCKOUT_SECONDS", 900),
            max_lockout_seconds=_positive_int("LOGIN_THROTTLE_MAX_LOCKOUT_SECONDS", 3600),
            enabled=os.getenv("LOGIN_THROTTLE_ENABLED", "true").strip().lower()
            not in {"0", "false", "no", "off"},
        )


def _positive_int(env_name: str, default: int) -> int:
    """Read a strictly positive int from the environment, falling back to *default*."""
    raw = os.getenv(env_name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("login_throttle: %s=%r is not an integer; using %d", env_name, raw, default)
        return default
    if value <= 0:
        logger.warning("login_throttle: %s=%d is not positive; using %d", env_name, value, default)
        return default
    return value


@dataclass(slots=True, frozen=True)
class ThrottleDecision:
    """Outcome of a throttle check or a recorded failure."""

    allowed: bool
    retry_after_seconds: int = 0
    failure_count: int = 0


@dataclass(slots=True)
class _LocalEntry:
    """In-process failure bookkeeping for a single account."""

    failures: int = 0
    window_expires_at: float = 0.0
    locked_until: float = 0.0


class _LocalCounter:
    """Bounded, thread-safe in-process fallback used when no cache is reachable.

    Entries are capped (`_MAX_ENTRIES`) because the key space is attacker
    controlled -- an unbounded dict keyed on submitted usernames is a memory
    exhaustion vector. When the cap is hit the least-recently-touched entries
    are evicted, which can only ever *forgive* failures, never invent them.
    """

    _MAX_ENTRIES: ClassVar[int] = 4096

    def __init__(self) -> None:
        """Create an empty counter."""
        self._lock = threading.Lock()
        self._entries: dict[str, _LocalEntry] = {}

    def _prune(self, now: float, reserve: int = 0) -> None:
        """Drop expired entries, then evict oldest ones if still over cap.

        *reserve* is the number of entries the caller is about to insert, so
        the cap holds after the insert rather than one entry later.
        """
        expired = [
            key
            for key, entry in self._entries.items()
            if entry.window_expires_at <= now and entry.locked_until <= now
        ]
        for key in expired:
            del self._entries[key]
        overflow = len(self._entries) + reserve - self._MAX_ENTRIES
        if overflow <= 0:
            return
        for key in sorted(self._entries, key=lambda k: self._entries[k].window_expires_at)[
            :overflow
        ]:
            del self._entries[key]

    def check(self, key: str, now: float) -> ThrottleDecision:
        """Return the current decision for *key* without recording a failure."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return ThrottleDecision(allowed=True)
            if entry.locked_until > now:
                return ThrottleDecision(
                    allowed=False,
                    retry_after_seconds=max(1, int(entry.locked_until - now)),
                    failure_count=entry.failures,
                )
            failures = entry.failures if entry.window_expires_at > now else 0
            return ThrottleDecision(allowed=True, failure_count=failures)

    def register_failure(self, key: str, now: float, config: ThrottleConfig) -> ThrottleDecision:
        """Record one failure for *key* and return the resulting decision."""
        with self._lock:
            self._prune(now, reserve=1)
            entry = self._entries.get(key)
            if entry is None or entry.window_expires_at <= now:
                entry = _LocalEntry(window_expires_at=now + config.window_seconds)
                self._entries[key] = entry
            entry.failures += 1
            if entry.failures >= config.max_failures:
                lockout = _lockout_seconds(entry.failures, config)
                entry.locked_until = now + lockout
                return ThrottleDecision(
                    allowed=False, retry_after_seconds=lockout, failure_count=entry.failures
                )
            return ThrottleDecision(allowed=True, failure_count=entry.failures)

    def reset(self, key: str) -> None:
        """Forget all failure state for *key*."""
        with self._lock:
            self._entries.pop(key, None)


def _lockout_seconds(failures: int, config: ThrottleConfig) -> int:
    """Return the progressive-backoff lockout length for a given failure count.

    The lockout doubles for each failure past the threshold and is clamped to
    ``max_lockout_seconds`` so a long-running attack cannot push an account
    into an effectively permanent lockout.
    """
    over = max(0, failures - config.max_failures)
    # Cap the exponent before shifting: 2 ** over on an unbounded failure count
    # would build an enormous int before the min() ever runs.
    over = min(over, 16)
    return min(config.base_lockout_seconds * (2**over), config.max_lockout_seconds)


def account_key(username: str) -> str:
    """Return the stable, bounded cache key fragment for *username*.

    The raw username is hashed so that cache keys and any log line derived
    from them carry no PII and have a fixed length regardless of what an
    unauthenticated caller submitted.
    """
    return hashlib.sha256(username.strip().lower().encode("utf-8")).hexdigest()


@dataclass(slots=True)
class LoginThrottle:
    """Account-scoped failed-login throttle with a degraded in-process mode.

    Prefers the shared cache (Valkey/Redis) so the counter is consistent across
    replicas, and silently degrades to a per-process counter when no client is
    configured or the cache errors. Degrading *open* to a local counter is
    deliberate: failing closed here would lock every user out of the product
    the moment the cache blipped.
    """

    config: ThrottleConfig = field(default_factory=ThrottleConfig.from_env)
    client_provider: Callable[[], _CounterClient | None] = field(default=lambda: None)
    _local: _LocalCounter = field(default_factory=_LocalCounter, init=False)
    _degraded_logged: bool = field(default=False, init=False)

    def _client(self) -> _CounterClient | None:
        """Return the shared cache client, or None when unavailable."""
        try:
            return self.client_provider()
        except Exception:  # pragma: no cover -- provider is a plain attribute read
            return None

    def _degrade(self, reason: str, exc: BaseException | None = None) -> None:
        """Log the first fall-back to the in-process counter at WARNING."""
        if self._degraded_logged:
            return
        self._degraded_logged = True
        logger.warning(
            "login_throttle: shared counter unavailable (%s); "
            "degrading to a per-process limiter -- lockouts are no longer "
            "consistent across replicas",
            reason,
            exc_info=exc is not None,
        )

    def check(self, username: str) -> ThrottleDecision:
        """Return whether *username* may attempt a login right now."""
        if not self.config.enabled:
            return ThrottleDecision(allowed=True)
        key = account_key(username)
        client = self._client()
        if client is None:
            return self._local.check(key, time.monotonic())
        try:
            remaining = int(client.ttl(f"{_KEY_PREFIX}login_lock:{key}"))
        except Exception as exc:
            self._degrade("ttl lookup failed", exc)
            return self._local.check(key, time.monotonic())
        if remaining > 0:
            return ThrottleDecision(allowed=False, retry_after_seconds=remaining)
        return ThrottleDecision(allowed=True)

    def register_failure(self, username: str) -> ThrottleDecision:
        """Record a failed login for *username* and return the new decision."""
        if not self.config.enabled:
            return ThrottleDecision(allowed=True)
        key = account_key(username)
        client = self._client()
        if client is None:
            return self._local.register_failure(key, time.monotonic(), self.config)
        counter_key = f"{_KEY_PREFIX}login_fail:{key}"
        try:
            failures = int(client.incr(counter_key))
            if failures == 1:
                client.expire(counter_key, self.config.window_seconds)
            if failures >= self.config.max_failures:
                lockout = _lockout_seconds(failures, self.config)
                client.setex(f"{_KEY_PREFIX}login_lock:{key}", lockout, "1")
                logger.warning(
                    "login_throttle: account locked after %d failed attempts (account_hash=%s)",
                    failures,
                    key[:12],
                )
                return ThrottleDecision(
                    allowed=False, retry_after_seconds=lockout, failure_count=failures
                )
        except Exception as exc:
            self._degrade("counter update failed", exc)
            return self._local.register_failure(key, time.monotonic(), self.config)
        return ThrottleDecision(allowed=True, failure_count=failures)

    def reset(self, username: str) -> None:
        """Clear the failure counter for *username* after a successful login."""
        key = account_key(username)
        self._local.reset(key)
        client = self._client()
        if client is None:
            return
        try:
            client.delete(f"{_KEY_PREFIX}login_fail:{key}", f"{_KEY_PREFIX}login_lock:{key}")
        except Exception as exc:
            self._degrade("counter reset failed", exc)


def _default_client_provider() -> _CounterClient | None:
    """Return the process-wide cache client when it is a real Redis handle.

    Anything that is not an actual ``redis.Redis`` (an unconfigured ``None``,
    or a test double) is reported as "no shared cache" so the caller uses the
    in-process counter instead of issuing calls whose return types it cannot
    trust.
    """
    import redis

    from .. import extensions as ext

    client = ext.redis_client
    if isinstance(client, redis.Redis):
        return client
    return None


_throttle: LoginThrottle | None = None
_throttle_lock = threading.Lock()


def get_login_throttle() -> LoginThrottle:
    """Return the process-wide login throttle, building it on first use."""
    global _throttle
    if _throttle is None:
        with _throttle_lock:
            if _throttle is None:
                _throttle = LoginThrottle(client_provider=_default_client_provider)
    return _throttle


def reset_login_throttle(throttle: LoginThrottle | None = None) -> None:
    """Replace the process-wide throttle; passing None rebuilds it from env.

    Exists so tests can install a throttle with known thresholds and a known
    backend without reaching into module internals.
    """
    global _throttle
    with _throttle_lock:
        _throttle = throttle
