"""KnowledgeService gRPC servicer (F2): server-side docs-RAG, GraphRAG, memory, code graph.

Implements F1's six `KnowledgeService` RPCs by deriving each request's
`ScopeContext` exclusively from the caller's validated WaddleAI JWT
(`auth.middleware.current_scope_context()`, populated by
`WaddleAIAuthInterceptor` -- see `server/main.py`'s interceptor-reconciliation
wiring) and delegating to the existing knowledge modules
(`docs_rag.indexer`, `retrieval.graphrag`, `tools.memory`, `graphs.code`,
`stores.graph`) with that scope. This file owns request/response mapping
only -- it never derives or trusts a tenant/org/team/user from the request
message itself (see `knowledge.proto`'s scope-model contract): every handler
aborts `UNAUTHENTICATED` before doing any work if no `ScopeContext` is
present, with no fallback to client-supplied identity.

**CodeGraphStatus's local cache.** `stores.graph.GraphStore` deliberately
exposes no count/read-back query (mirroring `docs_rag.indexer`'s own local
freshness-cache design, see that module's docstring for the same rationale)
-- so this servicer keeps a small per-tenant, in-process cache of the most
recent `IndexCode` call's node/edge counts and reports that from
`CodeGraphStatus`, rather than querying `GraphStore` directly. It is
ephemeral (reset on process restart, not shared across replicas) by design;
`IndexCode` itself is always the source of truth for a fresh count.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol, runtime_checkable

import grpc
from google.protobuf import struct_pb2

from penguincode_cli.auth.middleware import current_scope_context
from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import GraphConfig, MemoryConfig, Settings
from penguincode_cli.docs_rag.indexer import DocumentationIndexer
from penguincode_cli.docs_rag.models import Language as ModelLanguage
from penguincode_cli.docs_rag.models import Library
from penguincode_cli.flags import CODE_GRAPH_FLAG, is_enabled
from penguincode_cli.graphs.code import index_code
from penguincode_cli.observability.otel import store_span
from penguincode_cli.proto import (
    CodeGraphStatusRequest,
    CodeGraphStatusResponse,
    IndexCodeRequest,
    IndexCodeResponse,
    IndexRequest,
    IndexResponse,
    KnowledgeServiceServicer,
    MemoryAddRequest,
    MemoryAddResponse,
    MemoryAddResult,
    MemoryItem,
    MemorySearchRequest,
    MemorySearchResponse,
    QueryRequest,
    QueryResponse,
    Visibility,
)
from penguincode_cli.proto import GraphEdge as ProtoGraphEdge
from penguincode_cli.proto import GraphNode as ProtoGraphNode
from penguincode_cli.proto import Language as ProtoLanguage
from penguincode_cli.proto import VectorHit as ProtoVectorHit
from penguincode_cli.retrieval.graphrag import retrieve
from penguincode_cli.server.services.lessons import LESSONS_APPROVE_SCOPE
from penguincode_cli.stores.graph import GraphEdge, GraphNode
from penguincode_cli.stores.vector import TableName
from penguincode_cli.tools.memory import (
    DEFAULT_VISIBILITY,
    MemoryManager,
    ScopedMemoryManager,
    create_memory_manager,
    create_scoped_memory_manager,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class _IndexerLike(Protocol):
    """Structural match for the two `DocumentationIndexer` methods `Index` calls.

    Typed as a narrow local Protocol (mirroring `stores.vector.VectorStore`'s
    own structural-typing style) rather than the concrete
    `DocumentationIndexer` class, so a test double needs only satisfy this
    exact shape -- not subclass or fully construct the real indexer.
    """

    async def index_library(
        self,
        ctx: ScopeContext | None,
        library: Library,
        doc_contents: list[str],
        *,
        force_reindex: bool = False,
        visibility: str = "tenant",
        team_id: str | None = None,
    ) -> int: ...

    async def index_language(
        self,
        ctx: ScopeContext | None,
        language: ModelLanguage,
        doc_contents: list[str],
        *,
        force_reindex: bool = False,
        visibility: str = "tenant",
        team_id: str | None = None,
    ) -> int: ...


@runtime_checkable
class _ScopedMemoryLike(Protocol):
    """Structural match for the two `ScopedMemoryManager` methods this servicer calls."""

    async def add(
        self,
        ctx: ScopeContext,
        content: str,
        *,
        visibility: str = DEFAULT_VISIBILITY,
        team_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None: ...

    async def search(
        self, ctx: ScopeContext, query: str, *, limit: int = 5
    ) -> list[dict[str, Any]]: ...


#: `stores.vector.TableName`'s allow-list, duplicated as a plain set so a
#: caller-supplied `QueryRequest.vector_tables` entry can be checked without
#: importing `stores.vector`'s private validator.
_VALID_VECTOR_TABLES: frozenset[str] = frozenset({"docs_vectors", "memory_vectors"})

#: Proto `Language` enum -> `docs_rag.models.Language`, the verbatim set the
#: proto's own comment promises (`knowledge.proto`'s `Language` enum
#: docstring). `LANGUAGE_UNSPECIFIED` intentionally has no entry -- callers
#: must supply a concrete language for `Index`.
_PROTO_LANGUAGE_TO_MODEL: dict[ProtoLanguage.ValueType, ModelLanguage] = {
    ProtoLanguage.LANGUAGE_PYTHON: ModelLanguage.PYTHON,
    ProtoLanguage.LANGUAGE_JAVASCRIPT: ModelLanguage.JAVASCRIPT,
    ProtoLanguage.LANGUAGE_TYPESCRIPT: ModelLanguage.TYPESCRIPT,
    ProtoLanguage.LANGUAGE_GO: ModelLanguage.GO,
    ProtoLanguage.LANGUAGE_RUST: ModelLanguage.RUST,
    ProtoLanguage.LANGUAGE_HCL: ModelLanguage.HCL,
    ProtoLanguage.LANGUAGE_ANSIBLE: ModelLanguage.ANSIBLE,
    ProtoLanguage.LANGUAGE_RUBY: ModelLanguage.RUBY,
    ProtoLanguage.LANGUAGE_PHP: ModelLanguage.PHP,
    ProtoLanguage.LANGUAGE_DART: ModelLanguage.DART,
}

#: Proto `Visibility` -> the plain `str` every store layer expects.
#: `VISIBILITY_UNSPECIFIED` has no entry -- see `_visibility_from_proto`'s
#: per-call default.
_PROTO_VISIBILITY_TO_STR: dict[Visibility.ValueType, str] = {
    Visibility.VISIBILITY_USER: "user",
    Visibility.VISIBILITY_TEAM: "team",
    Visibility.VISIBILITY_TENANT: "tenant",
}


def _visibility_from_proto(value: Visibility.ValueType, *, default: str) -> str:
    """Map a proto `Visibility` to the store-layer `str`; `UNSPECIFIED` -> `default`."""
    if value == Visibility.VISIBILITY_UNSPECIFIED:
        return default
    return _PROTO_VISIBILITY_TO_STR[value]


def _valid_table(name: str) -> TableName | None:
    """Narrow a caller-supplied table name to `TableName`, or `None` if not allow-listed."""
    if name == "docs_vectors":
        return "docs_vectors"
    if name == "memory_vectors":
        return "memory_vectors"
    return None


def _struct(data: dict[str, Any] | None) -> struct_pb2.Struct:
    """Build a `google.protobuf.Struct` from a plain dict, defaulting to empty."""
    proto_struct = struct_pb2.Struct()
    if data:
        proto_struct.update(data)
    return proto_struct


def _proto_nodes(nodes: list[GraphNode]) -> list[ProtoGraphNode]:
    """Map `stores.graph.GraphNode` rows to their proto equivalents."""
    return [
        ProtoGraphNode(node_type=node.node_type, key=node.key, props=_struct(node.props))
        for node in nodes
    ]


def _proto_edges(edges: list[GraphEdge]) -> list[ProtoGraphEdge]:
    """Map `stores.graph.GraphEdge` rows to their proto equivalents."""
    return [
        ProtoGraphEdge(
            src_type=edge.src_type,
            src_key=edge.src_key,
            dst_type=edge.dst_type,
            dst_key=edge.dst_key,
            rel_type=edge.rel_type,
            props=_struct(edge.props),
        )
        for edge in edges
    ]


async def _require_scope(context: grpc.aio.ServicerContext) -> ScopeContext:
    """Return the in-flight request's `ScopeContext`, or abort `UNAUTHENTICATED`.

    The sole source of scope is `current_scope_context()` (set by
    `WaddleAIAuthInterceptor` from the caller's validated JWT) -- there is no
    fallback to a client-supplied tenant/org/team/user, per
    `knowledge.proto`'s scope-model contract.
    """
    ctx = current_scope_context()
    if ctx is None:
        await context.abort(
            grpc.StatusCode.UNAUTHENTICATED,
            "no ScopeContext available -- missing or invalid WaddleAI JWT",
        )
    assert ctx is not None  # context.abort() always raises; unreachable otherwise
    return ctx


def _build_scoped_memory_manager(settings: Settings) -> ScopedMemoryManager:
    """Construct a `ScopedMemoryManager`, degrading to disabled on construction failure.

    Mirrors `core/repl.py`'s guarded `MemoryManager(...)` construction -- an
    unreachable Ollama/pgvector at server startup must never crash the
    process; `ScopedMemoryManager.add`/`.search` already degrade gracefully
    (return `None`/`[]`) when the wrapped manager is disabled.
    """
    try:
        manager = create_memory_manager(
            settings.memory, settings.ollama.api_url, settings.models.orchestration
        )
    except Exception as exc:  # noqa: BLE001 -- mem0/Ollama outage at construction must not crash the server
        logger.warning("knowledge: MemoryManager construction failed, memory disabled: %s", exc)
        manager = MemoryManager(MemoryConfig(enabled=False), settings.ollama.api_url)
    return create_scoped_memory_manager(manager)


class KnowledgeServiceImpl(KnowledgeServiceServicer):
    """Server-side implementation of all six `KnowledgeService` RPCs.

    Every knowledge-module dependency is constructed from `settings` by
    default but keyword-only injectable for tests, mirroring the seams those
    modules already expose (`DocumentationIndexer(store=...)`,
    `index_code(graph_store=...)`).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        indexer: _IndexerLike | None = None,
        scoped_memory: _ScopedMemoryLike | None = None,
        graph_config: GraphConfig | None = None,
    ) -> None:
        self._settings = settings
        self._indexer = indexer if indexer is not None else DocumentationIndexer()
        self._scoped_memory = (
            scoped_memory if scoped_memory is not None else _build_scoped_memory_manager(settings)
        )
        self._graph_config = graph_config if graph_config is not None else settings.graph
        # See module docstring's "CodeGraphStatus's local cache" section.
        self._code_graph_status: dict[str, tuple[int, int]] = {}

    async def Index(
        self, request: IndexRequest, context: grpc.aio.ServicerContext
    ) -> IndexResponse:
        """Index a library's or a language's docs via `DocumentationIndexer`."""
        ctx = await _require_scope(context)
        visibility = _visibility_from_proto(request.visibility, default="tenant")
        team_id = request.team_id or None
        doc_contents = list(request.doc_contents)
        target = request.WhichOneof("target")

        with store_span("knowledge.Index", target=target or "none", doc_count=len(doc_contents)):
            if target == "library":
                library_language = _PROTO_LANGUAGE_TO_MODEL.get(request.library.language)
                if library_language is None:
                    await context.abort(
                        grpc.StatusCode.INVALID_ARGUMENT, "library.language is required"
                    )
                    raise AssertionError("unreachable")  # abort() always raises
                library = Library(
                    name=request.library.name,
                    language=library_language,
                    version=request.library.version or None,
                )
                chunks_indexed = await self._indexer.index_library(
                    ctx,
                    library,
                    doc_contents,
                    force_reindex=request.force_reindex,
                    visibility=visibility,
                    team_id=team_id,
                )
            elif target == "language":
                doc_language = _PROTO_LANGUAGE_TO_MODEL.get(request.language)
                if doc_language is None:
                    await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "language is required")
                    raise AssertionError("unreachable")  # abort() always raises
                chunks_indexed = await self._indexer.index_language(
                    ctx,
                    doc_language,
                    doc_contents,
                    force_reindex=request.force_reindex,
                    visibility=visibility,
                    team_id=team_id,
                )
            else:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT, "target (library or language) is required"
                )
                raise AssertionError("unreachable")  # abort() always raises

        return IndexResponse(chunks_indexed=chunks_indexed)

    async def Query(
        self, request: QueryRequest, context: grpc.aio.ServicerContext
    ) -> QueryResponse:
        """Hybrid GraphRAG retrieval via `retrieval.graphrag.retrieve`."""
        ctx = await _require_scope(context)
        n_vector = request.n_vector or 8
        graph_depth = request.graph_depth or 1
        requested_tables: list[TableName] = [
            table for name in request.vector_tables if (table := _valid_table(name)) is not None
        ]

        with store_span(
            "knowledge.Query",
            n_vector=n_vector,
            graph_depth=graph_depth,
            table_count=len(requested_tables),
        ):
            if requested_tables:
                result = await retrieve(
                    ctx,
                    request.query,
                    n_vector=n_vector,
                    graph_depth=graph_depth,
                    vector_tables=tuple(requested_tables),
                )
            else:
                result = await retrieve(
                    ctx, request.query, n_vector=n_vector, graph_depth=graph_depth
                )

        response = QueryResponse(
            vector_hits=[
                ProtoVectorHit(
                    id=hit.id,
                    document=hit.document,
                    metadata=_struct(hit.metadata),
                    score=hit.score,
                )
                for hit in result.vector_hits
            ],
            context=result.context,
        )
        for kind, subgraph in result.subgraphs.items():
            proto_subgraph = response.subgraphs[kind]
            proto_subgraph.nodes.extend(_proto_nodes(subgraph.nodes))
            proto_subgraph.edges.extend(_proto_edges(subgraph.edges))
        return response

    async def MemoryAdd(
        self, request: MemoryAddRequest, context: grpc.aio.ServicerContext
    ) -> MemoryAddResponse:
        """Write one scope-stamped memory via `ScopedMemoryManager.add`.

        `VISIBILITY_UNSPECIFIED` (a client that didn't set the field at all)
        maps to `DEFAULT_VISIBILITY` ("team" -- the shared-team-brain
        product intent), matching `ScopedMemoryManager.add()`'s own default
        exactly -- this handler must never hardcode a different default than
        the library it wraps. `team_id` is resolved the identical way: this
        just forwards whatever `team_id` the request carries (`None` when
        unset) straight through to `ScopedMemoryManager.add()`, which runs
        the SAME `_resolve_default_team_scope` single/multiple/zero-team
        rules on it (see `tools/memory.py`) -- there is no separate
        resolution to duplicate here.

        **`"tenant"`-visibility writes require `LESSONS_APPROVE_SCOPE`**
        (security review F1): without this gate, ANY authenticated caller
        could pass `visibility=tenant` here and write arbitrary,
        never-scrubbed content firm-wide, completely bypassing
        `lessons.scrub`'s generalize-and-verify pipeline and
        `LessonsService`'s propose -> review workflow. Reusing the same
        scope `ApproveLesson`/`RejectLesson` require (rather than minting a
        second one) means there is exactly one elevated privilege that
        grants firm-wide-visibility write access, held by the same
        reviewers, everywhere in the codebase. Team/user-visibility writes
        are completely unaffected by this check.
        """
        ctx = await _require_scope(context)
        visibility = _visibility_from_proto(request.visibility, default=DEFAULT_VISIBILITY)
        if visibility == "tenant" and LESSONS_APPROVE_SCOPE not in ctx.scopes:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                f"tenant-visibility memory writes require the {LESSONS_APPROVE_SCOPE!r} scope -- "
                "use the lessons-promotion pipeline instead "
                "(LessonsService.PromoteLesson then LessonsService.ApproveLesson) to share "
                "content firm-wide",
            )
            raise AssertionError("unreachable")  # abort() always raises
        team_id = request.team_id or None
        metadata = dict(request.metadata)

        with store_span("knowledge.MemoryAdd", visibility=visibility, has_metadata=bool(metadata)):
            result = await self._scoped_memory.add(
                ctx, request.content, visibility=visibility, team_id=team_id, metadata=metadata
            )

        if result is None:
            return MemoryAddResponse(stored=False, results=[])

        raw_results = result.get("results") or []
        return MemoryAddResponse(
            stored=True,
            results=[
                MemoryAddResult(
                    id=str(row.get("id", "")),
                    memory=str(row.get("memory", "")),
                    event=str(row.get("event", "")),
                )
                for row in raw_results
            ],
        )

    async def MemorySearch(
        self, request: MemorySearchRequest, context: grpc.aio.ServicerContext
    ) -> MemorySearchResponse:
        """Search memories within the caller's scope via `ScopedMemoryManager.search`."""
        ctx = await _require_scope(context)
        limit = request.limit or 5

        with store_span("knowledge.MemorySearch", limit=limit):
            rows = await self._scoped_memory.search(ctx, request.query, limit=limit)

        return MemorySearchResponse(
            results=[
                MemoryItem(
                    id=str(row.get("id", "")),
                    memory=str(row.get("memory", "")),
                    metadata=_struct(row.get("metadata")),
                    score=float(row.get("score") or 0.0),
                )
                for row in rows
            ]
        )

    async def IndexCode(
        self, request: IndexCodeRequest, context: grpc.aio.ServicerContext
    ) -> IndexCodeResponse:
        """(Re)build the code graph for a source tree via `graphs.code.index_code`."""
        ctx = await _require_scope(context)
        visibility = _visibility_from_proto(request.visibility, default="team")
        team_id = request.team_id or None

        with store_span("knowledge.IndexCode", visibility=visibility):
            # `index_code` is a synchronous function performing blocking file
            # I/O and psycopg calls -- run off the event loop.
            result = await asyncio.to_thread(
                index_code,
                ctx,
                request.root_path,
                visibility=visibility,
                team_id=team_id,
                config=self._graph_config,
            )

        if result is None:
            return IndexCodeResponse(indexed=False, node_count=0, edge_count=0)

        node_count = len(result.nodes)
        edge_count = len(result.edges)
        self._code_graph_status[ctx.tenant_id] = (node_count, edge_count)
        return IndexCodeResponse(indexed=True, node_count=node_count, edge_count=edge_count)

    async def CodeGraphStatus(
        self, request: CodeGraphStatusRequest, context: grpc.aio.ServicerContext
    ) -> CodeGraphStatusResponse:
        """Report the caller-scoped code graph's last-known availability/size.

        See the module docstring's "CodeGraphStatus's local cache" section --
        this reads the server-local cache populated by `IndexCode`, never a
        live `GraphStore` query.
        """
        ctx = await _require_scope(context)
        enabled = is_enabled(CODE_GRAPH_FLAG, ctx)
        node_count, edge_count = self._code_graph_status.get(ctx.tenant_id, (0, 0))
        return CodeGraphStatusResponse(
            enabled=enabled, node_count=node_count, edge_count=edge_count
        )


__all__ = ["KnowledgeServiceImpl"]
