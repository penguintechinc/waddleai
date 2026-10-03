"""Async indexing job queue (O10-a): load-leveling for `Index`/`IndexCode`.

`KnowledgeServiceImpl.Index`/`.IndexCode` previously ran the whole
doc-indexing/code-graph job (sequential Ollama embedding per chunk, or a
blocking `graphs.code.index_code` tree-sitter pass) inline in a unary gRPC
handler, on the shared `ThreadPoolExecutor(10)` every other RPC (Chat,
Health) also runs on -- a few large `Index` calls starved that pool,
timing out `Health.Check` and triggering a restart loop in production.

This package moves that work off the gRPC executor entirely: handlers
enqueue a durable job row (`jobs.py`/`store.py`) and a matching in-memory
work item (`queue.py`), and a small bounded worker pool (`worker.py`)
drains it on its own asyncio tasks. See `server/services/knowledge.py`'s
`Index`/`IndexCode`/`IndexStatus` handlers for the integration point, and
`docs/penguincode/KNOWLEDGE_PLATFORM.md`'s "Async indexing" section for the
full design (kill-switch flag, env vars, restart-recovery policy).
"""

from penguincode_cli.indexing.jobs import IndexJob, JobState, JobType
from penguincode_cli.indexing.queue import IndexJobQueue, IndexQueueFullError, QueuedIndexWork
from penguincode_cli.indexing.store import IndexJobOutcome, IndexJobStore, IndexJobStoreLike
from penguincode_cli.indexing.worker import IndexWorkerPool

__all__ = [
    "IndexJob",
    "JobState",
    "JobType",
    "IndexJobQueue",
    "IndexQueueFullError",
    "QueuedIndexWork",
    "IndexJobOutcome",
    "IndexJobStore",
    "IndexJobStoreLike",
    "IndexWorkerPool",
]
