"""Tests for the HMAC content-hash NER cache (PII hygiene is the point)."""

# ruff: noqa: D103
from __future__ import annotations

import hashlib

import fakeredis
import pytest

from services.classification.app.cache import NerCache
from services.classification.app.engine import RawEntity

TEXT = "Call Jane Q. Smithers on 555-867-5309 about the merger"
RAW = [RawEntity("PERSON", 5, 21, 0.85), RawEntity("PHONE_NUMBER", 25, 37, 0.7)]


@pytest.fixture
def client() -> fakeredis.FakeAsyncRedis:
    """Fresh in-memory async Valkey stand-in."""
    return fakeredis.FakeAsyncRedis()


@pytest.fixture
def cache(client: fakeredis.FakeAsyncRedis) -> NerCache:
    """Cache with a known HMAC key and TTL."""
    return NerCache(client, "k1-secret", 120)


def test_key_contains_no_plaintext_and_is_hmac(cache: NerCache) -> None:
    key = cache.key("fp", TEXT)
    assert key.startswith("clsf:ner:v1:")
    assert "Jane" not in key and "555" not in key
    digest = key.rsplit(":", 1)[1]
    assert len(digest) == 64
    assert digest != hashlib.sha256(TEXT.encode()).hexdigest()
    assert key != NerCache(fakeredis.FakeAsyncRedis(), "k2-secret").key("fp", TEXT)


def test_key_only_covers_first_2000_chars(cache: NerCache) -> None:
    base = "a" * 2000
    assert cache.key("fp", base + "X") == cache.key("fp", base + "Y")
    assert cache.key("fp", base) != cache.key("fp", "b" + base[1:])


def test_fingerprint_change_invalidates(cache: NerCache) -> None:
    assert cache.key("model-a:1", TEXT) != cache.key("model-a:2", TEXT)


def test_blank_key_or_ttl_rejected(client: fakeredis.FakeAsyncRedis) -> None:
    with pytest.raises(ValueError):
        NerCache(client, "  ")
    with pytest.raises(ValueError):
        NerCache(client, "k", 0)


async def test_same_text_different_org_shares_entry(cache: NerCache) -> None:
    key = cache.key("fp", TEXT)
    await cache.put(key, RAW)  # org A's raw, pre-floor/pre-disabled
    assert await cache.get(cache.key("fp", TEXT)) == RAW  # org B hits the same entry


async def test_value_contains_no_text(cache: NerCache, client: fakeredis.FakeAsyncRedis) -> None:
    key = cache.key("fp", TEXT)
    await cache.put(key, RAW)
    stored = await client.get(key)
    assert stored is not None
    for needle in ("Jane", "Smithers", "555-867-5309", "merger", TEXT):
        assert needle.encode() not in stored
    for k in await client.keys("*"):
        assert b"Jane" not in k and k.startswith(b"clsf:")


async def test_degraded_results_never_cached(
    cache: NerCache, client: fakeredis.FakeAsyncRedis
) -> None:
    async def boom() -> list[RawEntity]:
        raise RuntimeError("engine degraded")

    with pytest.raises(RuntimeError):
        await cache.get_or_compute(cache.key("fp", TEXT), boom)
    assert await client.keys("*") == []


async def test_get_or_compute_caches_success_once(cache: NerCache) -> None:
    calls = 0

    async def ok() -> list[RawEntity]:
        nonlocal calls
        calls += 1
        return RAW

    key = cache.key("fp", TEXT)
    assert await cache.get_or_compute(key, ok) == RAW
    assert await cache.get_or_compute(key, ok) == RAW
    assert calls == 1


class _Down:
    async def get(self, *_: object, **__: object) -> bytes:
        raise ConnectionError("valkey down")

    async def set(self, *_: object, **__: object) -> None:
        raise ConnectionError("valkey down")


async def test_valkey_down_is_miss_not_error() -> None:
    cache = NerCache(_Down(), "k")
    key = cache.key("fp", TEXT)
    assert await cache.get(key) is None
    await cache.put(key, RAW)  # no raise
    assert cache.cache_error == 2


async def test_corrupt_value_is_counted_miss(
    cache: NerCache, client: fakeredis.FakeAsyncRedis
) -> None:
    key = cache.key("fp", TEXT)
    await client.set(key, b"not-json")
    assert await cache.get(key) is None
    assert cache.cache_error == 1


async def test_empty_result_is_cached(cache: NerCache) -> None:
    key = cache.key("fp", "nothing sensitive here")
    assert await cache.get(key) is None  # miss
    await cache.put(key, [])
    assert await cache.get(key) == []  # cached empty, distinct from miss


async def test_ttl_applied(cache: NerCache, client: fakeredis.FakeAsyncRedis) -> None:
    key = cache.key("fp", TEXT)
    await cache.put(key, RAW)
    assert 0 < await client.ttl(key) <= 120


def test_from_config_builds_cache() -> None:
    from services.classification.app.config import ClassificationConfig

    cfg = ClassificationConfig.from_env(
        {
            "NER_CACHE_TTL_SECONDS": "60",
            "NER_CACHE_HMAC_KEY": "abc",
            "REDIS_URL": "redis://localhost:6379/0",
        }
    )
    assert NerCache.from_config(cfg).key("fp", "x").startswith("clsf:ner:v1:")
