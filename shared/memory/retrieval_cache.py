"""Valkey-only retrieval-result cache (§6A.3): (org, query_hash, corpus_version, top_k).

Unlike embedding_cache (a pure content-addressed function cache with no
org column), this cache DOES hold readable search results -- it is
strictly org-scoped, and cross-org isolation is a security test, not just
a correctness one. There is no durable tier: results are derivable from
the underlying store at any time (§6A.5).

Corpus version is a monotonic Valkey counter bumped on every write/delete
to the underlying memory store. A bump changes every subsequent cache key
for that (org, store), so previously-cached entries become unreachable --
invalidation is "the keyspace moved on", not an explicit purge; stale
entries simply age out via TTL.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import orjson

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 300


class RetrievalResultCache:
    """Valkey cache-aside wrapper for vector-search results, org-isolated."""

    def __init__(
        self, valkey: Any, ttl_seconds: int = DEFAULT_TTL_SECONDS, enabled: bool = True
    ) -> None:
        """Wire the Valkey client, result TTL, and the enable gate (transparent passthrough off)."""
        self.valkey = valkey
        self.ttl_seconds = ttl_seconds
        self.enabled = enabled

    @staticmethod
    def _corpus_version_key(org_id: int, store: str) -> str:
        return f"waddleai:corpus_ver:{org_id}:{store}"

    async def _current_corpus_version(self, org_id: int, store: str) -> int:
        raw = await self.valkey.get(self._corpus_version_key(org_id, store))
        return int(raw) if raw is not None else 0

    async def bump_corpus_version(self, org_id: int, store: str) -> int:
        """Invalidate every cached result for (org_id, store). Call on any write/delete."""
        return await self.valkey.incr(self._corpus_version_key(org_id, store))

    @staticmethod
    def _query_hash(query: str) -> str:
        return hashlib.sha256(query.encode("utf-8")).hexdigest()

    def _result_key(self, org_id: int, store: str, corpus_ver: int, query: str, top_k: int) -> str:
        return f"waddleai:rr:{org_id}:{store}:{corpus_ver}:{self._query_hash(query)}:{top_k}"

    async def get_or_compute(
        self,
        org_id: int,
        store: str,
        query: str,
        top_k: int,
        compute: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Return cached results for (org_id, store, query, top_k), computing on miss.

        `compute` is only invoked on a miss. Results are cached as JSON;
        callers pass a `compute` whose return value is orjson-serializable
        (e.g. a list of plain dicts, not ORM objects).
        """
        if not self.enabled:
            return await compute()

        corpus_ver = await self._current_corpus_version(org_id, store)
        key = self._result_key(org_id, store, corpus_ver, query, top_k)

        cached = await self.valkey.get(key)
        if cached is not None:
            return orjson.loads(cached)

        result = await compute()
        await self.valkey.set(key, orjson.dumps(result, default=str), ex=self.ttl_seconds)
        return result
