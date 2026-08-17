"""Tokenizer-length cache (§6A.4): cached token counts of stable blocks.

Keyed ``(model, sha256(text))`` in Valkey only -- token counts are purely
derivable from (model, text), so there is no durable tier (§6A.5). Consumed
by the summarizer (threshold counting) and dedup store (savings
accounting). Counting itself always delegates to a caller-supplied
callable (typically the resolved connector's ``count_tokens``, tiktoken
fallback included) -- this module owns caching only, never tokenization.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

DEFAULT_TTL_SECONDS = 7 * 86400  # 7 days


class TokenLenCache:
    """Valkey-only cache of token counts keyed (model, content_hash)."""

    def __init__(self, valkey: Any, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        """Wire the Valkey client and the TTL applied to every cached count."""
        self.valkey = valkey
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _key(model: str, text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"waddleai:toklen:{model}:{digest}"

    async def count(self, model: str, text: str, counter: Callable[[str], Awaitable[int]]) -> int:
        """Return the token count for `text` under `model`, caching the result.

        `counter` is only invoked on a cache miss. Exceptions from `counter`
        propagate uncached -- a failed count must never be memoized as if
        it succeeded.
        """
        key = self._key(model, text)
        cached = await self.valkey.get(key)
        if cached is not None:
            return int(cached)

        value = await counter(text)
        await self.valkey.set(key, str(value), ex=self.ttl_seconds)
        return value
