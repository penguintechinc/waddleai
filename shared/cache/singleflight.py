"""Cache-stampede (thundering-herd) protection shared by every layer (ops O11).

A burst of identical cache misses must never all dispatch upstream (or all
call the embedder) at once. Two coordination layers are combined:

1. :class:`InProcessSingleFlight` -- a per-worker ``asyncio`` de-dup keyed by
   cache key. The first caller for a key becomes the *leader* and is
   responsible for eventually calling :meth:`InProcessSingleFlight.resolve`
   or :meth:`InProcessSingleFlight.discard`; concurrent callers for the same
   key within the same process become *followers* and await the leader's
   result directly -- no Valkey round trip at all for the N-identical-
   requests-in-one-process case.
2. :class:`StampedeLease` -- a cross-process Valkey lease (``SET NX PX``) so
   that across N worker processes/pods, only the lease holder populates the
   cache; losers wait briefly (jittered polling, :func:`wait_for_value`) and
   fall through to computing their own value if the lease expires or the
   wait times out.

:func:`guard_miss` is the single entry point every cache layer (exact,
semantic, and the response-cache facade) calls on a miss -- it ties both
layers together and NEVER blocks indefinitely or fails the caller's request:
every degraded path (kill-switch off, no Valkey, Valkey error, lease denied
and the wait times out) resolves to "you are the leader, compute your own
value," just without the stampede-avoidance optimization for that one
caller. :func:`jittered_ttl` is the paired write-side helper so hot keys
don't all expire in lockstep.

Opt-out kill-switch: ``waddleai.disable-cache-singleflight`` (unseen/OFF =
mechanism ON, ON = legacy "every miss dispatches independently" behavior).
:func:`resolve_singleflight_enabled` duck-types against the same ``features``
helper shape the proxy pipeline already uses
(``proxy.apps.proxy_server.pipeline.stages._resolve_flag``: async
``resolve()`` if present, else a sync ``is_feature_enabled()`` moved off the
loop via a worker thread) instead of importing that helper directly --
``shared/cache`` has no dependency today on ``proxy.apps.proxy_server``, and
this keeps it that way (a ``None`` features, or any resolution failure,
fails open to "mechanism enabled"). Callers resolve once per request and
pass the plain ``bool`` into :func:`guard_miss`/:func:`embed_with_budget`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from shared.utils.metrics import get_proxy_metrics

logger = logging.getLogger(__name__)

#: Opt-out kill-switch flag key (see module docstring).
DISABLE_SINGLEFLIGHT_FLAG = "waddleai.disable-cache-singleflight"

#: How long a Valkey lease is held before it auto-expires (PX), bounding how
#: long a crashed/slow leader can block followers. Should roughly track the
#: slowest expected upstream call (LLM completion, embedding, etc.).
DEFAULT_LEASE_TTL_SECONDS = float(os.getenv("CACHE_STAMPEDE_LEASE_TTL_SECONDS", "30"))

#: How long a follower waits for the leader's value before falling through
#: and computing its own -- "a few seconds," per spec, never indefinite.
DEFAULT_WAIT_TIMEOUT_SECONDS = float(os.getenv("CACHE_STAMPEDE_WAIT_TIMEOUT_SECONDS", "3"))

#: Base jittered-poll interval while a follower waits on the Valkey path.
DEFAULT_POLL_INTERVAL_SECONDS = float(os.getenv("CACHE_STAMPEDE_POLL_INTERVAL_SECONDS", "0.1"))

#: +/- fraction applied to write-side TTLs so hot keys don't expire in lockstep.
DEFAULT_TTL_JITTER_FRACTION = float(os.getenv("CACHE_TTL_JITTER_FRACTION", "0.1"))

#: Embedding-call latency budget for the semantic cache (ops O11, Gemini note).
DEFAULT_SEMANTIC_CACHE_EMBED_TIMEOUT_MS = float(os.getenv("SEMANTIC_CACHE_EMBED_TIMEOUT_MS", "300"))


def is_singleflight_enabled(is_enabled: Callable[[str], bool] | None = None) -> bool:
    """Sync resolution of the kill-switch for non-async callers (tests, scripts).

    ``is_enabled`` is a caller-supplied ``flag_key -> is_disabled`` callable;
    absent, or raising, both fail open to "mechanism ON." Request-path code
    should prefer :func:`resolve_singleflight_enabled` (async, off-loop).
    """
    if is_enabled is None:
        return True
    try:
        return not is_enabled(DISABLE_SINGLEFLIGHT_FLAG)
    except Exception:  # noqa: BLE001 - any flag-store failure fails open
        logger.warning(
            "singleflight: %s lookup failed; defaulting to mechanism ON",
            DISABLE_SINGLEFLIGHT_FLAG,
        )
        return True


async def resolve_singleflight_enabled(features: Any, distinct_id: str | None = None) -> bool:
    """Async, off-event-loop resolution of the kill-switch for request-path callers.

    Duck-types against the ``features`` helper shape already used by
    ``proxy.apps.proxy_server.pipeline.stages._resolve_flag`` (async
    ``resolve(flag, distinct_id, default=...)`` if present, else a sync
    ``is_feature_enabled(flag, distinct_id, default=...)`` dispatched via
    ``asyncio.to_thread`` so it never blocks the event loop) rather than
    importing that helper directly -- see module docstring. ``features=None``
    or any resolution failure fails open to "mechanism enabled."
    """
    if features is None:
        return True
    try:
        resolve = getattr(features, "resolve", None)
        if callable(resolve):
            disabled = await resolve(DISABLE_SINGLEFLIGHT_FLAG, distinct_id, default=False)
        else:
            is_feature_enabled = getattr(features, "is_feature_enabled", None)
            if not callable(is_feature_enabled):
                return True
            disabled = await asyncio.to_thread(
                is_feature_enabled, DISABLE_SINGLEFLIGHT_FLAG, distinct_id, default=False
            )
    except Exception:  # noqa: BLE001 - any flag-store failure fails open
        logger.warning(
            "singleflight: %s lookup failed; defaulting to mechanism ON",
            DISABLE_SINGLEFLIGHT_FLAG,
        )
        return True
    return not disabled


def jittered_ttl(base_seconds: int, jitter_fraction: float = DEFAULT_TTL_JITTER_FRACTION) -> int:
    """Return ``base_seconds`` +/- ``jitter_fraction`` (uniform), floored at 1.

    Applied on every cache *write* (exact/semantic/response layers) so a
    burst of identical writes made around the same time don't all expire at
    the same instant and re-trigger a synchronized stampede later.
    """
    if base_seconds <= 0 or jitter_fraction <= 0:
        return max(base_seconds, 0)
    spread = base_seconds * jitter_fraction
    # Non-cryptographic jitter (cache TTL spread, never a security boundary).
    jittered = base_seconds + random.uniform(-spread, spread)  # noqa: S311 # nosec B311
    return max(1, int(round(jittered)))


class InProcessSingleFlight:
    """Per-worker ``asyncio`` de-dup of concurrent callers for the same cache key.

    Not a cache itself -- just coordination. Owned per cache-layer instance
    (``ExactCache``, ``SemanticCache``, ...) so it lives exactly as long as
    the layer it protects.
    """

    __slots__ = ("_pending",)

    def __init__(self) -> None:
        """Initialize an empty key -> (future, created_at) registry."""
        self._pending: dict[str, tuple[asyncio.Future, float]] = {}

    def enter(self, key: str, max_age_seconds: float) -> tuple[bool, asyncio.Future]:
        """Register as leader or follower for ``key``.

        Returns ``(is_leader, future)``. A leader must eventually call
        :meth:`resolve` or :meth:`discard` with the *same* future object so
        followers unblock. An entry older than ``max_age_seconds`` (a dead
        leader that never resolved -- e.g. its request errored out before
        reaching the write-back) is treated as stale and replaced rather
        than stranding every subsequent caller behind a future that will
        never complete.
        """
        entry = self._pending.get(key)
        if entry is not None:
            future, created_at = entry
            if not future.done() and (time.monotonic() - created_at) < max_age_seconds:
                return False, future
        future = asyncio.get_event_loop().create_future()
        self._pending[key] = (future, time.monotonic())
        return True, future

    def resolve(self, key: str, future: asyncio.Future, value: Any) -> None:
        """Leader-only: complete ``future`` with ``value`` and clear the registry entry."""
        if not future.done():
            future.set_result(value)
        entry = self._pending.get(key)
        if entry is not None and entry[0] is future:
            del self._pending[key]

    def discard(self, key: str, future: asyncio.Future) -> None:
        """Leader-only: complete ``future`` with ``None`` (no value materialized)."""
        self.resolve(key, future, None)


def _lock_key(cache_key: str) -> str:
    return f"{cache_key}:lock"


class StampedeLease:
    """Cross-process Valkey lease: one holder computes, everyone else waits or falls through."""

    def __init__(self, valkey: Any) -> None:
        """Initialize with an async Valkey/redis client (redis.asyncio-compatible)."""
        self.valkey = valkey

    async def try_acquire(self, cache_key: str, ttl_seconds: float, token: str) -> bool:
        """Attempt ``SET key:lock NX PX <ttl>``; True iff this caller now holds the lease."""
        lock_key = _lock_key(cache_key)
        px = max(1, int(ttl_seconds * 1000))
        try:
            acquired = await self.valkey.set(lock_key, token, nx=True, px=px)
        except TypeError:
            # Minimal test doubles may not implement nx/px -- fall back to a
            # plain TTL'd set, which only degrades the mutual-exclusion
            # guarantee for that double, never for the real Valkey client.
            acquired = await self.valkey.set(lock_key, token, ex=max(1, int(ttl_seconds)))
        return bool(acquired)

    async def release(self, cache_key: str, token: str) -> None:
        """Best-effort release: deletes the lock only if we still hold it (token matches).

        Not atomic (get-then-delete, not a Lua check-and-delete) -- the same
        documented tradeoff as ``shared.cache.exact``'s eviction sequence: a
        lost race here means the lease simply expires on its own TTL a
        little later, it never lets two leaders write conflicting data.
        """
        lock_key = _lock_key(cache_key)
        try:
            current = await self.valkey.get(lock_key)
        except Exception:  # noqa: BLE001 - release is always best-effort
            logger.warning("StampedeLease: release check failed for %s", cache_key)
            return
        current_token = current.decode() if isinstance(current, bytes) else current
        if current_token == token:
            await self.valkey.delete(lock_key)


async def wait_for_value(
    fetch: Callable[[], Awaitable[Any]],
    timeout_seconds: float,
    poll_interval_seconds: float,
    jitter_fraction: float = 0.3,
) -> Any | None:
    """Jittered-poll ``fetch()`` until it returns non-``None`` or ``timeout_seconds`` elapses.

    Used by a stampede follower to watch for the leader's write without a
    pub/sub channel (this codebase has no existing Valkey pub/sub usage to
    extend -- see module docstring).
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        value = await fetch()
        if value is not None:
            return value
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        spread = poll_interval_seconds * jitter_fraction
        # Non-cryptographic jitter (poll-interval spread, never a security boundary).
        jittered_interval = poll_interval_seconds + random.uniform(  # noqa: S311 # nosec B311
            -spread, spread
        )
        delay = max(0.01, min(remaining, jittered_interval))
        await asyncio.sleep(delay)


@dataclass(slots=True)
class MissGuardResult:
    """Outcome of :func:`guard_miss`: whether this caller should compute, and any value found."""

    is_leader: bool
    cached: Any | None
    lease_token: str | None
    in_process_future: asyncio.Future | None = field(default=None)


async def guard_miss(
    *,
    cache_key: str,
    fetch_cached: Callable[[], Awaitable[Any]],
    in_process: InProcessSingleFlight,
    valkey: Any | None,
    lease_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
    wait_timeout_seconds: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    metrics_layer: str = "exact",
    enabled: bool = True,
) -> MissGuardResult:
    """Single-flight guard for one cache-key miss, shared by exact/semantic/response layers.

    ``enabled`` is the caller's already-resolved kill-switch state (see
    :func:`resolve_singleflight_enabled`) -- resolved once per request by the
    caller rather than per-call here, since flag resolution is itself async.

    Flow for the caller:

    * ``is_leader=True, cached=None`` -- proceed to compute (dispatch
      upstream / embed+query) and write back; if a Valkey lease was taken
      (``lease_token`` is not ``None``) release it after writing, and if an
      in-process future was handed back, resolve it with the final value so
      any in-process followers unblock immediately instead of waiting out
      their timeout.
    * ``is_leader=False, cached=<value>`` -- a leader (in this process or
      another) already computed the value; use it directly, equivalent to a
      cache hit.

    Degrades safely and loudly (never raises, never blocks past
    ``wait_timeout_seconds`` plus one lease TTL check): kill-switch ON,
    Valkey absent, or a Valkey error all return ``is_leader=True`` so the
    caller just computes its own value, same as before this mechanism
    existed.
    """
    metrics = get_proxy_metrics()
    if not enabled:
        metrics.record_cache_lookup(layer=metrics_layer, result="bypass")
        return MissGuardResult(is_leader=True, cached=None, lease_token=None)

    is_in_process_leader, future = in_process.enter(cache_key, lease_ttl_seconds)
    if not is_in_process_leader:
        try:
            # asyncio.shield() is required here, not just asyncio.wait_for(future, ...)
            # directly: `future` is *shared* by every follower racing on this
            # key, and plain wait_for cancels the awaited future itself on
            # timeout -- the first follower to time out would cancel the
            # leader's shared future out from under every other follower
            # still waiting on it (and the eventual `future.set_result()`
            # call). shield() isolates the timeout's cancellation to this
            # follower's own wait, leaving the shared future untouched.
            cached = await asyncio.wait_for(asyncio.shield(future), timeout=wait_timeout_seconds)
        except TimeoutError:
            cached = None
        if cached is not None:
            metrics.record_cache_lookup(layer=metrics_layer, result="stampede_wait")
            return MissGuardResult(is_leader=False, cached=cached, lease_token=None)
        metrics.record_cache_lookup(layer=metrics_layer, result="stampede_fallthrough")
        return MissGuardResult(is_leader=True, cached=None, lease_token=None)

    if valkey is None:
        # No cross-process coordination available -- in-process-only
        # single-flight still applies (this caller is already the
        # in-process leader above); degrade gracefully per spec item 7.
        return MissGuardResult(
            is_leader=True, cached=None, lease_token=None, in_process_future=future
        )

    token = secrets.token_hex(8)
    try:
        acquired = await StampedeLease(valkey).try_acquire(cache_key, lease_ttl_seconds, token)
    except Exception:  # noqa: BLE001 - a Valkey outage degrades, never fails the request
        logger.warning(
            "guard_miss: Valkey lease acquire failed for %s; degrading to in-process only",
            cache_key,
        )
        return MissGuardResult(
            is_leader=True, cached=None, lease_token=None, in_process_future=future
        )

    if acquired:
        return MissGuardResult(
            is_leader=True, cached=None, lease_token=token, in_process_future=future
        )

    cached = await wait_for_value(fetch_cached, wait_timeout_seconds, poll_interval_seconds)
    in_process.discard(cache_key, future)
    if cached is not None:
        metrics.record_cache_lookup(layer=metrics_layer, result="stampede_wait")
        return MissGuardResult(is_leader=False, cached=cached, lease_token=None)
    metrics.record_cache_lookup(layer=metrics_layer, result="stampede_fallthrough")
    return MissGuardResult(is_leader=True, cached=None, lease_token=None)


async def dedup_embed(
    in_process: InProcessSingleFlight,
    key: str,
    compute: Callable[[], Awaitable[list[float]]],
    *,
    max_age_seconds: float = 10.0,
) -> list[float]:
    """In-process single-flight for the embedding call itself (ops O11, Gemini note).

    Concurrent requests for the *same normalized prompt* in one worker share
    one embedding call instead of each hitting Ollama independently -- cheap
    de-dup that needs no Valkey round trip, matching the exact-cache-key
    in-process case. ``max_age_seconds`` is intentionally short (default
    10s, well under the embed-timeout-bypass budget's practical retry
    window) since an embedding call either returns quickly or the caller
    bypasses the semantic cache entirely (see
    ``shared.cache.semantic.SemanticCache``).
    """
    is_leader, future = in_process.enter(key, max_age_seconds)
    if not is_leader:
        result = await future
        return list(result) if result is not None else []
    try:
        value = await compute()
    except Exception:
        in_process.discard(key, future)
        raise
    in_process.resolve(key, future, value)
    return value


async def embed_with_budget(
    compute: Callable[[], Awaitable[list[float]]],
    *,
    timeout_ms: float = DEFAULT_SEMANTIC_CACHE_EMBED_TIMEOUT_MS,
    is_healthy: Callable[[], bool] | None = None,
    metrics_layer: str = "semantic",
) -> list[float] | None:
    """Run ``compute()`` (an embedding call) under a latency budget; ``None`` means bypass.

    Bypasses (returns ``None`` without raising) rather than blocking the
    semantic-cache lookup behind a saturated embedder when either:

    * ``is_healthy`` is provided and reports unhealthy/circuit-open, or
    * the call itself exceeds ``timeout_ms``.

    A bypass is recorded on ``cache_lookups_total{layer=semantic,
    result=bypass}`` -- the caller falls through to a semantic-cache miss
    (which, since the exact cache is always checked first, is the common
    and already-expected path for most requests).
    """
    metrics = get_proxy_metrics()
    if is_healthy is not None:
        try:
            healthy = is_healthy()
        except Exception:  # noqa: BLE001 - a broken health check must not block lookups
            healthy = True
        if not healthy:
            logger.debug("%s: embedder reported unhealthy; bypassing semantic cache", metrics_layer)
            metrics.record_cache_lookup(layer=metrics_layer, result="bypass")
            return None

    try:
        return await asyncio.wait_for(compute(), timeout=timeout_ms / 1000.0)
    except TimeoutError:
        logger.warning(
            "%s: embedding exceeded %.0fms budget; bypassing semantic cache",
            metrics_layer,
            timeout_ms,
        )
        metrics.record_cache_lookup(layer=metrics_layer, result="bypass")
        return None


__all__ = [
    "DISABLE_SINGLEFLIGHT_FLAG",
    "DEFAULT_LEASE_TTL_SECONDS",
    "DEFAULT_WAIT_TIMEOUT_SECONDS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_TTL_JITTER_FRACTION",
    "DEFAULT_SEMANTIC_CACHE_EMBED_TIMEOUT_MS",
    "is_singleflight_enabled",
    "resolve_singleflight_enabled",
    "jittered_ttl",
    "InProcessSingleFlight",
    "StampedeLease",
    "wait_for_value",
    "MissGuardResult",
    "guard_miss",
    "dedup_embed",
    "embed_with_budget",
]
