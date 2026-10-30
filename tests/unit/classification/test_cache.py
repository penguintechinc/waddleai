"""Tests for the HMAC content-hash NER cache (PII hygiene is the point)."""

# ruff: noqa: D103
from __future__ import annotations

import asyncio
import hashlib
import time

import fakeredis
import pytest

from services.classification.app.cache import NerCache
from services.classification.app.engine import RawEntity

TEXT = "Call Jane Q. Smithers on 555-867-5309 about the merger"
K1 = "a" * 40
K2 = "b" * 40
ORG = "11111111-1111-1111-1111-111111111111"
RAW = [RawEntity("PERSON", 5, 21, 0.85), RawEntity("PHONE_NUMBER", 25, 37, 0.7)]


@pytest.fixture
def client() -> fakeredis.FakeAsyncRedis:
    """Fresh in-memory async Valkey stand-in."""
    return fakeredis.FakeAsyncRedis()


@pytest.fixture
def cache(client: fakeredis.FakeAsyncRedis) -> NerCache:
    """Cache with a known HMAC key and TTL."""
    return NerCache(client, K1, 120)


def test_key_contains_no_plaintext_and_is_hmac(cache: NerCache) -> None:
    key = cache.key("fp", ORG, TEXT)
    assert key.startswith("clsf:ner:v1:")
    assert "Jane" not in key and "555" not in key
    digest = key.rsplit(":", 1)[1]
    assert len(digest) == 64
    assert digest != hashlib.sha256(TEXT.encode()).hexdigest()
    assert key != NerCache(fakeredis.FakeAsyncRedis(), K2, 60).key("fp", ORG, TEXT)


def test_key_only_covers_first_2000_chars(cache: NerCache) -> None:
    base = "a" * 2000
    assert cache.key("fp", ORG, base + "X") == cache.key("fp", ORG, base + "Y")
    assert cache.key("fp", ORG, base) != cache.key("fp", ORG, "b" + base[1:])


def test_fingerprint_change_invalidates(cache: NerCache) -> None:
    assert cache.key("model-a:1", ORG, TEXT) != cache.key("model-a:2", ORG, TEXT)


def test_blank_key_or_ttl_rejected(client: fakeredis.FakeAsyncRedis) -> None:
    with pytest.raises(ValueError):
        NerCache(client, "short", 60)
    with pytest.raises(ValueError):
        NerCache(client, K1, -1)


async def test_same_text_different_org_does_NOT_share(cache: NerCache) -> None:  # noqa: N802
    other = "22222222-2222-2222-2222-222222222222"
    await cache.put(cache.key("fp", ORG, TEXT), RAW)
    assert cache.key("fp", other, TEXT) != cache.key("fp", ORG, TEXT)
    assert await cache.get(cache.key("fp", other, TEXT)) is None  # no cross-tenant oracle


async def test_same_org_same_text_hits(cache: NerCache) -> None:
    await cache.put(cache.key("fp", ORG, TEXT), RAW)
    assert await cache.get(cache.key("fp", ORG, TEXT)) == RAW


def test_org_id_not_ambiguous_with_text(cache: NerCache) -> None:
    assert cache.key("fp", "ab", "c") != cache.key("fp", "a", "bc")
    assert ORG not in cache.key("fp", ORG, TEXT)
    assert cache.key("fp", "", TEXT).startswith("clsf:ner:v1:")  # empty org still sane


async def test_value_contains_no_text(cache: NerCache, client: fakeredis.FakeAsyncRedis) -> None:
    key = cache.key("fp", ORG, TEXT)
    await cache.put(key, RAW)
    stored = await client.get(key)
    assert stored is not None
    for needle in ("Jane", "Smithers", "555-867-5309", "merger", TEXT):
        assert needle.encode() not in stored
    for k in await client.keys("*"):
        kb = k.encode() if isinstance(k, str) else k
        assert b"Jane" not in kb and kb.startswith(b"clsf:")


async def test_degraded_results_never_cached(
    cache: NerCache, client: fakeredis.FakeAsyncRedis
) -> None:
    async def boom() -> list[RawEntity]:
        raise RuntimeError("engine degraded")

    with pytest.raises(RuntimeError):
        await cache.get_or_compute(cache.key("fp", ORG, TEXT), boom)
    assert await client.keys("*") == []


async def test_get_or_compute_caches_success_once(cache: NerCache) -> None:
    calls = 0

    async def ok() -> list[RawEntity]:
        nonlocal calls
        calls += 1
        return RAW

    key = cache.key("fp", ORG, TEXT)
    assert await cache.get_or_compute(key, ok) == RAW
    assert await cache.get_or_compute(key, ok) == RAW
    assert calls == 1


class _Down:
    async def get(self, *_: object, **__: object) -> bytes:
        raise ConnectionError("valkey down")

    async def set(self, *_: object, **__: object) -> None:
        raise ConnectionError("valkey down")


async def test_valkey_down_is_miss_not_error() -> None:
    cache = NerCache(_Down(), K1, 60)
    key = cache.key("fp", ORG, TEXT)
    assert await cache.get(key) is None
    await cache.put(key, RAW)  # no raise
    assert cache.cache_error == 2


async def test_corrupt_value_is_counted_miss(
    cache: NerCache, client: fakeredis.FakeAsyncRedis
) -> None:
    key = cache.key("fp", ORG, TEXT)
    await client.set(key, b"not-json")
    assert await cache.get(key) is None
    assert cache.cache_error == 1


async def test_empty_result_is_cached(cache: NerCache) -> None:
    key = cache.key("fp", ORG, "nothing sensitive here")
    assert await cache.get(key) is None  # miss
    await cache.put(key, [])
    assert await cache.get(key) == []  # cached empty, distinct from miss


async def test_ttl_applied(cache: NerCache, client: fakeredis.FakeAsyncRedis) -> None:
    key = cache.key("fp", ORG, TEXT)
    await cache.put(key, RAW)
    assert 0 < await client.ttl(key) <= 120


def test_from_config_builds_cache() -> None:
    from services.classification.app.config import ClassificationConfig

    cfg = ClassificationConfig.from_env(
        {
            "NER_CACHE_TTL_SECONDS": "60",
            "NER_CACHE_HMAC_KEY": "k" * 40,
            "REDIS_URL": "redis://localhost:6379/0",
        }
    )
    assert NerCache.from_config(cfg).key("fp", ORG, "x").startswith("clsf:ner:v1:")


@pytest.mark.parametrize(
    "poison",
    [
        b'[["PERSON", 5, 3, 0.5]]',  # end < start
        b'[["PERSON", -1, 3, 0.5]]',  # negative offset
        b'[["PERSON", 0, 3, 7.5]]',  # score > 1
        b'[["PERSON", 0, 3, NaN]]',  # NaN
        b'[["PERSON", 0, 3, "x"]]',  # wrong type
        b'[["PERSON", true, 3, 0.5]]',  # bool offset
        b"[[1, 0, 3, 0.5]]",  # non-str type
        b'[["PERSON", 0, 3]]',  # wrong arity
    ],
)
async def test_poisoned_value_is_miss(
    cache: NerCache, client: fakeredis.FakeAsyncRedis, poison: bytes
) -> None:
    key = cache.key("fp", ORG, TEXT)
    await client.set(key, poison)
    assert await cache.get(key) is None
    assert cache.cache_error == 1


async def test_ttl_zero_disables_cache(client: fakeredis.FakeAsyncRedis) -> None:
    cache = NerCache(client, K1, 0)
    key = cache.key("fp", ORG, TEXT)
    await cache.put(key, RAW)
    assert await client.keys("*") == []
    assert await cache.get(key) is None
    assert cache.cache_error == 0


def test_from_config_ttl_zero_disabled() -> None:
    from services.classification.app.config import ClassificationConfig

    assert not NerCache.from_config(ClassificationConfig.from_env({})).enabled


class _Hung:
    async def get(self, *_: object, **__: object) -> bytes:
        await asyncio.sleep(30)
        return b"[]"

    async def set(self, *_: object, **__: object) -> None:
        await asyncio.sleep(30)


async def test_hung_valkey_degrades_to_miss_quickly() -> None:
    cache = NerCache(_Hung(), K1, 60, op_timeout_s=0.05)
    key = cache.key("fp", ORG, TEXT)
    start = time.monotonic()
    assert await cache.get(key) is None
    await cache.put(key, RAW)
    assert time.monotonic() - start < 2
    assert cache.cache_error == 2
