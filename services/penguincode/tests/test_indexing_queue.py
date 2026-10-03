"""Tests for `penguincode_cli.indexing.queue` (O10-a async index-job queue).

Pure in-memory unit tests -- `IndexJobQueue` never touches Postgres, so
these run with no live DB and no fixtures beyond plain asyncio.

# regression: penguincode-index-job-queue (O10-a -- load leveling for Index/IndexCode)
"""

from __future__ import annotations

import asyncio

import pytest

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.indexing.jobs import JobType
from penguincode_cli.indexing.queue import IndexJobQueue, IndexQueueFullError, QueuedIndexWork
from penguincode_cli.indexing.store import IndexJobOutcome


def _ctx() -> ScopeContext:
    return ScopeContext(tenant_id="tenant-a", org_id=None, team_ids=(), user_id="user-a", scopes=())


async def _noop_run() -> IndexJobOutcome:
    return IndexJobOutcome()


def _work(job_id: str = "job-1") -> QueuedIndexWork:
    return QueuedIndexWork(
        job_id=job_id,
        ctx=_ctx(),
        job_type=JobType.INDEX_DOCS,
        run=_noop_run,
        timeout_seconds=30.0,
    )


class TestConstruction:
    def test_rejects_zero_maxsize(self) -> None:
        with pytest.raises(ValueError, match="maxsize"):
            IndexJobQueue(maxsize=0)

    def test_rejects_negative_maxsize(self) -> None:
        with pytest.raises(ValueError, match="maxsize"):
            IndexJobQueue(maxsize=-1)


class TestPutAndGet:
    async def test_put_then_get_round_trips_the_same_work_item(self) -> None:
        queue = IndexJobQueue(maxsize=4)
        work = _work()

        queue.put_nowait(work)

        assert await queue.get() is work

    def test_qsize_reflects_pending_depth(self) -> None:
        queue = IndexJobQueue(maxsize=4)
        assert queue.qsize() == 0

        queue.put_nowait(_work("a"))
        queue.put_nowait(_work("b"))

        assert queue.qsize() == 2

    async def test_get_blocks_until_an_item_is_put(self) -> None:
        queue = IndexJobQueue(maxsize=4)

        async def _delayed_put() -> None:
            await asyncio.sleep(0.05)
            queue.put_nowait(_work())

        asyncio.create_task(_delayed_put())
        work = await asyncio.wait_for(queue.get(), timeout=2.0)
        assert work.job_id == "job-1"


class TestBackpressure:
    def test_put_nowait_raises_when_full(self) -> None:
        queue = IndexJobQueue(maxsize=1)
        queue.put_nowait(_work("a"))

        with pytest.raises(IndexQueueFullError, match="full"):
            queue.put_nowait(_work("b"))

    def test_full_queue_never_grows_past_maxsize(self) -> None:
        queue = IndexJobQueue(maxsize=2)
        queue.put_nowait(_work("a"))
        queue.put_nowait(_work("b"))

        for _ in range(5):
            with pytest.raises(IndexQueueFullError):
                queue.put_nowait(_work("overflow"))

        assert queue.qsize() == 2
