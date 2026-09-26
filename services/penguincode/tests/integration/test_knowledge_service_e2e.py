"""T16: full end-to-end proof of the penguincode knowledge platform.

Every other live-Postgres test in this repo (`test_twire_live_integration.py`,
`test_server_knowledge_service.py`'s live class, `test_stores_*.py`) either
calls a module function directly or invokes `KnowledgeServiceImpl` against a
fake `grpc.aio.ServicerContext`. This module is the first to drive the real
`KnowledgeClient` -> real wire gRPC -> `WaddleAIAuthInterceptor` (RS256) ->
`KnowledgeServiceImpl` -> live pgvector Postgres path end to end, proving:

- `Index`/`Query`: docs vectors stored, knowledge graph populated, hybrid
  retrieval returns both vector hits and graph expansion.
- `MemoryAdd`/`MemorySearch`: memory stored and searchable, memory graph
  populated.
- `IndexCode`/`CodeGraphStatus`: code graph built from a real fixture repo,
  status reflects it.
- Scope isolation holds *through the RPC layer*, not just at the store layer.
- A graph flag OFF is a true no-op (no LLM call at all, not just an empty
  result).
- RS256 dev tokens are accepted; HS256 legacy tokens and missing tokens are
  both rejected `UNAUTHENTICATED`.
- The server actually emits OTel spans + metrics (and, since penguincode has
  no OTel *Logs* SDK pipeline today -- see conftest/otel.py -- stdlib log
  records via `caplog`, the closest available proxy for "logging occurred").

The LLM boundary (triple extraction) is mocked throughout, mirroring
`test_twire_live_integration.py`'s established convention -- the orchestration
model it would call (`gemma4:12b-it-qat`) is not pulled in every environment
this suite runs in, and every other "live" test in this repo makes the same
choice ("the LLM stays mocked even there; only the store write is live").
Embeddings are NOT mocked: `nomic-embed-text` is a small, deterministic,
already-pulled model, so every `Index`/`Query` call below exercises a real
Ollama HTTP round trip via `DocumentationIndexer`/`retrieval.graphrag`'s own
default (unmocked) embedding path.

# regression: penguincode-knowledge-platform (T16 -- end-to-end integration gate)
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import grpc
import pytest

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.client.knowledge_client import KnowledgeAuthError
from penguincode_cli.docs_rag.indexer import DocumentationIndexer
from penguincode_cli.graphs.knowledge import extract_knowledge as real_extract_knowledge
from penguincode_cli.graphs.memory import extract_memory_graph as real_extract_memory_graph
from penguincode_cli.ollama.types import ChatResponse, Message
from penguincode_cli.proto import CodeGraphStatusRequest, KnowledgeServiceStub
from penguincode_cli.stores.graph import GraphEdge, PostgresGraphStore
from penguincode_cli.stores.vector import PgVectorStore, VectorItem
from tests.integration.conftest import (
    DEFAULT_TEST_USER_ID,
    DevKeypair,
    RunningServer,
    StaticTokenProvider,
    TelemetrySink,
    mint_token,
)


def _ctx(
    tenant: str, *, team_ids: tuple[str, ...] = (), user_id: str = DEFAULT_TEST_USER_ID
) -> ScopeContext:
    return ScopeContext(
        tenant_id=tenant, org_id=None, team_ids=team_ids, user_id=user_id, scopes=("*",)
    )


def _chat_response(content: str) -> ChatResponse:
    return ChatResponse(
        model="gemma4:12b-it-qat",
        created_at="2026-09-25T00:00:00Z",
        message=Message(role="assistant", content=content),
        done=True,
    )


def _mock_ollama_client(*responses: str) -> MagicMock:
    """A mocked `OllamaClient` whose `.chat()` streams one chunk per string in `responses`.

    Mirrors `test_twire_live_integration.py`'s identical helper -- the LLM
    boundary is mocked, never the store write.
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


async def _ollama_embed(text: str, *, base_url: str = "http://localhost:11434") -> list[float]:
    """A real, direct `nomic-embed-text` embedding call -- mirrors
    `DocumentationIndexer._get_embedding`'s own default (unmocked) HTTP path.
    """
    import aiohttp

    async with (
        aiohttp.ClientSession() as session,
        session.post(
            f"{base_url}/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": text},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response,
    ):
        response.raise_for_status()
        data = await response.json()
        embedding: list[float] = data.get("embedding", [])
        return embedding


def _count_metric_points(metrics_data: Any) -> int:
    """Total data points across every instrument in an `InMemoryMetricReader` snapshot."""
    total = 0
    for rm in getattr(metrics_data, "resource_metrics", []) or []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                total += len(list(m.data.data_points))
    return total


def _repo_with_call_graph(root: Path) -> None:
    """A minimal two-function fixture repo with a guaranteed defines+calls edge shape."""
    (root / "main.py").write_text(
        "def helper():\n    return 1\n\n\ndef main():\n    return helper()\n",
        encoding="utf-8",
    )


class TestIndexQueryAndKnowledgeGraph:
    """`Index` -> docs vector + knowledge graph; `Query` -> hybrid retrieval."""

    async def test_index_writes_vector_row_and_knowledge_graph_scoped_to_tenant(
        self,
        knowledge_server: RunningServer,
        dev_keypair: DevKeypair,
        monkeypatch: pytest.MonkeyPatch,
        live_dsn: str,
        ollama_ready: None,
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "true")
        monkeypatch.setenv("PENGUINCODE_FLAG_KNOWLEDGE_GRAPH", "true")

        tenant_a = str(uuid.uuid4())
        tenant_b = str(uuid.uuid4())
        client_a = knowledge_server.client_for_tenant(dev_keypair, tenant=tenant_a)

        llm_client = _mock_ollama_client(_triples_json("penguincode", "uses", "pgvector"))

        async def _wired_extract_knowledge(ctx_arg: ScopeContext, text: str, **kwargs: Any) -> Any:
            return await real_extract_knowledge(ctx_arg, text, ollama_client=llm_client, **kwargs)

        with patch("penguincode_cli.docs_rag.indexer.extract_knowledge", _wired_extract_knowledge):
            chunks_indexed = await client_a.index(
                doc_contents=["penguincode uses pgvector for its vector store."],
                language="python",
                visibility="tenant",
            )

        print(f"T16 Index: {chunks_indexed} chunk(s) indexed for tenant {tenant_a}")
        assert chunks_indexed >= 1

        # Vector row landed for real, scoped to tenant A only.
        indexer = DocumentationIndexer(dsn=live_dsn)
        hits_a = await indexer.search(_ctx(tenant_a), "penguincode uses pgvector")
        hits_b = await indexer.search(_ctx(tenant_b), "penguincode uses pgvector")
        assert len(hits_a) >= 1
        assert hits_b == []

        # Knowledge graph enrichment landed for real, scoped to tenant A only.
        graph_store = PostgresGraphStore(dsn=live_dsn, schema="penguincode")
        subgraph_a = graph_store.subgraph(_ctx(tenant_a), "knowledge", ["penguincode"], depth=1)
        subgraph_b = graph_store.subgraph(_ctx(tenant_b), "knowledge", ["penguincode"], depth=1)
        print(
            f"T16 knowledge graph: {len(subgraph_a.nodes)} node(s), "
            f"{len(subgraph_a.edges)} edge(s) for tenant A; {len(subgraph_b.nodes)} for tenant B"
        )
        assert {n.key for n in subgraph_a.nodes} == {"penguincode", "pgvector"}
        assert len(subgraph_a.edges) == 1
        assert subgraph_b.nodes == []


class TestQueryHybridRetrieval:
    """`Query` RPC: real vector top-k merged with real graph expansion, over the wire."""

    async def test_query_returns_vector_hits_and_graph_subgraph(
        self,
        knowledge_server: RunningServer,
        dev_keypair: DevKeypair,
        monkeypatch: pytest.MonkeyPatch,
        live_dsn: str,
        ollama_ready: None,
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "true")
        monkeypatch.setenv("PENGUINCODE_FLAG_KNOWLEDGE_GRAPH", "true")

        tenant = str(uuid.uuid4())
        ctx = _ctx(tenant)
        query_text = "fastapi supports async routes"

        embedding = await _ollama_embed(query_text)
        chunk_id = str(uuid.uuid4())
        PgVectorStore(live_dsn, table="docs_vectors").upsert(
            ctx,
            [
                VectorItem(
                    id=chunk_id,
                    embedding=embedding,
                    document="FastAPI supports async routes",
                    metadata={"library": "fastapi"},
                )
            ],
            visibility="tenant",
            team_id=None,
        )
        # Seed-key contract (`graphrag._seed_keys_from_hits`): the vector
        # hit's own `id` is always a candidate graph seed, so an edge keyed
        # to `chunk_id` is guaranteed to be found by `Query`'s expansion.
        PostgresGraphStore(dsn=live_dsn, schema="penguincode").upsert_edges(
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

        client = knowledge_server.client(
            StaticTokenProvider(mint_token(dev_keypair, tenant=tenant, sub=ctx.user_id))
        )
        result = await client.query(query=query_text, n_vector=5, graph_depth=1)

        print(
            f"T16 Query: {len(result.vector_hits)} vector hit(s), "
            f"{sum(len(s.nodes) for s in result.subgraphs.values())} graph node(s) expanded"
        )
        assert len(result.vector_hits) >= 1
        assert result.vector_hits[0].id == chunk_id
        assert "knowledge" in result.subgraphs
        assert {n.key for n in result.subgraphs["knowledge"].nodes} >= {chunk_id, "async-routing"}


class TestMemoryAddSearchAndGraph:
    """`MemoryAdd` -> mem0 write + memory graph; `MemorySearch` reads it back."""

    async def test_memory_add_is_searchable_and_populates_memory_graph(
        self,
        knowledge_server: RunningServer,
        dev_keypair: DevKeypair,
        monkeypatch: pytest.MonkeyPatch,
        live_dsn: str,
        ollama_ready: None,
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "true")
        monkeypatch.setenv("PENGUINCODE_FLAG_MEMORY_GRAPH", "true")

        tenant = str(uuid.uuid4())
        team_id = str(uuid.uuid4())
        client = knowledge_server.client_for_tenant(dev_keypair, tenant=tenant, teams=(team_id,))

        llm_client = _mock_ollama_client(_triples_json("dark mode", "is_a", "preference"))

        async def _wired_extract_memory_graph(
            ctx_arg: ScopeContext, content: str, **kwargs: Any
        ) -> Any:
            return await real_extract_memory_graph(
                ctx_arg, content, ollama_client=llm_client, **kwargs
            )

        with patch(
            "penguincode_cli.tools.memory.extract_memory_graph", _wired_extract_memory_graph
        ):
            add_result = await client.memory_add(
                content="I like dark mode", visibility="team", team_id=team_id
            )

        assert add_result is not None
        assert len(add_result["results"]) >= 1
        print(f"T16 MemoryAdd: {len(add_result['results'])} result(s) stored")

        search_results = await client.memory_search(query="dark mode")
        print(f"T16 MemorySearch: {len(search_results)} item(s) returned")
        assert len(search_results) >= 1
        assert any("dark mode" in r["memory"] for r in search_results)

        graph_store = PostgresGraphStore(dsn=live_dsn, schema="penguincode")
        subgraph = graph_store.subgraph(
            _ctx(tenant, team_ids=(team_id,)), "memory", ["dark mode"], depth=1
        )
        print(f"T16 memory graph: {len(subgraph.nodes)} node(s)")
        assert {n.key for n in subgraph.nodes} == {"dark mode", "preference"}


class TestIndexCodeAndStatus:
    """`IndexCode` -> code graph from a real fixture repo; `CodeGraphStatus` reflects it."""

    async def test_index_code_populates_graph_and_status_matches(
        self,
        knowledge_server: RunningServer,
        dev_keypair: DevKeypair,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_CODE_GRAPH", "true")
        tenant = str(uuid.uuid4())
        client = knowledge_server.client_for_tenant(dev_keypair, tenant=tenant)

        repo = tmp_path / "fixture_repo"
        repo.mkdir()
        _repo_with_call_graph(repo)

        result = await client.index_code(root_path=str(repo), visibility="tenant")
        assert result is not None
        node_count, edge_count = result
        print(f"T16 IndexCode: {node_count} node(s), {edge_count} edge(s)")
        assert node_count >= 3  # file + helper() + main()
        assert edge_count >= 3  # 2 defines + 1 calls

        enabled, status_nodes, status_edges = await client.code_graph_status()
        assert enabled is True
        assert (status_nodes, status_edges) == (node_count, edge_count)


class TestFlagGatingIsANoOp:
    """A graph flag OFF must skip the LLM call entirely, not just return an empty result."""

    async def test_knowledge_graph_flag_off_never_calls_the_llm(
        self,
        knowledge_server: RunningServer,
        dev_keypair: DevKeypair,
        monkeypatch: pytest.MonkeyPatch,
        live_dsn: str,
        ollama_ready: None,
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "true")
        monkeypatch.setenv("PENGUINCODE_FLAG_KNOWLEDGE_GRAPH", "false")

        tenant = str(uuid.uuid4())
        client = knowledge_server.client_for_tenant(dev_keypair, tenant=tenant)

        # Wrap the REAL `extract_knowledge` (its `is_enabled(KNOWLEDGE_GRAPH_FLAG, ctx)`
        # gate must fire *before* touching `ollama_client`) -- swapping in a
        # full replacement instead would bypass the very gate this test is
        # proving, and trivially "pass" even if the gate were broken.
        llm_call_count = 0
        llm_client = _mock_ollama_client(_triples_json("penguincode", "uses", "pgvector"))
        real_chat = llm_client.chat

        def _counting_chat(*args: Any, **kwargs: Any) -> Any:
            nonlocal llm_call_count
            llm_call_count += 1
            return real_chat(*args, **kwargs)

        llm_client.chat = MagicMock(side_effect=_counting_chat)

        async def _wired_extract_knowledge(ctx_arg: ScopeContext, text: str, **kwargs: Any) -> Any:
            return await real_extract_knowledge(ctx_arg, text, ollama_client=llm_client, **kwargs)

        with patch("penguincode_cli.docs_rag.indexer.extract_knowledge", _wired_extract_knowledge):
            chunks_indexed = await client.index(
                doc_contents=["some text mentioning pgvector"], language="python"
            )

        assert chunks_indexed >= 1
        assert llm_call_count == 0, (
            "the real extract_knowledge flag gate must short-circuit before any LLM call"
        )

        graph_store = PostgresGraphStore(dsn=live_dsn, schema="penguincode")
        subgraph = graph_store.subgraph(_ctx(tenant), "knowledge", ["pgvector"], depth=1)
        assert subgraph.nodes == []


class TestScopeIsolationAcrossRPCs:
    """A second tenant's token must retrieve none of tenant A's docs/memory/code graph."""

    async def test_tenant_b_sees_none_of_tenant_as_data(
        self,
        knowledge_server: RunningServer,
        dev_keypair: DevKeypair,
        monkeypatch: pytest.MonkeyPatch,
        live_dsn: str,
        tmp_path: Path,
        ollama_ready: None,
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "true")
        monkeypatch.setenv("PENGUINCODE_FLAG_CODE_GRAPH", "true")

        tenant_a = str(uuid.uuid4())
        tenant_b = str(uuid.uuid4())
        client_a = knowledge_server.client_for_tenant(dev_keypair, tenant=tenant_a)
        client_b = knowledge_server.client_for_tenant(dev_keypair, tenant=tenant_b)

        tenant_a_doc = "waddleai grpc knowledge service isolation fixture"
        chunks_indexed = await client_a.index(doc_contents=[tenant_a_doc], language="python")
        assert chunks_indexed >= 1

        result_b = await client_b.query(query=tenant_a_doc, n_vector=5)
        print(f"T16 isolation/docs: tenant B query returned {len(result_b.vector_hits)} hit(s)")
        assert result_b.vector_hits == []

        add_result = await client_a.memory_add(content="tenant-a-only-secret-preference")
        assert add_result is not None
        memories_b = await client_b.memory_search(query="tenant-a-only-secret-preference")
        print(f"T16 isolation/memory: tenant B search returned {len(memories_b)} item(s)")
        assert memories_b == []

        repo = tmp_path / "isolation_repo"
        repo.mkdir()
        _repo_with_call_graph(repo)
        code_result_a = await client_a.index_code(root_path=str(repo))
        assert code_result_a is not None

        graph_store = PostgresGraphStore(dsn=live_dsn, schema="penguincode")
        isolated_code = graph_store.subgraph(_ctx(tenant_b), "code", ["main.py"], depth=1)
        print(f"T16 isolation/code-graph: tenant B subgraph has {len(isolated_code.nodes)} node(s)")
        assert isolated_code.nodes == []

        status_b = await client_b.code_graph_status()
        assert status_b == (True, 0, 0)


class TestAuthRejection:
    """RS256 dev tokens validate; HS256 legacy tokens and missing tokens do not."""

    async def test_missing_token_is_unauthenticated(self, knowledge_server: RunningServer) -> None:
        channel = grpc.aio.insecure_channel(f"{knowledge_server.host}:{knowledge_server.port}")
        try:
            stub = KnowledgeServiceStub(channel)  # type: ignore[no-untyped-call]
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.CodeGraphStatus(CodeGraphStatusRequest(api_version="v1"), metadata=[])
            assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED
        finally:
            await channel.close()

    async def test_hs256_legacy_token_is_rejected(
        self, knowledge_server: RunningServer, dev_keypair: DevKeypair
    ) -> None:
        hs256_token = mint_token(
            dev_keypair,
            tenant="tenant-x",
            algorithm="HS256",
            key="not-the-real-rs256-keypair",
        )
        channel = grpc.aio.insecure_channel(f"{knowledge_server.host}:{knowledge_server.port}")
        try:
            stub = KnowledgeServiceStub(channel)  # type: ignore[no-untyped-call]
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.CodeGraphStatus(
                    CodeGraphStatusRequest(api_version="v1"),
                    metadata=[("authorization", f"Bearer {hs256_token}")],
                )
            assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED
        finally:
            await channel.close()

    async def test_knowledge_client_wraps_rejection_as_knowledge_auth_error(
        self, knowledge_server: RunningServer
    ) -> None:
        client = knowledge_server.client(StaticTokenProvider(None))
        with pytest.raises(KnowledgeAuthError):
            await client.code_graph_status()
        await client.close()


class TestTelemetryEmission:
    """The server must emit real OTel spans + metrics (and stdlib log records) during these ops."""

    async def test_index_and_query_emit_otel_spans_metrics_and_log_records(
        self,
        knowledge_server: RunningServer,
        dev_keypair: DevKeypair,
        monkeypatch: pytest.MonkeyPatch,
        otel_sink: TelemetrySink,
        caplog: pytest.LogCaptureFixture,
        ollama_ready: None,
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "true")
        monkeypatch.setenv("PENGUINCODE_FLAG_KNOWLEDGE_GRAPH", "true")
        caplog.set_level("INFO", logger="penguincode_cli")

        tenant = str(uuid.uuid4())
        client = knowledge_server.client_for_tenant(dev_keypair, tenant=tenant)

        llm_client = _mock_ollama_client(_triples_json("penguincode", "uses", "otel"))

        async def _wired_extract_knowledge(ctx_arg: ScopeContext, text: str, **kwargs: Any) -> Any:
            return await real_extract_knowledge(ctx_arg, text, ollama_client=llm_client, **kwargs)

        with patch("penguincode_cli.docs_rag.indexer.extract_knowledge", _wired_extract_knowledge):
            chunks_indexed = await client.index(
                doc_contents=["penguincode uses otel for telemetry."], language="python"
            )
        assert chunks_indexed >= 1

        query_result = await client.query(query="penguincode uses otel for telemetry.", n_vector=5)
        assert len(query_result.vector_hits) >= 1

        spans = otel_sink.spans.get_finished_spans()
        metric_points = _count_metric_points(otel_sink.metrics.get_metrics_data())
        log_records = [r for r in caplog.records if r.name.startswith("penguincode_cli")]

        print(
            f"T16 telemetry: {len(spans)} span(s), {metric_points} metric data point(s), "
            f"{len(log_records)} stdlib log record(s) "
            "(no OTel Logs SDK pipeline exists in penguincode yet -- see observability/otel.py; "
            "stdlib logging via caplog is the closest available proxy for 'logging occurred')"
        )

        assert len(spans) >= 1, "expected at least one span across Index/Query"
        assert metric_points >= 1, "expected at least one metric data point recorded"
        assert len(log_records) >= 1, "expected at least one stdlib log record captured"
