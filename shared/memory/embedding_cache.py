"""Content-addressed embedding cache (§6A.3): (model, content_hash) -> vector.

Wraps an EmbeddingManager-shaped backend so identical (model, text) pairs
never re-embed. Valkey is the hot tier; the `embedding_cache` Postgres
table (migration 009b) is the durable tier. `EmbeddingManager.embed` is a
blocking call, so every invocation (cache miss included) dispatches via
`asyncio.to_thread` -- never on the event loop (§3.5).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import orjson

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 7 * 86400  # 7 days


class CachedEmbedder:
    """Valkey -> Postgres -> EmbeddingManager cache-aside wrapper.

    `enabled=False` makes this a transparent passthrough (still routes the
    blocking backend through asyncio.to_thread, but performs no cache
    reads/writes) -- used when the §6A.5 per-key `embedding_cache` config
    is off.
    """

    def __init__(self, valkey: Any, db: Any, manager: Any, enabled: bool = True) -> None:
        """Wire the Valkey/db tiers and the wrapped EmbeddingManager backend."""
        self.valkey = valkey
        self.db = db
        self.manager = manager
        self.enabled = enabled

    @staticmethod
    def _valkey_key(model: str, content_hash: str) -> str:
        return f"waddleai:emb:{model}:{content_hash}"

    @staticmethod
    def _content_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    async def embed(self, model: str, text: str) -> list[float]:
        """Return the embedding vector for `text` under `model`, caching by content hash."""
        if not self.enabled:
            return await asyncio.to_thread(self.manager.embed, text)

        content_hash = self._content_hash(text)
        vkey = self._valkey_key(model, content_hash)

        cached = await self.valkey.get(vkey)
        if cached is not None:
            return orjson.loads(cached)

        row_vector = await asyncio.to_thread(self._select_row, model, content_hash)
        if row_vector is not None:
            await self.valkey.set(vkey, orjson.dumps(row_vector), ex=DEFAULT_TTL_SECONDS)
            return row_vector

        vector = await asyncio.to_thread(self.manager.embed, text)
        await asyncio.to_thread(self._upsert_row, model, content_hash, vector)
        await self.valkey.set(vkey, orjson.dumps(vector), ex=DEFAULT_TTL_SECONDS)
        return vector

    # ------------------------------------------------------------------
    # Postgres access (raw SQL, matching ScratchpadStore's convention).
    # ------------------------------------------------------------------

    def _select_row(self, model: str, content_hash: str) -> list[float] | None:
        rows = self.db.executesql(
            "SELECT embedding_json FROM embedding_cache WHERE model = %s AND content_hash = %s",
            (model, content_hash),
        )
        if not rows or rows[0][0] is None:
            return None
        return orjson.loads(rows[0][0])

    def _upsert_row(self, model: str, content_hash: str, vector: list[float]) -> None:
        self.db.executesql(
            "INSERT INTO embedding_cache (model, content_hash, embedding_json, created_at) "
            "VALUES (%s, %s, %s, now()) "
            "ON CONFLICT (model, content_hash) DO NOTHING",
            (model, content_hash, orjson.dumps(vector).decode("utf-8")),
        )
