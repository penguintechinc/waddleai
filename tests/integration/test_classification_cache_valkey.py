"""Real-Valkey round trip for the classification NER cache (run by ``make test-valkey``)."""

# ruff: noqa: D103
from __future__ import annotations

from collections.abc import Iterator

import pytest
import redis.asyncio as aredis

from services.classification.app.cache import NerCache
from services.classification.app.engine import RawEntity
from tests.integration.valkey_harness import ValkeyServer, start_valkey


@pytest.fixture(scope="module")
def server() -> Iterator[ValkeyServer]:
    """Module-lived Valkey container (skip/fail policy lives in the harness)."""
    yield from start_valkey()


async def test_real_valkey_roundtrip_ttl_and_no_plaintext(server: ValkeyServer) -> None:
    text = "Jane Q. Smithers 555-867-5309"
    client = aredis.from_url(server.url)
    try:
        cache = NerCache(client, "i" * 40, 90)
        key = cache.key("fp", "org-1", text)
        assert await cache.get(key) is None
        await cache.put(key, [RawEntity("PERSON", 0, 16, 0.9)])
        assert await cache.get(key) == [RawEntity("PERSON", 0, 16, 0.9)]
        assert 0 < await client.ttl(key) <= 90
        stored = await client.get(key)
        assert stored is not None and b"Jane" not in stored and b"Jane" not in key.encode()
        await cache.put(cache.key("fp", "org-1", "clean"), [])
        assert await cache.get(cache.key("fp", "org-1", "clean")) == []
        assert cache.cache_error == 0
        await client.delete(key, cache.key("fp", "org-1", "clean"))
    finally:
        await client.aclose()
