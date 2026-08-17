"""CachedEmbedder tests: re-embed avoidance, Postgres fallback, isolation by model."""

import orjson
import pytest

from shared.memory.embedding_cache import CachedEmbedder
from tests.unit.memory.test_scratchpad import FakeValkey


class FakeEmbeddingManager:
    """Deterministic in-process embedding stub -- no real model, no I/O."""

    def __init__(self, dimension: int = 4):
        """Start with an empty call log and the given vector dimension."""
        self.dimension = dimension
        self.calls: list = []

    def embed(self, text: str):
        """Record the call and return a deterministic vector for the text."""
        self.calls.append(text)
        # Deterministic per-text vector so equality assertions are meaningful.
        seed = sum(text.encode("utf-8"))
        return [float((seed + i) % 97) for i in range(self.dimension)]


class FakeEmbeddingCacheDB:
    """Mirrors the embedding_cache table shape: (model, content_hash) -> embedding_json only."""

    def __init__(self):
        """Start with an empty in-memory row store."""
        self.rows: dict = {}  # (model, content_hash) -> embedding_json str

    def executesql(self, sql: str, params):
        """Dispatch a SELECT/INSERT against the in-memory row store by SQL prefix."""
        s = sql.strip().upper()
        if s.startswith("SELECT"):
            model, content_hash = params
            value = self.rows.get((model, content_hash))
            return [(value,)] if value is not None else []
        if s.startswith("INSERT"):
            model, content_hash, embedding_json = params
            self.rows.setdefault((model, content_hash), embedding_json)
            return None
        raise AssertionError(f"unexpected SQL in FakeEmbeddingCacheDB: {sql}")


@pytest.fixture
def valkey() -> FakeValkey:
    """Fresh in-memory Valkey double per test."""
    return FakeValkey()


@pytest.fixture
def db() -> FakeEmbeddingCacheDB:
    """Fresh in-memory embedding_cache double per test."""
    return FakeEmbeddingCacheDB()


@pytest.fixture
def manager() -> FakeEmbeddingManager:
    """Fresh deterministic embedding-manager stub per test."""
    return FakeEmbeddingManager()


class TestCachedEmbedderHitAvoidsReembed:
    """CachedEmbedder: re-embed avoidance, Postgres fallback, model isolation, no plaintext."""

    @pytest.mark.asyncio
    async def test_first_call_invokes_manager(self, valkey, db, manager):
        """The first embed() call invokes the underlying manager and returns its vector."""
        embedder = CachedEmbedder(valkey, db, manager, enabled=True)
        vector = await embedder.embed("nomic-embed-text", "hello world")
        assert manager.calls == ["hello world"]
        assert len(vector) == 4

    @pytest.mark.asyncio
    async def test_second_identical_call_zero_manager_calls(self, valkey, db, manager):
        """A repeat embed() for the same (model, text) hits the cache -- zero manager calls."""
        embedder = CachedEmbedder(valkey, db, manager, enabled=True)
        v1 = await embedder.embed("nomic-embed-text", "hello world")
        v2 = await embedder.embed("nomic-embed-text", "hello world")
        assert manager.calls == ["hello world"]  # invoked exactly once
        assert v1 == v2

    @pytest.mark.asyncio
    async def test_valkey_flush_falls_through_to_postgres_no_reembed(self, valkey, db, manager):
        """A cold Valkey cache falls through to Postgres, re-warms, and never re-embeds."""
        embedder = CachedEmbedder(valkey, db, manager, enabled=True)
        v1 = await embedder.embed("nomic-embed-text", "hello world")
        valkey.flush()

        v2 = await embedder.embed("nomic-embed-text", "hello world")
        assert manager.calls == ["hello world"]  # still just once
        assert v1 == v2

        vkey = embedder._valkey_key("nomic-embed-text", embedder._content_hash("hello world"))
        assert vkey in valkey.store  # re-warmed

    @pytest.mark.asyncio
    async def test_different_model_same_text_separate_entry(self, valkey, db, manager):
        """The same text under a different model is a separate cache entry."""
        embedder = CachedEmbedder(valkey, db, manager, enabled=True)
        await embedder.embed("nomic-embed-text", "hello world")
        await embedder.embed("text-embedding-3-small", "hello world")
        assert manager.calls == ["hello world", "hello world"]

    @pytest.mark.asyncio
    async def test_disabled_is_transparent_passthrough(self, valkey, db, manager):
        """enabled=False re-embeds every call and performs no cache reads/writes."""
        embedder = CachedEmbedder(valkey, db, manager, enabled=False)
        await embedder.embed("nomic-embed-text", "hello world")
        await embedder.embed("nomic-embed-text", "hello world")
        assert manager.calls == ["hello world", "hello world"]  # no caching -> re-embeds every call
        assert valkey.store == {}
        assert db.rows == {}

    @pytest.mark.asyncio
    async def test_cache_rows_hold_vectors_only_no_plaintext(self, valkey, db, manager):
        """Cache rows decode to a plain float list -- the source text never appears in them."""
        embedder = CachedEmbedder(valkey, db, manager, enabled=True)
        await embedder.embed("nomic-embed-text", "this text must never appear in the cache row")

        for stored in db.rows.values():
            decoded = orjson.loads(stored)
            assert isinstance(decoded, list)
            assert all(isinstance(x, (int, float)) for x in decoded)
            assert "this text must never appear" not in stored
