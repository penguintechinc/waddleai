"""Knowledge-graph extractor (T12): LLM triples -> GraphNode/GraphEdge writes.

Static tests use a mocked `OllamaClient` (no network) and a fake `GraphStore`
(no DB) -- the extraction logic (prompting, JSON parsing/repair, node/edge
construction, scope-stamped writes) is fully exercised without either live
dependency. One optional live test at the bottom writes through a real
`PostgresGraphStore` against `TEST_DATABASE_URL`, mirroring
`tests/test_stores_graph.py`'s skip-with-reason pattern -- the LLM stays
mocked even there; only the store write is live.

# regression: penguincode-knowledge-platform (T12 -- knowledge graph)
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import psycopg
import pytest

from db.migrate import run_migrations
from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.graphs.knowledge import DEFAULT_VISIBILITY, extract_knowledge
from penguincode_cli.ollama.types import ChatResponse, Message
from penguincode_cli.stores.graph import GraphStore, PostgresGraphStore

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set -- live-Postgres knowledge-graph tests are CI-pending (T16)",
)


def _ctx(tenant_id: str = "t1", **overrides: Any) -> ScopeContext:
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "org_id": None,
        "team_ids": (),
        "user_id": str(uuid.uuid4()),
        "scopes": (),
    }
    defaults.update(overrides)
    return ScopeContext(**defaults)


def _chat_response(content: str) -> ChatResponse:
    return ChatResponse(
        model="gemma4:12b-it-qat",
        created_at="2026-09-25T00:00:00Z",
        message=Message(role="assistant", content=content),
        done=True,
    )


def _mock_ollama_client(*responses: str) -> MagicMock:
    """A mocked `OllamaClient` whose `.chat()` streams one chunk per string in `responses`."""

    async def _chat(*_args: Any, **_kwargs: Any) -> AsyncIterator[ChatResponse]:
        for content in responses:
            yield _chat_response(content)

    client = MagicMock()
    client.chat = MagicMock(side_effect=lambda *a, **kw: _chat(*a, **kw))
    return client


def _fake_graph_store() -> MagicMock:
    store = MagicMock(spec=GraphStore)
    return store


TRIPLES_JSON = json.dumps(
    {
        "triples": [
            {
                "subject": "penguincode",
                "subject_type": "component",
                "relation": "uses",
                "object": "Ollama",
                "object_type": "technology",
            },
            {
                "subject": "penguincode",
                "subject_type": "component",
                "relation": "uses",
                "object": "pgvector",
                "object_type": "technology",
            },
        ]
    }
)


# ---------------------------------------------------------------------------
# Flag gating: OFF must call neither the LLM nor the store.
# ---------------------------------------------------------------------------


class TestFlagGating:
    async def test_flag_off_skips_llm_and_writes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: False)
        client = _mock_ollama_client(TRIPLES_JSON)
        store = _fake_graph_store()

        result = await extract_knowledge(
            _ctx(), "some documentation text", ollama_client=client, graph_store=store
        )

        assert result.nodes == []
        assert result.edges == []
        client.chat.assert_not_called()
        store.upsert_nodes.assert_not_called()
        store.upsert_edges.assert_not_called()

    async def test_flag_on_calls_llm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client(TRIPLES_JSON)
        store = _fake_graph_store()

        await extract_knowledge(
            _ctx(), "some documentation text", ollama_client=client, graph_store=store
        )

        client.chat.assert_called_once()
        store.upsert_nodes.assert_called_once()
        store.upsert_edges.assert_called_once()


# ---------------------------------------------------------------------------
# Happy path: valid JSON triples -> expected nodes/edges, scope-stamped write.
# ---------------------------------------------------------------------------


class TestHappyPath:
    async def test_produces_expected_nodes_and_edges(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client(TRIPLES_JSON)
        store = _fake_graph_store()
        ctx = _ctx()

        result = await extract_knowledge(
            ctx, "penguincode uses Ollama and pgvector", ollama_client=client, graph_store=store
        )

        node_keys = {(n.node_type, n.key) for n in result.nodes}
        assert node_keys == {
            ("component", "penguincode"),
            ("technology", "Ollama"),
            ("technology", "pgvector"),
        }
        edge_tuples = {
            (e.src_type, e.src_key, e.rel_type, e.dst_type, e.dst_key) for e in result.edges
        }
        assert edge_tuples == {
            ("component", "penguincode", "uses", "technology", "Ollama"),
            ("component", "penguincode", "uses", "technology", "pgvector"),
        }

        store.upsert_nodes.assert_called_once_with(
            ctx, "knowledge", result.nodes, visibility=DEFAULT_VISIBILITY, team_id=None
        )
        store.upsert_edges.assert_called_once_with(
            ctx, "knowledge", result.edges, visibility=DEFAULT_VISIBILITY, team_id=None
        )

    async def test_default_visibility_is_tenant(self) -> None:
        assert DEFAULT_VISIBILITY == "tenant"

    async def test_explicit_visibility_and_team_id_passed_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client(TRIPLES_JSON)
        store = _fake_graph_store()
        ctx = _ctx(team_ids=("team-a",))

        await extract_knowledge(
            ctx,
            "text",
            visibility="team",
            team_id="team-a",
            ollama_client=client,
            graph_store=store,
        )

        _, kwargs = store.upsert_nodes.call_args
        assert kwargs["visibility"] == "team"
        assert kwargs["team_id"] == "team-a"

    async def test_source_id_stamped_into_node_props(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client(TRIPLES_JSON)
        store = _fake_graph_store()

        result = await extract_knowledge(
            _ctx(), "text", source_id="chunk-42", ollama_client=client, graph_store=store
        )

        assert all(n.props.get("source_id") == "chunk-42" for n in result.nodes)
        assert all(e.props.get("source_id") == "chunk-42" for e in result.edges)

    async def test_missing_source_id_yields_empty_props(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client(TRIPLES_JSON)
        store = _fake_graph_store()

        result = await extract_knowledge(_ctx(), "text", ollama_client=client, graph_store=store)

        assert all(n.props == {} for n in result.nodes)

    async def test_relation_is_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: True)
        raw = json.dumps(
            {"triples": [{"subject": "A", "relation": "Is Related To", "object": "B"}]}
        )
        client = _mock_ollama_client(raw)
        store = _fake_graph_store()

        result = await extract_knowledge(_ctx(), "text", ollama_client=client, graph_store=store)

        assert result.edges[0].rel_type == "is_related_to"

    async def test_missing_type_fields_default_to_entity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: True)
        raw = json.dumps({"triples": [{"subject": "A", "relation": "relates_to", "object": "B"}]})
        client = _mock_ollama_client(raw)
        store = _fake_graph_store()

        result = await extract_knowledge(_ctx(), "text", ollama_client=client, graph_store=store)

        assert {n.node_type for n in result.nodes} == {"entity"}

    async def test_empty_text_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client(TRIPLES_JSON)
        store = _fake_graph_store()

        result = await extract_knowledge(_ctx(), "   ", ollama_client=client, graph_store=store)

        assert result.nodes == []
        client.chat.assert_not_called()
        store.upsert_nodes.assert_not_called()


# ---------------------------------------------------------------------------
# Malformed LLM output: never crashes, degrades to a partial/empty extraction.
# ---------------------------------------------------------------------------


class TestMalformedOutputResilience:
    async def test_non_json_response_yields_empty_subgraph_no_writes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client("I'm sorry, I cannot extract triples from that.")
        store = _fake_graph_store()

        result = await extract_knowledge(_ctx(), "text", ollama_client=client, graph_store=store)

        assert result.nodes == []
        assert result.edges == []
        store.upsert_nodes.assert_not_called()
        store.upsert_edges.assert_not_called()

    async def test_json_wrapped_in_markdown_fence_is_recovered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: True)
        fenced = f"Here you go:\n```json\n{TRIPLES_JSON}\n```\nHope that helps!"
        client = _mock_ollama_client(fenced)
        store = _fake_graph_store()

        result = await extract_knowledge(_ctx(), "text", ollama_client=client, graph_store=store)

        assert len(result.edges) == 2

    async def test_bare_array_top_level_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: True)
        raw = json.dumps([{"subject": "A", "relation": "rel", "object": "B"}])
        client = _mock_ollama_client(raw)
        store = _fake_graph_store()

        result = await extract_knowledge(_ctx(), "text", ollama_client=client, graph_store=store)

        assert len(result.edges) == 1

    async def test_partial_malformed_entries_are_skipped_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: True)
        raw = json.dumps(
            {
                "triples": [
                    {"subject": "A", "relation": "rel", "object": "B"},  # valid
                    {"subject": "", "relation": "rel", "object": "C"},  # blank subject
                    {"subject": "D", "relation": "rel"},  # missing object
                    "not-a-dict",  # wrong type entirely
                    42,  # wrong type entirely
                ]
            }
        )
        client = _mock_ollama_client(raw)
        store = _fake_graph_store()

        result = await extract_knowledge(_ctx(), "text", ollama_client=client, graph_store=store)

        assert len(result.edges) == 1
        assert result.edges[0].src_key == "A"

    async def test_empty_triples_list_yields_empty_subgraph(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client(json.dumps({"triples": []}))
        store = _fake_graph_store()

        result = await extract_knowledge(_ctx(), "text", ollama_client=client, graph_store=store)

        assert result.nodes == []
        assert result.edges == []
        store.upsert_nodes.assert_not_called()

    async def test_unexpected_top_level_shape_yields_empty_subgraph(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client(json.dumps({"unexpected": "shape"}))
        store = _fake_graph_store()

        result = await extract_knowledge(_ctx(), "text", ollama_client=client, graph_store=store)

        assert result.nodes == []
        assert result.edges == []

    async def test_ollama_http_error_degrades_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: True)

        async def _raising_chat(*_args: Any, **_kwargs: Any) -> AsyncIterator[ChatResponse]:
            raise httpx.ConnectError("connection refused")
            yield  # pragma: no cover -- unreachable, makes this an async generator

        client = MagicMock()
        client.chat = MagicMock(side_effect=lambda *a, **kw: _raising_chat(*a, **kw))
        store = _fake_graph_store()

        result = await extract_knowledge(_ctx(), "text", ollama_client=client, graph_store=store)

        assert result.nodes == []
        assert result.edges == []
        store.upsert_nodes.assert_not_called()


# ---------------------------------------------------------------------------
# Default OllamaClient construction: when no ollama_client is injected, one
# is opened via `async with` and closed -- verified by patching the class.
# ---------------------------------------------------------------------------


class TestDefaultOllamaClientConstruction:
    async def test_builds_and_closes_own_client_when_none_injected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: True)

        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client

        async def _chat(*_a: Any, **_kw: Any) -> AsyncIterator[ChatResponse]:
            yield _chat_response(TRIPLES_JSON)

        fake_client.chat = MagicMock(side_effect=lambda *a, **kw: _chat(*a, **kw))

        constructed: dict[str, Any] = {}

        def _factory(*_args: Any, **kwargs: Any) -> AsyncMock:
            constructed.update(kwargs)
            return fake_client

        monkeypatch.setattr("penguincode_cli.graphs.knowledge.OllamaClient", _factory)
        store = _fake_graph_store()

        result = await extract_knowledge(_ctx(), "text", graph_store=store)

        assert len(result.edges) == 2
        fake_client.__aenter__.assert_awaited_once()
        fake_client.__aexit__.assert_awaited_once()


# ---------------------------------------------------------------------------
# Live-Postgres test: real GraphStore write, mocked LLM (see module docstring).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def graph_dsn() -> str:
    assert TEST_DATABASE_URL is not None  # narrows type; skipif already guards this
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS penguincode CASCADE")
    run_migrations(dsn=TEST_DATABASE_URL)
    return TEST_DATABASE_URL


@requires_postgres
class TestLiveGraphStoreWrite:
    async def test_extract_knowledge_writes_through_real_store(
        self, graph_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.graphs.knowledge.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client(TRIPLES_JSON)
        store = PostgresGraphStore(dsn=graph_dsn, schema="penguincode")
        ctx = _ctx(tenant_id=str(uuid.uuid4()))

        result = await extract_knowledge(
            ctx, "penguincode uses Ollama and pgvector", ollama_client=client, graph_store=store
        )
        assert len(result.nodes) == 3

        subgraph = store.subgraph(ctx, "knowledge", ["penguincode"], depth=1)
        assert {n.key for n in subgraph.nodes} == {"penguincode", "Ollama", "pgvector"}
        assert len(subgraph.edges) == 2

        # regression: penguincode-knowledge-platform -- a second tenant's
        # identical extraction must never be visible from `ctx`'s traversal.
        other_ctx = _ctx(tenant_id=str(uuid.uuid4()))
        await extract_knowledge(
            other_ctx,
            "penguincode uses Ollama and pgvector",
            ollama_client=client,
            graph_store=store,
        )
        isolated = store.subgraph(ctx, "knowledge", ["penguincode"], depth=1)
        assert len(isolated.nodes) == 3  # unchanged by the other tenant's write
