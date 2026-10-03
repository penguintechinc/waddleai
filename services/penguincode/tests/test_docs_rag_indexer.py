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

import json
import logging
import os
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.db.migrate import run_migrations
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


class TestLoadJsonMetadataFallback:
    """``_load_json_metadata`` must degrade to the empty default, never crash."""

    def test_corrupt_metadata_file_falls_back_to_empty_default(self, tmp_path: Path) -> None:
        """A corrupt ``index_metadata.json`` is logged and ignored, not raised."""
        metadata_dir = tmp_path / "docs_index"
        metadata_dir.mkdir()
        (metadata_dir / "index_metadata.json").write_text("{not valid json")

        indexer = DocumentationIndexer(
            store=_FakeVectorStore(), embed_fn=_fake_embed, metadata_dir=str(metadata_dir)
        )
        assert indexer.index_metadata == {"libraries": {}, "languages": {}}

    def test_valid_existing_metadata_file_is_loaded(self, tmp_path: Path) -> None:
        """A well-formed on-disk cache is loaded as-is, not replaced with the default."""
        metadata_dir = tmp_path / "docs_index"
        metadata_dir.mkdir()
        payload = {"libraries": {"tenant:fastapi": {"chunk_count": 3}}, "languages": {}}
        (metadata_dir / "index_metadata.json").write_text(json.dumps(payload))

        indexer = DocumentationIndexer(
            store=_FakeVectorStore(), embed_fn=_fake_embed, metadata_dir=str(metadata_dir)
        )
        assert indexer.index_metadata == payload


class TestFreshnessChecks:
    """``is_library_indexed``/``is_language_indexed`` branch coverage."""

    def test_is_library_indexed_none_ctx_is_false(self) -> None:
        """No ``ScopeContext`` means there is no tenant cache entry to check."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        assert indexer.is_library_indexed(None, "fastapi") is False

    def test_is_library_indexed_never_indexed_is_false(self) -> None:
        """A library with no cache entry at all is reported as not indexed."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        assert indexer.is_library_indexed(_ctx(), "fastapi") is False

    def test_is_library_indexed_fresh_entry_is_true(self) -> None:
        """An entry indexed just now is within the freshness window."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        ctx = _ctx()
        meta_key = indexer._meta_key(ctx, "fastapi")
        indexer.index_metadata.setdefault("libraries", {})[meta_key] = {
            "indexed_at": datetime.now().isoformat()
        }
        assert indexer.is_library_indexed(ctx, "fastapi") is True

    def test_is_library_indexed_stale_entry_is_false(self) -> None:
        """An entry older than the freshness window is reported as not indexed."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        ctx = _ctx()
        meta_key = indexer._meta_key(ctx, "fastapi")
        stale = datetime.now() - timedelta(days=30)
        indexer.index_metadata.setdefault("libraries", {})[meta_key] = {
            "indexed_at": stale.isoformat()
        }
        assert indexer.is_library_indexed(ctx, "fastapi") is False

    def test_is_library_indexed_malformed_entry_missing_key_is_false(self) -> None:
        """A cache entry missing ``indexed_at`` is a KeyError, caught -> not indexed."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        ctx = _ctx()
        meta_key = indexer._meta_key(ctx, "fastapi")
        indexer.index_metadata.setdefault("libraries", {})[meta_key] = {}
        assert indexer.is_library_indexed(ctx, "fastapi") is False

    def test_is_library_indexed_malformed_entry_bad_date_is_false(self) -> None:
        """A cache entry with an unparsable date is a ValueError, caught -> not indexed."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        ctx = _ctx()
        meta_key = indexer._meta_key(ctx, "fastapi")
        indexer.index_metadata.setdefault("libraries", {})[meta_key] = {"indexed_at": "not-a-date"}
        assert indexer.is_library_indexed(ctx, "fastapi") is False

    def test_is_language_indexed_none_ctx_is_false(self) -> None:
        """No ``ScopeContext`` means there is no tenant cache entry to check."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        assert indexer.is_language_indexed(None, "python") is False

    def test_is_language_indexed_never_indexed_is_false(self) -> None:
        """A language with no cache entry at all is reported as not indexed."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        assert indexer.is_language_indexed(_ctx(), "python") is False

    def test_is_language_indexed_fresh_entry_is_true(self) -> None:
        """An entry indexed just now is within the freshness window."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        ctx = _ctx()
        meta_key = indexer._meta_key(ctx, "python")
        indexer.index_metadata.setdefault("languages", {})[meta_key] = {
            "indexed_at": datetime.now().isoformat()
        }
        assert indexer.is_language_indexed(ctx, "python") is True

    def test_is_language_indexed_stale_entry_is_false(self) -> None:
        """An entry older than the freshness window is reported as not indexed."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        ctx = _ctx()
        meta_key = indexer._meta_key(ctx, "python")
        stale = datetime.now() - timedelta(days=30)
        indexer.index_metadata.setdefault("languages", {})[meta_key] = {
            "indexed_at": stale.isoformat()
        }
        assert indexer.is_language_indexed(ctx, "python") is False

    def test_is_language_indexed_malformed_entry_missing_key_is_false(self) -> None:
        """A cache entry missing ``indexed_at`` is a KeyError, caught -> not indexed."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        ctx = _ctx()
        meta_key = indexer._meta_key(ctx, "python")
        indexer.index_metadata.setdefault("languages", {})[meta_key] = {}
        assert indexer.is_language_indexed(ctx, "python") is False

    def test_is_language_indexed_malformed_entry_bad_date_is_false(self) -> None:
        """A cache entry with an unparsable date is a ValueError, caught -> not indexed."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        ctx = _ctx()
        meta_key = indexer._meta_key(ctx, "python")
        indexer.index_metadata.setdefault("languages", {})[meta_key] = {"indexed_at": "nope"}
        assert indexer.is_language_indexed(ctx, "python") is False


class TestEmbeddingErrorHandling:
    """``_embed_and_upsert`` degrades per-chunk embedding failures, never crashes."""

    async def test_embed_and_upsert_skips_failed_chunks_and_logs_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A flaky embed_fn only logs the hint once per indexing pass, not per chunk."""
        calls = {"n": 0}

        async def _flaky(text: str) -> list[float]:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("ollama down")
            return await _fake_embed(text)

        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_flaky)
        ctx = _ctx()
        long_text = " ".join(f"word{i}" for i in range(600))

        with caplog.at_level(logging.WARNING, logger="penguincode_cli.docs_rag.indexer"):
            count = await indexer.index_library(ctx, _library(), [long_text])

        assert count == len(store.rows)
        assert count >= 1
        warnings = [r for r in caplog.records if "embedding failed" in r.message]
        assert len(warnings) == 1

    async def test_all_chunks_failing_embedding_yields_zero_and_skips_upsert(self) -> None:
        """When every chunk fails embedding, ``items`` stays empty: no upsert, no extraction."""

        async def _always_fail(_text: str) -> list[float]:
            raise RuntimeError("ollama down")

        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_always_fail)
        ctx = _ctx()
        count = await indexer.index_library(ctx, _library(), ["some content here"])
        assert count == 0
        assert store.rows == {}


class _FakeAiohttpResponse:
    """Minimal async-context-manager double for ``aiohttp``'s response object."""

    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self) -> _FakeAiohttpResponse:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def json(self) -> dict:  # type: ignore[type-arg]
        return {"embedding": [0.1]}


class _FakeAiohttpSession:
    """Minimal async-context-manager double for ``aiohttp.ClientSession``."""

    def __init__(self, status: int) -> None:
        self._status = status

    async def __aenter__(self) -> _FakeAiohttpSession:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def post(self, *_args: object, **_kwargs: object) -> _FakeAiohttpResponse:
        return _FakeAiohttpResponse(self._status)


class TestGetEmbeddingLiveAiohttpFailure:
    """``_get_embedding``'s real (non-test-double) Ollama HTTP path."""

    async def test_raises_runtime_error_on_non_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-200 Ollama response raises, so the caller's try/except can degrade."""
        import aiohttp

        monkeypatch.setattr(aiohttp, "ClientSession", lambda: _FakeAiohttpSession(500))
        indexer = DocumentationIndexer(store=_FakeVectorStore())
        with pytest.raises(RuntimeError, match="Embedding failed: 500"):
            await indexer._get_embedding("hello world")

    async def test_returns_embedding_on_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 200 Ollama response returns its ``embedding`` field."""
        import aiohttp

        monkeypatch.setattr(aiohttp, "ClientSession", lambda: _FakeAiohttpSession(200))
        indexer = DocumentationIndexer(store=_FakeVectorStore())
        result = await indexer._get_embedding("hello world")
        assert result == [0.1]


class TestGetEmbeddingBulkheadTelemetry:
    """Ops-audit O10/O5: `_get_embedding` records `record_embedding_call`
    labeled by `embedding_endpoint`, so chat vs dedicated embedding Ollama
    saturation stay independently visible.
    """

    async def test_success_records_ok_with_configured_endpoint_label(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiohttp

        import penguincode_cli.docs_rag.indexer as indexer_module

        monkeypatch.setattr(aiohttp, "ClientSession", lambda: _FakeAiohttpSession(200))
        calls: list[tuple[str, str, float]] = []
        monkeypatch.setattr(
            indexer_module,
            "record_embedding_call",
            lambda endpoint, outcome, ms: calls.append((endpoint, outcome, ms)),
        )
        indexer = DocumentationIndexer(
            store=_FakeVectorStore(), embedding_endpoint="embedding_ollama"
        )
        await indexer._get_embedding("hello world")

        assert len(calls) == 1
        endpoint, outcome, _duration = calls[0]
        assert endpoint == "embedding_ollama"
        assert outcome == "ok"

    async def test_failure_records_error_with_default_chat_label(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiohttp

        import penguincode_cli.docs_rag.indexer as indexer_module

        monkeypatch.setattr(aiohttp, "ClientSession", lambda: _FakeAiohttpSession(500))
        calls: list[tuple[str, str, float]] = []
        monkeypatch.setattr(
            indexer_module,
            "record_embedding_call",
            lambda endpoint, outcome, ms: calls.append((endpoint, outcome, ms)),
        )
        indexer = DocumentationIndexer(store=_FakeVectorStore())  # default label: chat_ollama
        with pytest.raises(RuntimeError, match="Embedding failed: 500"):
            await indexer._get_embedding("hello world")

        assert len(calls) == 1
        endpoint, outcome, _duration = calls[0]
        assert endpoint == "chat_ollama"
        assert outcome == "error"

    async def test_test_double_embed_fn_bypasses_telemetry_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The injected `embed_fn` test seam never touches Ollama -- no metric recorded."""
        import penguincode_cli.docs_rag.indexer as indexer_module

        calls: list[tuple[str, str, float]] = []
        monkeypatch.setattr(
            indexer_module,
            "record_embedding_call",
            lambda endpoint, outcome, ms: calls.append((endpoint, outcome, ms)),
        )

        async def _fake_embed(_text: str) -> list[float]:
            return [0.9]

        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        result = await indexer._get_embedding("hello world")

        assert result == [0.9]
        assert calls == []


class TestChunkTextEdgeCases:
    """``_chunk_text``'s empty-input short circuit."""

    async def test_index_library_with_empty_content_produces_zero_chunks(self) -> None:
        """Whitespace-only/empty doc content yields no chunks and no vector rows."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        count = await indexer.index_library(ctx, _library(), [""])
        assert count == 0
        assert store.rows == {}


class TestIndexLibraryFreshnessAndForceReindex:
    """``index_library``'s cache-hit short circuit and ``force_reindex`` clear-then-write."""

    async def test_returns_cached_count_when_fresh_and_not_forced(self) -> None:
        """A second call within the freshness window skips re-embedding entirely."""
        store = _FakeVectorStore()
        embed_calls = {"n": 0}

        async def _counting_embed(text: str) -> list[float]:
            embed_calls["n"] += 1
            return await _fake_embed(text)

        indexer = DocumentationIndexer(store=store, embed_fn=_counting_embed)
        ctx = _ctx()
        lib = _library("fastapi")

        first = await indexer.index_library(ctx, lib, ["fastapi content one"])
        assert first >= 1
        calls_after_first = embed_calls["n"]

        second = await indexer.index_library(ctx, lib, ["fastapi content one"])
        assert second == first
        assert embed_calls["n"] == calls_after_first

    async def test_force_reindex_calls_clear_library_index(self) -> None:
        """``force_reindex=True`` clears the existing index before re-embedding."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        lib = _library("fastapi")
        await indexer.index_library(ctx, lib, ["fastapi content one"])

        with patch.object(indexer, "clear_library_index", wraps=indexer.clear_library_index) as spy:
            await indexer.index_library(ctx, lib, ["fastapi content two"], force_reindex=True)
        spy.assert_awaited_once_with(ctx, lib.name)

    async def test_stale_cache_entry_without_force_reindex_falls_through_to_reembed(self) -> None:
        """A stale (expired) cache entry is not returned early -- re-embedding proceeds
        even without ``force_reindex``."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        lib = _library("fastapi")
        meta_key = indexer._meta_key(ctx, "fastapi")
        stale = datetime.now() - timedelta(days=30)
        indexer.index_metadata.setdefault("libraries", {})[meta_key] = {
            "indexed_at": stale.isoformat(),
            "chunk_count": 99,
            "chunk_ids": [],
        }

        count = await indexer.index_library(ctx, lib, ["fastapi content one"])
        assert count == 1
        assert count != 99
        assert store.rows


class TestIndexLanguageFreshnessAndForceReindex:
    """``index_language``'s cache-hit short circuit and ``force_reindex`` clear-then-write."""

    async def test_returns_cached_count_when_fresh_and_not_forced(self) -> None:
        """A second call within the freshness window skips re-embedding entirely."""
        store = _FakeVectorStore()
        embed_calls = {"n": 0}

        async def _counting_embed(text: str) -> list[float]:
            embed_calls["n"] += 1
            return await _fake_embed(text)

        indexer = DocumentationIndexer(store=store, embed_fn=_counting_embed)
        ctx = _ctx()

        first = await indexer.index_language(ctx, Language.PYTHON, ["python core docs content"])
        assert first >= 1
        calls_after_first = embed_calls["n"]

        second = await indexer.index_language(ctx, Language.PYTHON, ["python core docs content"])
        assert second == first
        assert embed_calls["n"] == calls_after_first

    async def test_force_reindex_calls_clear_language_index(self) -> None:
        """``force_reindex=True`` clears the existing index before re-embedding."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        await indexer.index_language(ctx, Language.PYTHON, ["python core docs content"])

        with patch.object(
            indexer, "clear_language_index", wraps=indexer.clear_language_index
        ) as spy:
            await indexer.index_language(
                ctx, Language.PYTHON, ["python core docs content v2"], force_reindex=True
            )
        spy.assert_awaited_once_with(ctx, Language.PYTHON)

    async def test_noop_when_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``penguincode.rag`` OFF makes ``index_language`` a no-op, mirroring ``index_library``."""
        monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "false")
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        count = await indexer.index_language(_ctx(), Language.PYTHON, ["python docs content"])
        assert count == 0
        assert store.rows == {}

    async def test_stale_cache_entry_without_force_reindex_falls_through_to_reembed(self) -> None:
        """A stale (expired) cache entry is not returned early -- re-embedding proceeds
        even without ``force_reindex``."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        meta_key = indexer._meta_key(ctx, Language.PYTHON.value)
        stale = datetime.now() - timedelta(days=30)
        indexer.index_metadata.setdefault("languages", {})[meta_key] = {
            "indexed_at": stale.isoformat(),
            "chunk_count": 99,
            "chunk_ids": [],
        }

        count = await indexer.index_language(ctx, Language.PYTHON, ["python core docs content"])
        assert count == 1
        assert count != 99
        assert store.rows


class _RaisingQueryStore(_FakeVectorStore):
    """``_FakeVectorStore`` whose ``query`` always raises, to exercise ``search``'s guard."""

    def query(self, ctx, embedding, *, n, where=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("pgvector outage")


class TestSearchWhereVariantsAndErrors:
    """``search``'s where-variant construction, error degradation, and dedup."""

    async def test_filters_by_language_only(self) -> None:
        """A ``languages``-only filter (no ``libraries``) still builds a where-variant."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        await indexer.index_library(ctx, _library("fastapi"), ["python web framework content"])
        await indexer.index_library(
            ctx, Library(name="ferris", language=Language.RUST, version="1.0"), ["rust content"]
        )

        results = await indexer.search(ctx, "python web framework content", languages=["python"])
        assert results
        assert all(r.language == "python" for r in results)

    async def test_returns_empty_when_embedding_fails(self) -> None:
        """A query-embedding failure degrades to no results, not a crash."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        await indexer.index_library(ctx, _library("fastapi"), ["fastapi content"])

        async def _boom(_text: str) -> list[float]:
            raise RuntimeError("ollama outage")

        indexer._embed_fn = _boom
        results = await indexer.search(ctx, "fastapi content")
        assert results == []

    async def test_returns_empty_when_store_query_raises(self) -> None:
        """A store outage on ``query`` is logged and degrades to no results."""
        store = _RaisingQueryStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        results = await indexer.search(ctx, "anything")
        assert results == []

    async def test_dedup_keeps_first_when_scores_tie_across_filters(self) -> None:
        """The same chunk hit by two where-variants with an equal score is not duplicated."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        await indexer.index_library(ctx, _library("fastapi"), ["fastapi python content"])

        results = await indexer.search(
            ctx,
            "fastapi python content",
            libraries=["fastapi"],
            languages=["python"],
            limit=10,
        )
        assert len(results) == 1


class TestClearLibraryIndex:
    """``clear_library_index`` branch coverage."""

    async def test_none_ctx_is_noop(self) -> None:
        """No ``ScopeContext`` means there is nothing tenant-scoped to clear."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        assert await indexer.clear_library_index(None, "fastapi") == 0

    async def test_never_indexed_returns_zero(self) -> None:
        """A library with no cache entry has nothing to clear."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        assert await indexer.clear_library_index(_ctx(), "nope") == 0

    async def test_removes_rows_and_metadata_entry(self) -> None:
        """A successful clear deletes both the vector rows and the freshness cache entry."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        await indexer.index_library(ctx, _library("fastapi"), ["fastapi content"])
        meta_key = indexer._meta_key(ctx, "fastapi")
        assert meta_key in indexer.index_metadata["libraries"]

        removed = await indexer.clear_library_index(ctx, "fastapi")
        assert removed >= 1
        assert store.rows == {}
        assert meta_key not in indexer.index_metadata["libraries"]

    async def test_store_delete_failure_preserves_metadata_and_returns_zero(self) -> None:
        """A store outage on delete leaves the cache entry intact and reports 0 removed."""

        class _BoomStore(_FakeVectorStore):
            def delete(self, ctx, ids):  # type: ignore[no-untyped-def]
                raise RuntimeError("pgvector outage")

        store = _BoomStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        await indexer.index_library(ctx, _library("fastapi"), ["fastapi content"])
        meta_key = indexer._meta_key(ctx, "fastapi")

        removed = await indexer.clear_library_index(ctx, "fastapi")
        assert removed == 0
        assert meta_key in indexer.index_metadata["libraries"]

    async def test_entry_with_no_chunk_ids_still_removes_metadata(self) -> None:
        """An entry with an empty ``chunk_ids`` list skips the delete call but still clears."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        meta_key = indexer._meta_key(ctx, "fastapi")
        indexer.index_metadata.setdefault("libraries", {})[meta_key] = {
            "indexed_at": datetime.now().isoformat(),
            "chunk_count": 0,
            "chunk_ids": [],
        }

        removed = await indexer.clear_library_index(ctx, "fastapi")
        assert removed == 0
        assert meta_key not in indexer.index_metadata["libraries"]


class TestClearLanguageIndex:
    """``clear_language_index`` branch coverage (mirrors ``TestClearLibraryIndex``)."""

    async def test_none_ctx_is_noop(self) -> None:
        """No ``ScopeContext`` means there is nothing tenant-scoped to clear."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        assert await indexer.clear_language_index(None, Language.PYTHON) == 0

    async def test_never_indexed_returns_zero(self) -> None:
        """A language with no cache entry has nothing to clear."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        assert await indexer.clear_language_index(_ctx(), Language.PYTHON) == 0

    async def test_removes_rows_and_metadata_entry(self) -> None:
        """A successful clear deletes both the vector rows and the freshness cache entry."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        await indexer.index_language(ctx, Language.PYTHON, ["python core docs content"])
        meta_key = indexer._meta_key(ctx, Language.PYTHON.value)
        assert meta_key in indexer.index_metadata["languages"]

        removed = await indexer.clear_language_index(ctx, Language.PYTHON)
        assert removed >= 1
        assert store.rows == {}
        assert meta_key not in indexer.index_metadata["languages"]

    async def test_store_delete_failure_preserves_metadata_and_returns_zero(self) -> None:
        """A store outage on delete leaves the cache entry intact and reports 0 removed."""

        class _BoomStore(_FakeVectorStore):
            def delete(self, ctx, ids):  # type: ignore[no-untyped-def]
                raise RuntimeError("pgvector outage")

        store = _BoomStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        await indexer.index_language(ctx, Language.PYTHON, ["python core docs content"])
        meta_key = indexer._meta_key(ctx, Language.PYTHON.value)

        removed = await indexer.clear_language_index(ctx, Language.PYTHON)
        assert removed == 0
        assert meta_key in indexer.index_metadata["languages"]

    async def test_entry_with_no_chunk_ids_still_removes_metadata(self) -> None:
        """An entry with an empty ``chunk_ids`` list skips the delete call but still clears."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        meta_key = indexer._meta_key(ctx, Language.PYTHON.value)
        indexer.index_metadata.setdefault("languages", {})[meta_key] = {
            "indexed_at": datetime.now().isoformat(),
            "chunk_count": 0,
            "chunk_ids": [],
        }

        removed = await indexer.clear_language_index(ctx, Language.PYTHON)
        assert removed == 0
        assert meta_key not in indexer.index_metadata["languages"]


class TestCleanupUnused:
    """``cleanup_unused`` branch coverage."""

    async def test_none_ctx_returns_empty_dict(self) -> None:
        """No ``ScopeContext`` means there is nothing tenant-scoped to clean up."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        assert await indexer.cleanup_unused(None, [], []) == {}

    async def test_removes_libraries_and_languages_no_longer_present(self) -> None:
        """Libraries/languages absent from the current project are cleared and reported."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        await indexer.index_library(ctx, _library("fastapi"), ["fastapi content"])
        await indexer.index_language(ctx, Language.PYTHON, ["python core docs content"])

        removed = await indexer.cleanup_unused(ctx, [], [])
        assert removed == {"fastapi": 1, "_lang_python": 1}
        assert store.rows == {}

    async def test_keeps_libraries_and_languages_still_present(self) -> None:
        """Libraries/languages still in the current project are left untouched."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        lib = _library("fastapi")
        await indexer.index_library(ctx, lib, ["fastapi content"])
        await indexer.index_language(ctx, Language.PYTHON, ["python core docs content"])

        removed = await indexer.cleanup_unused(ctx, [lib], [Language.PYTHON])
        assert removed == {}
        assert store.rows

    async def test_ignores_other_tenants_metadata_entries(self) -> None:
        """A cleanup run scoped to tenant B never touches tenant A's cache entries
        (library or language)."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx_a = _ctx()
        ctx_b = _ctx()
        await indexer.index_library(ctx_a, _library("fastapi"), ["fastapi content a"])
        await indexer.index_language(ctx_a, Language.PYTHON, ["python docs content a"])

        removed = await indexer.cleanup_unused(ctx_b, [], [])
        assert removed == {}
        assert indexer._meta_key(ctx_a, "fastapi") in indexer.index_metadata["libraries"]
        assert (
            indexer._meta_key(ctx_a, Language.PYTHON.value) in indexer.index_metadata["languages"]
        )

    async def test_zero_count_clears_are_not_reported_as_removed(self) -> None:
        """A library/language whose cache entry has no ``chunk_ids`` clears to 0 and
        is omitted from the ``removed`` dict (the ``count > 0`` guard)."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        lib_key = indexer._meta_key(ctx, "fastapi")
        lang_key = indexer._meta_key(ctx, Language.PYTHON.value)
        indexer.index_metadata.setdefault("libraries", {})[lib_key] = {
            "indexed_at": datetime.now().isoformat(),
            "chunk_count": 0,
            "chunk_ids": [],
        }
        indexer.index_metadata.setdefault("languages", {})[lang_key] = {
            "indexed_at": datetime.now().isoformat(),
            "chunk_count": 0,
            "chunk_ids": [],
        }

        removed = await indexer.cleanup_unused(ctx, [], [])
        assert removed == {}
        assert lib_key not in indexer.index_metadata.get("libraries", {})
        assert lang_key not in indexer.index_metadata.get("languages", {})

    async def test_invalid_cached_language_value_is_skipped(self) -> None:
        """A cache key that no longer maps to a valid ``Language`` is skipped, not raised."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        ctx = _ctx()
        bad_key = indexer._meta_key(ctx, "not-a-real-language")
        indexer.index_metadata.setdefault("languages", {})[bad_key] = {
            "indexed_at": datetime.now().isoformat(),
            "chunk_count": 1,
            "chunk_ids": ["x"],
        }

        removed = await indexer.cleanup_unused(ctx, [], [])
        assert removed == {}


class TestGetIndexStatus:
    """``get_index_status`` branch coverage."""

    def test_none_ctx_returns_empty_status(self) -> None:
        """No ``ScopeContext`` returns the same empty shape as a tenant with nothing indexed."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        status = indexer.get_index_status(None)
        assert status == {"libraries": {}, "languages": {}, "total_chunks": 0}

    async def test_reports_indexed_libraries_and_languages_for_this_tenant(self) -> None:
        """Both library and language entries contribute to ``total_chunks``."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx = _ctx()
        await indexer.index_library(ctx, _library("fastapi"), ["fastapi content"])
        await indexer.index_language(ctx, Language.PYTHON, ["python core docs content"])

        status = indexer.get_index_status(ctx)
        assert status["libraries"]["fastapi"]["chunk_count"] == 1
        assert status["libraries"]["fastapi"]["is_expired"] is False
        assert status["languages"]["python"]["chunk_count"] == 1
        assert status["total_chunks"] == 2

    async def test_excludes_other_tenants_entries(self) -> None:
        """Status scoped to tenant B never reports tenant A's indexed libraries
        or languages."""
        store = _FakeVectorStore()
        indexer = DocumentationIndexer(store=store, embed_fn=_fake_embed)
        ctx_a = _ctx()
        ctx_b = _ctx()
        await indexer.index_library(ctx_a, _library("fastapi"), ["fastapi content"])
        await indexer.index_language(ctx_a, Language.PYTHON, ["python docs content"])

        status = indexer.get_index_status(ctx_b)
        assert status == {"libraries": {}, "languages": {}, "total_chunks": 0}

    def test_marks_stale_entries_as_expired(self) -> None:
        """An entry past the freshness window is reported with ``is_expired=True``."""
        indexer = DocumentationIndexer(store=_FakeVectorStore(), embed_fn=_fake_embed)
        ctx = _ctx()
        meta_key = indexer._meta_key(ctx, "fastapi")
        stale = datetime.now() - timedelta(days=30)
        indexer.index_metadata.setdefault("libraries", {})[meta_key] = {
            "indexed_at": stale.isoformat(),
            "chunk_count": 3,
            "chunk_ids": ["a", "b", "c"],
            "language": "python",
        }

        status = indexer.get_index_status(ctx)
        assert status["libraries"]["fastapi"]["is_expired"] is True


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
