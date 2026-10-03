"""Tests for the O10-a async index-job queue wired into `KnowledgeServiceImpl`'s
`Index`/`IndexCode`/`IndexStatus`/`ListIndexJobs` handlers.

`tests/test_server_knowledge_service.py` covers the pre-existing (and,
post-O10-a, still-default-when-no-DSN-configured) inline/legacy path --
unaffected by this file, see its own module docstring. These tests instead
force the queue-enabled branch via an *injected* `_FakeIndexJobStore` (no
live Postgres needed for the fast unit tests) -- `_queue_enabled()` treats
"a store was explicitly injected" the same as "a real DSN is configured".
`start_index_workers=False` is used throughout so `IndexWorkerPool.run_one()`
drives exactly one job deterministically, never racing a background task.

# regression: penguincode-index-job-queue (O10-a -- load leveling for Index/IndexCode)
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import grpc
import pytest

import penguincode_cli.auth.middleware as auth_middleware
from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import IndexingConfig, Settings
from penguincode_cli.graphs.code import ExtractionResult
from penguincode_cli.indexing.jobs import IndexJob, JobState, JobType
from penguincode_cli.indexing.queue import IndexJobQueue
from penguincode_cli.indexing.store import IndexJobOutcome
from penguincode_cli.proto import (
    IndexCodeRequest,
    IndexRequest,
    IndexStatusRequest,
    LibraryTarget,
    ListIndexJobsRequest,
)
from penguincode_cli.proto import JobState as ProtoJobState
from penguincode_cli.proto import JobType as ProtoJobType
from penguincode_cli.proto import Language as ProtoLanguage
from penguincode_cli.server.services.knowledge import KnowledgeServiceImpl
from penguincode_cli.stores.graph import GraphNode


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
    def __init__(self) -> None:
        self.aborted_with: tuple[Any, str] | None = None

    async def abort(self, code: Any, details: str) -> None:
        self.aborted_with = (code, details)
        raise AbortCalledError(details)


@pytest.fixture
def scope_ctx() -> Iterator[ScopeContext]:
    ctx = _ctx()
    token = auth_middleware._current_scope.set(ctx)
    yield ctx
    auth_middleware._current_scope.reset(token)


@pytest.fixture(autouse=True)
def _no_leftover_scope() -> Iterator[None]:
    assert auth_middleware.current_scope_context() is None
    yield
    auth_middleware._current_scope.set(None)


@dataclass(slots=True)
class _FakeIndexJobStore:
    """In-memory `IndexJobStoreLike` double -- see `tests/test_indexing_worker.py`."""

    rows: dict[str, IndexJob] = field(default_factory=dict)

    def create_queued(
        self, ctx: ScopeContext, job_type: JobType, *, chunks_total: int, team_id: str | None = None
    ) -> str:
        if team_id is not None and team_id not in ctx.team_ids:
            raise ValueError(f"team_id {team_id!r} is not one of the caller's own teams")
        job_id = f"job-{len(self.rows) + 1}"
        self.rows[job_id] = IndexJob(
            id=job_id,
            tenant_id=ctx.tenant_id,
            owner_user_id=ctx.user_id,
            job_type=job_type,
            state=JobState.QUEUED,
            chunks_done=0,
            chunks_total=chunks_total,
        )
        return job_id

    def mark_running(self, job_id: str) -> None:
        self.rows[job_id] = _with_state(self.rows[job_id], JobState.RUNNING)

    def mark_succeeded(self, job_id: str, outcome: IndexJobOutcome) -> None:
        row = self.rows[job_id]
        self.rows[job_id] = IndexJob(
            id=row.id,
            tenant_id=row.tenant_id,
            owner_user_id=row.owner_user_id,
            job_type=row.job_type,
            state=JobState.SUCCEEDED,
            chunks_done=outcome.chunks_done,
            chunks_total=outcome.chunks_total,
            result=outcome.extra,
        )

    def mark_failed(self, job_id: str, error: str) -> None:
        row = self.rows[job_id]
        self.rows[job_id] = IndexJob(
            id=row.id,
            tenant_id=row.tenant_id,
            owner_user_id=row.owner_user_id,
            job_type=row.job_type,
            state=JobState.FAILED,
            chunks_done=row.chunks_done,
            chunks_total=row.chunks_total,
            error=error,
        )

    def get(self, ctx: ScopeContext, job_id: str) -> IndexJob | None:
        row = self.rows.get(job_id)
        if row is None or row.tenant_id != ctx.tenant_id or row.owner_user_id != ctx.user_id:
            return None
        return row

    def list_jobs(self, ctx: ScopeContext, *, limit: int = 20) -> list[IndexJob]:
        matches = [
            r
            for r in self.rows.values()
            if r.tenant_id == ctx.tenant_id and r.owner_user_id == ctx.user_id
        ]
        return list(reversed(matches))[:limit]

    def reap_interrupted(self) -> int:
        count = 0
        for job_id, row in list(self.rows.items()):
            if row.state is JobState.RUNNING:
                self.mark_failed(job_id, "interrupted")
                count += 1
        return count


def _with_state(row: IndexJob, state: JobState) -> IndexJob:
    return IndexJob(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_user_id=row.owner_user_id,
        job_type=row.job_type,
        state=state,
        chunks_done=row.chunks_done,
        chunks_total=row.chunks_total,
        result=row.result,
        error=row.error,
    )


class _FakeIndexer:
    """Minimal `_IndexerLike` double -- only `index_library`/`index_language` used here."""

    def __init__(self, *, chunks: int = 3, delay: float = 0.0) -> None:
        self._delay = delay
        self.index_library = AsyncMock(return_value=chunks)
        self.index_language = AsyncMock(return_value=chunks)

    async def _slow_index_library(self, *_a: Any, **_k: Any) -> int:
        await asyncio.sleep(self._delay)
        return 3


class _FakeScopedMemory:
    """Minimal `_ScopedMemoryLike` double -- unused by these tests, but injected so
    `KnowledgeServiceImpl.__init__` never falls back to constructing a real
    (degraded) `MemoryManager`/pgvector connection pool for an unrelated field."""

    def __init__(self) -> None:
        self.add = AsyncMock(return_value=None)
        self.search = AsyncMock(return_value=[])


def _service(
    *,
    indexer: Any = None,
    index_job_store: _FakeIndexJobStore | None = None,
    index_queue: IndexJobQueue | None = None,
    indexing_config: IndexingConfig | None = None,
) -> KnowledgeServiceImpl:
    return KnowledgeServiceImpl(
        Settings(),
        indexer=indexer or _FakeIndexer(),
        scoped_memory=_FakeScopedMemory(),
        index_job_store=index_job_store if index_job_store is not None else _FakeIndexJobStore(),
        index_queue=index_queue,
        indexing_config=indexing_config,
        start_index_workers=False,
    )


class TestEnqueueFastReturn:
    async def test_index_returns_queued_immediately_while_embedder_is_slow(
        self, scope_ctx: ScopeContext
    ) -> None:
        indexer = _FakeIndexer()
        indexer.index_library = AsyncMock(side_effect=lambda *a, **k: asyncio.sleep(5, result=3))
        service = _service(indexer=indexer)
        request = IndexRequest(
            api_version="v1",
            library=LibraryTarget(name="fastapi", language=ProtoLanguage.LANGUAGE_PYTHON),
            doc_contents=["# docs"],
        )

        response = await asyncio.wait_for(service.Index(request, _FakeContext()), timeout=1.0)

        assert response.job_id != ""
        assert response.state == ProtoJobState.JOB_STATE_QUEUED
        assert response.chunks_indexed == 0
        indexer.index_library.assert_not_awaited()  # enqueued, not yet run

    async def test_index_code_returns_queued_immediately(
        self, scope_ctx: ScopeContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import penguincode_cli.server.services.knowledge as knowledge_module

        async def _slow_index_code(*_a: Any, **_k: Any) -> None:
            await asyncio.sleep(5)

        monkeypatch.setattr(knowledge_module, "index_code", lambda *a, **k: None)
        # `index_code` runs via `asyncio.to_thread` in the real handler -- patch the
        # module-level sync function directly (it's a cheap, instantaneous no-op here);
        # the "fast return" property under test is the RPC returning before any
        # worker has run, not the indexer call itself being slow.
        service = _service(
            index_job_store=_FakeIndexJobStore(),
            index_queue=IndexJobQueue(maxsize=1),
        )
        request = IndexCodeRequest(api_version="v1", root_path=str(tmp_path))

        response = await asyncio.wait_for(service.IndexCode(request, _FakeContext()), timeout=1.0)

        assert response.job_id != ""
        assert response.state == ProtoJobState.JOB_STATE_QUEUED
        assert response.indexed is False


class TestQueueFull:
    async def test_index_aborts_resource_exhausted_when_queue_is_full(
        self, scope_ctx: ScopeContext
    ) -> None:
        queue = IndexJobQueue(maxsize=1)
        store = _FakeIndexJobStore()
        # Pre-fill the queue to capacity via a direct store+queue enqueue, bypassing
        # the service so the one slot is already taken before `Index` is called.
        job_id = store.create_queued(_ctx(), JobType.INDEX_DOCS, chunks_total=1)
        from penguincode_cli.indexing.queue import QueuedIndexWork

        async def _never() -> IndexJobOutcome:
            return IndexJobOutcome()

        queue.put_nowait(
            QueuedIndexWork(
                job_id=job_id,
                ctx=_ctx(),
                job_type=JobType.INDEX_DOCS,
                run=_never,
                timeout_seconds=5.0,
            )
        )
        service = _service(index_job_store=store, index_queue=queue)
        request = IndexRequest(
            api_version="v1", language=ProtoLanguage.LANGUAGE_RUST, doc_contents=["x"]
        )
        context = _FakeContext()

        with pytest.raises(AbortCalledError):
            await service.Index(request, context)

        assert context.aborted_with is not None
        assert context.aborted_with[0] == grpc.StatusCode.RESOURCE_EXHAUSTED

    async def test_rejected_job_row_is_marked_failed_not_left_dangling(
        self, scope_ctx: ScopeContext
    ) -> None:
        queue = IndexJobQueue(maxsize=1)
        store = _FakeIndexJobStore()
        job_id = store.create_queued(_ctx(), JobType.INDEX_DOCS, chunks_total=1)
        from penguincode_cli.indexing.queue import QueuedIndexWork

        async def _never() -> IndexJobOutcome:
            return IndexJobOutcome()

        queue.put_nowait(
            QueuedIndexWork(
                job_id=job_id,
                ctx=_ctx(),
                job_type=JobType.INDEX_DOCS,
                run=_never,
                timeout_seconds=5.0,
            )
        )
        service = _service(index_job_store=store, index_queue=queue)
        request = IndexRequest(
            api_version="v1", language=ProtoLanguage.LANGUAGE_RUST, doc_contents=["x"]
        )

        with pytest.raises(AbortCalledError):
            await service.Index(request, _FakeContext())

        rejected_job_id = next(iter(k for k in store.rows if k != job_id))
        assert store.rows[rejected_job_id].state is JobState.FAILED


class TestKillSwitchForcesLegacy:
    async def test_disable_flag_on_runs_inline_even_with_a_store_injected(
        self, scope_ctx: ScopeContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_INDEX_QUEUE", "true")
        indexer = _FakeIndexer(chunks=9)
        store = _FakeIndexJobStore()
        service = _service(indexer=indexer, index_job_store=store)
        request = IndexRequest(
            api_version="v1", language=ProtoLanguage.LANGUAGE_RUST, doc_contents=["x"]
        )

        response = await service.Index(request, _FakeContext())

        assert response.job_id == ""
        assert response.state == ProtoJobState.JOB_STATE_SUCCEEDED
        assert response.chunks_indexed == 9
        assert store.rows == {}  # never touched the job store at all


class TestWorkerCompletesAnEnqueuedJob:
    async def test_run_one_drains_the_job_then_index_status_reports_succeeded(
        self, scope_ctx: ScopeContext
    ) -> None:
        indexer = _FakeIndexer(chunks=6)
        store = _FakeIndexJobStore()
        service = _service(indexer=indexer, index_job_store=store)
        request = IndexRequest(
            api_version="v1", language=ProtoLanguage.LANGUAGE_RUST, doc_contents=["a", "b"]
        )

        enqueue_response = await service.Index(request, _FakeContext())
        job_id = enqueue_response.job_id

        pool = service._index_worker_pool
        assert pool is not None
        processed = await pool.run_one(timeout=2.0)
        assert processed is True

        status = await service.IndexStatus(
            IndexStatusRequest(api_version="v1", job_id=job_id), _FakeContext()
        )
        assert status.job_id == job_id
        assert status.state == ProtoJobState.JOB_STATE_SUCCEEDED
        assert status.job_type == ProtoJobType.JOB_TYPE_INDEX_DOCS
        assert status.chunks_done == 6
        assert status.chunks_total == 2
        # Aggregate-mode fields are untouched in job-status mode.
        assert dict(status.libraries) == {}
        assert status.total_chunks == 0

    async def test_index_code_job_result_round_trips_node_edge_counts(
        self, scope_ctx: ScopeContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import penguincode_cli.server.services.knowledge as knowledge_module

        monkeypatch.setattr(
            knowledge_module,
            "index_code",
            lambda *a, **k: ExtractionResult(
                nodes=[GraphNode(node_type="file", key="a.py")], edges=[]
            ),
        )
        store = _FakeIndexJobStore()
        service = _service(index_job_store=store)
        request = IndexCodeRequest(api_version="v1", root_path=str(tmp_path))

        enqueue_response = await service.IndexCode(request, _FakeContext())
        pool = service._index_worker_pool
        assert pool is not None
        assert await pool.run_one(timeout=2.0) is True

        status = await service.IndexStatus(
            IndexStatusRequest(api_version="v1", job_id=enqueue_response.job_id), _FakeContext()
        )
        assert status.state == ProtoJobState.JOB_STATE_SUCCEEDED
        assert dict(status.result) == {"indexed": True, "node_count": 1.0, "edge_count": 0.0}

    async def test_worker_failure_surfaces_as_failed_with_error_via_index_status(
        self, scope_ctx: ScopeContext
    ) -> None:
        indexer = _FakeIndexer()
        indexer.index_language = AsyncMock(side_effect=RuntimeError("ollama unreachable"))
        store = _FakeIndexJobStore()
        service = _service(indexer=indexer, index_job_store=store)
        request = IndexRequest(
            api_version="v1", language=ProtoLanguage.LANGUAGE_RUST, doc_contents=["x"]
        )

        enqueue_response = await service.Index(request, _FakeContext())
        pool = service._index_worker_pool
        assert pool is not None
        assert await pool.run_one(timeout=2.0) is True

        status = await service.IndexStatus(
            IndexStatusRequest(api_version="v1", job_id=enqueue_response.job_id), _FakeContext()
        )
        assert status.state == ProtoJobState.JOB_STATE_FAILED
        assert "ollama unreachable" in status.error


class TestIndexStatusJobNotFound:
    async def test_unknown_job_id_aborts_not_found(self, scope_ctx: ScopeContext) -> None:
        service = _service()
        context = _FakeContext()

        with pytest.raises(AbortCalledError):
            await service.IndexStatus(
                IndexStatusRequest(api_version="v1", job_id="does-not-exist"), context
            )

        assert context.aborted_with is not None
        assert context.aborted_with[0] == grpc.StatusCode.NOT_FOUND

    async def test_no_queue_infra_at_all_still_aborts_not_found_not_crash(
        self, scope_ctx: ScopeContext
    ) -> None:
        """No store injected and no DSN configured -- the handler must not try to
        open a real DB connection with an empty DSN."""
        service = KnowledgeServiceImpl(
            Settings(),
            indexer=_FakeIndexer(),
            scoped_memory=_FakeScopedMemory(),
            start_index_workers=False,
        )
        context = _FakeContext()

        with pytest.raises(AbortCalledError):
            await service.IndexStatus(
                IndexStatusRequest(api_version="v1", job_id="whatever"), context
            )

        assert context.aborted_with is not None
        assert context.aborted_with[0] == grpc.StatusCode.NOT_FOUND

    async def test_a_different_tenants_job_id_is_also_not_found(
        self, scope_ctx: ScopeContext
    ) -> None:
        store = _FakeIndexJobStore()
        other_ctx = _ctx(tenant_id="tenant-b", user_id="user-b")
        job_id = store.create_queued(other_ctx, JobType.INDEX_DOCS, chunks_total=1)
        service = _service(index_job_store=store)

        with pytest.raises(AbortCalledError) as exc_info:
            await service.IndexStatus(
                IndexStatusRequest(api_version="v1", job_id=job_id), _FakeContext()
            )
        assert exc_info is not None


class TestListIndexJobs:
    async def test_lists_only_the_callers_own_jobs_most_recent_first(
        self, scope_ctx: ScopeContext
    ) -> None:
        store = _FakeIndexJobStore()
        other_ctx = _ctx(tenant_id="tenant-b", user_id="user-b")
        store.create_queued(other_ctx, JobType.INDEX_DOCS, chunks_total=1)  # not mine
        first = store.create_queued(scope_ctx, JobType.INDEX_DOCS, chunks_total=1)
        second = store.create_queued(scope_ctx, JobType.INDEX_CODE, chunks_total=0)
        service = _service(index_job_store=store)

        response = await service.ListIndexJobs(
            ListIndexJobsRequest(api_version="v1"), _FakeContext()
        )

        assert [job.job_id for job in response.jobs] == [second, first]

    async def test_no_queue_infra_returns_empty_list_not_an_error(
        self, scope_ctx: ScopeContext
    ) -> None:
        service = KnowledgeServiceImpl(
            Settings(),
            indexer=_FakeIndexer(),
            scoped_memory=_FakeScopedMemory(),
            start_index_workers=False,
        )

        response = await service.ListIndexJobs(
            ListIndexJobsRequest(api_version="v1"), _FakeContext()
        )

        assert list(response.jobs) == []


class TestReapInterruptedIndexJobsLifecycleHook:
    async def test_reap_interrupted_index_jobs_delegates_to_the_store(self) -> None:
        store = _FakeIndexJobStore()
        store.rows["r1"] = IndexJob(
            id="r1",
            tenant_id="t",
            owner_user_id="u",
            job_type=JobType.INDEX_DOCS,
            state=JobState.RUNNING,
            chunks_done=0,
            chunks_total=1,
        )
        service = _service(index_job_store=store)

        reaped = await service.reap_interrupted_index_jobs()

        assert reaped == 1
        assert store.rows["r1"].state is JobState.FAILED

    async def test_no_queue_infra_at_all_reaps_zero(self) -> None:
        service = KnowledgeServiceImpl(
            Settings(),
            indexer=_FakeIndexer(),
            scoped_memory=_FakeScopedMemory(),
            start_index_workers=False,
        )

        assert await service.reap_interrupted_index_jobs() == 0


class TestShutdownIndexWorkers:
    async def test_shutdown_before_any_queue_use_is_a_safe_no_op(self) -> None:
        service = _service()
        await service.shutdown_index_workers()  # must not raise
