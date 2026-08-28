"""Exact Valkey response-cache layer (spec §6.1).

Org-scoped Valkey entries: ``waddleai:cache:exact:{org_id}:{sha256}``.
org_id is baked into both the SHA-256 hash input (shared.cache.keys) *and*
the Valkey key namespace here -- defense in depth so a cross-org hit is
impossible even if a caller ever managed to derive another org's exact key.

Each org gets a byte-quota-bounded LRU: a sorted set
(``waddleai:cache:idx:{org_id}``, score = last-access epoch) tracks access
recency, and a byte counter (``waddleai:cache:bytes:{org_id}``) tracks
current usage against ``org_quota_kb``. Writes exceeding the per-entry
``max_entry_kb`` bound are rejected outright (never written, quota
untouched); writes that would push an org over its total quota evict the
org's least-recently-accessed entries first, and only that org's entries --
eviction never touches another org's namespace.

Atomicity is best-effort (each individual Valkey command is atomic; the
multi-command put/evict sequence here is not wrapped in MULTI/Lua). That is
an acceptable tradeoff for a cache -- a lost race degrades hit rate or
quota accounting slightly, it never leaks data across orgs or corrupts
billing (billing uses token_usage via MeteringBuffer, not this counter).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import orjson

from shared.utils.metrics import get_proxy_metrics

logger = logging.getLogger(__name__)

_NAMESPACE = "waddleai:cache"


@dataclass(slots=True)
class CachedResponse:
    """A cached provider response, ready for direct replay."""

    response: dict
    usage: dict
    stored_at: float


def _entry_key(org_id: int, key: str) -> str:
    return f"{_NAMESPACE}:exact:{org_id}:{key}"


def _idx_key(org_id: int) -> str:
    return f"{_NAMESPACE}:idx:{org_id}"


def _bytes_key(org_id: int) -> str:
    return f"{_NAMESPACE}:bytes:{org_id}"


class ExactCache:
    """Valkey-backed exact response cache with TTL, size bound, and per-org LRU quota."""

    def __init__(self, valkey: Any) -> None:
        """Initialize with an async Valkey/redis client (redis.asyncio-compatible)."""
        self.valkey = valkey

    async def get(self, org_id: int, key: str) -> CachedResponse | None:
        """Fetch a cached response, refreshing its LRU access score on hit."""
        redis_key = _entry_key(org_id, key)
        raw = await self.valkey.get(redis_key)
        if raw is None:
            return None

        await self.valkey.zadd(_idx_key(org_id), {key: time.time()})

        try:
            payload = orjson.loads(raw)
        except orjson.JSONDecodeError:
            logger.error(
                "ExactCache: corrupt payload for org=%s key=%s; treating as miss", org_id, key
            )
            return None

        return CachedResponse(
            response=payload["response"],
            usage=payload["usage"],
            stored_at=payload["stored_at"],
        )

    async def put(
        self,
        org_id: int,
        key: str,
        value: CachedResponse,
        ttl_seconds: int,
        max_entry_kb: int,
        org_quota_kb: int,
    ) -> bool:
        """Write a cached response. Returns False (no write) if it exceeds ``max_entry_kb``."""
        payload = orjson.dumps(
            {"response": value.response, "usage": value.usage, "stored_at": value.stored_at}
        )
        size_bytes = len(payload)
        if size_bytes > max_entry_kb * 1024:
            logger.debug(
                "ExactCache: entry for org=%s exceeds max_entry_kb=%d (%d bytes); not written",
                org_id,
                max_entry_kb,
                size_bytes,
            )
            return False

        redis_key = _entry_key(org_id, key)
        old_raw = await self.valkey.get(redis_key)
        old_size = len(old_raw) if old_raw else 0

        await self._evict_to_fit(
            org_id, needed_bytes=size_bytes - old_size, org_quota_kb=org_quota_kb
        )

        await self.valkey.set(redis_key, payload, ex=ttl_seconds)
        await self.valkey.zadd(_idx_key(org_id), {key: time.time()})
        await self._adjust_bytes(org_id, size_bytes - old_size)
        return True

    async def _current_bytes(self, org_id: int) -> int:
        raw = await self.valkey.get(_bytes_key(org_id))
        if raw is None:
            return 0
        return int(raw)

    async def _adjust_bytes(self, org_id: int, delta: int) -> None:
        current = await self._current_bytes(org_id)
        await self.valkey.set(_bytes_key(org_id), max(0, current + delta))

    async def _evict_to_fit(self, org_id: int, needed_bytes: int, org_quota_kb: int) -> None:
        """Evict this org's least-recently-accessed entries until the new write fits.

        Only ever touches ``org_id``'s own namespace (idx/bytes/entry keys are
        all org-prefixed) -- eviction can never remove another org's entries.
        """
        org_quota_bytes = org_quota_kb * 1024
        current = await self._current_bytes(org_id)

        while current + needed_bytes > org_quota_bytes:
            lru = await self.valkey.zrange(_idx_key(org_id), 0, 0)
            if not lru:
                break
            member = lru[0]
            evicted_key = _entry_key(org_id, member)
            evicted_raw = await self.valkey.get(evicted_key)
            evicted_size = len(evicted_raw) if evicted_raw else 0

            await self.valkey.delete(evicted_key)
            await self.valkey.zrem(_idx_key(org_id), member)
            current = max(0, current - evicted_size)
            await self.valkey.set(_bytes_key(org_id), current)
            get_proxy_metrics().record_cache_eviction(layer="exact")
            logger.debug(
                "ExactCache: evicted LRU entry org=%s key=%s (%d bytes)",
                org_id,
                member,
                evicted_size,
            )
