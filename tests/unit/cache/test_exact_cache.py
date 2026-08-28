"""ExactCache unit tests (Valkey exact response-cache layer, spec §6.1)."""

from shared.cache.exact import CachedResponse, ExactCache
from shared.utils.metrics import get_proxy_metrics


def _counter_value(counter, **labels) -> float:
    """Read a Prometheus counter's current value for a given label set."""
    return counter.labels(**labels)._value.get()


def _cached(text: str = "hello") -> CachedResponse:
    """Cached."""
    return CachedResponse(
        response={"choices": [{"message": {"content": text}}]},
        usage={"input_tokens": 10, "output_tokens": 5},
        stored_at=1000.0,
    )


class TestExactCachePutGet:
    """Tests for exact cache put get."""

    async def test_put_then_get_round_trips(self, fake_valkey):
        """Put then get round trips."""
        cache = ExactCache(fake_valkey)
        value = _cached("round trip")

        ok = await cache.put(
            org_id=1, key="abc", value=value, ttl_seconds=86400, max_entry_kb=256, org_quota_kb=1024
        )
        assert ok is True

        result = await cache.get(org_id=1, key="abc")
        assert result is not None
        assert result.response == value.response
        assert result.usage == value.usage
        assert result.stored_at == value.stored_at

    async def test_get_missing_key_returns_none(self, fake_valkey):
        """Get missing key returns none."""
        cache = ExactCache(fake_valkey)
        result = await cache.get(org_id=1, key="does-not-exist")
        assert result is None

    async def test_ttl_is_honored(self, fake_valkey):
        """Ttl is honored."""
        cache = ExactCache(fake_valkey)
        fake_valkey.now = lambda: 0  # deterministic clock for the time-travel assertion below
        await cache.put(
            org_id=1,
            key="ttl-key",
            value=_cached(),
            ttl_seconds=100,
            max_entry_kb=256,
            org_quota_kb=1024,
        )

        redis_key = "waddleai:cache:exact:1:ttl-key"
        ttl = await fake_valkey.ttl(redis_key)
        assert 0 < ttl <= 100

        # Time-travel past expiry: entry must no longer be gettable.
        fake_valkey.now = lambda: 101
        result = await cache.get(org_id=1, key="ttl-key")
        assert result is None

    async def test_entry_larger_than_max_entry_kb_not_written(self, fake_valkey):
        """Entry larger than max entry kb not written."""
        cache = ExactCache(fake_valkey)
        # ~2KB payload against a 1KB max_entry_kb bound.
        big_value = _cached("x" * 2000)

        ok = await cache.put(
            org_id=1,
            key="too-big",
            value=big_value,
            ttl_seconds=86400,
            max_entry_kb=1,
            org_quota_kb=1024,
        )
        assert ok is False

        result = await cache.get(org_id=1, key="too-big")
        assert result is None
        redis_key = "waddleai:cache:exact:1:too-big"
        assert await fake_valkey.exists(redis_key) == 0

    async def test_quota_eviction_removes_least_recently_accessed_first(self, fake_valkey):
        """Quota eviction removes least recently accessed first."""
        cache = ExactCache(fake_valkey)
        # Each entry ~1KB; quota fits ~2 entries (2KB org_quota_kb rounds to ~2 entries).
        payload = "x" * 900
        # org_quota_kb chosen so writing 3 entries forces eviction of the first.
        quota_kb = 2  # 2048 bytes

        await cache.put(
            org_id=1,
            key="k1",
            value=_cached(payload),
            ttl_seconds=86400,
            max_entry_kb=256,
            org_quota_kb=quota_kb,
        )
        # Access k1 to bump its recency below k2's insert-time score isn't needed;
        # k1 was inserted first so it's the LRU candidate by default.
        await cache.put(
            org_id=1,
            key="k2",
            value=_cached(payload),
            ttl_seconds=86400,
            max_entry_kb=256,
            org_quota_kb=quota_kb,
        )
        await cache.put(
            org_id=1,
            key="k3",
            value=_cached(payload),
            ttl_seconds=86400,
            max_entry_kb=256,
            org_quota_kb=quota_kb,
        )

        # k1 (oldest, least-recently-accessed) should have been evicted.
        assert await cache.get(org_id=1, key="k1") is None
        assert await cache.get(org_id=1, key="k3") is not None

    async def test_eviction_does_not_touch_other_orgs(self, fake_valkey):
        """Eviction does not touch other orgs."""
        cache = ExactCache(fake_valkey)
        payload = "x" * 900
        quota_kb = 2

        await cache.put(
            org_id=1,
            key="a1",
            value=_cached(payload),
            ttl_seconds=86400,
            max_entry_kb=256,
            org_quota_kb=quota_kb,
        )
        await cache.put(
            org_id=2,
            key="b1",
            value=_cached(payload),
            ttl_seconds=86400,
            max_entry_kb=256,
            org_quota_kb=quota_kb,
        )
        await cache.put(
            org_id=1,
            key="a2",
            value=_cached(payload),
            ttl_seconds=86400,
            max_entry_kb=256,
            org_quota_kb=quota_kb,
        )
        await cache.put(
            org_id=1,
            key="a3",
            value=_cached(payload),
            ttl_seconds=86400,
            max_entry_kb=256,
            org_quota_kb=quota_kb,
        )

        # org 1 evicted its own LRU (a1), but org 2's entry is untouched.
        assert await cache.get(org_id=2, key="b1") is not None

    async def test_get_refreshes_access_score(self, fake_valkey):
        """Get refreshes access score."""
        cache = ExactCache(fake_valkey)
        await cache.put(
            org_id=1,
            key="refresh-me",
            value=_cached(),
            ttl_seconds=86400,
            max_entry_kb=256,
            org_quota_kb=1024,
        )

        idx_key = "waddleai:cache:idx:1"
        score_before = await fake_valkey.zscore(idx_key, "refresh-me")

        fake_valkey.now = lambda: (score_before or 0) + 500
        await cache.get(org_id=1, key="refresh-me")

        score_after = await fake_valkey.zscore(idx_key, "refresh-me")
        assert score_after > score_before

    async def test_quota_eviction_increments_evicted_metric(self, fake_valkey):
        """A real LRU/quota eviction increments cache_entries_evicted_total{layer=exact}."""
        cache = ExactCache(fake_valkey)
        metrics = get_proxy_metrics()
        before = _counter_value(metrics.cache_entries_evicted_total, layer="exact")
        payload = "x" * 900
        quota_kb = 2  # fits ~2 entries; the 3rd write forces an eviction

        await cache.put(
            org_id=1,
            key="m1",
            value=_cached(payload),
            ttl_seconds=86400,
            max_entry_kb=256,
            org_quota_kb=quota_kb,
        )
        await cache.put(
            org_id=1,
            key="m2",
            value=_cached(payload),
            ttl_seconds=86400,
            max_entry_kb=256,
            org_quota_kb=quota_kb,
        )
        # No eviction yet -- counter unchanged.
        assert _counter_value(metrics.cache_entries_evicted_total, layer="exact") == before

        await cache.put(
            org_id=1,
            key="m3",
            value=_cached(payload),
            ttl_seconds=86400,
            max_entry_kb=256,
            org_quota_kb=quota_kb,
        )

        assert _counter_value(metrics.cache_entries_evicted_total, layer="exact") == before + 1
