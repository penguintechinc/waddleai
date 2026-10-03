"""Tests for `penguincode_cli.indexing.worker.IndexWorkerPool` (O10-a).

Uses `_FakeIndexJobStore` (an in-memory double satisfying
`indexing.store.IndexJobStoreLike`) -- no live Postgres needed, mirroring
`stores.vector.VectorStore`'s own Protocol-based test-double convention.
`run_one()` drives the pool deterministically (no background task racing
a test's own assertions).

# regression: penguincode-index-job-queue (O10-a -- load leveling for Index/IndexCode)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.indexing.jobs import IndexJob, JobState, JobType
from penguincode_cli.indexing.queue import IndexJobQueue, QueuedIndexWork
from penguincode_cli.indexing.store import IndexJobOutcome
from penguincode_cli.indexing.worker import IndexWorkerPool


def _ctx() -> ScopeContext:
    return ScopeContext(tenant_id="tenant-a", org_id=None, team_ids=(), user_id="user-a", scopes=())


@dataclass(slots=True)
class _FakeIndexJobStore:
    """In-memory `IndexJobStoreLike` double -- records every state transition."""

    rows: dict[str, IndexJob] = field(default_factory=dict)

    def create_queued(
        self, ctx: ScopeContext, job_type: JobType, *, chunks_total: int, team_id: str | None = None
    ) -> str:
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
        row = self.rows[job_id]
        self.rows[job_id] = IndexJob(
            id=row.id,
            tenant_id=row.tenant_id,
            owner_user_id=row.owner_user_id,
            job_type=row.job_type,
            state=JobState.RUNNING,
            chunks_done=row.chunks_done,
            chunks_total=row.chunks_total,
        )

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
        return [
            r
            for r in self.rows.values()
            if r.tenant_id == ctx.tenant_id and r.owner_user_id == ctx.user_id
        ][:limit]

    def reap_interrupted(self) -> int:
        count = 0
        for job_id, row in list(self.rows.items()):
            if row.state is JobState.RUNNING:
                self.mark_failed(job_id, "interrupted")
                count += 1
        return count


def _pool(
    store: _FakeIndexJobStore, *, worker_count: int = 2
) -> tuple[IndexWorkerPool, IndexJobQueue]:
    queue = IndexJobQueue(maxsize=8)
    pool = IndexWorkerPool(queue, store, worker_count=worker_count, default_timeout_seconds=5.0)
    return pool, queue


class TestConstruction:
    def test_rejects_zero_worker_count(self) -> None:
        store = _FakeIndexJobStore()
        queue = IndexJobQueue(maxsize=4)
        with pytest.raises(ValueError, match="worker_count"):
            IndexWorkerPool(queue, store, worker_count=0, default_timeout_seconds=5.0)


class TestRunOneSuccess:
    async def test_processes_to_succeeded_with_progress(self) -> None:
        store = _FakeIndexJobStore()
        pool, queue = _pool(store)
        job_id = store.create_queued(_ctx(), JobType.INDEX_DOCS, chunks_total=3)

        async def _run() -> IndexJobOutcome:
            return IndexJobOutcome(chunks_done=3, chunks_total=3)

        queue.put_nowait(
            QueuedIndexWork(
                job_id=job_id,
                ctx=_ctx(),
                job_type=JobType.INDEX_DOCS,
                run=_run,
                timeout_seconds=5.0,
            )
        )

        processed = await pool.run_one(timeout=2.0)

        assert processed is True
        row = store.rows[job_id]
        assert row.state is JobState.SUCCEEDED
        assert row.chunks_done == 3
        assert row.chunks_total == 3

    async def test_returns_false_when_queue_empty(self) -> None:
        store = _FakeIndexJobStore()
        pool, _queue = _pool(store)

        processed = await pool.run_one(timeout=0.2)

        assert processed is False


class TestRunOneFailure:
    async def test_job_exception_marks_failed_with_message_and_keeps_pool_alive(self) -> None:
        store = _FakeIndexJobStore()
        pool, queue = _pool(store)
        job_id = store.create_queued(_ctx(), JobType.INDEX_DOCS, chunks_total=1)

        async def _run() -> IndexJobOutcome:
            raise RuntimeError("embedding backend unreachable")

        queue.put_nowait(
            QueuedIndexWork(
                job_id=job_id,
                ctx=_ctx(),
                job_type=JobType.INDEX_DOCS,
                run=_run,
                timeout_seconds=5.0,
            )
        )

        processed = await pool.run_one(timeout=2.0)

        assert processed is True
        row = store.rows[job_id]
        assert row.state is JobState.FAILED
        assert "embedding backend unreachable" in (row.error or "")

        # The pool itself must still be usable for the next job.
        job_id_2 = store.create_queued(_ctx(), JobType.INDEX_DOCS, chunks_total=1)

        async def _run_ok() -> IndexJobOutcome:
            return IndexJobOutcome(chunks_done=1, chunks_total=1)

        queue.put_nowait(
            QueuedIndexWork(
                job_id=job_id_2,
                ctx=_ctx(),
                job_type=JobType.INDEX_DOCS,
                run=_run_ok,
                timeout_seconds=5.0,
            )
        )
        assert await pool.run_one(timeout=2.0) is True
        assert store.rows[job_id_2].state is JobState.SUCCEEDED

    async def test_job_timeout_marks_failed_with_timeout_message(self) -> None:
        store = _FakeIndexJobStore()
        pool, queue = _pool(store)
        job_id = store.create_queued(_ctx(), JobType.INDEX_DOCS, chunks_total=1)

        async def _hangs_forever() -> IndexJobOutcome:
            await asyncio.sleep(10)
            return IndexJobOutcome()

        queue.put_nowait(
            QueuedIndexWork(
                job_id=job_id,
                ctx=_ctx(),
                job_type=JobType.INDEX_DOCS,
                run=_hangs_forever,
                timeout_seconds=0.05,
            )
        )

        processed = await pool.run_one(timeout=2.0)

        assert processed is True
        row = store.rows[job_id]
        assert row.state is JobState.FAILED
        assert "timed out" in (row.error or "")


class TestStopWithoutStart:
    async def test_stop_before_start_is_a_safe_no_op(self) -> None:
        store = _FakeIndexJobStore()
        pool, _queue = _pool(store, worker_count=1)

        await pool.stop(grace_period=0.1)  # must not raise, never started any task


class TestStartStopLifecycle:
    async def test_background_loop_drains_a_queued_job_without_run_one(self) -> None:
        """The real `start()`/`stop()` lifecycle (not the `run_one` test seam) also works."""
        store = _FakeIndexJobStore()
        pool, queue = _pool(store, worker_count=1)
        job_id = store.create_queued(_ctx(), JobType.INDEX_DOCS, chunks_total=2)
        done = asyncio.Event()

        async def _run() -> IndexJobOutcome:
            done.set()
            return IndexJobOutcome(chunks_done=2, chunks_total=2)

        pool.start()
        try:
            queue.put_nowait(
                QueuedIndexWork(
                    job_id=job_id,
                    ctx=_ctx(),
                    job_type=JobType.INDEX_DOCS,
                    run=_run,
                    timeout_seconds=5.0,
                )
            )
            await asyncio.wait_for(done.wait(), timeout=2.0)
            # Give the worker a beat to persist the terminal state after
            # signaling `done` (the signal happens inside `_run`, before
            # `_process` writes `mark_succeeded`).
            for _ in range(50):
                if store.rows[job_id].state is JobState.SUCCEEDED:
                    break
                await asyncio.sleep(0.02)
            assert store.rows[job_id].state is JobState.SUCCEEDED
        finally:
            await pool.stop(grace_period=1.0)

    async def test_stop_cancels_a_still_running_worker_after_grace_period(self) -> None:
        """A worker idle in its poll loop (no job ever queued) must still be cancelled
        cleanly by `stop()`'s grace-period timeout path."""
        store = _FakeIndexJobStore()
        pool, _queue = _pool(store, worker_count=1)

        pool.start()
        await pool.stop(grace_period=0.05)

        assert pool._tasks == []

    async def test_start_is_idempotent(self) -> None:
        store = _FakeIndexJobStore()
        pool, _queue = _pool(store, worker_count=1)
        pool.start()
        pool.start()  # must not spawn a second set of tasks
        assert len(pool._tasks) == 1
        await pool.stop(grace_period=1.0)
