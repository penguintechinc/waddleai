"""Tests for ``ContextInjector.get_relevant_context`` (T-wire: hybrid GraphRAG retrieval).

``get_relevant_context`` used to call ``DocumentationIndexer.search`` (vector
only); it now calls ``retrieval.graphrag.retrieve`` (vector + scoped graph
expansion) via an injectable ``retrieve_fn`` seam, so these tests never touch
a real Ollama/Postgres.

# regression: penguincode-knowledge-platform (T-wire -- hybrid retrieval upgrade)
"""

from __future__ import annotations

import uuid
from typing import Any

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.docs_rag.indexer import DocumentationIndexer
from penguincode_cli.docs_rag.injector import ContextInjector
from penguincode_cli.docs_rag.models import Language, Library, ProjectContext
from penguincode_cli.retrieval.graphrag import RetrievalResult
from penguincode_cli.stores.graph import GraphEdge, GraphNode, Subgraph
from penguincode_cli.stores.vector import VectorHit


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


def _injector(retrieve_fn) -> ContextInjector:  # type: ignore[no-untyped-def]
    # `indexer` is unused by the hybrid path but still required by the
    # constructor -- a bare, unconfigured instance is fine (never called).
    indexer = DocumentationIndexer.__new__(DocumentationIndexer)
    return ContextInjector(indexer=indexer, retrieve_fn=retrieve_fn)


class TestGetRelevantContextHybrid:
    async def test_no_scope_ctx_returns_empty_without_calling_retrieve(self) -> None:
        calls: list[Any] = []

        async def _retrieve(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append((args, kwargs))
            return RetrievalResult(vector_hits=[], subgraphs={}, context="")

        injector = _injector(_retrieve)
        result = await injector.get_relevant_context(
            None, "how do I route requests", _project_context()
        )

        assert result == ""
        assert calls == []

    async def test_no_project_context_returns_empty_without_calling_retrieve(self) -> None:
        calls: list[Any] = []

        async def _retrieve(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append((args, kwargs))
            return RetrievalResult(vector_hits=[], subgraphs={}, context="")

        injector = _injector(_retrieve)
        empty_project = ProjectContext(languages=[], libraries=[])
        result = await injector.get_relevant_context(_ctx(), "anything", empty_project)

        assert result == ""
        assert calls == []

    async def test_vector_hits_are_adapted_into_formatted_context(self) -> None:
        ctx = _ctx()
        hit = VectorHit(
            id="chunk-1",
            document="FastAPI routing uses decorators",
            metadata={"library": "fastapi", "section": "routing", "language": "python"},
            score=0.9,
        )

        async def _retrieve(passed_ctx, query, **kwargs):  # type: ignore[no-untyped-def]
            assert passed_ctx is ctx
            assert query == "how do routes work"
            return RetrievalResult(vector_hits=[hit], subgraphs={}, context="")

        injector = _injector(_retrieve)
        result = await injector.get_relevant_context(
            ctx, "how do routes work", _project_context(["fastapi"])
        )

        assert "FastAPI routing uses decorators" in result
        assert "fastapi" in result.lower()

    async def test_graph_expansion_is_appended_as_a_section(self) -> None:
        ctx = _ctx()
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
                )
            ],
        )

        async def _retrieve(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            return RetrievalResult(vector_hits=[hit], subgraphs={"knowledge": subgraph}, context="")

        injector = _injector(_retrieve)
        result = await injector.get_relevant_context(
            ctx, "fastapi routing", _project_context(["fastapi"])
        )

        assert "Related Knowledge Graph" in result
        assert "FastAPI --supports--> entity:routing" in result

    async def test_hits_outside_project_libraries_and_languages_are_filtered_out(self) -> None:
        ctx = _ctx()
        # Neither the library nor the language matches this project
        # (Python-only, fastapi-only) -- an OR-of-library-or-language match,
        # same semantics as the pre-hybrid `_where_variants` filter.
        unrelated_hit = VectorHit(
            id="chunk-2",
            document="Rocket routing macros",
            metadata={"library": "rocket", "language": "rust"},
            score=0.99,
        )

        async def _retrieve(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            return RetrievalResult(vector_hits=[unrelated_hit], subgraphs={}, context="")

        injector = _injector(_retrieve)
        result = await injector.get_relevant_context(ctx, "routing", _project_context(["fastapi"]))

        assert result == ""

    async def test_empty_retrieval_result_returns_empty_string(self) -> None:
        async def _retrieve(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            return RetrievalResult(vector_hits=[], subgraphs={}, context="")

        injector = _injector(_retrieve)
        result = await injector.get_relevant_context(_ctx(), "anything", _project_context())

        assert result == ""

    async def test_retrieve_failure_degrades_to_empty_string_never_raises(self) -> None:
        async def _boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("ollama outage")

        injector = _injector(_boom)
        result = await injector.get_relevant_context(_ctx(), "anything", _project_context())

        assert result == ""

    async def test_default_retrieve_fn_is_graphrag_retrieve(self) -> None:
        """Without an explicit `retrieve_fn=`, the injector wires the real `graphrag.retrieve`."""
        from penguincode_cli.retrieval import graphrag

        indexer = DocumentationIndexer.__new__(DocumentationIndexer)
        injector = ContextInjector(indexer=indexer)

        assert injector._retrieve is graphrag.retrieve
