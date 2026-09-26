"""In-app request-volume rate limiting for unauthenticated credential-verification routes.

Security review 2026-09-26 (headless-auth-secrev M2): ``POST /api/v1/auth/token``
and ``POST /api/v1/auth/login`` had no in-app control on raw *request volume*.
``login_throttle.py`` (see its own module docstring) counts *failed* attempts
per account and locks the account out, but that is a different defense --
it does nothing to stop a caller from hammering either endpoint with a stream
of syntactically valid requests as fast as the network allows (e.g. guessing
`wa-` key prefixes against ``/auth/token``, or burning CPU/DB cycles on
``/auth/login`` before a single failure is even recorded). WaddleAI's
Cilium-layer network rate limiting (``waddleai.native_rate_limit``) can cover
this in a cluster that has the flag on, but the flag defaults OFF, so this
module is the in-app fallback that holds regardless of cluster config --
mirrors ``login_throttle``'s own defense-in-depth rationale for why an
in-app control exists alongside (not instead of) the network layer.

A single, bounded, thread-safe, in-process token bucket per client, keyed by
source IP plus a hashed, truncated fragment of the presented credential
(never the raw secret -- see ``client_rate_limit_key``) so that one IP
cannot be starved by another tenant's traffic and a single guessed
key/username cannot be retried faster than the configured rate regardless of
which source IP it comes from. Bounded like ``login_throttle._LocalCounter``:
the key space is attacker-controlled, so entries are capped and the
least-recently-touched ones evicted, never allowed to grow without limit.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import ClassVar

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class RateLimitDecision:
    """Outcome of a rate-limit check."""

    allowed: bool
    retry_after_seconds: int = 0


@dataclass(slots=True, frozen=True)
class RateLimiterConfig:
    """Tunables for the auth request-volume limiter, overridable from the environment.

    Default of 10 requests per 60-second window per (IP, credential-fragment)
    pair is deliberately conservative -- a legitimate CLI/CI caller retries a
    single credential far less often than that; a brute-force sweep does not.
    """

    max_requests: int = 10
    window_seconds: int = 60
    enabled: bool = True

    def __post_init__(self) -> None:
        """Reject a non-positive rate or window -- a limiter that cannot fire is not a limiter."""
        if self.max_requests <= 0:
            raise ValueError("max_requests must be greater than 0")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be greater than 0")

    @classmethod
    def from_env(cls) -> RateLimiterConfig:
        """Build a config from ``AUTH_RATE_LIMIT_*`` environment variables.

        Any variable that is unset, non-numeric, or non-positive falls back
        to the safe default rather than disabling the control.
        """
        return cls(
            max_requests=_positive_int("AUTH_RATE_LIMIT_MAX_REQUESTS", 10),
            window_seconds=_positive_int("AUTH_RATE_LIMIT_WINDOW_SECONDS", 60),
            enabled=os.getenv("AUTH_RATE_LIMIT_ENABLED", "true").strip().lower()
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
        logger.warning("rate_limiter: %s=%r is not an integer; using %d", env_name, raw, default)
        return default
    if value <= 0:
        logger.warning("rate_limiter: %s=%d is not positive; using %d", env_name, value, default)
        return default
    return value


@dataclass(slots=True)
class _Bucket:
    """In-process token-bucket state for a single (IP, credential) key."""

    tokens: float
    last_refill: float


class RequestRateLimiter:
    """Bounded, thread-safe, in-process token-bucket rate limiter.

    One bucket per key, refilled continuously at ``max_requests /
    window_seconds`` tokens per second up to a ``max_requests`` cap; each
    request consumes one token. There is no shared-cache-backed mode (unlike
    ``LoginThrottle``) -- this is a best-effort, per-replica volume control,
    not a correctness-critical lockout, so per-process state is an accepted
    tradeoff for the added simplicity.
    """

    _MAX_ENTRIES: ClassVar[int] = 4096

    def __init__(self, config: RateLimiterConfig) -> None:
        """Bind this limiter to *config*."""
        self._config = config
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    def _refill_rate(self) -> float:
        """Return tokens regenerated per second."""
        return self._config.max_requests / self._config.window_seconds

    def _prune(self, reserve: int = 0) -> None:
        """Evict the least-recently-refilled buckets if still over the entry cap.

        *reserve* is the number of buckets the caller is about to insert, so
        the cap holds after the insert rather than one bucket later. Caller
        holds ``self._lock``.
        """
        overflow = len(self._buckets) + reserve - self._MAX_ENTRIES
        if overflow <= 0:
            return
        for key in sorted(self._buckets, key=lambda k: self._buckets[k].last_refill)[:overflow]:
            del self._buckets[key]

    def check(self, key: str) -> RateLimitDecision:
        """Consume one token for *key*, or return the wait time until one is available."""
        if not self._config.enabled:
            return RateLimitDecision(allowed=True)
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                self._prune(reserve=1)
                bucket = _Bucket(tokens=float(self._config.max_requests), last_refill=now)
                self._buckets[key] = bucket
            else:
                elapsed = max(0.0, now - bucket.last_refill)
                bucket.tokens = min(
                    float(self._config.max_requests),
                    bucket.tokens + elapsed * self._refill_rate(),
                )
                bucket.last_refill = now

            if bucket.tokens < 1.0:
                deficit = 1.0 - bucket.tokens
                retry_after = max(1, math.ceil(deficit / self._refill_rate()))
                return RateLimitDecision(allowed=False, retry_after_seconds=retry_after)

            bucket.tokens -= 1.0
            return RateLimitDecision(allowed=True)


def client_rate_limit_key(remote_addr: str | None, credential: str) -> str:
    """Return a stable, bounded, secret-free key for (client IP, credential fragment).

    *credential* (a username or a presented API key) is hashed and truncated
    -- only the client IP is ever kept in the clear, since it is already
    routinely logged elsewhere; a credential value never is.
    """
    ip = remote_addr or "unknown"
    cred_hash = hashlib.sha256(credential.encode("utf-8")).hexdigest()[:16]
    return f"{ip}:{cred_hash}"


_limiter: RequestRateLimiter | None = None
_limiter_lock = threading.Lock()


def get_auth_rate_limiter() -> RequestRateLimiter:
    """Return the process-wide auth rate limiter, building it from env on first use."""
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = RequestRateLimiter(RateLimiterConfig.from_env())
    return _limiter


def reset_auth_rate_limiter(limiter: RequestRateLimiter | None = None) -> None:
    """Replace the process-wide limiter; passing ``None`` rebuilds it from env on next use.

    Exists so tests can install a limiter with known thresholds, and so the
    module-level singleton does not leak state between test cases.
    """
    global _limiter
    with _limiter_lock:
        _limiter = limiter
