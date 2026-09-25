"""Live-Postgres end-to-end tests for T-wire's four wirings.

Unlike the static spy/mock tests in ``test_docs_rag_indexer.py``,
``test_memory.py``, ``test_repl_index_code.py``, and ``test_docs_rag_injector.py``
(which prove the wiring calls the right function with the right arguments),
these tests prove the full ``index -> extract -> retrieve`` path actually
persists and reads back through a **real** ``pgvector/pgvector:pg17``
Postgres (``TEST_DATABASE_URL``) -- vector rows, graph nodes/edges, and the
scope filter, all for real. The LLM call inside each extractor is still
mocked (an ``OllamaClient`` double streaming canned JSON triples), mirroring
``test_graphs_knowledge.py``/``test_graphs_memory.py``'s own live tests --
"the LLM stays mocked even there; only the store write is live."

# regression: penguincode-knowledge-platform (T-wire -- live index->extract->retrieve)
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import MemoryConfig, MemoryStoresConfig, PGVectorStoreConfig
from penguincode_cli.db.migrate import run_migrations
from penguincode_cli.docs_rag.indexer import DocumentationIndexer
from penguincode_cli.docs_rag.injector import ContextInjector
from penguincode_cli.docs_rag.models import Language, Library, ProjectContext
from penguincode_cli.ollama.types import ChatResponse, Message
from penguincode_cli.retrieval.graphrag import retrieve as graphrag_retrieve
from penguincode_cli.stores.graph import GraphEdge, PostgresGraphStore
from penguincode_cli.stores.vector import PgVectorStore, VectorItem
from penguincode_cli.tools.memory import MemoryManager, ScopedMemoryManager

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set -- live-Postgres T-wire integration tests are CI-pending",
)


def _ctx(**overrides: Any) -> ScopeContext:
    defaults: dict[str, Any] = {
        "tenant_id": str(uuid.uuid4()),
        "org_id": None,
        "team_ids": (),
        "user_id": str(uuid.uuid4()),
        "scopes": (),
    }
    defaults.update(overrides)
    return ScopeContext(**defaults)


async def _fake_embed(text: str) -> list[float]:
    """Deterministic 768-dim embedding: identical text -> identical vector."""
    vec = [0.0] * 768
    seed = float((hash(text) % 1000) + 1)
    vec[0] = seed
    vec[1] = 1.0
    return vec


def _chat_response(content: str) -> ChatResponse:
    return ChatResponse(
        model="gemma4:12b-it-qat",
        created_at="2026-09-25T00:00:00Z",
        message=Message(role="assistant", content=content),
        done=True,
    )


def _mock_ollama_client(*responses: str) -> MagicMock:
    """A mocked `OllamaClient` whose `.chat()` streams one chunk per string in `responses`.

    Mirrors `test_graphs_knowledge.py`/`test_graphs_memory.py`'s identical helper.
    """

    async def _chat(*_args: Any, **_kwargs: Any) -> AsyncIterator[ChatResponse]:
        for content in responses:
            yield _chat_response(content)

    client = MagicMock()
    client.chat = MagicMock(side_effect=lambda *a, **kw: _chat(*a, **kw))
    return client


def _triples_json(subject: str, relation: str, obj: str) -> str:
    return json.dumps(
        {
            "triples": [
                {
                    "subject": subject,
                    "subject_type": "component",
                    "relation": relation,
                    "object": obj,
                    "object_type": "technology",
                }
            ]
        }
    )


@pytest.fixture
def live_dsn() -> Iterator[str]:
    """Fresh, migrated `penguincode` schema for every test."""
    assert TEST_DATABASE_URL is not None
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS penguincode CASCADE")
    run_migrations(dsn=TEST_DATABASE_URL)
    yield TEST_DATABASE_URL


@requires_postgres
class TestLiveDocsIndexTriggersKnowledgeExtraction:
    """T-wire wiring 1: `DocumentationIndexer` -> `graphs.knowledge.extract_knowledge`."""

    async def test_indexing_writes_both_vector_row_and_knowledge_graph(
        self, live_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "true")
        monkeypatch.setenv("PENGUINCODE_FLAG_KNOWLEDGE_GRAPH", "true")

        ctx = _ctx()
        graph_store = PostgresGraphStore(dsn=live_dsn, schema="penguincode")
        llm_client = _mock_ollama_client(_triples_json("penguincode", "uses", "pgvector"))

        from penguincode_cli.graphs.knowledge import extract_knowledge as real_extract_knowledge

        async def _wired_extract_knowledge(ctx_arg, text, **kwargs):  # type: ignore[no-untyped-def]
            # Same wiring the indexer performs (ctx/text/source_id/visibility/
            # team_id) -- only the LLM call and the GraphStore instance are
            # swapped for a mock/live-but-injected double, exactly like
            # `test_graphs_knowledge.py`'s own live test.
            return await real_extract_knowledge(
                ctx_arg, text, ollama_client=llm_client, graph_store=graph_store, **kwargs
            )

        indexer = DocumentationIndexer(
            dsn=live_dsn, embed_fn=_fake_embed, metadata_dir=str(tmp_path)
        )
        library = Library(name="fastapi", language=Language.PYTHON, version="1.0")

        with patch("penguincode_cli.docs_rag.indexer.extract_knowledge", _wired_extract_knowledge):
            count = await indexer.index_library(ctx, library, ["penguincode uses pgvector"])

        # Primary write: the vector row landed for real.
        assert count == 1
        results = await indexer.search(ctx, "penguincode uses pgvector")
        assert len(results) == 1

        # Enrichment write: the knowledge graph landed for real, scoped to ctx.
        subgraph = graph_store.subgraph(ctx, "knowledge", ["penguincode"], depth=1)
        assert {n.key for n in subgraph.nodes} == {"penguincode", "pgvector"}
        assert len(subgraph.edges) == 1

        # Cross-tenant isolation still holds for the enrichment write.
        other_ctx = _ctx()
        isolated = graph_store.subgraph(other_ctx, "knowledge", ["penguincode"], depth=1)
        assert isolated.nodes == []


@requires_postgres
class TestLiveMemoryAddTriggersMemoryGraphExtraction:
    """T-wire wiring 2: `ScopedMemoryManager.add` -> `graphs.memory.extract_memory_graph`."""

    async def test_add_writes_memory_graph_with_the_exact_write_time_scope_stamp(
        self, live_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "true")
        monkeypatch.setenv("PENGUINCODE_FLAG_MEMORY_GRAPH", "true")

        team_id = str(uuid.uuid4())
        ctx = _ctx(team_ids=(team_id,))
        graph_store = PostgresGraphStore(dsn=live_dsn, schema="penguincode")
        llm_client = _mock_ollama_client(_triples_json("dark mode", "is_a", "preference"))

        # mem0 itself is mocked at its own boundary (same pattern as
        # `test_memory.py`'s `_enabled_manager_with_fake_mem0`) -- only the
        # memory-graph extraction's GraphStore write is live here.
        config = MemoryConfig(
            enabled=True,
            vector_store="pgvector",
            stores=MemoryStoresConfig(
                pgvector=PGVectorStoreConfig(url=live_dsn, table_name="test_memory")
            ),
        )
        with patch("penguincode_cli.tools.memory.Memory") as mock_memory_cls:
            fake_mem0 = MagicMock()
            fake_mem0.add.return_value = {
                "results": [{"id": "1", "memory": "I like dark mode", "event": "ADD"}]
            }
            mock_memory_cls.from_config.return_value = fake_mem0
            manager = MemoryManager(config, ollama_url="http://localhost:11434")

        scoped = ScopedMemoryManager(manager)

        from penguincode_cli.graphs.memory import extract_memory_graph as real_extract_memory_graph

        async def _wired_extract_memory_graph(ctx_arg, content, **kwargs):  # type: ignore[no-untyped-def]
            return await real_extract_memory_graph(
                ctx_arg, content, ollama_client=llm_client, graph_store=graph_store, **kwargs
            )

        with patch(
            "penguincode_cli.tools.memory.extract_memory_graph", _wired_extract_memory_graph
        ):
            result = await scoped.add(ctx, "I like dark mode", visibility="team", team_id=team_id)

        assert result is not None

        # The memory-graph write landed for real, at the team visibility the
        # memory was written with (T13's `source_metadata` override), never
        # the `visibility="user"` keyword default.
        subgraph = graph_store.subgraph(ctx, "memory", ["dark mode"], depth=1)
        assert {n.key for n in subgraph.nodes} == {"dark mode", "preference"}

        # A caller in a *different* team, same tenant, must not see it --
        # proving the write really landed as team-scoped, not tenant-wide.
        other_team_ctx = _ctx(tenant_id=ctx.tenant_id, team_ids=(str(uuid.uuid4()),))
        isolated = graph_store.subgraph(other_team_ctx, "memory", ["dark mode"], depth=1)
        assert isolated.nodes == []


@requires_postgres
class TestLiveHybridRetrieval:
    """T-wire wiring 4: `ContextInjector.get_relevant_context` -> `graphrag.retrieve`."""

    async def test_get_relevant_context_returns_vector_and_graph_content(
        self, live_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "true")
        monkeypatch.setenv("PENGUINCODE_FLAG_KNOWLEDGE_GRAPH", "true")
        ctx = _ctx()
        vector_store = PgVectorStore(live_dsn, table="docs_vectors")
        graph_store = PostgresGraphStore(dsn=live_dsn, schema="penguincode")

        query = "fastapi supports async routes"
        chunk_id = str(uuid.uuid4())
        vector_store.upsert(
            ctx,
            [
                VectorItem(
                    id=chunk_id,
                    embedding=await _fake_embed(query),
                    document="FastAPI supports async routes",
                    metadata={"library": "fastapi", "language": "python"},
                )
            ],
            visibility="tenant",
            team_id=None,
        )
        # The graph node's key intentionally equals the vector row's id --
        # `graphrag._seed_keys_from_hits`' documented `hit.id` fallback seed
        # candidate, for a producer that reuses the same identifier as both
        # (``graph_nodes.key`` is plain text, so a UUID string is fine).
        graph_store.upsert_edges(
            ctx,
            "knowledge",
            [
                GraphEdge(
                    src_type="entity",
                    src_key=chunk_id,
                    dst_type="entity",
                    dst_key="async-routing",
                    rel_type="describes",
                )
            ],
            visibility="tenant",
            team_id=None,
        )

        async def _retrieve_live(ctx_arg, q, **kwargs):  # type: ignore[no-untyped-def]
            return await graphrag_retrieve(ctx_arg, q, embed_fn=_fake_embed, dsn=live_dsn, **kwargs)

        indexer = DocumentationIndexer.__new__(DocumentationIndexer)
        injector = ContextInjector(indexer=indexer, retrieve_fn=_retrieve_live)
        project_context = ProjectContext(
            languages=[Language.PYTHON],
            libraries=[Library(name="fastapi", language=Language.PYTHON)],
        )

        context = await injector.get_relevant_context(ctx, query, project_context)

        assert "FastAPI supports async routes" in context
        assert "Related Knowledge Graph" in context
        assert f"{chunk_id} --describes--> entity:async-routing" in context
