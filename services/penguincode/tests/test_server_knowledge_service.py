"""Tests for `penguincode_cli.server.services.knowledge` -- `KnowledgeServiceImpl` (F2).

TDD: written before `penguincode_cli/server/services/knowledge.py` existed;
must fail with an ImportError/ModuleNotFoundError until implemented. Proves,
per RPC:

- No `ScopeContext` present (`current_scope_context()` returns `None`) ->
  `UNAUTHENTICATED`, before any knowledge-module call.
- The right underlying module is called with the caller's `ctx` and the
  request's fields mapped correctly (never a client-supplied tenant/org/
  team/user -- only `ctx` from the auth layer).
- The Python-side result is mapped back into the right proto response shape.

`retrieve` (`retrieval.graphrag`) and `index_code` (`graphs.code`) are
patched at the `knowledge` module's own import site (`monkeypatch.setattr`)
since the servicer calls them as plain functions, not injected objects.
`DocumentationIndexer`/`ScopedMemoryManager` are injected via the
constructor's `_IndexerLike`/`_ScopedMemoryLike` structural seams instead,
mirroring `stores.vector.VectorStore`'s Protocol-based test-double style
used throughout this codebase (see e.g. `test_graphs_code.py`'s
`_FakeGraphStore`).

A live-Postgres class at the bottom proves the security property no mock can:
tenant A's `IndexCode` write is invisible to tenant B's `ScopeContext`.

# regression: penguincode-knowledge-platform (F2 -- KnowledgeService server + auth wiring)
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import grpc
import psycopg
import pytest

import penguincode_cli.auth.middleware as auth_middleware
import penguincode_cli.server.services.knowledge as knowledge_module
from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import GraphConfig, PostgresGraphStoreConfig, Settings
from penguincode_cli.db.migrate import run_migrations
from penguincode_cli.docs_rag.models import Language as ModelLanguage
from penguincode_cli.docs_rag.models import Library
from penguincode_cli.graphs.code import ExtractionResult
from penguincode_cli.proto import (
    CodeGraphStatusRequest,
    IndexCodeRequest,
    IndexRequest,
    LibraryTarget,
    MemoryAddRequest,
    MemorySearchRequest,
    QueryRequest,
    Visibility,
)
from penguincode_cli.proto import (
    Language as ProtoLanguage,
)
from penguincode_cli.retrieval.graphrag import RetrievalResult
from penguincode_cli.server.services.knowledge import KnowledgeServiceImpl
from penguincode_cli.stores.graph import GraphEdge, GraphNode, PostgresGraphStore, Subgraph
from penguincode_cli.stores.vector import VectorHit

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set -- live-Postgres KnowledgeService tests are CI-pending",
)


def _ctx(tenant_id: str = "tenant-a", **overrides: Any) -> ScopeContext:
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "org_id": "org-a",
        "team_ids": ("team-a",),
        "user_id": "user-a",
        "scopes": ("knowledge:read", "knowledge:write"),
    }
    defaults.update(overrides)
    return ScopeContext(**defaults)


class AbortCalledError(Exception):
    """Raised by `_FakeContext.abort` -- mirrors real `grpc.aio` abort semantics."""


class _FakeContext:
    """Minimal `grpc.aio.ServicerContext` double: records the abort call and raises."""

    def __init__(self) -> None:
        self.aborted_with: tuple[Any, str] | None = None

    async def abort(self, code: Any, details: str) -> None:
        self.aborted_with = (code, details)
        raise AbortCalledError(details)


@pytest.fixture
def scope_ctx() -> Iterator[ScopeContext]:
    """Install a real `ScopeContext` into the auth contextvar for the test's duration."""
    ctx = _ctx()
    token = auth_middleware._current_scope.set(ctx)
    yield ctx
    auth_middleware._current_scope.reset(token)


@pytest.fixture(autouse=True)
def _no_leftover_scope() -> Iterator[None]:
    """Guarantee `current_scope_context()` is `None` by default in every test."""
    assert auth_middleware.current_scope_context() is None
    yield
    auth_middleware._current_scope.set(None)


class _FakeIndexer:
    """Records `index_library`/`index_language` calls; returns a fixed chunk count."""

    def __init__(self, *, chunks: int = 3) -> None:
        self.index_library = AsyncMock(return_value=chunks)
        self.index_language = AsyncMock(return_value=chunks)


class _FakeScopedMemory:
    """Records `add`/`search` calls; results are set per-test via the AsyncMock."""

    def __init__(self) -> None:
        self.add = AsyncMock(return_value=None)
        self.search = AsyncMock(return_value=[])


def _service(
    *,
    indexer: _FakeIndexer | None = None,
    scoped_memory: _FakeScopedMemory | None = None,
    graph_config: GraphConfig | None = None,
) -> KnowledgeServiceImpl:
    return KnowledgeServiceImpl(
        Settings(),
        indexer=indexer or _FakeIndexer(),
        scoped_memory=scoped_memory or _FakeScopedMemory(),
        graph_config=graph_config,
    )


# ---------------------------------------------------------------------------
# UNAUTHENTICATED: every RPC aborts before calling any knowledge module.
# ---------------------------------------------------------------------------


class TestRequireScope:
    @pytest.mark.asyncio
    async def test_index_without_scope_aborts_unauthenticated(self) -> None:
        service = _service()
        context = _FakeContext()
        with pytest.raises(AbortCalledError):
            await service.Index(IndexRequest(api_version="v1"), context)
        assert context.aborted_with is not None
        assert context.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_query_without_scope_aborts_unauthenticated(self) -> None:
        service = _service()
        context = _FakeContext()
        with pytest.raises(AbortCalledError):
            await service.Query(QueryRequest(api_version="v1", query="q"), context)
        assert context.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_memory_add_without_scope_aborts_unauthenticated(self) -> None:
        service = _service()
        context = _FakeContext()
        with pytest.raises(AbortCalledError):
            await service.MemoryAdd(MemoryAddRequest(api_version="v1", content="x"), context)
        assert context.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_memory_search_without_scope_aborts_unauthenticated(self) -> None:
        service = _service()
        context = _FakeContext()
        with pytest.raises(AbortCalledError):
            await service.MemorySearch(MemorySearchRequest(api_version="v1", query="q"), context)
        assert context.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_index_code_without_scope_aborts_unauthenticated(self, tmp_path: Path) -> None:
        service = _service()
        context = _FakeContext()
        with pytest.raises(AbortCalledError):
            await service.IndexCode(
                IndexCodeRequest(api_version="v1", root_path=str(tmp_path)), context
            )
        assert context.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_code_graph_status_without_scope_aborts_unauthenticated(self) -> None:
        service = _service()
        context = _FakeContext()
        with pytest.raises(AbortCalledError):
            await service.CodeGraphStatus(CodeGraphStatusRequest(api_version="v1"), context)
        assert context.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED  # type: ignore[index]


# ---------------------------------------------------------------------------
# Index -> DocumentationIndexer.index_library / .index_language
# ---------------------------------------------------------------------------


class TestIndex:
    @pytest.mark.asyncio
    async def test_library_target_calls_index_library_with_ctx(
        self, scope_ctx: ScopeContext
    ) -> None:
        indexer = _FakeIndexer(chunks=7)
        service = _service(indexer=indexer)
        request = IndexRequest(
            api_version="v1",
            library=LibraryTarget(
                name="fastapi", language=ProtoLanguage.LANGUAGE_PYTHON, version="0.1"
            ),
            doc_contents=["# docs"],
            force_reindex=True,
            visibility=Visibility.VISIBILITY_TEAM,
            team_id="team-a",
        )

        response = await service.Index(request, _FakeContext())

        assert response.chunks_indexed == 7
        indexer.index_library.assert_awaited_once_with(
            scope_ctx,
            Library(name="fastapi", language=ModelLanguage.PYTHON, version="0.1"),
            ["# docs"],
            force_reindex=True,
            visibility="team",
            team_id="team-a",
        )

    @pytest.mark.asyncio
    async def test_language_target_calls_index_language_with_ctx(
        self, scope_ctx: ScopeContext
    ) -> None:
        indexer = _FakeIndexer(chunks=4)
        service = _service(indexer=indexer)
        request = IndexRequest(
            api_version="v1",
            language=ProtoLanguage.LANGUAGE_RUST,
            doc_contents=["# rust docs"],
        )

        response = await service.Index(request, _FakeContext())

        assert response.chunks_indexed == 4
        # Visibility/team_id defaulted (UNSPECIFIED, empty) -> "tenant"/None.
        indexer.index_language.assert_awaited_once_with(
            scope_ctx,
            ModelLanguage.RUST,
            ["# rust docs"],
            force_reindex=False,
            visibility="tenant",
            team_id=None,
        )

    @pytest.mark.asyncio
    async def test_missing_target_aborts_invalid_argument(self, scope_ctx: ScopeContext) -> None:
        service = _service()
        context = _FakeContext()
        with pytest.raises(AbortCalledError):
            await service.Index(IndexRequest(api_version="v1"), context)
        assert context.aborted_with[0] == grpc.StatusCode.INVALID_ARGUMENT  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_library_target_missing_language_aborts_invalid_argument(
        self, scope_ctx: ScopeContext
    ) -> None:
        service = _service()
        context = _FakeContext()
        request = IndexRequest(api_version="v1", library=LibraryTarget(name="no-lang"))
        with pytest.raises(AbortCalledError):
            await service.Index(request, context)
        assert context.aborted_with[0] == grpc.StatusCode.INVALID_ARGUMENT  # type: ignore[index]


# ---------------------------------------------------------------------------
# Query -> retrieval.graphrag.retrieve
# ---------------------------------------------------------------------------


class TestQuery:
    @pytest.mark.asyncio
    async def test_calls_retrieve_and_maps_result(
        self, scope_ctx: ScopeContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def _fake_retrieve(ctx: ScopeContext, query: str, **kwargs: Any) -> RetrievalResult:
            captured["ctx"] = ctx
            captured["query"] = query
            captured["kwargs"] = kwargs
            return RetrievalResult(
                vector_hits=[
                    VectorHit(id="v1", document="doc text", metadata={"library": "x"}, score=0.9)
                ],
                subgraphs={
                    "code": Subgraph(
                        nodes=[GraphNode(node_type="file", key="a.py", props={})],
                        edges=[
                            GraphEdge(
                                src_type="file",
                                src_key="a.py",
                                dst_type="symbol",
                                dst_key="os",
                                rel_type="imports",
                                props={},
                            )
                        ],
                    )
                },
                context="[vector score=0.900] doc text",
            )

        monkeypatch.setattr(knowledge_module, "retrieve", _fake_retrieve)
        service = _service()
        request = QueryRequest(
            api_version="v1",
            query="how to auth",
            n_vector=3,
            graph_depth=2,
            vector_tables=["docs_vectors", "not-a-real-table"],
        )

        response = await service.Query(request, _FakeContext())

        assert captured["ctx"] is scope_ctx
        assert captured["query"] == "how to auth"
        assert captured["kwargs"] == {
            "n_vector": 3,
            "graph_depth": 2,
            "vector_tables": ("docs_vectors",),
        }
        assert response.vector_hits[0].id == "v1"
        assert response.vector_hits[0].score == pytest.approx(0.9)
        assert set(response.subgraphs.keys()) == {"code"}
        assert response.subgraphs["code"].nodes[0].key == "a.py"
        assert response.subgraphs["code"].edges[0].rel_type == "imports"
        assert response.context == "[vector score=0.900] doc text"

    @pytest.mark.asyncio
    async def test_defaults_when_unset(
        self, scope_ctx: ScopeContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def _fake_retrieve(ctx: ScopeContext, query: str, **kwargs: Any) -> RetrievalResult:
            captured["kwargs"] = kwargs
            return RetrievalResult(vector_hits=[], subgraphs={}, context="")

        monkeypatch.setattr(knowledge_module, "retrieve", _fake_retrieve)
        service = _service()

        await service.Query(QueryRequest(api_version="v1", query="q"), _FakeContext())

        assert captured["kwargs"] == {"n_vector": 8, "graph_depth": 1}


# ---------------------------------------------------------------------------
# MemoryAdd / MemorySearch -> ScopedMemoryManager.add / .search
# ---------------------------------------------------------------------------


class TestMemoryAdd:
    @pytest.mark.asyncio
    async def test_calls_add_and_maps_stored_result(self, scope_ctx: ScopeContext) -> None:
        scoped_memory = _FakeScopedMemory()
        scoped_memory.add.return_value = {
            "results": [{"id": "m1", "memory": "the user likes dark mode", "event": "ADD"}]
        }
        service = _service(scoped_memory=scoped_memory)
        request = MemoryAddRequest(
            api_version="v1",
            content="the user likes dark mode",
            visibility=Visibility.VISIBILITY_TEAM,
            team_id="team-a",
        )

        response = await service.MemoryAdd(request, _FakeContext())

        assert response.stored is True
        assert response.results[0].id == "m1"
        assert response.results[0].event == "ADD"
        scoped_memory.add.assert_awaited_once_with(
            scope_ctx,
            "the user likes dark mode",
            visibility="team",
            team_id="team-a",
            metadata={},
        )

    @pytest.mark.asyncio
    async def test_none_result_maps_to_not_stored(self, scope_ctx: ScopeContext) -> None:
        scoped_memory = _FakeScopedMemory()
        scoped_memory.add.return_value = None
        service = _service(scoped_memory=scoped_memory)

        response = await service.MemoryAdd(
            MemoryAddRequest(api_version="v1", content="x"), _FakeContext()
        )

        assert response.stored is False
        assert list(response.results) == []
        # Default visibility (UNSPECIFIED) -> "team", per ScopedMemoryManager.add's own
        # default (the shared-team-brain product intent) -- this handler must never
        # hardcode a different default than the library it wraps.
        # regression: penguincode-memory-team-default (GAP 1 -- gRPC-facing default)
        scoped_memory.add.assert_awaited_once_with(
            scope_ctx, "x", visibility="team", team_id=None, metadata={}
        )


# ---------------------------------------------------------------------------
# F1 (security review, lessons-promotion HIGH): a tenant-visibility MemoryAdd
# is the promotion pipeline's back door -- it must require the same elevated
# scope ApproveLesson does, never be reachable by a plain-scoped caller.
#
# # regression: lessons-promotion-secrev
# ---------------------------------------------------------------------------


class TestMemoryAddTenantVisibilityGate:
    @pytest.mark.asyncio
    async def test_plain_scoped_caller_requesting_tenant_visibility_is_denied(
        self, scope_ctx: ScopeContext
    ) -> None:
        scoped_memory = _FakeScopedMemory()
        service = _service(scoped_memory=scoped_memory)
        request = MemoryAddRequest(
            api_version="v1",
            content="the client's Q3 migration plan is...",
            visibility=Visibility.VISIBILITY_TENANT,
        )

        with pytest.raises(AbortCalledError):
            await service.MemoryAdd(request, _FakeContext())

        scoped_memory.add.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plain_scoped_caller_denial_is_permission_denied(
        self, scope_ctx: ScopeContext
    ) -> None:
        service = _service()
        context = _FakeContext()
        request = MemoryAddRequest(
            api_version="v1", content="x", visibility=Visibility.VISIBILITY_TENANT
        )

        with pytest.raises(AbortCalledError):
            await service.MemoryAdd(request, context)

        assert context.aborted_with is not None
        assert context.aborted_with[0] == grpc.StatusCode.PERMISSION_DENIED

    @pytest.mark.asyncio
    async def test_approver_scoped_caller_may_write_tenant_visibility(self) -> None:
        from penguincode_cli.server.services.lessons import LESSONS_APPROVE_SCOPE

        ctx = _ctx(scopes=("knowledge:read", "knowledge:write", LESSONS_APPROVE_SCOPE))
        token = auth_middleware._current_scope.set(ctx)
        try:
            scoped_memory = _FakeScopedMemory()
            scoped_memory.add.return_value = {"results": []}
            service = _service(scoped_memory=scoped_memory)
            request = MemoryAddRequest(
                api_version="v1", content="a generalized lesson", visibility=Visibility.VISIBILITY_TENANT
            )

            response = await service.MemoryAdd(request, _FakeContext())

            assert response.stored is True
            scoped_memory.add.assert_awaited_once_with(
                ctx, "a generalized lesson", visibility="tenant", team_id=None, metadata={}
            )
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_team_visibility_write_still_works_for_a_plain_scoped_caller(
        self, scope_ctx: ScopeContext
    ) -> None:
        """Unaffected by the tenant-visibility gate -- team/user writes are unchanged."""
        scoped_memory = _FakeScopedMemory()
        scoped_memory.add.return_value = {"results": []}
        service = _service(scoped_memory=scoped_memory)
        request = MemoryAddRequest(
            api_version="v1",
            content="the user likes dark mode",
            visibility=Visibility.VISIBILITY_TEAM,
            team_id="team-a",
        )

        response = await service.MemoryAdd(request, _FakeContext())

        assert response.stored is True
        scoped_memory.add.assert_awaited_once_with(
            scope_ctx, "the user likes dark mode", visibility="team", team_id="team-a", metadata={}
        )


class TestMemorySearch:
    @pytest.mark.asyncio
    async def test_calls_search_and_maps_results(self, scope_ctx: ScopeContext) -> None:
        scoped_memory = _FakeScopedMemory()
        scoped_memory.search.return_value = [
            {"id": "m1", "memory": "dark mode", "score": 0.87, "metadata": {"visibility": "user"}}
        ]
        service = _service(scoped_memory=scoped_memory)

        response = await service.MemorySearch(
            MemorySearchRequest(api_version="v1", query="dark mode", limit=3), _FakeContext()
        )

        assert response.results[0].id == "m1"
        assert response.results[0].score == pytest.approx(0.87)
        scoped_memory.search.assert_awaited_once_with(scope_ctx, "dark mode", limit=3)

    @pytest.mark.asyncio
    async def test_default_limit(self, scope_ctx: ScopeContext) -> None:
        scoped_memory = _FakeScopedMemory()
        service = _service(scoped_memory=scoped_memory)

        await service.MemorySearch(MemorySearchRequest(api_version="v1", query="q"), _FakeContext())

        scoped_memory.search.assert_awaited_once_with(scope_ctx, "q", limit=5)


# ---------------------------------------------------------------------------
# GAP 1: unspecified-visibility MemoryAdd requests share with same-team
# teammates by default, through a REAL ScopedMemoryManager (not the
# handler-level AsyncMock double above) -- proves the gRPC-facing default
# actually resolves and enforces team sharing end to end, not just that the
# right string gets forwarded to a mock.
#
# # regression: penguincode-memory-team-default (GAP 1 -- gRPC-facing default)
# ---------------------------------------------------------------------------


class _FakeMem0ForGrpcDefault:
    """Minimal mem0 ``Memory`` double -- just the ``add``/``search`` surface this test needs.

    Deliberately local (not imported from ``tests/test_memory.py``'s own
    ``_FakeMem0Memory`` -- no cross-test-module import precedent in this
    codebase; ``tests/test_admin_api.py``'s ``from tests.conftest import
    ...`` is the only exception, for shared fixtures, not test-local fakes).
    Mirrors mem0ai==2.2.0's real ``add(infer=False)`` envelope shape (no
    ``"metadata"`` key in the returned row -- see GAP 2 above) so this test
    exercises the same real-world shape ``ScopedMemoryManager.add()`` must
    handle without relying on mem0 echoing scope back.
    """

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._next_id = 0

    def add(
        self,
        messages: list[dict[str, str]],
        *,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        infer: bool = True,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self._next_id += 1
        row = {
            "id": str(self._next_id),
            "memory": messages[0]["content"],
            "user_id": user_id,
            "metadata": dict(metadata or {}),
        }
        self._rows.append(row)
        return {"results": [{"id": row["id"], "memory": row["memory"], "event": "ADD"}]}

    def search(
        self, query: str, *, filters: dict[str, Any] | None = None, top_k: int = 20, **_kwargs: Any
    ) -> dict[str, Any]:
        user_id = (filters or {}).get("user_id")
        hits = [row for row in self._rows if row["user_id"] == user_id]
        return {
            "results": [
                {
                    "id": row["id"],
                    "memory": row["memory"],
                    "metadata": row["metadata"],
                    "score": 1.0,
                }
                for row in hits[:top_k]
            ]
        }


def _real_scoped_memory(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build a real ``ScopedMemoryManager`` with mem0 mocked at its own library boundary."""
    from penguincode_cli.config.settings import (
        MemoryConfig,
        MemoryStoresConfig,
        PGVectorStoreConfig,
    )
    from penguincode_cli.tools.memory import (
        MemoryManager,
        create_scoped_memory_manager,
    )

    monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "1")
    config = MemoryConfig(
        enabled=True,
        vector_store="pgvector",
        stores=MemoryStoresConfig(
            pgvector=PGVectorStoreConfig(
                url="postgresql://localhost/testdb", table_name="test_memory"
            )
        ),
    )
    fake = _FakeMem0ForGrpcDefault()
    with patch("penguincode_cli.tools.memory.Memory") as mock_memory_cls:
        mock_memory_cls.from_config.return_value = fake
        manager = MemoryManager(config, ollama_url="http://localhost:11434")
    return create_scoped_memory_manager(manager)


class TestMemoryAddDefaultVisibilitySharing:
    """An unspecified-visibility gRPC ``MemoryAdd`` request shares with a same-team teammate.

    Before the GAP 1 fix, ``_visibility_from_proto(..., default="user")``
    made every unspecified-visibility write private, so the teammate's
    search below returned nothing -- see the commit history for the
    failing-first run against the pre-fix ``default="user"``.
    """

    @pytest.mark.asyncio
    async def test_unspecified_visibility_shares_with_same_team_teammate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scoped_memory = _real_scoped_memory(monkeypatch)
        service = _service(scoped_memory=scoped_memory)

        writer = _ctx(tenant_id="t-grpc-share", team_ids=("team-1",), user_id="user-a")
        teammate = _ctx(tenant_id="t-grpc-share", team_ids=("team-1",), user_id="user-b")
        stranger = _ctx(tenant_id="t-grpc-share", team_ids=("team-2",), user_id="user-c")

        # No `visibility` field set on the request at all -- proto default
        # (VISIBILITY_UNSPECIFIED, value 0) -- the actual "did the caller's
        # agent just write a memory with no opinion on sharing" case.
        token = auth_middleware._current_scope.set(writer)
        try:
            add_response = await service.MemoryAdd(
                MemoryAddRequest(api_version="v1", content="the client kickoff is Monday"),
                _FakeContext(),
            )
        finally:
            auth_middleware._current_scope.reset(token)
        assert add_response.stored is True

        token = auth_middleware._current_scope.set(teammate)
        try:
            teammate_response = await service.MemorySearch(
                MemorySearchRequest(api_version="v1", query="client kickoff"), _FakeContext()
            )
        finally:
            auth_middleware._current_scope.reset(token)
        assert any("client kickoff" in r.memory for r in teammate_response.results)

        token = auth_middleware._current_scope.set(stranger)
        try:
            stranger_response = await service.MemorySearch(
                MemorySearchRequest(api_version="v1", query="client kickoff"), _FakeContext()
            )
        finally:
            auth_middleware._current_scope.reset(token)
        assert not any("client kickoff" in r.memory for r in stranger_response.results)


# ---------------------------------------------------------------------------
# IndexCode -> graphs.code.index_code ; CodeGraphStatus -> local cache
# ---------------------------------------------------------------------------


class TestIndexCodeAndStatus:
    @pytest.mark.asyncio
    async def test_index_code_calls_index_code_with_ctx_and_updates_cache(
        self, scope_ctx: ScopeContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_index_code(ctx: ScopeContext, root_path: Any, **kwargs: Any) -> ExtractionResult:
            captured["ctx"] = ctx
            captured["root_path"] = root_path
            captured["kwargs"] = kwargs
            return ExtractionResult(
                nodes=[GraphNode(node_type="file", key="a.py")],
                edges=[
                    GraphEdge(
                        src_type="file",
                        src_key="a.py",
                        dst_type="symbol",
                        dst_key="os",
                        rel_type="imports",
                    )
                ],
            )

        monkeypatch.setattr(knowledge_module, "index_code", _fake_index_code)
        service = _service()
        request = IndexCodeRequest(
            api_version="v1",
            root_path=str(tmp_path),
            visibility=Visibility.VISIBILITY_TENANT,
        )

        response = await service.IndexCode(request, _FakeContext())

        assert response.indexed is True
        assert response.node_count == 1
        assert response.edge_count == 1
        assert captured["ctx"] is scope_ctx
        assert captured["root_path"] == str(tmp_path)
        assert captured["kwargs"]["visibility"] == "tenant"
        assert captured["kwargs"]["team_id"] is None

        # CodeGraphStatus now reports the cached counts from that IndexCode call.
        monkeypatch.setenv("PENGUINCODE_FLAG_CODE_GRAPH", "true")
        status = await service.CodeGraphStatus(
            CodeGraphStatusRequest(api_version="v1"), _FakeContext()
        )
        assert status.enabled is True
        assert status.node_count == 1
        assert status.edge_count == 1

    @pytest.mark.asyncio
    async def test_index_code_flag_off_returns_not_indexed(
        self, scope_ctx: ScopeContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(knowledge_module, "index_code", lambda *a, **k: None)
        service = _service()

        response = await service.IndexCode(
            IndexCodeRequest(api_version="v1", root_path=str(tmp_path)), _FakeContext()
        )

        assert response.indexed is False
        assert response.node_count == 0
        assert response.edge_count == 0

    @pytest.mark.asyncio
    async def test_code_graph_status_defaults_to_zero_when_never_indexed(
        self, scope_ctx: ScopeContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_CODE_GRAPH", "false")
        service = _service()

        status = await service.CodeGraphStatus(
            CodeGraphStatusRequest(api_version="v1"), _FakeContext()
        )

        assert status.enabled is False
        assert status.node_count == 0
        assert status.edge_count == 0

    @pytest.mark.asyncio
    async def test_code_graph_status_is_isolated_per_tenant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tenant A's `IndexCode` cache entry never leaks into tenant B's status."""
        monkeypatch.setattr(
            knowledge_module,
            "index_code",
            lambda *a, **k: ExtractionResult(
                nodes=[GraphNode(node_type="file", key="a.py")], edges=[]
            ),
        )
        monkeypatch.setenv("PENGUINCODE_FLAG_CODE_GRAPH", "true")
        service = _service()

        token_a = auth_middleware._current_scope.set(_ctx(tenant_id="tenant-a"))
        try:
            await service.IndexCode(
                IndexCodeRequest(api_version="v1", root_path=str(tmp_path)), _FakeContext()
            )
        finally:
            auth_middleware._current_scope.reset(token_a)

        token_b = auth_middleware._current_scope.set(_ctx(tenant_id="tenant-b"))
        try:
            status_b = await service.CodeGraphStatus(
                CodeGraphStatusRequest(api_version="v1"), _FakeContext()
            )
        finally:
            auth_middleware._current_scope.reset(token_b)

        assert status_b.node_count == 0
        assert status_b.edge_count == 0


# ---------------------------------------------------------------------------
# Live-Postgres: proves scope isolation the mocks above cannot -- tenant B's
# ScopeContext can never read tenant A's IndexCode write, through a real RPC
# call into a real store.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def knowledge_live_dsn() -> Iterator[str]:
    assert TEST_DATABASE_URL is not None  # narrows type; skipif already guards this
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS penguincode CASCADE")
    run_migrations(dsn=TEST_DATABASE_URL)
    yield TEST_DATABASE_URL


@requires_postgres
class TestIndexCodeLiveScopeIsolation:
    @pytest.mark.asyncio
    async def test_tenant_b_cannot_read_tenant_a_indexed_code(
        self, tmp_path: Path, knowledge_live_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_CODE_GRAPH", "true")
        (tmp_path / "a.py").write_text("def foo():\n    pass\n")

        graph_config = GraphConfig(postgres=PostgresGraphStoreConfig(url=knowledge_live_dsn))
        service = _service(graph_config=graph_config)

        # `graph_nodes`' tenant_id/org_id/owner_user_id are `uuid` columns
        # (T1 schema) -- unlike the mocked tests above, every scope field
        # that reaches Postgres here must be a real UUID, not a plain string.
        tenant_a = str(uuid.uuid4())
        team_a = str(uuid.uuid4())
        ctx_a = _ctx(tenant_id=tenant_a, org_id=None, team_ids=(team_a,), user_id=str(uuid.uuid4()))
        token_a = auth_middleware._current_scope.set(ctx_a)
        try:
            response = await service.IndexCode(
                IndexCodeRequest(
                    api_version="v1",
                    root_path=str(tmp_path),
                    visibility=Visibility.VISIBILITY_TEAM,
                    team_id=team_a,
                ),
                _FakeContext(),
            )
        finally:
            auth_middleware._current_scope.reset(token_a)

        assert response.indexed is True
        assert response.node_count > 0

        # The real security property: tenant B's ScopeContext, querying the
        # same live GraphStore directly, sees none of tenant A's nodes.
        tenant_b = str(uuid.uuid4())
        ctx_b = _ctx(tenant_id=tenant_b, org_id=None, team_ids=(), user_id=str(uuid.uuid4()))
        store = PostgresGraphStore(dsn=knowledge_live_dsn, schema="penguincode")

        subgraph_b = store.subgraph(ctx_b, "code", seed_keys=["a.py"], depth=0)
        assert subgraph_b.nodes == []

        subgraph_a = store.subgraph(ctx_a, "code", seed_keys=["a.py"], depth=0)
        assert any(n.key == "a.py" for n in subgraph_a.nodes)
