"""HMAC content-hash cache of raw NER entities for the classification service.

Keys are HMAC-SHA256 digests of the analyzed text prefix (a bare hash of
low-entropy PII such as names is dictionary-reversible); values hold only
offsets/type/score. The cache never fails a request: Valkey errors are counted
and treated as a miss / no-op.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as aredis

from services.classification.app.config import ClassificationConfig
from services.classification.app.engine import RawEntity
from services.classification.app.obs import get_logger

KEY_PREFIX = "clsf:ner:v1"
MAX_KEYED_CHARS = 2000  # Presidio analyzes only this many code points.
DEFAULT_TTL_SECONDS = 3600

log = get_logger(__name__)


class NerCache:
    """Valkey-backed cache of pre-floor, pre-disabled raw entities per content hash."""

    def __init__(self, client: Any, hmac_key: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        """Bind a redis.asyncio client, the secret HMAC key and the entry TTL."""
        if not hmac_key.strip():
            raise ValueError("NER cache HMAC key must not be blank")
        if ttl_seconds < 1:
            raise ValueError("NER cache TTL must be >= 1 second")
        self._client = client
        self._hmac_key = hmac_key.encode("utf-8")
        self._ttl = ttl_seconds
        self.cache_error: int = 0

    @classmethod
    def from_config(cls, cfg: ClassificationConfig) -> NerCache:
        """Build a cache from config; the REDIS_URL user should be ACL-limited to ``clsf:*``."""
        client = aredis.from_url(cfg.redis_url)
        return cls(client, cfg.ner_cache_hmac_key, cfg.ner_cache_ttl_seconds or DEFAULT_TTL_SECONDS)

    def key(self, fingerprint: str, text: str) -> str:
        """Return ``clsf:ner:v1:{fingerprint_hash}:{HMAC-SHA256(key, text[:2000])}``."""
        fp = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
        body = text[:MAX_KEYED_CHARS].encode("utf-8", errors="surrogatepass")
        digest = hmac.new(self._hmac_key, body, hashlib.sha256).hexdigest()
        return f"{KEY_PREFIX}:{fp}:{digest}"

    async def get(self, key: str) -> list[RawEntity] | None:
        """Return cached entities (possibly empty) or None on a miss or any error."""
        try:
            blob = await self._client.get(key)
            if blob is None:
                return None
            return [RawEntity(str(t), int(s), int(e), float(c)) for t, s, e, c in json.loads(blob)]
        except Exception as exc:
            self._count_error("get", exc)
            return None

    async def put(self, key: str, raw: list[RawEntity]) -> None:
        """Store successful raw entities with the TTL; errors are counted, never raised."""
        try:
            blob = json.dumps([[r.entity_type, r.start, r.end, r.score] for r in raw])
            await self._client.set(key, blob, ex=self._ttl)
        except Exception as exc:
            self._count_error("put", exc)

    async def get_or_compute(
        self, key: str, compute: Callable[[], Awaitable[list[RawEntity]]]
    ) -> list[RawEntity]:
        """Serve from cache, else run ``compute`` and cache only its successful result.

        A raising ``compute`` (degraded/engine error) propagates and writes nothing.
        """
        cached = await self.get(key)
        if cached is not None:
            return cached
        raw = await compute()
        await self.put(key, raw)
        return raw

    def _count_error(self, op: str, exc: Exception) -> None:
        """Count and log a cache failure by type only (never key, text or secret)."""
        self.cache_error += 1
        log.warning("ner_cache_error", op=op, error_type=type(exc).__name__)
