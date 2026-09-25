"""Tests for ``ContextInjector.get_relevant_context`` (F3: thin gRPC client).

``get_relevant_context`` used to call ``retrieval.graphrag.retrieve`` directly (T-wire); it
now calls the server's `Query` RPC via an injectable ``query_fn`` seam matching
``KnowledgeClient.query``'s signature, so these tests never touch a real Ollama/Postgres
*or* a real gRPC channel -- ``ContextInjector`` is constructed with
``KnowledgeClient.__new__`` (never connected) purely to satisfy the constructor's type, and
every RPC-shaped call goes through the injected ``query_fn``.

# regression: penguincode-knowledge-platform (F3 -- thin gRPC client + CLI conversion)
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.client.knowledge_client import (
    GraphEdge,
    GraphNode,
    KnowledgeClient,
    KnowledgeClientError,
    QueryResult,
    Subgraph,
    VectorHit,
)
from penguincode_cli.docs_rag.injector import ContextInjector
from penguincode_cli.docs_rag.models import Language, Library, ProjectContext


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


def _project_context(libraries: list[str] | None = None) -> ProjectContext:
    return ProjectContext(
        languages=[Language.PYTHON],
        libraries=[
            Library(name=name, language=Language.PYTHON) for name in (libraries or ["fastapi"])
        ],
    )


def _injector(query_fn) -> ContextInjector:  # type: ignore[no-untyped-def]
    # A bare, unconnected `KnowledgeClient` satisfies the constructor's type -- `query_fn`
    # is always what actually gets called in these tests, so the client is never used.
    client = KnowledgeClient.__new__(KnowledgeClient)
    return ContextInjector(client, query_fn=query_fn)


class TestGetRelevantContextThinClient:
    async def test_no_project_context_returns_empty_without_calling_query(self) -> None:
        calls: list[Any] = []

        async def _query(*args: Any, **kwargs: Any) -> QueryResult:
            calls.append((args, kwargs))
            return QueryResult(vector_hits=[], subgraphs={}, context="")

        injector = _injector(_query)
        empty_project = ProjectContext(languages=[], libraries=[])
        result = await injector.get_relevant_context(_ctx(), "anything", empty_project)

        assert result == ""
        assert calls == []

    async def test_ctx_is_ignored_query_is_still_called(self) -> None:
        """Unlike the pre-F3 local path, a `None` ScopeContext no longer skips the call --
        identity comes from the WaddleAI JWT the KnowledgeClient attaches, not `ctx`.
        """
        calls: list[Any] = []

        async def _query(*args: Any, **kwargs: Any) -> QueryResult:
            calls.append((args, kwargs))
            return QueryResult(vector_hits=[], subgraphs={}, context="")

        injector = _injector(_query)
        await injector.get_relevant_context(None, "how do I route requests", _project_context())

        assert len(calls) == 1

    async def test_query_called_with_query_and_n_vector_and_vector_tables(self) -> None:
        calls: list[Any] = []

        async def _query(**kwargs: Any) -> QueryResult:
            calls.append(kwargs)
            return QueryResult(vector_hits=[], subgraphs={}, context="")

        injector = _injector(_query)
        injector.max_chunks = 7
        await injector.get_relevant_context(_ctx(), "how do routes work", _project_context())

        assert len(calls) == 1
        assert calls[0]["query"] == "how do routes work"
        assert calls[0]["n_vector"] == 7
        assert calls[0]["vector_tables"] == ["docs_vectors"]

    async def test_vector_hits_are_adapted_into_formatted_context(self) -> None:
        hit = VectorHit(
            id="chunk-1",
            document="FastAPI routing uses decorators",
            metadata={"library": "fastapi", "section": "routing", "language": "python"},
            score=0.9,
        )

        async def _query(**kwargs: Any) -> QueryResult:
            return QueryResult(vector_hits=[hit], subgraphs={}, context="")

        injector = _injector(_query)
        result = await injector.get_relevant_context(
            _ctx(), "how do routes work", _project_context(["fastapi"])
        )

        assert "FastAPI routing uses decorators" in result
        assert "fastapi" in result.lower()

    async def test_graph_expansion_is_appended_as_a_section(self) -> None:
        hit = VectorHit(
            id="chunk-1",
            document="FastAPI routing uses decorators",
            metadata={"library": "fastapi"},
            score=0.9,
        )
        subgraph = Subgraph(
            nodes=[GraphNode(node_type="entity", key="FastAPI", props={})],
            edges=[
                GraphEdge(
                    src_type="entity",
                    src_key="FastAPI",
                    dst_type="entity",
                    dst_key="routing",
                    rel_type="supports",
                    props={},
                )
            ],
        )

        async def _query(**kwargs: Any) -> QueryResult:
            return QueryResult(vector_hits=[hit], subgraphs={"knowledge": subgraph}, context="")

        injector = _injector(_query)
        result = await injector.get_relevant_context(
            _ctx(), "fastapi routing", _project_context(["fastapi"])
        )

        assert "Related Knowledge Graph" in result
        assert "FastAPI --supports--> entity:routing" in result

    async def test_hits_outside_project_libraries_and_languages_are_filtered_out(self) -> None:
        # Neither the library nor the language matches this project
        # (Python-only, fastapi-only) -- an OR-of-library-or-language match.
        unrelated_hit = VectorHit(
            id="chunk-2",
            document="Rocket routing macros",
            metadata={"library": "rocket", "language": "rust"},
            score=0.99,
        )

        async def _query(**kwargs: Any) -> QueryResult:
            return QueryResult(vector_hits=[unrelated_hit], subgraphs={}, context="")

        injector = _injector(_query)
        result = await injector.get_relevant_context(
            _ctx(), "routing", _project_context(["fastapi"])
        )

        assert result == ""

    async def test_empty_query_result_returns_empty_string(self) -> None:
        async def _query(**kwargs: Any) -> QueryResult:
            return QueryResult(vector_hits=[], subgraphs={}, context="")

        injector = _injector(_query)
        result = await injector.get_relevant_context(_ctx(), "anything", _project_context())

        assert result == ""

    async def test_knowledge_client_error_degrades_to_empty_string_never_raises(self) -> None:
        async def _boom(**kwargs: Any) -> QueryResult:
            raise KnowledgeClientError("penguincode server unreachable")

        injector = _injector(_boom)
        result = await injector.get_relevant_context(_ctx(), "anything", _project_context())

        assert result == ""

    async def test_unexpected_failure_also_degrades_to_empty_string_never_raises(self) -> None:
        async def _boom(**kwargs: Any) -> QueryResult:
            raise RuntimeError("ollama outage")

        injector = _injector(_boom)
        result = await injector.get_relevant_context(_ctx(), "anything", _project_context())

        assert result == ""

    async def test_default_query_fn_is_the_bound_clients_query_method(self) -> None:
        """Without an explicit `query_fn=`, the injector wires `client.query` itself."""
        client = KnowledgeClient.__new__(KnowledgeClient)
        injector = ContextInjector(client)

        assert injector._query == client.query


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
