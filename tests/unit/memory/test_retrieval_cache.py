"""RetrievalResultCache tests: hit/miss/TTL/corpus-version invalidation + org isolation."""

import pytest

from shared.memory.retrieval_cache import RetrievalResultCache
from tests.unit.memory.test_scratchpad import FakeValkey


def _compute_factory(return_value=None):
    calls: list = []

    async def _compute():
        calls.append(1)
        return return_value if return_value is not None else [{"id": "doc-1", "score": 0.9}]

    return _compute, calls


@pytest.fixture
def valkey() -> FakeValkey:
    """Fresh in-memory Valkey double per test."""
    return FakeValkey()


class TestHitMiss:
    """Cache hit/miss and TTL-expiry-equivalent recompute."""

    @pytest.mark.asyncio
    async def test_identical_query_hits_cache_second_time(self, valkey):
        """A repeat query hits the cache -- compute runs once, results match."""
        cache = RetrievalResultCache(valkey)
        compute, calls = _compute_factory()

        r1 = await cache.get_or_compute(1, "memory", "what is the plan", 5, compute)
        r2 = await cache.get_or_compute(1, "memory", "what is the plan", 5, compute)

        assert len(calls) == 1
        assert r1 == r2 == [{"id": "doc-1", "score": 0.9}]

    @pytest.mark.asyncio
    async def test_ttl_expiry_recomputes(self, valkey):
        """A dropped (TTL-expired) key is recomputed on the next call."""
        cache = RetrievalResultCache(valkey, ttl_seconds=300)
        compute, calls = _compute_factory()

        await cache.get_or_compute(1, "memory", "q", 5, compute)
        # Simulate TTL elapsing: real Valkey drops the key after `ex` seconds,
        # which is functionally identical to it never having been cached.
        corpus_ver = await cache._current_corpus_version(1, "memory")
        key = cache._result_key(1, "memory", corpus_ver, "q", 5)
        valkey.store.pop(key, None)

        await cache.get_or_compute(1, "memory", "q", 5, compute)
        assert len(calls) == 2


class TestCorpusVersionInvalidation:
    """A corpus-version bump invalidates only the bumped (org, store)."""

    @pytest.mark.asyncio
    async def test_corpus_bump_invalidates_previously_cached_query(self, valkey):
        """A corpus-version bump forces the next identical query to recompute."""
        cache = RetrievalResultCache(valkey)
        compute, calls = _compute_factory()

        await cache.get_or_compute(1, "memory", "q", 5, compute)
        assert len(calls) == 1

        await cache.bump_corpus_version(1, "memory")

        await cache.get_or_compute(1, "memory", "q", 5, compute)
        assert len(calls) == 2  # recomputed after invalidation

    @pytest.mark.asyncio
    async def test_bump_only_affects_the_bumped_store(self, valkey):
        """Bumping one store's corpus version leaves other stores' cache entries intact."""
        cache = RetrievalResultCache(valkey)
        compute, calls = _compute_factory()

        await cache.get_or_compute(1, "memory", "q", 5, compute)
        await cache.get_or_compute(1, "rag", "q", 5, compute)
        assert len(calls) == 2

        await cache.bump_corpus_version(1, "memory")

        await cache.get_or_compute(1, "memory", "q", 5, compute)  # invalidated -> recompute
        await cache.get_or_compute(1, "rag", "q", 5, compute)  # untouched -> still cached
        assert len(calls) == 3


class TestOrgIsolationSecurity:
    """SECURITY: org A's cached results are never served to org B."""

    @pytest.mark.asyncio
    async def test_org_a_cache_never_served_to_org_b(self, valkey):
        """Org A's cached results are never returned for org B's identical query."""
        cache = RetrievalResultCache(valkey)
        compute_a, calls_a = _compute_factory([{"id": "org-a-secret"}])
        compute_b, calls_b = _compute_factory([{"id": "org-b-result"}])

        result_a = await cache.get_or_compute(1, "memory", "same query", 5, compute_a)
        result_b = await cache.get_or_compute(2, "memory", "same query", 5, compute_b)

        assert result_a == [{"id": "org-a-secret"}]
        assert result_b == [{"id": "org-b-result"}]
        assert len(calls_a) == 1
        assert len(calls_b) == 1  # org B computed its own result, never served org A's


class TestTopKIsolation:
    """Different top_k values are distinct cache entries."""

    @pytest.mark.asyncio
    async def test_different_top_k_are_distinct_entries(self, valkey):
        """The same query at a different top_k recomputes as a separate entry."""
        cache = RetrievalResultCache(valkey)
        compute, calls = _compute_factory()

        await cache.get_or_compute(1, "memory", "q", 5, compute)
        await cache.get_or_compute(1, "memory", "q", 10, compute)
        assert len(calls) == 2


class TestDisabledPassthrough:
    """config/flag off: passthrough, recomputes every call."""

    @pytest.mark.asyncio
    async def test_disabled_recomputes_every_time(self, valkey):
        """enabled=False recomputes every call and writes nothing to Valkey."""
        cache = RetrievalResultCache(valkey, enabled=False)
        compute, calls = _compute_factory()

        await cache.get_or_compute(1, "memory", "q", 5, compute)
        await cache.get_or_compute(1, "memory", "q", 5, compute)
        assert len(calls) == 2
        assert valkey.store == {}
