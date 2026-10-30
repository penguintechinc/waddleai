"""HMAC content-hash cache of raw NER entities for the classification service.

Keys are HMAC-SHA256 digests of org id + analyzed text prefix (a bare hash of
low-entropy PII such as names is dictionary-reversible; the org id partitions
the cache per tenant so a hit cannot reveal another tenant's content); values
hold only offsets/type/score. The cache never fails a request: Valkey errors
and hangs are counted and treated as a miss / no-op.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as aredis

from services.classification.app.config import ClassificationConfig
from services.classification.app.engine import RawEntity
from services.classification.app.obs import get_logger

KEY_PREFIX = "clsf:ner:v1"
MAX_KEYED_CHARS = 2000  # Presidio analyzes only this many code points.
MIN_HMAC_KEY_BYTES = 32
OP_TIMEOUT_SECONDS = 0.25  # bounded wait: a hung Valkey degrades to a miss.

log = get_logger(__name__)


class NerCache:
    """Valkey-backed per-tenant cache of pre-floor, pre-disabled raw entities."""

    def __init__(
        self,
        client: Any,
        hmac_key: str,
        ttl_seconds: int,
        op_timeout_s: float = OP_TIMEOUT_SECONDS,
    ) -> None:
        """Bind a redis.asyncio client, secret HMAC key (>=32 bytes), TTL (0 = disabled)."""
        key_bytes = hmac_key.encode("utf-8")
        weak = len(key_bytes) < MIN_HMAC_KEY_BYTES or not hmac_key.strip()
        if ttl_seconds > 0 and weak:  # a disabled cache never uses the key
            raise ValueError(f"NER cache HMAC key must be >= {MIN_HMAC_KEY_BYTES} bytes")
        if ttl_seconds < 0:
            raise ValueError("NER cache TTL must be >= 0 (0 disables the cache)")
        self._client = client
        self._hmac_key = key_bytes
        self._ttl = ttl_seconds
        self._timeout = op_timeout_s
        self.cache_error: int = 0

    @property
    def enabled(self) -> bool:
        """Report whether caching is on (TTL 0 disables get/put entirely)."""
        return self._ttl > 0

    @classmethod
    def from_config(cls, cfg: ClassificationConfig) -> NerCache:
        """Build a cache from config; the REDIS_URL user should be ACL-limited to ``clsf:*``."""
        client = None
        if cfg.cache_enabled:
            client = aredis.Redis.from_url(
                cfg.redis_url,
                socket_timeout=OP_TIMEOUT_SECONDS,
                socket_connect_timeout=OP_TIMEOUT_SECONDS,
            )
        return cls(client, cfg.ner_cache_hmac_key, cfg.ner_cache_ttl_seconds)

    def key(self, fingerprint: str, org_public_id: str, text: str) -> str:
        """Return ``clsf:ner:v1:{fp_hash}:{HMAC-SHA256(key, org + NUL + text[:2000])}``."""
        fp = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
        body = (
            org_public_id.encode("utf-8")
            + b"\x00"
            + text[:MAX_KEYED_CHARS].encode("utf-8", errors="surrogatepass")
        )
        digest = hmac.new(self._hmac_key, body, hashlib.sha256).hexdigest()
        return f"{KEY_PREFIX}:{fp}:{digest}"

    async def get(self, key: str) -> list[RawEntity] | None:
        """Return cached entities (possibly empty) or None on miss, error, hang or bad shape."""
        if not self.enabled:
            return None
        try:
            blob = await asyncio.wait_for(self._client.get(key), self._timeout)
            if blob is None:
                return None
            return _decode(blob)
        except Exception as exc:
            self._count_error("get", exc)
            return None

    async def put(self, key: str, raw: list[RawEntity]) -> None:
        """Store successful raw entities with the TTL; errors/hangs are counted, never raised."""
        if not self.enabled:
            return
        try:
            blob = json.dumps([[r.entity_type, r.start, r.end, r.score] for r in raw])
            await asyncio.wait_for(self._client.set(key, blob, ex=self._ttl), self._timeout)
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


def _decode(blob: bytes | str) -> list[RawEntity]:
    """Parse and validate a stored value; any out-of-range entry raises (-> counted miss)."""
    out: list[RawEntity] = []
    for t, s, e, c in json.loads(blob):
        if not isinstance(t, str) or isinstance(s, bool) or isinstance(e, bool):
            raise ValueError("malformed cached entity")
        if not (isinstance(s, int) and isinstance(e, int) and isinstance(c, int | float)):
            raise ValueError("malformed cached entity")
        if not (0 <= s <= e) or not math.isfinite(c) or not (0.0 <= c <= 1.0):
            raise ValueError("out-of-range cached entity")
        out.append(RawEntity(t, s, e, float(c)))
    return out
