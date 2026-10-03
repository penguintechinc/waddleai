"""Off-event-loop, Valkey-backed API-key auth-lookup cache for the proxy hot path.

release-audit-2026-10-02 O7-a / O11: every authenticated proxy request used to
run a synchronous PyDAL query *and* a bcrypt verify *and* a synchronous
``UPDATE last_used`` directly on the Hypercorn event loop (via
``shared.auth.rbac.RBACManager.authenticate_api_key``), with zero cache in
front of the DB read. bcrypt alone costs 50-100ms of CPU; at any real request
rate that collapses a worker's throughput to the tens of requests per second.

This module fixes both findings together:

* **Zero sync DB/bcrypt calls remain on the request path** -- every blocking
  operation (the PyDAL lookup, the bcrypt verify, the debounced ``last_used``
  write) runs on a dedicated, bounded ``ThreadPoolExecutor``
  (``PROXY_AUTH_EXECUTOR_WORKERS``, default 8), never the default
  ``asyncio.to_thread`` pool shared with the rest of the process.
* **A cache fronts the DB read** -- keyed by the non-secret ``key_id`` segment
  of the ``wa-{key_id}-{secret}`` contract (never the secret, never a hash of
  the secret), Valkey-backed with an in-process fallback when Valkey is
  unreachable. bcrypt verification still happens on *every* request, hit or
  miss -- only the DB round trip is skipped on a hit.
* **``last_used`` is debounced** -- written at most once per key per
  ``PROXY_AUTH_LAST_USED_INTERVAL_SECONDS`` (default 60s), in the background,
  never inline on the request path.
* **A kill switch** (``waddleai.disable-auth-cache``, opt-out convention: OFF
  means the cache mechanism is on) bypasses the cache entirely on a flag flip,
  falling back to a per-request DB+bcrypt lookup that is still executor-offloaded
  -- the event-loop fix is not optional, only the cache is.

Revocation: deleting or disabling a key through the management service's
``DELETE /api/v1/proxy-keys/<key_id>`` (``services/management/app/api/v1/proxy_keys.py``)
best-effort-deletes this cache's Valkey entry. The cache TTL
(``PROXY_AUTH_CACHE_TTL_SECONDS``, default 60s) is the documented worst-case
staleness bound when that invalidation itself cannot reach Valkey, or when a
sibling proxy replica is serving the same key_id from its own in-process
fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

from passlib.hash import bcrypt

from shared.auth.rbac import (
    AuthenticationError,
    RBACManager,
    UserContext,
    auth_cache_key,
    parse_wa_key_id,
)

if TYPE_CHECKING:
    from proxy.apps.proxy_server.feature_flag_cache import FeatureFlagsHelper
    from shared.utils.metrics import WaddleAIMetrics

logger = logging.getLogger(__name__)

#: Opt-out kill-switch (unseen/OFF = cache mechanism ON, ON = legacy
#: uncached-but-still-offloaded DB lookup). See module docstring.
AUTH_CACHE_DISABLE_FLAG = "waddleai.disable-auth-cache"

_ENV_EXECUTOR_WORKERS = "PROXY_AUTH_EXECUTOR_WORKERS"
_ENV_CACHE_TTL_SECONDS = "PROXY_AUTH_CACHE_TTL_SECONDS"
_ENV_NEGATIVE_TTL_SECONDS = "PROXY_AUTH_NEGATIVE_CACHE_TTL_SECONDS"
_ENV_LAST_USED_INTERVAL_SECONDS = "PROXY_AUTH_LAST_USED_INTERVAL_SECONDS"

_DEFAULT_EXECUTOR_WORKERS = 8
_DEFAULT_CACHE_TTL_SECONDS = 60.0
_DEFAULT_NEGATIVE_TTL_SECONDS = 5.0
_DEFAULT_LAST_USED_INTERVAL_SECONDS = 60.0


def _float_env(name: str, default: float) -> float:
    """Parse a float env var, falling back to ``default`` on anything unparsable."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default %s", name, raw, default)
        return default


# ---------------------------------------------------------------------------
# Dedicated, bounded executor -- deliberately NOT asyncio.to_thread's shared
# default pool. Auth is latency-sensitive and runs on every request; sharing
# a pool with arbitrary other blocking work in the process would let an
# unrelated slow `to_thread` call starve auth, and vice versa.
# ---------------------------------------------------------------------------

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def get_auth_executor() -> ThreadPoolExecutor:
    """Return the process-wide, lazily-built auth executor (Borg-style singleton)."""
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                workers = max(
                    1, int(os.getenv(_ENV_EXECUTOR_WORKERS, str(_DEFAULT_EXECUTOR_WORKERS)))
                )
                _executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="proxy-auth")
    return _executor


async def run_in_auth_executor(fn: Any, *args: Any) -> Any:
    """Run a blocking callable on the dedicated auth executor, off the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(get_auth_executor(), fn, *args)


# ---------------------------------------------------------------------------
# Cached record shape
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class CachedKeyRecord:
    """Snapshot of the DB fields needed to verify a request without a DB round trip.

    Deliberately carries the bcrypt ``key_hash`` (never the plaintext secret
    or credential) plus the user fields ``RBACManager.build_user_context``
    needs -- enough to authenticate every subsequent request for this
    ``key_id`` entirely from cache until the TTL expires or an explicit
    invalidation arrives.
    """

    key_record_id: int
    key_hash: str
    user_id: int
    username: str
    role: str
    organization_id: int
    managed_orgs: list[int]
    user_enabled: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for ``json.dumps``."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CachedKeyRecord:
        """Reconstruct from the dict produced by :meth:`to_dict`."""
        return cls(
            key_record_id=payload["key_record_id"],
            key_hash=payload["key_hash"],
            user_id=payload["user_id"],
            username=payload["username"],
            role=payload["role"],
            organization_id=payload["organization_id"],
            managed_orgs=list(payload.get("managed_orgs") or []),
            user_enabled=bool(payload.get("user_enabled", True)),
        )


def _normalize_managed_orgs(raw: Any) -> list[int]:
    """Coerce PyDAL's ``managed_orgs`` (str, list, or None) into a plain ``list[int]``."""
    if not raw:
        return []
    if isinstance(raw, str):
        return [int(x.strip()) for x in raw.split(",") if x.strip()]
    return [int(x) for x in raw]


class _Outcome(Enum):
    """Classification of one cache lookup -- drives both behavior and the metric label."""

    HIT = auto()
    MISS = auto()
    NEGATIVE = auto()
    BYPASS = auto()


# ---------------------------------------------------------------------------
# Cache backend: Valkey-backed with an in-process fallback
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ApiKeyAuthCache:
    """Valkey-backed lookup cache with an in-process fallback, keyed by ``key_id``.

    Never fails open to "allowed": every read error (Valkey unreachable,
    malformed payload) is treated as a cache miss, which always falls
    through to the real DB+bcrypt check. The in-process fallback is a plain
    dict pruned lazily on read (bounded by the tenant's own key_id
    cardinality, not by an eviction policy) so a Valkey outage degrades to
    per-process caching rather than no caching at all.

    ``clock`` is the TTL time source, injectable for deterministic tests
    (construct with a fake ``clock`` callable and advance it explicitly --
    never monkeypatch the global ``time`` module: this class's own ``clock``
    calls are the sanctioned seam, and mixing a frozen fake clock with any
    real-clock-stamped entry written before the patch was applied is exactly
    the bug this seam avoids).
    """

    valkey: Any | None
    ttl_seconds: float = field(
        default_factory=lambda: _float_env(_ENV_CACHE_TTL_SECONDS, _DEFAULT_CACHE_TTL_SECONDS)
    )
    negative_ttl_seconds: float = field(
        default_factory=lambda: _float_env(_ENV_NEGATIVE_TTL_SECONDS, _DEFAULT_NEGATIVE_TTL_SECONDS)
    )
    clock: Callable[[], float] = time.monotonic
    _local: dict[str, tuple[float, str]] = field(default_factory=dict)
    _local_lock: threading.Lock = field(default_factory=threading.Lock, compare=False)

    async def get(self, key_id: str) -> CachedKeyRecord | None | bool:
        """Return a :class:`CachedKeyRecord`, ``True`` for a negative hit, or ``None`` on a miss."""
        raw = await self._read(key_id)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            logger.warning("Auth cache: malformed payload for key_id=%s: %s", key_id, exc)
            return None
        if payload.get("kind") == "negative":
            return True
        try:
            return CachedKeyRecord.from_dict(payload)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Auth cache: malformed record for key_id=%s: %s", key_id, exc)
            return None

    async def set(self, key_id: str, record: CachedKeyRecord) -> None:
        """Cache a verified key record for ``ttl_seconds``."""
        raw = json.dumps({"kind": "record", **record.to_dict()})
        await self._write(key_id, raw, self.ttl_seconds)

    async def set_negative(self, key_id: str) -> None:
        """Negative-cache an unknown ``key_id`` for ``negative_ttl_seconds``.

        Keeps a key-id enumeration sweep from hitting the DB for every guess.
        """
        raw = json.dumps({"kind": "negative"})
        await self._write(key_id, raw, self.negative_ttl_seconds)

    async def invalidate(self, key_id: str) -> None:
        """Drop this process's cache entry for ``key_id`` (Valkey and local fallback)."""
        with self._local_lock:
            self._local.pop(key_id, None)
        if self.valkey is not None:
            try:
                await self.valkey.delete(auth_cache_key(key_id))
            except Exception as exc:  # noqa: BLE001 -- never let invalidation raise
                logger.warning(
                    "Auth cache: Valkey invalidate failed for key_id=%s: %s", key_id, exc
                )

    async def _read(self, key_id: str) -> str | None:
        if self.valkey is not None:
            try:
                value = await self.valkey.get(auth_cache_key(key_id))
                if value is not None:
                    # The proxy's Valkey client is constructed with
                    # decode_responses=True (str in, str out), but a bare
                    # redis.asyncio client (or a test double) may still
                    # return bytes -- decode defensively rather than
                    # `str(b"...")`, which would wrap the bytes repr.
                    return value.decode() if isinstance(value, bytes) else str(value)
            except Exception as exc:  # noqa: BLE001 -- fail open to DB, never to "allowed"
                logger.warning("Auth cache: Valkey read failed for key_id=%s: %s", key_id, exc)
        return self._read_local(key_id)

    async def _write(self, key_id: str, raw: str, ttl: float) -> None:
        self._write_local(key_id, raw, ttl)
        if self.valkey is not None:
            try:
                await self.valkey.set(auth_cache_key(key_id), raw, ex=max(1, int(ttl)))
            except Exception as exc:  # noqa: BLE001 -- cache write failure must not fail auth
                logger.warning("Auth cache: Valkey write failed for key_id=%s: %s", key_id, exc)

    def _read_local(self, key_id: str) -> str | None:
        now = self.clock()
        with self._local_lock:
            entry = self._local.get(key_id)
            if entry is None:
                return None
            expires_at, raw = entry
            if expires_at < now:
                del self._local[key_id]
                return None
            return raw

    def _write_local(self, key_id: str, raw: str, ttl: float) -> None:
        with self._local_lock:
            self._local[key_id] = (self.clock() + ttl, raw)


# ---------------------------------------------------------------------------
# Blocking helpers -- always dispatched through run_in_auth_executor
# ---------------------------------------------------------------------------


def _verify_cached(credential: str, key_hash: str) -> bool:
    """bcrypt-verify ``credential`` against a cached hash.

    Blocking -- always run via the executor, never called inline.
    """
    return bool(bcrypt.verify(credential, key_hash))


def _db_lookup_and_verify(
    rbac: RBACManager, credential: str, key_id: str
) -> tuple[CachedKeyRecord, bool] | None:
    """Fetch the key+user row and bcrypt-verify ``credential``, entirely off the event loop.

    Returns ``None`` when ``key_id`` itself is unknown (negative-cacheable).
    Otherwise returns ``(record, secret_ok)`` -- the record is always safe to
    cache (it carries only the already-hashed secret), independent of
    whether this particular request's ``secret_ok`` passed.
    """
    found = rbac.fetch_key_and_user(key_id)
    if found is None:
        return None
    key_record, user = found
    secret_ok = bool(bcrypt.verify(credential, key_record.key_hash))
    record = CachedKeyRecord(
        key_record_id=key_record.id,
        key_hash=key_record.key_hash,
        user_id=user.id,
        username=user.username,
        role=user.role,
        organization_id=user.organization_id,
        managed_orgs=_normalize_managed_orgs(user.managed_orgs),
        user_enabled=bool(user.enabled),
    )
    return record, secret_ok


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ApiKeyAuthenticator:
    """Cache-fronted, executor-offloaded ``wa-``/``sk-`` API-key authentication.

    The single entry point the proxy's HTTP auth hot path
    (``main.py:_api_key_verifier``, ``main.py:get_current_user``) calls for
    every request carrying a raw API-key credential. Never used by the gRPC
    surface, which already runs on its own thread pool (``grpc_identity_resolver``
    stays on the original, synchronous ``RBACManager.authenticate_api_key``).

    ``clock`` is the debounce time source, injectable for deterministic
    tests (pass the SAME fake ``clock`` callable to both this and ``cache``
    so cache-TTL and debounce logic advance together under one controllable
    clock -- never monkeypatch the global ``time`` module, which also
    freezes asyncio's own event-loop clock and hangs any real
    ``asyncio.sleep``/executor await in the same test).
    """

    rbac: RBACManager
    cache: ApiKeyAuthCache
    metrics: WaddleAIMetrics | None = None
    features: FeatureFlagsHelper | None = None
    clock: Callable[[], float] = time.monotonic
    last_used_interval_seconds: float = field(
        default_factory=lambda: _float_env(
            _ENV_LAST_USED_INTERVAL_SECONDS, _DEFAULT_LAST_USED_INTERVAL_SECONDS
        )
    )
    _last_used_at: dict[int, float] = field(default_factory=dict)
    _last_used_lock: threading.Lock = field(default_factory=threading.Lock, compare=False)
    #: Holds references to in-flight background `_touch()` tasks so they
    #: cannot be garbage-collected mid-run (a well-known asyncio footgun:
    #: `asyncio.ensure_future` alone does not keep the task alive) -- each
    #: discards itself via `add_done_callback` once it completes.
    _background_tasks: set[asyncio.Task[None]] = field(default_factory=set)

    async def authenticate(self, credential: str) -> UserContext:
        """Authenticate a raw ``wa-``/``sk-`` credential, caching the DB lookup by ``key_id``.

        ``outcome`` is a local to this call -- never shared instance state --
        so concurrent requests on the same event loop can never cross-write
        each other's metric label (a bug this implementation specifically
        avoids: the obvious "stash the outcome on self for a shared
        ``finally``" shortcut races under real concurrency).
        """
        key_id = parse_wa_key_id(credential)

        bypass = False
        if self.features is not None:
            bypass = await self.features.resolve(AUTH_CACHE_DISABLE_FLAG, distinct_id="server")

        start = self.clock()
        outcome = _Outcome.BYPASS if bypass else _Outcome.MISS
        try:
            if bypass:
                return await self._authenticate_uncached(credential, key_id, populate_cache=False)

            cached = await self.cache.get(key_id)
            if cached is True:
                outcome = _Outcome.NEGATIVE
                raise AuthenticationError("Invalid API key")
            if isinstance(cached, CachedKeyRecord):
                outcome = _Outcome.HIT
                return await self._authenticate_from_cache(credential, cached)

            outcome = _Outcome.MISS
            return await self._authenticate_uncached(credential, key_id, populate_cache=True)
        finally:
            self._record(outcome, self.clock() - start)

    async def _authenticate_from_cache(
        self, credential: str, cached: CachedKeyRecord
    ) -> UserContext:
        """Verify a cache hit: bcrypt still runs (off the loop), the DB does not."""
        verified = await run_in_auth_executor(_verify_cached, credential, cached.key_hash)
        if not verified:
            raise AuthenticationError("Invalid API key")
        context = self.rbac.build_user_context(
            user_id=cached.user_id,
            username=cached.username,
            role=cached.role,
            organization_id=cached.organization_id,
            managed_orgs=cached.managed_orgs,
            api_key_id=cached.key_record_id,
        )
        self._schedule_last_used_touch(cached.key_record_id)
        return context

    async def _authenticate_uncached(
        self,
        credential: str,
        key_id: str,
        *,
        populate_cache: bool,
    ) -> UserContext:
        """DB+bcrypt fallback, always executor-offloaded regardless of cache state."""
        outcome_result = await run_in_auth_executor(
            _db_lookup_and_verify, self.rbac, credential, key_id
        )
        if outcome_result is None:
            if populate_cache:
                await self.cache.set_negative(key_id)
            raise AuthenticationError("Invalid API key")

        record, secret_ok = outcome_result
        if populate_cache:
            await self.cache.set(key_id, record)
        if not secret_ok:
            raise AuthenticationError("Invalid API key")

        context = self.rbac.build_user_context(
            user_id=record.user_id,
            username=record.username,
            role=record.role,
            organization_id=record.organization_id,
            managed_orgs=record.managed_orgs,
            api_key_id=record.key_record_id,
        )
        self._schedule_last_used_touch(record.key_record_id)
        return context

    def _schedule_last_used_touch(self, key_record_id: int) -> None:
        """Fire a debounced, background ``last_used`` write -- never inline on the request path."""
        now = self.clock()
        with self._last_used_lock:
            due = self._last_used_at.get(key_record_id, 0.0) + self.last_used_interval_seconds
            if now < due:
                return
            self._last_used_at[key_record_id] = now

        async def _touch() -> None:
            try:
                await run_in_auth_executor(self.rbac.touch_api_key_last_used, key_record_id)
                if self.metrics is not None:
                    self.metrics.record_database_operation("update", "api_keys", success=True)
            except Exception as exc:  # noqa: BLE001 -- a missed last_used write must not break auth
                logger.warning(
                    "Auth cache: last_used touch failed for key_record_id=%s: %s",
                    key_record_id,
                    exc,
                )
                if self.metrics is not None:
                    self.metrics.record_database_operation("update", "api_keys", success=False)

        task = asyncio.ensure_future(_touch())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _record(self, outcome: _Outcome, duration: float) -> None:
        """Record the lookup's latency/result, plus the DB RED metric on a path that hit the DB."""
        if self.metrics is None:
            return
        self.metrics.record_auth_lookup(outcome.name.lower(), duration)
        if outcome in (_Outcome.MISS, _Outcome.BYPASS):
            self.metrics.record_database_operation(
                "select", "api_keys", duration=duration, success=True
            )


__all__ = [
    "AUTH_CACHE_DISABLE_FLAG",
    "ApiKeyAuthCache",
    "ApiKeyAuthenticator",
    "CachedKeyRecord",
    "get_auth_executor",
    "run_in_auth_executor",
]
