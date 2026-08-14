"""Tests for shared.knowledge.embed: cache dedup + embedding-model resolution."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

from shared.knowledge.embed import (
    DEFAULT_EMBEDDING_DIMENSIONS,
    DEFAULT_EMBEDDING_MODEL,
    embed_cached,
    resolve_embedding_model,
)


def _content_hash_for(content: str) -> str:
    """Mirror embed.py's internal hashing so tests can pre-seed the fake cache."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class _FakeRow:
    """Minimal stand-in for a PyDAL Row."""

    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


class _FieldEq:
    """Stand-in for `table.field == value`; `&` merges predicates like PyDAL queries."""

    def __init__(self, field_name: str, value: object) -> None:
        self.conditions: dict[str, object] = {field_name: value}

    def __and__(self, other: _FieldEq) -> _FieldEq:
        merged = _FieldEq.__new__(_FieldEq)
        merged.conditions = {**self.conditions, **other.conditions}
        return merged


class _FakeField:
    """Stand-in for a PyDAL table column, e.g. `table.model`."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __eq__(self, other: object) -> _FieldEq:  # type: ignore[override]
        return _FieldEq(self.name, other)


class _FakeEmbeddingCacheTable:
    """In-memory fake of a PyDAL table exposing embedding_cache's shape."""

    def __init__(self) -> None:
        self.rows: list[_FakeRow] = []
        self.model = _FakeField("model")
        self.content_hash = _FakeField("content_hash")

    def insert(self, **kwargs: object) -> None:
        self.rows.append(_FakeRow(**kwargs))


class _FakeSelect:
    """Stand-in for a PyDAL `.select()` result set."""

    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    def first(self) -> _FakeRow | None:
        return self._rows[0] if self._rows else None


class _FakeDB:
    """Fake penguin-dal handle: supports `db.embedding_cache` + `db(query).select().first()`."""

    def __init__(self) -> None:
        self.embedding_cache = _FakeEmbeddingCacheTable()
        self.committed = False
        self._pending: dict[str, object] = {}

    def __call__(self, query: _FieldEq) -> _FakeDB:
        self._pending = query.conditions
        return self

    def select(self) -> _FakeSelect:
        matches = [
            row
            for row in self.embedding_cache.rows
            if all(getattr(row, key, None) == value for key, value in self._pending.items())
        ]
        return _FakeSelect(matches)

    def commit(self) -> None:
        self.committed = True


@pytest.fixture
def fake_db() -> _FakeDB:
    """A fresh in-memory embedding_cache-backed fake DB for each test."""
    return _FakeDB()


def _mock_manager(vector: list[float]) -> MagicMock:
    manager = MagicMock()
    manager.embed = MagicMock(return_value=vector)
    return manager


class TestEmbedCached:
    """embed_cached: cache-hit avoids re-embed, miss embeds + writes back."""

    @pytest.mark.asyncio
    async def test_miss_embeds_and_writes_cache(self, fake_db: _FakeDB) -> None:
        """A cache miss calls the backend embedder once and persists the result."""
        vector = [0.1] * DEFAULT_EMBEDDING_DIMENSIONS
        manager = _mock_manager(vector)

        result = await embed_cached(
            "hello world", db=fake_db, model="nomic-embed-text", embedding_manager=manager
        )

        assert result == vector
        assert manager.embed.call_count == 1
        assert len(fake_db.embedding_cache.rows) == 1
        assert fake_db.committed is True

    @pytest.mark.asyncio
    async def test_cache_hit_avoids_reembed(self, fake_db: _FakeDB) -> None:
        """A cache hit returns the stored vector without calling the backend."""
        vector = [0.2] * DEFAULT_EMBEDDING_DIMENSIONS
        manager = _mock_manager(vector)

        # Prime the cache directly, simulating a prior embed_cached() write.
        fake_db.embedding_cache.rows.append(
            _FakeRow(
                model="nomic-embed-text",
                content_hash=_content_hash_for("hello world"),
                embedding=vector,
            )
        )

        result = await embed_cached(
            "hello world", db=fake_db, model="nomic-embed-text", embedding_manager=manager
        )

        assert result == vector
        assert manager.embed.call_count == 0

    @pytest.mark.asyncio
    async def test_identical_content_embeds_once_across_two_calls(self, fake_db: _FakeDB) -> None:
        """Two calls with identical content only embed once (second is a cache hit)."""
        vector = [0.3] * DEFAULT_EMBEDDING_DIMENSIONS
        manager = _mock_manager(vector)

        first = await embed_cached(
            "same content", db=fake_db, model="nomic-embed-text", embedding_manager=manager
        )
        second = await embed_cached(
            "same content", db=fake_db, model="nomic-embed-text", embedding_manager=manager
        )

        assert first == second == vector
        assert manager.embed.call_count == 1

    @pytest.mark.asyncio
    async def test_returns_768_dim_vector_for_default_model(self, fake_db: _FakeDB) -> None:
        """The default nomic-embed-text path returns a 768-dim vector."""
        vector = [0.0] * 768
        manager = _mock_manager(vector)

        result = await embed_cached("dimension check", db=fake_db, embedding_manager=manager)

        assert len(result) == 768

    @pytest.mark.asyncio
    async def test_no_db_still_embeds_without_caching(self) -> None:
        """db=None disables caching but embedding still succeeds."""
        vector = [0.4] * DEFAULT_EMBEDDING_DIMENSIONS
        manager = _mock_manager(vector)

        result = await embed_cached("no cache available", db=None, embedding_manager=manager)

        assert result == vector
        assert manager.embed.call_count == 1


class TestResolveEmbeddingModel:
    """resolve_embedding_model: §7 assignment lookup with hardcoded fallback."""

    def test_none_db_returns_default(self) -> None:
        """No DB handle -> hardcoded default."""
        assert resolve_embedding_model(None) == DEFAULT_EMBEDDING_MODEL

    def test_missing_model_assignments_table_returns_default(self, fake_db: _FakeDB) -> None:
        """fake_db has no `model_assignments` (migration 010 not landed here) -> default."""
        assert resolve_embedding_model(fake_db) == DEFAULT_EMBEDDING_MODEL

    def test_assignment_row_present_returns_its_model(self) -> None:
        """An `embeddings` tool_type row resolves to its configured model."""
        db = MagicMock()
        row = MagicMock()
        row.model_name = "mxbai-embed-large"
        db.model_assignments = MagicMock()
        db.return_value.select.return_value.first.return_value = row

        assert resolve_embedding_model(db) == "mxbai-embed-large"

    def test_assignment_lookup_error_falls_back_to_default(self) -> None:
        """A DB error during resolution never raises -- falls back to default."""
        db = MagicMock()
        db.model_assignments = MagicMock()
        db.side_effect = RuntimeError("db unavailable")

        assert resolve_embedding_model(db) == DEFAULT_EMBEDDING_MODEL
