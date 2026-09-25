"""GraphRAG hybrid retrieval (T14): scoped vector top-k + scoped graph expansion.

Static/mocked tests (no DB, no network) exercise flag gating, embedding
failure degradation, per-table/per-graph-kind degradation, seed-key
derivation, and context assembly. Live-Postgres tests connect to
`TEST_DATABASE_URL` and are skipped -- with an explicit reason, never
silently -- when that env var is unset, mirroring `tests/test_stores_graph.py`
and `tests/test_graphs_knowledge.py`'s pattern. One live test also calls a
real Ollama `nomic-embed-text` embedding when Ollama is reachable at
`http://localhost:11434`, skipped otherwise.

# regression: penguincode-knowledge-platform (T14 -- GraphRAG retrieval)
"""

from __future__ import annotations

import os
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import psycopg
import pytest

from db.migrate import run_migrations
from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.flags.client import (
    CODE_GRAPH_FLAG,
    KNOWLEDGE_GRAPH_FLAG,
    RAG_FLAG,
)
from penguincode_cli.retrieval import graphrag
from penguincode_cli.retrieval.graphrag import EmbedFn, RetrievalResult, retrieve
from penguincode_cli.stores.graph import (
    GraphEdge,
    GraphNode,
    GraphStore,
    PostgresGraphStore,
    Subgraph,
)
from penguincode_cli.stores.vector import PgVectorStore, VectorHit, VectorItem, VectorStore

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
_TEST_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set -- live-Postgres graphrag tests are CI-pending (T16)",
)


def _ollama_reachable() -> bool:
    try:
        httpx.get(f"{_TEST_OLLAMA_URL}/api/tags", timeout=1.0)
        return True
    except httpx.HTTPError:
        return False


requires_ollama = pytest.mark.skipif(
    not _ollama_reachable(),
    reason=f"Ollama not reachable at {_TEST_OLLAMA_URL} -- live-embedding test skipped",
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ctx(tenant_id: str, **overrides: Any) -> ScopeContext:
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "org_id": None,
        "team_ids": (),
        "user_id": str(uuid.uuid4()),
        "scopes": (),
    }
    defaults.update(overrides)
    return ScopeContext(**defaults)


def _hit(
    id_: str, score: float, document: str = "doc", metadata: dict[str, Any] | None = None
) -> VectorHit:
    return VectorHit(id=id_, document=document, metadata=metadata or {}, score=score)


def _mock_vector_store(hits: list[VectorHit] | None = None, *, raises: bool = False) -> MagicMock:
    store = MagicMock(spec=VectorStore)
    if raises:
        store.query = MagicMock(side_effect=RuntimeError("boom"))
    else:
        store.query = MagicMock(return_value=list(hits or []))
    return store


def _mock_graph_store(
    subgraph_by_kind: dict[str, Subgraph] | None = None, *, raise_for: set[str] | None = None
) -> MagicMock:
    mapping = subgraph_by_kind or {}
    failing = raise_for or set()

    def _subgraph(_ctx: ScopeContext, kind: str, _seed_keys: list[str], *, depth: int) -> Subgraph:
        if kind in failing:
            raise RuntimeError(f"boom:{kind}")
        return mapping.get(kind, Subgraph(nodes=[], edges=[]))

    store = MagicMock(spec=GraphStore)
    store.subgraph = MagicMock(side_effect=_subgraph)
    return store


def _const_embed_fn(vector: list[float]) -> EmbedFn:
    async def _embed(_query: str) -> list[float]:
        return vector

    return _embed


_ZERO_VECTOR = [0.0] * 768


# ---------------------------------------------------------------------------
# Static helper unit tests: _seed_keys_from_hits / _assemble_context
# ---------------------------------------------------------------------------


class TestSeedKeyDerivation:
    def test_uses_hit_id_node_key_and_node_keys_deduped_in_order(self) -> None:
        hits = [
            _hit("id1", 0.9, metadata={"node_key": "a", "node_keys": ["b", "a"]}),
            _hit("id2", 0.8, metadata={}),
        ]
        assert graphrag._seed_keys_from_hits(hits) == ["id1", "a", "b", "id2"]

    def test_ignores_non_string_metadata_values(self) -> None:
        hits = [_hit("id1", 0.9, metadata={"node_key": 123, "node_keys": "not-a-list"})]
        assert graphrag._seed_keys_from_hits(hits) == ["id1"]

    def test_empty_hits_yields_empty_seeds(self) -> None:
        assert graphrag._seed_keys_from_hits([]) == []


class TestAssembleContext:
    def test_renders_vector_hits_and_graph_lines(self) -> None:
        hits = [_hit("id1", 0.876, document="hello world")]
        subgraphs = {
            "knowledge": Subgraph(
                nodes=[GraphNode(node_type="entity", key="Ollama")],
                edges=[
                    GraphEdge(
                        src_type="component",
                        src_key="penguincode",
                        dst_type="technology",
                        dst_key="Ollama",
                        rel_type="uses",
                    )
                ],
            )
        }
        context = graphrag._assemble_context(hits, subgraphs, max_chars=4000)
        assert "[vector score=0.876] hello world" in context
        assert "[graph:knowledge] node entity:Ollama" in context
        assert "[graph:knowledge] component:penguincode --uses--> technology:Ollama" in context

    def test_truncates_to_max_chars(self) -> None:
        hits = [_hit("id1", 0.5, document="x" * 100)]
        context = graphrag._assemble_context(hits, {}, max_chars=10)
        assert len(context) == 10

    def test_no_subgraphs_still_renders_vector_only(self) -> None:
        context = graphrag._assemble_context([_hit("id1", 0.5, document="solo")], {}, max_chars=100)
        assert context == "[vector score=0.500] solo"


# ---------------------------------------------------------------------------
# penguincode.rag gating -- OFF short-circuits everything, before any I/O.
# ---------------------------------------------------------------------------


class TestRagFlagOff:
    async def test_rag_flag_off_returns_empty_and_touches_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.retrieval.graphrag.is_enabled", lambda flag, ctx: False)
        docs_store = _mock_vector_store()
        mem_store = _mock_vector_store()
        graph_store = _mock_graph_store()
        embed_fn = AsyncMock()

        result = await retrieve(
            _ctx("t1"),
            "q",
            embed_fn=embed_fn,
            vector_stores={"docs_vectors": docs_store, "memory_vectors": mem_store},
            graph_store=graph_store,
        )

        assert result == RetrievalResult(vector_hits=[], subgraphs={}, context="")
        embed_fn.assert_not_called()
        docs_store.query.assert_not_called()
        mem_store.query.assert_not_called()
        graph_store.subgraph.assert_not_called()


class TestEmbeddingFailure:
    async def test_embedding_failure_degrades_to_empty_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.retrieval.graphrag.is_enabled", lambda flag, ctx: True)
        docs_store = _mock_vector_store()
        graph_store = _mock_graph_store()

        async def _boom(_query: str) -> list[float]:
            raise RuntimeError("ollama down")

        result = await retrieve(
            _ctx("t1"),
            "q",
            embed_fn=_boom,
            vector_stores={"docs_vectors": docs_store, "memory_vectors": docs_store},
            graph_store=graph_store,
        )

        assert result == RetrievalResult(vector_hits=[], subgraphs={}, context="")
        docs_store.query.assert_not_called()
        graph_store.subgraph.assert_not_called()

    async def test_raw_ollama_call_with_missing_embedding_field_degrades(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No `embed_fn` injected -- exercises `_get_embedding`'s raw-httpx path."""
        monkeypatch.setattr("penguincode_cli.retrieval.graphrag.is_enabled", lambda flag, ctx: True)
        docs_store = _mock_vector_store()
        graph_store = _mock_graph_store()

        class _FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {}  # missing "embedding" field

        class _FakeAsyncClient:
            async def __aenter__(self) -> _FakeAsyncClient:
                return self

            async def __aexit__(self, *exc_info: object) -> None:
                return None

            async def post(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
                return _FakeResponse()

        monkeypatch.setattr(
            "penguincode_cli.retrieval.graphrag.httpx.AsyncClient",
            lambda *a, **kw: _FakeAsyncClient(),
        )

        result = await retrieve(
            _ctx("t1"),
            "q",
            vector_stores={"docs_vectors": docs_store, "memory_vectors": docs_store},
            graph_store=graph_store,
        )

        assert result == RetrievalResult(vector_hits=[], subgraphs={}, context="")
        docs_store.query.assert_not_called()
        graph_store.subgraph.assert_not_called()


# ---------------------------------------------------------------------------
# Vector-side behavior: merge/rank/truncate, per-table degradation.
# ---------------------------------------------------------------------------


class TestVectorRetrieval:
    async def test_merges_and_ranks_across_tables_truncated_to_n_vector(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "penguincode_cli.retrieval.graphrag.is_enabled", lambda flag, ctx: flag == RAG_FLAG
        )
        ctx = _ctx("t1")
        docs_store = _mock_vector_store([_hit("a", 0.5), _hit("b", 0.9)])
        mem_store = _mock_vector_store([_hit("c", 0.7)])

        result = await retrieve(
            ctx,
            "q",
            n_vector=2,
            embed_fn=_const_embed_fn(_ZERO_VECTOR),
            vector_stores={"docs_vectors": docs_store, "memory_vectors": mem_store},
            graph_store=_mock_graph_store(),
        )

        assert [h.id for h in result.vector_hits] == ["b", "c"]
        docs_store.query.assert_called_once_with(ctx, _ZERO_VECTOR, n=2)
        mem_store.query.assert_called_once_with(ctx, _ZERO_VECTOR, n=2)

    async def test_one_table_failure_degrades_others_still_contribute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "penguincode_cli.retrieval.graphrag.is_enabled", lambda flag, ctx: flag == RAG_FLAG
        )
        good_store = _mock_vector_store([_hit("ok", 0.8)])
        bad_store = _mock_vector_store(raises=True)

        result = await retrieve(
            _ctx("t1"),
            "q",
            embed_fn=_const_embed_fn(_ZERO_VECTOR),
            vector_stores={"docs_vectors": good_store, "memory_vectors": bad_store},
            graph_store=_mock_graph_store(),
        )

        assert [h.id for h in result.vector_hits] == ["ok"]


# ---------------------------------------------------------------------------
# Per-graph-kind flag gating: independent, absent-when-off, degrades on error.
# ---------------------------------------------------------------------------


class TestGraphFlagGating:
    async def test_all_graph_flags_off_yields_empty_subgraphs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "penguincode_cli.retrieval.graphrag.is_enabled", lambda flag, ctx: flag == RAG_FLAG
        )
        graph_store = _mock_graph_store()

        result = await retrieve(
            _ctx("t1"),
            "q",
            embed_fn=_const_embed_fn(_ZERO_VECTOR),
            vector_stores={"docs_vectors": _mock_vector_store(), "memory_vectors": _mock_vector_store()},
            graph_store=graph_store,
        )

        assert result.subgraphs == {}
        graph_store.subgraph.assert_not_called()

    async def test_only_knowledge_flag_on_expands_only_that_kind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        on = {RAG_FLAG, KNOWLEDGE_GRAPH_FLAG}
        monkeypatch.setattr("penguincode_cli.retrieval.graphrag.is_enabled", lambda flag, ctx: flag in on)
        ctx = _ctx("t1")
        sub = Subgraph(nodes=[GraphNode(node_type="entity", key="x")], edges=[])
        graph_store = _mock_graph_store({"knowledge": sub})

        result = await retrieve(
            ctx,
            "q",
            embed_fn=_const_embed_fn(_ZERO_VECTOR),
            vector_stores={
                "docs_vectors": _mock_vector_store([_hit("h1", 0.5, metadata={"node_key": "x"})]),
                "memory_vectors": _mock_vector_store(),
            },
            graph_store=graph_store,
        )

        assert set(result.subgraphs.keys()) == {"knowledge"}
        assert result.subgraphs["knowledge"] is sub
        graph_store.subgraph.assert_called_once_with(ctx, "knowledge", ["h1", "x"], depth=1)

    async def test_all_graph_flags_on_expands_all_three_kinds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.retrieval.graphrag.is_enabled", lambda flag, ctx: True)
        graph_store = _mock_graph_store(
            {k: Subgraph(nodes=[GraphNode(node_type="e", key=k)], edges=[]) for k in ("code", "knowledge", "memory")}
        )

        result = await retrieve(
            _ctx("t1"),
            "q",
            embed_fn=_const_embed_fn(_ZERO_VECTOR),
            vector_stores={
                "docs_vectors": _mock_vector_store([_hit("h1", 0.5)]),
                "memory_vectors": _mock_vector_store(),
            },
            graph_store=graph_store,
        )

        assert set(result.subgraphs.keys()) == {"code", "knowledge", "memory"}
        assert graph_store.subgraph.call_count == 3

    async def test_one_kind_failure_degrades_to_empty_subgraph_others_intact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.retrieval.graphrag.is_enabled", lambda flag, ctx: True)
        graph_store = _mock_graph_store(
            {"knowledge": Subgraph(nodes=[GraphNode(node_type="e", key="k")], edges=[])},
            raise_for={"code"},
        )

        result = await retrieve(
            _ctx("t1"),
            "q",
            embed_fn=_const_embed_fn(_ZERO_VECTOR),
            vector_stores={
                "docs_vectors": _mock_vector_store([_hit("h1", 0.5)]),
                "memory_vectors": _mock_vector_store(),
            },
            graph_store=graph_store,
        )

        assert result.subgraphs["code"] == Subgraph(nodes=[], edges=[])
        assert len(result.subgraphs["knowledge"].nodes) == 1
        assert result.subgraphs["memory"] == Subgraph(nodes=[], edges=[])


# ---------------------------------------------------------------------------
# Live-Postgres tests: real PgVectorStore + PostgresGraphStore.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_dsn() -> str:
    assert TEST_DATABASE_URL is not None  # narrows type; skipif already guards this
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS penguincode CASCADE")
    run_migrations(dsn=TEST_DATABASE_URL)
    return TEST_DATABASE_URL


def _fixed_embedding(seed: float) -> list[float]:
    """A 768-dim vector distinguishable from other calls' by its first two dims."""
    vec = [0.0] * 768
    vec[0] = seed
    vec[1] = seed / 2.0
    return vec


def _vector_stores(db_dsn: str) -> dict[str, VectorStore]:
    return {
        "docs_vectors": PgVectorStore(db_dsn, table="docs_vectors"),
        "memory_vectors": PgVectorStore(db_dsn, table="memory_vectors"),
    }


@requires_postgres
class TestLiveHybridRetrieval:
    async def test_hybrid_retrieve_returns_vector_and_graph_within_scope(
        self, db_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.retrieval.graphrag.is_enabled", lambda flag, ctx: True)
        ctx = _ctx(tenant_id=str(uuid.uuid4()))
        embedding = _fixed_embedding(1.0)

        docs_store = PgVectorStore(db_dsn, table="docs_vectors")
        docs_store.upsert(
            ctx,
            [
                VectorItem(
                    id=str(uuid.uuid4()),
                    embedding=embedding,
                    document="penguincode uses Ollama for embeddings",
                    metadata={"node_key": "penguincode"},
                )
            ],
            visibility="tenant",
            team_id=None,
        )

        graph_store = PostgresGraphStore(dsn=db_dsn, schema="penguincode")
        for kind in ("code", "knowledge", "memory"):
            graph_store.upsert_edges(
                ctx,
                kind,
                [
                    GraphEdge(
                        src_type="component",
                        src_key="penguincode",
                        dst_type="technology",
                        dst_key="Ollama",
                        rel_type="uses",
                    )
                ],
                visibility="tenant",
                team_id=None,
            )

        result = await retrieve(
            ctx,
            "does penguincode use Ollama?",
            embed_fn=_const_embed_fn(embedding),
            vector_stores=_vector_stores(db_dsn),
            graph_store=graph_store,
            graph_depth=1,
        )

        assert len(result.vector_hits) == 1
        assert result.vector_hits[0].document == "penguincode uses Ollama for embeddings"
        assert set(result.subgraphs.keys()) == {"code", "knowledge", "memory"}
        for kind in ("code", "knowledge", "memory"):
            assert {n.key for n in result.subgraphs[kind].nodes} == {"penguincode", "Ollama"}
        assert "penguincode uses Ollama for embeddings" in result.context
        assert "Ollama" in result.context

    async def test_tenant_isolation_returns_nothing_for_a_different_tenant(
        self, db_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.retrieval.graphrag.is_enabled", lambda flag, ctx: True)
        ctx_a = _ctx(tenant_id=str(uuid.uuid4()))
        embedding = _fixed_embedding(2.0)

        docs_store = PgVectorStore(db_dsn, table="docs_vectors")
        docs_store.upsert(
            ctx_a,
            [
                VectorItem(
                    id=str(uuid.uuid4()),
                    embedding=embedding,
                    document="tenant-a-only secret document",
                    metadata={"node_key": "secret-node"},
                )
            ],
            visibility="tenant",
            team_id=None,
        )
        graph_store = PostgresGraphStore(dsn=db_dsn, schema="penguincode")
        graph_store.upsert_nodes(
            ctx_a,
            "knowledge",
            [GraphNode(node_type="entity", key="secret-node")],
            visibility="tenant",
            team_id=None,
        )

        ctx_b = _ctx(tenant_id=str(uuid.uuid4()))
        result = await retrieve(
            ctx_b,
            "secret",
            embed_fn=_const_embed_fn(embedding),
            vector_stores=_vector_stores(db_dsn),
            graph_store=graph_store,
        )

        # regression: penguincode-knowledge-platform -- a different tenant's
        # identical embedding must never surface tenant A's rows or nodes.
        assert result.vector_hits == []
        assert all(len(sg.nodes) == 0 for sg in result.subgraphs.values())

    async def test_graph_flag_independently_gates_code_while_others_expand(
        self, db_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx(tenant_id=str(uuid.uuid4()))
        embedding = _fixed_embedding(3.0)

        docs_store = PgVectorStore(db_dsn, table="docs_vectors")
        docs_store.upsert(
            ctx,
            [
                VectorItem(
                    id=str(uuid.uuid4()),
                    embedding=embedding,
                    document="gating document",
                    metadata={"node_key": "gate-node"},
                )
            ],
            visibility="tenant",
            team_id=None,
        )
        graph_store = PostgresGraphStore(dsn=db_dsn, schema="penguincode")
        for kind in ("code", "knowledge", "memory"):
            graph_store.upsert_nodes(
                ctx,
                kind,
                [GraphNode(node_type="entity", key="gate-node")],
                visibility="tenant",
                team_id=None,
            )

        def _flags(flag: str, _ctx: ScopeContext) -> bool:
            return flag != CODE_GRAPH_FLAG

        monkeypatch.setattr("penguincode_cli.retrieval.graphrag.is_enabled", _flags)

        result = await retrieve(
            ctx,
            "gating query",
            embed_fn=_const_embed_fn(embedding),
            vector_stores=_vector_stores(db_dsn),
            graph_store=graph_store,
        )

        assert "code" not in result.subgraphs
        assert set(result.subgraphs.keys()) == {"knowledge", "memory"}
        assert len(result.subgraphs["knowledge"].nodes) == 1
        assert len(result.subgraphs["memory"].nodes) == 1

    async def test_rag_flag_off_returns_empty_even_with_live_store_available(
        self, db_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.retrieval.graphrag.is_enabled", lambda flag, ctx: False)
        ctx = _ctx(tenant_id=str(uuid.uuid4()))
        graph_store = MagicMock(spec=GraphStore)

        result = await retrieve(
            ctx,
            "anything",
            embed_fn=AsyncMock(),
            vector_stores=_vector_stores(db_dsn),
            graph_store=graph_store,
        )

        assert result == RetrievalResult(vector_hits=[], subgraphs={}, context="")
        graph_store.subgraph.assert_not_called()


@requires_postgres
@requires_ollama
class TestLiveOllamaEmbedding:
    async def test_real_ollama_embedding_retrieves_the_matching_document(
        self, db_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "penguincode_cli.retrieval.graphrag.is_enabled", lambda flag, ctx: flag == RAG_FLAG
        )
        ctx = _ctx(tenant_id=str(uuid.uuid4()))
        text = "PenguinCode's GraphRAG module embeds queries with nomic-embed-text."

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{_TEST_OLLAMA_URL}/api/embeddings",
                json={"model": "nomic-embed-text", "prompt": text},
            )
            response.raise_for_status()
            embedding = [float(x) for x in response.json()["embedding"]]

        assert len(embedding) == 768

        docs_store = PgVectorStore(db_dsn, table="docs_vectors")
        docs_store.upsert(
            ctx,
            [VectorItem(id=str(uuid.uuid4()), embedding=embedding, document=text, metadata={})],
            visibility="tenant",
            team_id=None,
        )

        result = await retrieve(
            ctx,
            text,
            vector_stores=_vector_stores(db_dsn),
            graph_store=MagicMock(spec=GraphStore),  # no graph flags on -> never called
            ollama_base_url=_TEST_OLLAMA_URL,
        )

        assert len(result.vector_hits) == 1
        assert result.vector_hits[0].document == text
        assert result.vector_hits[0].score > 0.99
        assert result.subgraphs == {}
