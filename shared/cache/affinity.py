"""Ollama/llama.cpp KV-cache session affinity (spec §6.3).

The local inference fleet keeps its own KV cache per pod; routing the same
conversation (or the same stable prefix, when no conversation id is
available) back to the same pod lets *that* pod's own KV cache be reused
across turns, without WaddleAI managing the cache directly. A Valkey
affinity map (``waddleai:affinity:{org_id}:{session_or_prefix_sha}`` ->
backend/pod identifier) records the mapping with a sliding TTL, refreshed on
every successful lookup (not just on write), so an active conversation's
affinity stays warm and an idle one naturally expires.

This module owns the map and nothing else. Whether a hint is actually
honored at dispatch time is
``shared.utils.request_router.LLMRequestRouter.select_provider``'s
decision -- see that docstring for why a hint is never authoritative.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 1800  # 30 minutes


def _decode(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else value


class SessionAffinityMap:
    """Valkey-backed, org-scoped session/prefix -> backend affinity map."""

    def __init__(self, valkey: Any, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
        """Initialize with an async Valkey client and the sliding-TTL window in seconds."""
        self.valkey = valkey
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _key(org_id: int, session_hash: str) -> str:
        return f"waddleai:affinity:{org_id}:{session_hash}"

    async def record(self, org_id: int, session_hash: str, backend_id: str) -> None:
        """Record (or overwrite) the affinity for a session/prefix hash, org-namespaced."""
        await self.valkey.set(self._key(org_id, session_hash), backend_id, ex=self.ttl_seconds)

    async def lookup(self, org_id: int, session_hash: str) -> str | None:
        """Return the affine backend id for this org's session, sliding its TTL forward.

        A lookup under a different org_id than the one that recorded the
        affinity always misses (the namespace prefix, not just the hash, is
        the isolation boundary -- see shared.cache.exact for the same
        pattern applied to the response cache).
        """
        key = self._key(org_id, session_hash)
        raw = await self.valkey.get(key)
        backend_id = _decode(raw)
        if backend_id is None:
            return None
        await self.valkey.set(key, backend_id, ex=self.ttl_seconds)
        return backend_id
