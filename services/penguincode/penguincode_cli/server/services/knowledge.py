"""KnowledgeService gRPC servicer (F2): server-side docs-RAG, GraphRAG, memory, code graph.

Implements F1's `KnowledgeService` RPCs (plus C1's three docs-index-management
additions -- `IndexStatus`/`ClearIndex`/`CleanupIndex`) by deriving each request's
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
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

import grpc
from google.protobuf import struct_pb2

from penguincode_cli.auth.middleware import current_scope_context
from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import (
    GraphConfig,
    IndexingConfig,
    MemoryConfig,
    Settings,
    embedding_endpoint_label,
    resolve_embedding_url,
)
from penguincode_cli.docs_rag.indexer import DocumentationIndexer
from penguincode_cli.docs_rag.models import Language as ModelLanguage
from penguincode_cli.docs_rag.models import Library
from penguincode_cli.flags import CODE_GRAPH_FLAG, DISABLE_INDEX_QUEUE_FLAG, is_enabled
from penguincode_cli.graphs.code import index_code
from penguincode_cli.indexing import (
    IndexJob,
    IndexJobOutcome,
    IndexJobQueue,
    IndexJobStore,
    IndexJobStoreLike,
    IndexQueueFullError,
    IndexWorkerPool,
    JobType,
    QueuedIndexWork,
)
from penguincode_cli.indexing import metrics as index_metrics
from penguincode_cli.observability.otel import record_query_clamped, store_span
from penguincode_cli.proto import (
    CleanupIndexRequest,
    CleanupIndexResponse,
    ClearIndexRequest,
    ClearIndexResponse,
    CodeGraphStatusRequest,
    CodeGraphStatusResponse,
    IndexCodeRequest,
    IndexCodeResponse,
    IndexJobSummary,
    IndexRequest,
    IndexResponse,
    IndexStatusRequest,
    IndexStatusResponse,
    KnowledgeServiceServicer,
    LanguageIndexStatus,
    LibraryIndexStatus,
    ListIndexJobsRequest,
    ListIndexJobsResponse,
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
from penguincode_cli.proto import JobState as ProtoJobState
from penguincode_cli.proto import JobType as ProtoJobType
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

#: `JobType`/`JobState` (plain str enums, `indexing/jobs.py`) <-> their proto
#: counterparts (`JobType`/`JobState` ints, `knowledge.proto`) -- a small
#: closed mapping, not a generic enum-name trick, so an unexpected Python
#: value fails loudly (`KeyError`) instead of silently defaulting.
_JOB_TYPE_TO_PROTO: dict[JobType, ProtoJobType.ValueType] = {
    JobType.INDEX_DOCS: ProtoJobType.JOB_TYPE_INDEX_DOCS,
    JobType.INDEX_CODE: ProtoJobType.JOB_TYPE_INDEX_CODE,
}
_JOB_STATE_TO_PROTO: dict[str, ProtoJobState.ValueType] = {
    "queued": ProtoJobState.JOB_STATE_QUEUED,
    "running": ProtoJobState.JOB_STATE_RUNNING,
    "succeeded": ProtoJobState.JOB_STATE_SUCCEEDED,
    "failed": ProtoJobState.JOB_STATE_FAILED,
}


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

    def get_index_status(self, ctx: ScopeContext | None) -> dict[str, Any]: ...

    async def clear_library_index(self, ctx: ScopeContext | None, library_name: str) -> int: ...

    async def clear_language_index(
        self, ctx: ScopeContext | None, language: ModelLanguage
    ) -> int: ...

    async def cleanup_unused(
        self,
        ctx: ScopeContext | None,
        current_libraries: list[Library],
        current_languages: list[ModelLanguage],
    ) -> dict[str, int]: ...


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


def _clamp_param(value: int, maximum: int, *, param: str) -> int:
    """Coerce a caller-supplied request field down to `maximum` (never reject).

    Ops-audit O7: `n_vector`/`graph_depth`/`limit` previously came straight
    from the gRPC request with no server-side bound -- a caller could pin
    Postgres with an arbitrarily deep traversal or an arbitrarily large
    top-k. Mirrors the identical clamp inside `stores.vector.PgVectorStore.
    query`/`stores.graph.PostgresGraphStore._traverse` -- this handler-level
    clamp is defense in depth, not a substitute for those store-level ones.
    """
    if value > maximum:
        logger.debug("knowledge: clamped param=%s requested=%d max=%d", param, value, maximum)
        record_query_clamped(param)
        return maximum
    return value


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


def _job_status_fields(job: IndexJob) -> dict[str, Any]:
    """`IndexJob` -> the shared field set `IndexStatusResponse`'s job-mode and
    `IndexJobSummary` both carry (same names, same meaning in both messages).
    """
    return {
        "job_id": job.id,
        "job_type": _JOB_TYPE_TO_PROTO[job.job_type],
        "state": _JOB_STATE_TO_PROTO[job.state.value],
        "chunks_done": job.chunks_done,
        "chunks_total": job.chunks_total,
        "error": job.error or "",
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "result": _struct(job.result),
    }


def _build_scoped_memory_manager(settings: Settings) -> ScopedMemoryManager:
    """Construct a `ScopedMemoryManager`, degrading to disabled on construction failure.

    Mirrors `core/repl.py`'s guarded `MemoryManager(...)` construction -- an
    unreachable Ollama/pgvector at server startup must never crash the
    process; `ScopedMemoryManager.add`/`.search` already degrade gracefully
    (return `None`/`[]`) when the wrapped manager is disabled.
    """
    try:
        manager = create_memory_manager(
            settings.memory,
            settings.ollama.api_url,
            settings.models.orchestration,
            resolve_embedding_url(settings.ollama),
        )
    except Exception as exc:  # noqa: BLE001 -- mem0/Ollama outage at construction must not crash the server
        logger.warning("knowledge: MemoryManager construction failed, memory disabled: %s", exc)
        manager = MemoryManager(MemoryConfig(enabled=False), settings.ollama.api_url)
    return create_scoped_memory_manager(manager)


class KnowledgeServiceImpl(KnowledgeServiceServicer):
    """Server-side implementation of all nine `KnowledgeService` RPCs.

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
        indexing_config: IndexingConfig | None = None,
        index_job_store: IndexJobStoreLike | None = None,
        index_queue: IndexJobQueue | None = None,
        index_worker_pool: IndexWorkerPool | None = None,
        start_index_workers: bool = True,
    ) -> None:
        self._settings = settings
        self._indexer = (
            indexer
            if indexer is not None
            else DocumentationIndexer(
                ollama_base_url=resolve_embedding_url(settings.ollama),
                embedding_endpoint=embedding_endpoint_label(settings.ollama),
            )
        )
        self._scoped_memory = (
            scoped_memory if scoped_memory is not None else _build_scoped_memory_manager(settings)
        )
        self._graph_config = graph_config if graph_config is not None else settings.graph
        # See module docstring's "CodeGraphStatus's local cache" section.
        self._code_graph_status: dict[str, tuple[int, int]] = {}

        # --- Async index-job queue (O10-a) -----------------------------------
        # Lazily constructed (see `_ensure_index_queue_infra`) unless a test
        # injects its own doubles -- `index_job_store`/`index_queue`/
        # `index_worker_pool` are seams, mirroring `indexer`/`scoped_memory`
        # above. `start_index_workers=False` is a test-only knob: skip the
        # background drain loop so a test can call
        # `self._index_worker_pool.run_one()` deterministically instead.
        self._indexing_config = (
            indexing_config if indexing_config is not None else settings.indexing
        )
        self._index_job_store = index_job_store
        self._index_queue = index_queue
        self._index_worker_pool = index_worker_pool
        self._start_index_workers = start_index_workers
        self._index_workers_started = False

    def _queue_enabled(self, ctx: ScopeContext) -> bool:
        """Whether `Index`/`IndexCode` should enqueue rather than run inline.

        Three independent reasons to fall back to the pre-O10-a inline
        path, all treated identically (never a crash, always a clean
        degrade, logged at WARNING once per call): the
        `penguincode.disable-index-queue` kill switch is on; no job-store
        DSN is configured (`IndexingConfig.dsn` empty) *and* no store/queue
        was explicitly injected (the test-double seam); or constructing the
        queue infra failed for any other reason.
        """
        if is_enabled(DISABLE_INDEX_QUEUE_FLAG, ctx):
            return False
        return bool(self._index_job_store is not None or self._indexing_config.dsn)

    def _ensure_index_queue_infra(self) -> tuple[IndexJobStoreLike, IndexJobQueue]:
        """Lazily construct (once) the job store/queue/worker pool, and start the pool."""
        if self._index_job_store is None:
            self._index_job_store = IndexJobStore(self._indexing_config.dsn)
        if self._index_queue is None:
            self._index_queue = IndexJobQueue(maxsize=self._indexing_config.queue_maxsize)
        if self._index_worker_pool is None:
            self._index_worker_pool = IndexWorkerPool(
                self._index_queue,
                self._index_job_store,
                worker_count=self._indexing_config.worker_count,
                default_timeout_seconds=self._indexing_config.job_timeout_seconds,
            )
        if self._start_index_workers and not self._index_workers_started:
            self._index_worker_pool.start()
            self._index_workers_started = True
        return self._index_job_store, self._index_queue

    async def _enqueue_index_job(
        self,
        context: grpc.aio.ServicerContext,
        ctx: ScopeContext,
        job_type: JobType,
        *,
        chunks_total: int,
        team_id: str | None,
        run: Callable[[], Awaitable[IndexJobOutcome]],
    ) -> IndexJob:
        """Create a QUEUED job row, enqueue its work, and return the row.

        On `IndexQueueFullError`, the just-created row is marked `FAILED`
        (never left dangling as `queued` forever) and the RPC aborts
        `RESOURCE_EXHAUSTED` with a retry hint -- O10-a's required
        backpressure contract: the queue never grows unbounded.
        """
        store, queue = self._ensure_index_queue_infra()
        job_id = store.create_queued(ctx, job_type, chunks_total=chunks_total, team_id=team_id)
        index_metrics.record_job_enqueued(job_type.value)
        work = QueuedIndexWork(
            job_id=job_id,
            ctx=ctx,
            job_type=job_type,
            run=run,
            timeout_seconds=self._indexing_config.job_timeout_seconds,
        )
        try:
            queue.put_nowait(work)
        except IndexQueueFullError as exc:
            store.mark_failed(job_id, "rejected: queue full")
            index_metrics.record_job_rejected(job_type.value)
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, str(exc))
            raise AssertionError("unreachable") from exc  # abort() always raises
        job = store.get(ctx, job_id)
        assert job is not None  # just created under the same ctx; always found
        return job

    async def reap_interrupted_index_jobs(self) -> int:
        """Mark every `running` index-job row `failed` ("interrupted").

        Called once by `server/main.py` at startup, before the gRPC server
        accepts traffic -- see `IndexJobStore.reap_interrupted`'s docstring
        for why a `running` row at boot is always from a dead process.
        A no-op (returns 0) when the queue isn't configured at all.
        """
        if self._index_job_store is None and not self._indexing_config.dsn:
            return 0
        store = self._index_job_store or IndexJobStore(self._indexing_config.dsn)
        return store.reap_interrupted()

    async def shutdown_index_workers(self, grace_period: float = 5.0) -> None:
        """Stop the background worker pool, if one was ever started. Called at server shutdown."""
        if self._index_worker_pool is not None and self._index_workers_started:
            await self._index_worker_pool.stop(grace_period)

    async def Index(
        self, request: IndexRequest, context: grpc.aio.ServicerContext
    ) -> IndexResponse:
        """Index a library's or a language's docs via `DocumentationIndexer`.

        **O10-a load leveling.** The actual embedding work (sequential
        Ollama calls per chunk) used to run inline on the shared gRPC
        executor -- a few large calls starved `Health`/`Chat`. It now
        enqueues onto the async index-job queue by default and returns
        immediately with `job_id`/`state=QUEUED`; poll `IndexStatus(job_id=
        ...)` for completion. Falls back to the pre-O10-a inline behavior
        (this RPC blocks until done, `chunks_indexed` is the real count,
        `job_id` empty) when the `penguincode.disable-index-queue` kill
        switch is on, or no job-store DSN is configured -- see
        `_queue_enabled`'s docstring.
        """
        ctx = await _require_scope(context)
        visibility = _visibility_from_proto(request.visibility, default="tenant")
        team_id = request.team_id or None
        doc_contents = list(request.doc_contents)
        target = request.WhichOneof("target")

        library: Library | None = None
        doc_language: ModelLanguage | None = None
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
        elif target == "language":
            doc_language = _PROTO_LANGUAGE_TO_MODEL.get(request.language)
            if doc_language is None:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "language is required")
                raise AssertionError("unreachable")  # abort() always raises
        else:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "target (library or language) is required"
            )
            raise AssertionError("unreachable")  # abort() always raises

        async def _do_index() -> int:
            with store_span(
                "knowledge.Index", target=target or "none", doc_count=len(doc_contents)
            ):
                if library is not None:
                    return await self._indexer.index_library(
                        ctx,
                        library,
                        doc_contents,
                        force_reindex=request.force_reindex,
                        visibility=visibility,
                        team_id=team_id,
                    )
                assert doc_language is not None  # exactly one of the two branches set a value
                return await self._indexer.index_language(
                    ctx,
                    doc_language,
                    doc_contents,
                    force_reindex=request.force_reindex,
                    visibility=visibility,
                    team_id=team_id,
                )

        if not self._queue_enabled(ctx):
            chunks_indexed = await _do_index()
            return IndexResponse(
                chunks_indexed=chunks_indexed,
                job_id="",
                state=ProtoJobState.JOB_STATE_SUCCEEDED,
            )

        async def _run() -> IndexJobOutcome:
            chunks = await _do_index()
            return IndexJobOutcome(chunks_done=chunks, chunks_total=len(doc_contents))

        job = await self._enqueue_index_job(
            context,
            ctx,
            JobType.INDEX_DOCS,
            chunks_total=len(doc_contents),
            team_id=team_id,
            run=_run,
        )
        return IndexResponse(
            chunks_indexed=0, job_id=job.id, state=_JOB_STATE_TO_PROTO[job.state.value]
        )

    async def Query(
        self, request: QueryRequest, context: grpc.aio.ServicerContext
    ) -> QueryResponse:
        """Hybrid GraphRAG retrieval via `retrieval.graphrag.retrieve`.

        `n_vector`/`graph_depth` are clamped to `self._settings.limits`
        before being passed down (ops-audit O7) -- `retrieve()` and the
        stores it calls clamp again independently, but a caller-visible
        clamp here keeps the request-level trace/log attributes honest
        about what actually ran.
        """
        ctx = await _require_scope(context)
        limits = self._settings.limits
        n_vector = _clamp_param(request.n_vector or 8, limits.max_vector_results, param="n_vector")
        graph_depth = _clamp_param(
            request.graph_depth or 1, limits.max_graph_depth, param="graph_depth"
        )
        requested_tables: list[TableName] = [
            table for name in request.vector_tables if (table := _valid_table(name)) is not None
        ]

        with store_span(
            "knowledge.Query",
            n_vector=n_vector,
            graph_depth=graph_depth,
            table_count=len(requested_tables),
        ):
            embedding_url = resolve_embedding_url(self._settings.ollama)
            embedding_endpoint = embedding_endpoint_label(self._settings.ollama)
            if requested_tables:
                result = await retrieve(
                    ctx,
                    request.query,
                    n_vector=n_vector,
                    graph_depth=graph_depth,
                    vector_tables=tuple(requested_tables),
                    ollama_base_url=embedding_url,
                    embedding_endpoint=embedding_endpoint,
                )
            else:
                result = await retrieve(
                    ctx,
                    request.query,
                    n_vector=n_vector,
                    graph_depth=graph_depth,
                    ollama_base_url=embedding_url,
                    embedding_endpoint=embedding_endpoint,
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
        """Search memories within the caller's scope via `ScopedMemoryManager.search`.

        `limit` is clamped to `self._settings.limits.max_vector_results`
        (ops-audit O7) -- the same top-k bound applied to `Query`'s
        `n_vector`, since an unclamped memory-search limit is the identical
        unbounded-result-set risk.
        """
        ctx = await _require_scope(context)
        limit = _clamp_param(
            request.limit or 5, self._settings.limits.max_vector_results, param="limit"
        )

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
        """(Re)build the code graph for a source tree via `graphs.code.index_code`.

        Same O10-a queue-by-default / kill-switch-or-no-DSN-degrades-to-
        inline contract as `Index` -- see that method's docstring.
        """
        ctx = await _require_scope(context)
        visibility = _visibility_from_proto(request.visibility, default="team")
        team_id = request.team_id or None

        async def _do_index_code() -> tuple[int, int] | None:
            with store_span("knowledge.IndexCode", visibility=visibility):
                # `index_code` is a synchronous function performing blocking
                # file I/O and psycopg calls -- run off the event loop.
                result = await asyncio.to_thread(
                    index_code,
                    ctx,
                    request.root_path,
                    visibility=visibility,
                    team_id=team_id,
                    config=self._graph_config,
                )
            if result is None:
                return None
            node_count = len(result.nodes)
            edge_count = len(result.edges)
            self._code_graph_status[ctx.tenant_id] = (node_count, edge_count)
            return node_count, edge_count

        if not self._queue_enabled(ctx):
            counts = await _do_index_code()
            if counts is None:
                return IndexCodeResponse(
                    indexed=False,
                    node_count=0,
                    edge_count=0,
                    job_id="",
                    state=ProtoJobState.JOB_STATE_SUCCEEDED,
                )
            return IndexCodeResponse(
                indexed=True,
                node_count=counts[0],
                edge_count=counts[1],
                job_id="",
                state=ProtoJobState.JOB_STATE_SUCCEEDED,
            )

        async def _run() -> IndexJobOutcome:
            counts = await _do_index_code()
            if counts is None:
                return IndexJobOutcome(extra={"indexed": False, "node_count": 0, "edge_count": 0})
            return IndexJobOutcome(
                extra={"indexed": True, "node_count": counts[0], "edge_count": counts[1]}
            )

        job = await self._enqueue_index_job(
            context, ctx, JobType.INDEX_CODE, chunks_total=0, team_id=team_id, run=_run
        )
        return IndexCodeResponse(
            indexed=False,
            node_count=0,
            edge_count=0,
            job_id=job.id,
            state=_JOB_STATE_TO_PROTO[job.state.value],
        )

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

    async def IndexStatus(
        self, request: IndexStatusRequest, context: grpc.aio.ServicerContext
    ) -> IndexStatusResponse:
        """Report the caller's docs index status, or (O10-a) one async job's progress.

        When `request.job_id` is set, reports that job's state/progress
        (tenant + owner scoped, see `IndexJobStore.get`) instead of the
        aggregate docs-index stats below -- `NOT_FOUND` if the job doesn't
        exist or isn't visible to this caller. Otherwise unchanged:
        read-only, no elevated scope beyond `_require_scope`'s baseline
        authentication, mirroring `CodeGraphStatus`.
        """
        ctx = await _require_scope(context)

        if request.job_id:
            if self._index_job_store is None and not self._indexing_config.dsn:
                # No queue infra ever provisioned -- this job_id can't exist.
                await context.abort(
                    grpc.StatusCode.NOT_FOUND, f"index job {request.job_id!r} not found"
                )
                raise AssertionError("unreachable")  # abort() always raises
            store, _ = self._ensure_index_queue_infra()
            job = store.get(ctx, request.job_id)
            if job is None:
                await context.abort(
                    grpc.StatusCode.NOT_FOUND, f"index job {request.job_id!r} not found"
                )
                raise AssertionError("unreachable")  # abort() always raises
            return IndexStatusResponse(**_job_status_fields(job))

        with store_span("knowledge.IndexStatus"):
            status = self._indexer.get_index_status(ctx)

        response = IndexStatusResponse(total_chunks=int(status.get("total_chunks", 0)))
        for lib_key, info in status.get("libraries", {}).items():
            response.libraries[lib_key].CopyFrom(
                LibraryIndexStatus(
                    chunk_count=int(info.get("chunk_count", 0)),
                    indexed_at=str(info.get("indexed_at", "")),
                    expires_at=str(info.get("expires_at", "")),
                    is_expired=bool(info.get("is_expired", False)),
                    language=str(info.get("language", "unknown")),
                )
            )
        for lang_key, info in status.get("languages", {}).items():
            response.languages[lang_key].CopyFrom(
                LanguageIndexStatus(
                    chunk_count=int(info.get("chunk_count", 0)),
                    indexed_at=str(info.get("indexed_at", "")),
                    expires_at=str(info.get("expires_at", "")),
                    is_expired=bool(info.get("is_expired", False)),
                )
            )
        return response

    async def ClearIndex(
        self, request: ClearIndexRequest, context: grpc.aio.ServicerContext
    ) -> ClearIndexResponse:
        """Clear one library's or one language's docs index, scoped to the caller.

        A mutating call, but requires no additional elevated scope beyond
        `_require_scope`'s baseline authentication -- unlike `MemoryAdd`'s
        `tenant`-visibility gate, there is no broader-than-caller target to
        guard against here: `DocumentationIndexer.clear_library_index`/
        `.clear_language_index` delegate the actual row deletion to
        `stores.vector.PgVectorStore.delete`, which already restricts every
        delete to rows visible to `ctx` (`visibility = 'tenant' OR (visibility
        = 'team' AND team_id = ANY(ctx.team_ids)) OR (visibility = 'user' AND
        owner_user_id = ctx.user_id)`, see that module's `delete()`) -- a
        caller can never clear another team's or another user's
        docs, even within the same tenant, regardless of which library/
        language name they pass here.
        """
        ctx = await _require_scope(context)
        target = request.WhichOneof("target")

        with store_span("knowledge.ClearIndex", target=target or "none"):
            if target == "library_name":
                removed = await self._indexer.clear_library_index(ctx, request.library_name)
            elif target == "language":
                doc_language = _PROTO_LANGUAGE_TO_MODEL.get(request.language)
                if doc_language is None:
                    await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "language is required")
                    raise AssertionError("unreachable")  # abort() always raises
                removed = await self._indexer.clear_language_index(ctx, doc_language)
            else:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "target (library_name or language) is required",
                )
                raise AssertionError("unreachable")  # abort() always raises

        return ClearIndexResponse(chunks_removed=removed)

    async def CleanupIndex(
        self, request: CleanupIndexRequest, context: grpc.aio.ServicerContext
    ) -> CleanupIndexResponse:
        """Remove indexed docs no longer referenced by the caller's project.

        Mirrors `DocumentationIndexer.cleanup_unused`; the caller's current
        project state is supplied on the request (the server has no
        independent way to know what a project still references -- project
        detection is a client-side file-tree scan, see
        `docs_rag.detector.ProjectDetector`), the same caller-supplied-target
        pattern `Index` already uses. Same no-additional-scope rationale as
        `ClearIndex` -- `cleanup_unused` deletes exclusively through
        `clear_library_index`/`.clear_language_index`, so it inherits the
        identical store-layer visibility restriction.
        """
        ctx = await _require_scope(context)
        current_libraries: list[Library] = []
        for target in request.current_libraries:
            library_language = _PROTO_LANGUAGE_TO_MODEL.get(target.language)
            if library_language is None:
                continue
            current_libraries.append(
                Library(name=target.name, language=library_language, version=target.version or None)
            )
        current_languages = [
            mapped
            for proto_lang in request.current_languages
            if (mapped := _PROTO_LANGUAGE_TO_MODEL.get(proto_lang)) is not None
        ]

        with store_span(
            "knowledge.CleanupIndex",
            library_count=len(current_libraries),
            language_count=len(current_languages),
        ):
            removed = await self._indexer.cleanup_unused(ctx, current_libraries, current_languages)

        return CleanupIndexResponse(removed=dict(removed))

    async def ListIndexJobs(
        self, request: ListIndexJobsRequest, context: grpc.aio.ServicerContext
    ) -> ListIndexJobsResponse:
        """List the caller's own async index jobs (tenant + owner scoped), most recent first.

        Mirrors `IndexJobStore.list_jobs`. Returns an empty list (never an
        error) when the queue infra was never provisioned -- no DSN means
        no job has ever been created, so there is nothing to list.
        """
        ctx = await _require_scope(context)
        if self._index_job_store is None and not self._indexing_config.dsn:
            return ListIndexJobsResponse(jobs=[])

        store, _ = self._ensure_index_queue_infra()
        limit = request.limit or 20
        with store_span("knowledge.ListIndexJobs", limit=limit):
            jobs = store.list_jobs(ctx, limit=limit)

        return ListIndexJobsResponse(
            jobs=[IndexJobSummary(**_job_status_fields(job)) for job in jobs]
        )


__all__ = ["KnowledgeServiceImpl"]
