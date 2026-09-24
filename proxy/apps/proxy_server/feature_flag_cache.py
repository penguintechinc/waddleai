"""Cached, async-safe feature-flag resolution for the proxy data plane.

Wraps :func:`shared.utils.feature_flags.is_feature_enabled` -- which swallows
every PostHog failure and returns the caller default -- with the behaviour the
data-plane hot path needs and the release audit (release-audit-2026-09-23, ops
O3/O7) requires:

* **Off the event loop** -- the async :meth:`FeatureFlagsHelper.resolve` runs the
  blocking PostHog lookup in a thread and serves fresh cache hits with no I/O,
  so no request coroutine blocks on flag evaluation.
* **Last-known-value cache** -- a PostHog *outage* degrades to the last resolved
  value instead of snapping to a hardcoded default (the graceful-degradation
  contract in ``critical-rules.md``: "unreachable -> last-known cached value").
* **Fail CLOSED for security flags** -- when a *security* flag
  (``waddleai.security_v2``) cannot be resolved and has never been cached, it
  defaults to **ON** (redact), never OFF, so a flag-server outage can never
  silently disable upstream PII redaction. Non-security flags fall to their
  caller default. Every degradation is logged at WARNING.

The distinction that makes fail-closed correct: an *outage* (PostHog configured
but the call raised) is treated as unresolved; a deliberately *unconfigured*
flag store (no ``POSTHOG_KEY``) or an *undefined* flag are NOT outages -- they
fall to the caller default exactly as before, so test/alpha environments keep
their fail-safe-OFF behaviour and are never forced into redaction.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto

# These are the stable resolution primitives of the shared wrapper. Reusing them
# (rather than reimplementing PostHog client management) is deliberate: the
# shared wrapper cannot surface an outage distinctly -- it returns the caller
# default for both an outage and an undefined flag -- so the proxy resolves once
# more here to tell those two apart, without duplicating client construction.
from shared.utils.feature_flags import (
    _TRUTHY,
    _env_var_name,
    _get_posthog_client,
)

logger = logging.getLogger(__name__)

SECURITY_V2_FLAG = "waddleai.security_v2"

#: Flags whose degradation must fail CLOSED (treat as ON) rather than OFF.
_SECURITY_FLAGS = frozenset({SECURITY_V2_FLAG})

_DEFAULT_TTL_SECONDS = 30.0


class _Outcome(Enum):
    """Classification of a single flag-store lookup."""

    RESOLVED = auto()  #: definite value from an env override or a PostHog answer
    UNRESOLVED = auto()  #: PostHog configured but the lookup raised -- an outage
    DEFAULTED = auto()  #: no flag store, or the flag is undefined -> caller default


@dataclass(slots=True)
class _CacheEntry:
    """One resolved flag value and when it was resolved (monotonic seconds)."""

    value: bool
    resolved_at: float


@dataclass(slots=True)
class FeatureFlagsHelper:
    """Cached feature-flag resolver used by the proxy pipeline and startup wiring."""

    ttl_seconds: float = _DEFAULT_TTL_SECONDS
    _cache: dict[tuple[str, str], _CacheEntry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, compare=False)

    @staticmethod
    def _resolve_raw(flag_key: str, distinct_id: str) -> tuple[_Outcome, bool]:
        """Resolve a flag once, distinguishing a real outage from a deliberate default.

        Never raises: an env override wins first, an unconfigured client yields
        ``DEFAULTED``, a raising client yields ``UNRESOLVED`` (outage), and an
        undefined flag (``feature_enabled`` -> ``None``) yields ``DEFAULTED``.
        """
        env_val = os.getenv(_env_var_name(flag_key))
        if env_val is not None:
            return _Outcome.RESOLVED, env_val.strip().lower() in _TRUTHY

        client = _get_posthog_client()
        if client is None:
            return _Outcome.DEFAULTED, False

        try:
            result = client.feature_enabled(flag_key, distinct_id)
        except Exception as exc:  # noqa: BLE001 -- outage: configured but unreachable
            logger.warning("PostHog flag %s lookup failed: %s", flag_key, exc)
            return _Outcome.UNRESOLVED, False

        if result is None:
            return _Outcome.DEFAULTED, False
        return _Outcome.RESOLVED, bool(result)

    def _fresh(self, key: tuple[str, str], now: float) -> _CacheEntry | None:
        entry = self._cache.get(key)
        if entry is not None and (now - entry.resolved_at) < self.ttl_seconds:
            return entry
        return None

    def _apply(
        self, flag_key: str, distinct_id: str, outcome: _Outcome, value: bool, default: bool
    ) -> bool:
        """Turn a lookup outcome into a returned value, updating the cache and logging."""
        key = (flag_key, distinct_id)
        security = flag_key in _SECURITY_FLAGS

        if outcome is _Outcome.RESOLVED:
            with self._lock:
                self._cache[key] = _CacheEntry(value, time.monotonic())
            return value

        if outcome is _Outcome.DEFAULTED:
            # No flag store / undefined flag -- deliberate, not an outage. Preserve
            # the historical fail-safe-OFF (caller default) behaviour.
            return default

        # UNRESOLVED == flag-store outage: degrade to last-known, else fail closed.
        # A security flag degrading is WARNING-worthy; a non-security flag simply
        # falling to its default is expected graceful degradation -> INFO.
        log = logger.warning if security else logger.info
        with self._lock:
            entry = self._cache.get(key)
        if entry is not None:
            log(
                "feature flag %s unresolvable (flag-store outage); using last-known "
                "cached value=%s for distinct_id=%s",
                flag_key,
                entry.value,
                distinct_id,
            )
            return entry.value

        fallback = True if security else default
        log(
            "feature flag %s unresolvable (flag-store outage) and never cached; "
            "failing %s to %s for distinct_id=%s",
            flag_key,
            "CLOSED (redact)" if security else "to caller default",
            fallback,
            distinct_id,
        )
        return fallback

    async def resolve(
        self, flag_key: str, distinct_id: str | None = None, *, default: bool = False
    ) -> bool:
        """Async, off-event-loop flag resolution with caching and fail-closed security."""
        did = distinct_id or "server"
        key = (flag_key, did)
        now = time.monotonic()
        with self._lock:
            entry = self._fresh(key, now)
        if entry is not None:
            return entry.value
        outcome, value = await asyncio.to_thread(self._resolve_raw, flag_key, did)
        return self._apply(flag_key, did, outcome, value, default)

    def is_feature_enabled(
        self, flag_key: str, distinct_id: str | None = None, *, default: bool = False
    ) -> bool:
        """Synchronous cached resolution for startup / non-async contexts.

        Blocking is acceptable off the request hot path; the async pipeline uses
        :meth:`resolve`. Shares the same cache and fail-closed semantics.
        """
        did = distinct_id or "server"
        key = (flag_key, did)
        now = time.monotonic()
        with self._lock:
            entry = self._fresh(key, now)
        if entry is not None:
            return entry.value
        outcome, value = self._resolve_raw(flag_key, did)
        return self._apply(flag_key, did, outcome, value, default)


__all__ = ["SECURITY_V2_FLAG", "FeatureFlagsHelper"]
