"""Tests for ``penguincode_cli.docs_rag.indexer.DocumentationIndexer`` (pgvector).

TDD: written before the pgvector rewrite lands; the module still imports
chromadb until this task implements it, so these tests are expected to fail
(ImportError on `store`, or AttributeError on missing `ctx` params) until
``docs_rag/indexer.py`` is rewritten to use ``PgVectorStore``.

Static tests (fake embed_fn + fake VectorStore, no DB) always run. Live
tests connect to ``TEST_DATABASE_URL`` (a real pgvector/pgvector:pg17
instance) and are skipped -- with an explicit reason, never silently --
when that env var is unset, mirroring ``tests/test_stores_vector.py``.

# regression: penguincode-knowledge-platform (T7 -- docs-RAG -> pgvector)
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest

from db.migrate import run_migrations
from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.docs_rag.indexer import DocumentationIndexer
from penguincode_cli.docs_rag.models import Language, Library

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set -- live-Postgres docs_rag indexer tests are CI-pending (T16)",
)


def _ctx(
    *,
    tenant_id: str | None = None,
    org_id: str | None = None,
    team_ids: tuple[str, ...] = (),
    user_id: str | None = None,
    scopes: tuple[str, ...] = (),
) -> ScopeContext:
    return ScopeContext(
        tenant_id=tenant_id or str(uuid.uuid4()),
        org_id=org_id,
        team_ids=team_ids,
        user_id=user_id or str(uuid.uuid4()),
        scopes=scopes,
    )


async def _fake_embed(text: str) -> list[float]:
    """Deterministic 768-dim embedding derived from content.

    Same text -> same vector (so a round-trip query for the same content
    is a near-exact nearest neighbor); different text -> a different
    vector, via a trivial content-derived seed.
    """
    vec = [0.0] * 768
    seed = float((hash(text) % 1000) + 1)
    vec[0] = seed
    vec[1] = 1.0
    return vec


@pytest.fixture(autouse=True)
def _rag_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to the RAG flag being ON via the env override.

    ``flags.client.is_enabled`` reads ``PENGUINCODE_FLAG_RAG`` before ever
    touching PostHog -- see ``flags/client.py`` ``_env_var_name``.
    """
    monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "true")


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let the default ``./.penguincode/docs_index`` metadata dir leak into the repo."""
    monkeypatch.chdir(tmp_path)


def _library(name: str = "fastapi") -> Library:
    return Library(name=name, language=Language.PYTHON, version="1.0")


# ---------------------------------------------------------------------------
# Static tests: fake embed_fn + fake in-memory VectorStore, no DB needed.
# ---------------------------------------------------------------------------


class _FakeVectorStore:
    """Minimal in-memory ``VectorStore`` double for unit tests."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[ScopeContext, list[float], str, dict, str, str | None]] = {}

    def upsert(self, ctx, items, *, visibility, team_id):  # type: ignore[no-untyped-def]
        for item in items:
            self.rows[item.id] = (
                ctx,
                item.embedding,
                item.document,
                item.metadata,
                visibility,
                team_id,
            )

    def query(self, ctx, embedding, *, n, where=None):  # type: ignore[no-untyped-def]
        from penguincode_cli.stores.vector import VectorHit

        hits = []
        for row_id, (row_ctx, row_emb, doc, meta, _visibility, _team_id) in self.rows.items():
            if row_ctx.tenant_id != ctx.tenant_id:
                continue
            if where and not all(meta.get(k) == v for k, v in where.items()):
                continue
            score = 1.0 if row_emb == embedding else 0.0
            hits.append((score, row_id, doc, meta))
        hits.sort(key=lambda h: h[0], reverse=True)
        return [
            VectorHit(id=row_id, document=doc, metadata=meta, score=score)
            for score, row_id, doc, meta in hits[:n]
        ]

    def delete(self, ctx, ids):  # type: ignore[no-untyped-def]
        for i in ids:
            self.rows.pop(i, None)


class TestDocumentationIndexerFlagGating:
    async def test_index_library_noop_when_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "false")
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        count = await indexer.index_library(_ctx(), _library(), ["some content here"])
        assert count == 0
        assert store.rows == {}

    async def test_search_returns_empty_when_flag_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        await indexer.index_library(ctx, _library(), ["some content here"])
        assert store.rows  # sanity: flag was on for indexing

        monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "false")
        results = await indexer.search(ctx, "some content")
        assert results == []


class TestDocumentationIndexerScopeAware:
    async def test_search_requires_scope_context_none_returns_empty(self) -> None:
        """No ScopeContext (CLI not wired yet) degrades gracefully -- no crash."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        results = await indexer.search(None, "anything")
        assert results == []

    async def test_index_library_none_ctx_is_a_noop(self) -> None:
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        count = await indexer.index_library(None, _library(), ["content"])
        assert count == 0
        assert store.rows == {}

    async def test_index_library_stamps_tenant_visibility_by_default(self) -> None:
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        await indexer.index_library(ctx, _library(), ["hello world content"])
        assert store.rows
        _, _, _, _, visibility, team_id = next(iter(store.rows.values()))
        assert visibility == "tenant"
        assert team_id is None

    async def test_index_library_honors_explicit_team_visibility(self) -> None:
        store = _FakeVectorStore()
        team = str(uuid.uuid4())
        ctx = _ctx(team_ids=(team,))
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        await indexer.index_library(ctx, _library(), ["content"], visibility="team", team_id=team)
        _, _, _, _, visibility, team_id = next(iter(store.rows.values()))
        assert visibility == "team"
        assert team_id == team


class TestDocumentationIndexerRoundTrip:
    async def test_index_then_search_returns_matching_chunk(self) -> None:
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        await indexer.index_library(ctx, _library("fastapi"), ["fastapi routing docs content"])

        results = await indexer.search(ctx, "fastapi routing docs content")
        assert len(results) == 1
        assert results[0].library == "fastapi"

    async def test_metadata_filter_by_library(self) -> None:
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        await indexer.index_library(ctx, _library("fastapi"), ["fastapi content alpha"])
        await indexer.index_library(ctx, _library("django"), ["django content beta"])

        results = await indexer.search(ctx, "fastapi content alpha", libraries=["fastapi"])
        assert results
        assert all(r.library == "fastapi" for r in results)

    async def test_chromadb_not_imported(self) -> None:
        """# regression: penguincode-knowledge-platform -- chromadb fully removed from indexer.py."""
        import penguincode_cli.docs_rag.indexer as mod

        source = mod.__file__
        assert source is not None
        with open(source) as f:
            assert "chromadb" not in f.read()


class TestKnowledgeGraphWiring:
    """T-wire: indexing a doc chunk's vector also triggers knowledge-graph extraction.

    # regression: penguincode-knowledge-platform (T-wire -- docs-RAG -> knowledge graph)
    """

    async def test_indexing_a_chunk_triggers_knowledge_extraction(self) -> None:
        from penguincode_cli.stores.graph import Subgraph

        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()

        calls: list[dict] = []

        async def _spy(ctx_arg, text, *, source_id=None, visibility="tenant", team_id=None, **_kw):  # type: ignore[no-untyped-def]
            calls.append(
                {
                    "ctx": ctx_arg,
                    "text": text,
                    "source_id": source_id,
                    "visibility": visibility,
                    "team_id": team_id,
                }
            )
            return Subgraph(nodes=[], edges=[])

        with patch("penguincode_cli.docs_rag.indexer.extract_knowledge", _spy):
            await indexer.index_library(ctx, _library("fastapi"), ["fastapi routing docs content"])

        assert len(calls) == 1
        assert calls[0]["ctx"] is ctx
        assert calls[0]["text"] == "fastapi routing docs content"
        assert (
            calls[0]["source_id"] in store.rows
        )  # chunk id, same id the vector row was stored under
        assert calls[0]["visibility"] == "tenant"
        assert calls[0]["team_id"] is None

    async def test_knowledge_extraction_failure_does_not_break_indexing(self) -> None:
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()

        async def _boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("graph store outage")

        with patch("penguincode_cli.docs_rag.indexer.extract_knowledge", _boom):
            count = await indexer.index_library(
                ctx, _library("fastapi"), ["fastapi routing docs content"]
            )

        # The vector write (primary path) must have succeeded despite the
        # extractor raising.
        assert count == 1
        assert len(store.rows) == 1


# ---------------------------------------------------------------------------
# Live-Postgres tests: require TEST_DATABASE_URL (pgvector/pgvector image).
# ---------------------------------------------------------------------------


@pytest.fixture
def live_dsn() -> Iterator[str]:
    """Fresh, migrated ``penguincode`` schema for every test."""
    assert TEST_DATABASE_URL is not None
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS penguincode CASCADE")
    run_migrations(dsn=TEST_DATABASE_URL)
    yield TEST_DATABASE_URL


@requires_postgres
class TestDocumentationIndexerLivePgvector:
    async def test_index_then_search_round_trip_via_pgvector(
        self, live_dsn: str, tmp_path: Path
    ) -> None:
        indexer = DocumentationIndexer(
            dsn=live_dsn, embed_fn=_fake_embed, metadata_dir=str(tmp_path)
        )
        ctx = _ctx()
        count = await indexer.index_library(
            ctx, _library("fastapi"), ["fastapi routing guide content"]
        )
        assert count >= 1

        results = await indexer.search(ctx, "fastapi routing guide content")
        assert results
        assert results[0].library == "fastapi"

    async def test_metadata_filter_library_language_live(
        self, live_dsn: str, tmp_path: Path
    ) -> None:
        indexer = DocumentationIndexer(
            dsn=live_dsn, embed_fn=_fake_embed, metadata_dir=str(tmp_path)
        )
        ctx = _ctx()
        await indexer.index_library(ctx, _library("fastapi"), ["fastapi alpha content"])
        await indexer.index_library(
            ctx,
            Library(name="ferris", language=Language.RUST, version="1.0"),
            ["rust ferris content"],
        )

        by_library = await indexer.search(ctx, "content", libraries=["fastapi"], limit=10)
        assert by_library
        assert all(r.library == "fastapi" for r in by_library)

        by_language = await indexer.search(ctx, "content", languages=["rust"], limit=10)
        assert by_language
        assert all(r.language == "rust" for r in by_language)

    async def test_cross_tenant_isolation(self, live_dsn: str, tmp_path: Path) -> None:
        indexer = DocumentationIndexer(
            dsn=live_dsn, embed_fn=_fake_embed, metadata_dir=str(tmp_path)
        )
        tenant_a = _ctx()
        tenant_b = _ctx()

        await indexer.index_library(
            tenant_a, _library("fastapi"), ["tenant a private docs content"]
        )

        results_for_b = await indexer.search(tenant_b, "tenant a private docs content")
        assert results_for_b == []

        results_for_a = await indexer.search(tenant_a, "tenant a private docs content")
        assert results_for_a

    async def test_clear_library_index_removes_rows(self, live_dsn: str, tmp_path: Path) -> None:
        indexer = DocumentationIndexer(
            dsn=live_dsn, embed_fn=_fake_embed, metadata_dir=str(tmp_path)
        )
        ctx = _ctx()
        await indexer.index_library(ctx, _library("fastapi"), ["fastapi clear-me content"])
        assert await indexer.search(ctx, "fastapi clear-me content")

        removed = await indexer.clear_library_index(ctx, "fastapi")
        assert removed >= 1
        assert await indexer.search(ctx, "fastapi clear-me content") == []
