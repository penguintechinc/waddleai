"""`IndexJobQueue`: bounded in-process hand-off from a gRPC handler to the worker pool.

Deliberately separate from `IndexJobStore` (the durable row) -- this queue
carries the *in-memory* closure that actually does the work
(`QueuedIndexWork.run`), which cannot be serialized into Postgres. A
process restart loses whatever is still queued here (never mid-job
progress, since nothing is dequeued until a worker is ready for it) --
`IndexJobStore.reap_interrupted` is what makes that safe to reason about
(see its docstring): a lost queued-but-never-started job simply never
transitions past `queued`, and an operator re-submits.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.indexing.jobs import JobType
from penguincode_cli.indexing.store import IndexJobOutcome


class IndexQueueFullError(Exception):
    """Raised by `IndexJobQueue.put_nowait` when the bounded queue is at capacity.

    `server/services/knowledge.py`'s `Index`/`IndexCode` handlers catch this
    and abort `RESOURCE_EXHAUSTED` with a retry hint -- the queue never
    grows unbounded, per O10-a's required design.
    """


@dataclass(slots=True, frozen=True)
class QueuedIndexWork:
    """One unit of work handed from a gRPC handler to `IndexWorkerPool`.

    `ctx` is captured at enqueue time (the caller's validated `ScopeContext`)
    -- the worker runs on its own asyncio task, entirely outside the
    request's contextvar scope, so it must never rely on
    `auth.middleware.current_scope_context()`; `run` closes over whatever
    `ctx`/request fields it needs instead.
    """

    job_id: str
    ctx: ScopeContext
    job_type: JobType
    run: Callable[[], Awaitable[IndexJobOutcome]]
    timeout_seconds: float


class IndexJobQueue:
    """Thin wrapper over a bounded `asyncio.Queue[QueuedIndexWork]`.

    Exists (rather than using `asyncio.Queue` directly everywhere) so
    `put_nowait`'s backpressure contract (`IndexQueueFullError`, never an
    unbounded `asyncio.QueueFull` leaking past this layer) and the queue
    depth it exposes to `indexing/metrics.py`'s observable gauge both have
    one home.
    """

    def __init__(self, maxsize: int) -> None:
        if maxsize <= 0:
            raise ValueError(f"maxsize must be positive, got {maxsize!r}")
        self._queue: asyncio.Queue[QueuedIndexWork] = asyncio.Queue(maxsize=maxsize)

        # Registered (weakly) so `indexing/metrics.py`'s `index_queue_depth`
        # observable gauge sums this queue's depth without this module
        # depending on when/whether telemetry is initialized.
        from penguincode_cli.indexing.metrics import register_queue

        register_queue(self)

    def put_nowait(self, work: QueuedIndexWork) -> None:
        """Enqueue `work`, or raise `IndexQueueFullError` if at capacity.

        Never blocks -- a gRPC handler must return promptly either way
        (accepted-and-queued, or rejected `RESOURCE_EXHAUSTED`).
        """
        try:
            self._queue.put_nowait(work)
        except asyncio.QueueFull as exc:
            raise IndexQueueFullError(
                f"index job queue is full (maxsize={self._queue.maxsize}); retry shortly"
            ) from exc

    async def get(self) -> QueuedIndexWork:
        """Block until one item is available (worker-pool side only)."""
        return await self._queue.get()

    def qsize(self) -> int:
        """Current depth -- read by `indexing/metrics.py`'s `index_queue_depth` gauge callback."""
        return self._queue.qsize()
