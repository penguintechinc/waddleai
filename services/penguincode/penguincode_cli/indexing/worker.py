"""`IndexWorkerPool`: bounded background workers draining `IndexJobQueue` (O10-a).

Runs each job's actual work (the `QueuedIndexWork.run` closure built by
`server/services/knowledge.py`'s handlers) on its own set of dedicated
asyncio tasks, entirely off the gRPC `ThreadPoolExecutor` -- the whole
point of this package, see `indexing/__init__.py`'s module docstring.
Every job is time-bounded (`timeout_seconds`, env-configurable default) so
one hung embedding call can never park a worker forever.
"""

from __future__ import annotations

import asyncio
import logging
import time

from penguincode_cli.indexing import metrics
from penguincode_cli.indexing.jobs import JobState
from penguincode_cli.indexing.queue import IndexJobQueue, QueuedIndexWork
from penguincode_cli.indexing.store import IndexJobStoreLike
from penguincode_cli.observability.otel import store_span

logger = logging.getLogger(__name__)

#: How long `get()` is allowed to block per loop iteration before re-checking
#: the stop flag -- keeps `stop()` responsive without a busy-loop.
_POLL_INTERVAL_SECONDS = 1.0


class IndexWorkerPool:
    """`worker_count` asyncio tasks, each looping `queue.get()` -> run -> record.

    `start()`/`stop()` give the pool an explicit lifecycle (`server/main.py`
    calls these around the gRPC server's own start/stop) so no job is ever
    silently dropped on shutdown -- `stop()` cancels in-flight tasks only
    after they've had `grace_period` to finish the job they're already
    running.
    """

    def __init__(
        self,
        queue: IndexJobQueue,
        store: IndexJobStoreLike,
        *,
        worker_count: int,
        default_timeout_seconds: float,
    ) -> None:
        if worker_count <= 0:
            raise ValueError(f"worker_count must be positive, got {worker_count!r}")
        self._queue = queue
        self._store = store
        self._worker_count = worker_count
        self._default_timeout_seconds = default_timeout_seconds
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False

    def start(self) -> None:
        """Spawn `worker_count` background tasks. Idempotent -- a second call is a no-op."""
        if self._tasks:
            return
        metrics.ensure_queue_depth_gauge_registered()
        self._stopping = False
        for index in range(self._worker_count):
            self._tasks.append(asyncio.create_task(self._worker_loop(index)))
        logger.info("index worker pool started: %d worker(s)", self._worker_count)

    async def stop(self, grace_period: float = 5.0) -> None:
        """Stop accepting new work and cancel every worker task after `grace_period`."""
        self._stopping = True
        if not self._tasks:
            return
        _, pending = await asyncio.wait(self._tasks, timeout=grace_period)
        for task in pending:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("index worker pool stopped")

    async def _worker_loop(self, worker_index: int) -> None:
        while not self._stopping:
            try:
                work = await asyncio.wait_for(self._queue.get(), timeout=_POLL_INTERVAL_SECONDS)
            except TimeoutError:
                continue
            await self._process(work)

    async def _process(self, work: QueuedIndexWork) -> None:
        """Run one job to completion, updating the store and emitting telemetry.

        Never raises -- a job's own exception (including a timeout) is
        caught, recorded as `FAILED` with that exception's message, and
        logged; the worker loop above must keep running regardless of how
        any one job turns out.
        """
        logger.info("index job %s (%s) running", work.job_id, work.job_type.value)
        self._store.mark_running(work.job_id)
        start = time.perf_counter()
        timeout = work.timeout_seconds or self._default_timeout_seconds

        with store_span("indexing.worker.process", job_type=work.job_type.value):
            try:
                outcome = await asyncio.wait_for(work.run(), timeout=timeout)
            except Exception as exc:  # noqa: BLE001 -- any job failure must not crash the worker
                duration = time.perf_counter() - start
                error = f"timed out after {timeout}s" if isinstance(exc, TimeoutError) else str(exc)
                self._store.mark_failed(work.job_id, error)
                metrics.record_job_finished(work.job_type.value, JobState.FAILED.value, duration)
                logger.warning(
                    "index job %s (%s) failed: %s", work.job_id, work.job_type.value, error
                )
                return

        duration = time.perf_counter() - start
        self._store.mark_succeeded(work.job_id, outcome)
        metrics.record_job_finished(work.job_type.value, JobState.SUCCEEDED.value, duration)
        if work.job_type.value == "index_docs":
            metrics.record_chunk_embed_duration(work.job_type.value, duration)
        logger.info(
            "index job %s (%s) succeeded: chunks_done=%d chunks_total=%d",
            work.job_id,
            work.job_type.value,
            outcome.chunks_done,
            outcome.chunks_total,
        )

    async def run_one(self, timeout: float = 5.0) -> bool:
        """Test helper: dequeue and process exactly one job, deterministically.

        Used instead of the background loop by tests that need to assert a
        job reached a terminal state without racing a real worker task.
        Returns `False` if no job was queued within `timeout`.
        """
        try:
            work = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return False
        await self._process(work)
        return True
