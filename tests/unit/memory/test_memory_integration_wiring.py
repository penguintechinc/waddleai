"""PgvectorMemoryStore <-> CachedEmbedder / RetrievalResultCache wiring tests.

Verifies store_memory/search_memories route through the injected caches
(call-routing assertions) without needing a real Postgres+pgvector
backend -- the SQL correctness of the underlying query is covered by the
pre-existing memory_integration tests; this file covers only that the
new caches are actually consulted.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.utils.memory_integration import MemoryEntry, PgvectorMemoryStore


def _entry(**overrides) -> MemoryEntry:
    base = dict(
        id="1",
        user_id=10,
        organization_id=1,
        session_id="sess-a",
        content="the user prefers dark mode",
        metadata={},
        embedding=None,
        created_at=datetime(2026, 8, 12),
        relevance_score=0.0,
        scope_type="user",
        author_user_id=10,
    )
    base.update(overrides)
    return MemoryEntry(**base)


class TestStoreMemoryWiring:
    """store_memory routes embeddings through CachedEmbedder and bumps corpus version."""

    @pytest.mark.asyncio
    async def test_store_memory_routes_embedding_through_embed_cache(self):
        """store_memory embeds via embed_cache.embed, never embedding_manager.embed directly."""
        write_db = MagicMock()
        write_db.executesql = MagicMock(return_value=None)
        embed_cache = MagicMock()
        embed_cache.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        embedding_manager = MagicMock()
        embedding_manager.config.model = "nomic-embed-text"
        # If store_memory bypassed embed_cache, this would be called instead.
        embedding_manager.embed = MagicMock(
            side_effect=AssertionError("should not call embedding_manager.embed directly")
        )

        store = PgvectorMemoryStore(write_db, embedding_manager, embed_cache=embed_cache)
        ok = await store.store_memory(_entry())

        assert ok is True
        embed_cache.embed.assert_awaited_once_with("nomic-embed-text", "the user prefers dark mode")

    @pytest.mark.asyncio
    async def test_store_memory_bumps_corpus_version(self):
        """store_memory bumps the entry's organization_id corpus version."""
        write_db = MagicMock()
        write_db.executesql = MagicMock(return_value=None)
        embedding_manager = MagicMock()
        embedding_manager.embed = MagicMock(return_value=[0.1, 0.2])
        retrieval_cache = MagicMock()
        retrieval_cache.bump_corpus_version = AsyncMock()

        store = PgvectorMemoryStore(write_db, embedding_manager, retrieval_cache=retrieval_cache)
        await store.store_memory(_entry(organization_id=42))

        retrieval_cache.bump_corpus_version.assert_awaited_once_with(42, "memory")


class TestSearchMemoriesWiring:
    """search_memories routes through RetrievalResultCache, partitioned by scope/user."""

    @pytest.mark.asyncio
    async def test_search_memories_routes_through_retrieval_cache(self):
        """search_memories calls retrieval_cache.get_or_compute with org_id and query."""
        write_db = MagicMock()
        embedding_manager = MagicMock()
        retrieval_cache = MagicMock()

        async def fake_get_or_compute(org_id, store, query, top_k, compute):
            # Exercise `compute` so we can assert it's the real search path,
            # but return through the cache primitive's own contract.
            return await compute()

        retrieval_cache.get_or_compute = AsyncMock(side_effect=fake_get_or_compute)

        store = PgvectorMemoryStore(write_db, embedding_manager, retrieval_cache=retrieval_cache)
        store._search_memories_uncached = AsyncMock(return_value=[_entry()])

        results = await store.search_memories(
            "what theme do they like", user_id=10, organization_id=1
        )

        retrieval_cache.get_or_compute.assert_awaited_once()
        args = retrieval_cache.get_or_compute.await_args.args
        assert args[0] == 1  # organization_id
        assert args[2] == "what theme do they like"  # query
        assert len(results) == 1
        assert results[0].content == "the user prefers dark mode"

    @pytest.mark.asyncio
    async def test_search_memories_without_retrieval_cache_calls_uncached_directly(self):
        """With no retrieval_cache configured, search calls _search_memories_uncached directly."""
        write_db = MagicMock()
        embedding_manager = MagicMock()

        store = PgvectorMemoryStore(write_db, embedding_manager, retrieval_cache=None)
        store._search_memories_uncached = AsyncMock(return_value=[_entry()])

        results = await store.search_memories("q", user_id=10, organization_id=1)
        store._search_memories_uncached.assert_awaited_once()
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_personal_scope_partitions_cache_by_user(self):
        """Different users' scope='user' searches must not share a cache store namespace."""
        write_db = MagicMock()
        embedding_manager = MagicMock()
        retrieval_cache = MagicMock()
        seen_stores = []

        async def fake_get_or_compute(org_id, store, query, top_k, compute):
            seen_stores.append(store)
            return await compute()

        retrieval_cache.get_or_compute = AsyncMock(side_effect=fake_get_or_compute)
        store = PgvectorMemoryStore(write_db, embedding_manager, retrieval_cache=retrieval_cache)
        store._search_memories_uncached = AsyncMock(return_value=[])

        await store.search_memories("q", user_id=10, organization_id=1, scope="user")
        await store.search_memories("q", user_id=20, organization_id=1, scope="user")

        assert seen_stores[0] != seen_stores[1]


class TestDeleteAndClearWiring:
    """clear_memories/delete_memory bump the owning org's corpus version."""

    @pytest.mark.asyncio
    async def test_clear_memories_bumps_corpus_version(self):
        """clear_memories bumps the given organization_id's corpus version."""
        write_db = MagicMock()
        write_db.executesql = MagicMock(return_value=None)
        embedding_manager = MagicMock()
        retrieval_cache = MagicMock()
        retrieval_cache.bump_corpus_version = AsyncMock()

        store = PgvectorMemoryStore(write_db, embedding_manager, retrieval_cache=retrieval_cache)
        await store.clear_memories(user_id=10, organization_id=7)

        retrieval_cache.bump_corpus_version.assert_awaited_once_with(7, "memory")

    @pytest.mark.asyncio
    async def test_delete_memory_bumps_owning_orgs_corpus_version(self):
        """delete_memory looks up the owning org and bumps its corpus version."""
        write_db = MagicMock()
        write_db.executesql = MagicMock(side_effect=[[(7,)], None])  # SELECT org_id, then DELETE
        embedding_manager = MagicMock()
        retrieval_cache = MagicMock()
        retrieval_cache.bump_corpus_version = AsyncMock()

        store = PgvectorMemoryStore(write_db, embedding_manager, retrieval_cache=retrieval_cache)
        ok = await store.delete_memory("123")

        assert ok is True
        retrieval_cache.bump_corpus_version.assert_awaited_once_with(7, "memory")
