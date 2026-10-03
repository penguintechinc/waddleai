"""OTel instruments for the async index-job queue (O10-a, spec item 6).

Separate from `observability/otel.py`'s `timed_store_operation` family
(whose `OpKind` is a closed `vector_query`/`graph_query`/`extraction` set
that queue/worker events don't belong in) -- this module creates its own
three instruments against the same process-wide meter
(`observability.otel.get_meter`), so they share one OTLP pipeline without
widening that unrelated closed set.
"""

from __future__ import annotations

import weakref
from typing import TYPE_CHECKING, Final

from opentelemetry import metrics
from opentelemetry.util.types import AttributeValue

from penguincode_cli.observability.otel import get_meter

if TYPE_CHECKING:
    from penguincode_cli.indexing.queue import IndexJobQueue

QUEUE_DEPTH_GAUGE_NAME: Final = "penguincode.index_queue.depth"
JOBS_COUNTER_NAME: Final = "penguincode.index_jobs.total"
JOB_DURATION_HISTOGRAM_NAME: Final = "penguincode.index_job.duration"
CHUNK_EMBED_DURATION_HISTOGRAM_NAME: Final = "penguincode.index_chunk_embed.duration"

#: Every live `IndexJobQueue` whose depth should be reported -- a
#: `WeakSet` so a queue that falls out of scope (test teardown) stops
#: contributing without needing an explicit unregister call.
_registered_queues: weakref.WeakSet[IndexJobQueue] = weakref.WeakSet()

_queue_depth_gauge: metrics.ObservableGauge | None = None
_jobs_counter: metrics.Counter | None = None
_job_duration_histogram: metrics.Histogram | None = None
_chunk_embed_duration_histogram: metrics.Histogram | None = None


def register_queue(queue: IndexJobQueue) -> None:
    """Add `queue` to the set the depth gauge callback sums over."""
    _registered_queues.add(queue)


def _queue_depth_callback(
    options: metrics.CallbackOptions,
) -> list[metrics.Observation]:
    total = sum(q.qsize() for q in _registered_queues)
    return [metrics.Observation(total)]


def _queue_depth_gauge_instrument() -> metrics.ObservableGauge:
    global _queue_depth_gauge
    if _queue_depth_gauge is None:
        _queue_depth_gauge = get_meter().create_observable_gauge(
            QUEUE_DEPTH_GAUGE_NAME,
            callbacks=[_queue_depth_callback],
            unit="1",
            description="Current depth of the async index-job queue (summed over every "
            "live queue in this process)",
        )
    return _queue_depth_gauge


def _jobs_counter_instrument() -> metrics.Counter:
    global _jobs_counter
    if _jobs_counter is None:
        _jobs_counter = get_meter().create_counter(
            JOBS_COUNTER_NAME,
            unit="1",
            description="Index jobs processed, by job_type and state",
        )
    return _jobs_counter


def _job_duration_histogram_instrument() -> metrics.Histogram:
    global _job_duration_histogram
    if _job_duration_histogram is None:
        _job_duration_histogram = get_meter().create_histogram(
            JOB_DURATION_HISTOGRAM_NAME,
            unit="s",
            description="Wall-clock duration of one index job, by job_type",
        )
    return _job_duration_histogram


def _chunk_embed_duration_histogram_instrument() -> metrics.Histogram:
    global _chunk_embed_duration_histogram
    if _chunk_embed_duration_histogram is None:
        _chunk_embed_duration_histogram = get_meter().create_histogram(
            CHUNK_EMBED_DURATION_HISTOGRAM_NAME,
            unit="s",
            description="Wall-clock duration of one job's chunk-embedding phase "
            "(the whole Ollama-embedding portion of an index_docs job)",
        )
    return _chunk_embed_duration_histogram


def ensure_queue_depth_gauge_registered() -> None:
    """Force the observable gauge's lazy construction -- call once at worker-pool startup
    so the gauge exists (and therefore reports, even at depth 0) before the first job
    ever enqueues, matching the "metrics ≥1 data point" smoke-test assertion.
    """
    _queue_depth_gauge_instrument()


def record_job_enqueued(job_type: str) -> None:
    """Increment the jobs counter for a newly queued job."""
    _jobs_counter_instrument().add(1, attributes={"job_type": job_type, "state": "queued"})


def record_job_rejected(job_type: str) -> None:
    """Increment the jobs counter for a job rejected (`RESOURCE_EXHAUSTED`, queue full)."""
    _jobs_counter_instrument().add(1, attributes={"job_type": job_type, "state": "rejected"})


def record_job_finished(
    job_type: str, state: str, duration_seconds: float, **attrs: AttributeValue
) -> None:
    """Increment the jobs counter and record the duration histogram for a finished job.

    `state` is `"succeeded"` or `"failed"` -- a bounded label, same
    discipline `observability.otel.record_store_event` enforces for its
    own `outcome` label.
    """
    _jobs_counter_instrument().add(1, attributes={"job_type": job_type, "state": state})
    _job_duration_histogram_instrument().record(
        duration_seconds, attributes={"job_type": job_type, "state": state, **attrs}
    )


def record_chunk_embed_duration(job_type: str, duration_seconds: float) -> None:
    """Record one job's chunk-embedding-phase duration."""
    _chunk_embed_duration_histogram_instrument().record(
        duration_seconds, attributes={"job_type": job_type}
    )


def reset_for_testing() -> None:
    """Drop cached instruments so a test can install its own meter provider.

    Mirrors `observability.otel.reset_for_testing` -- call both together,
    in that order doesn't matter since this module only reads
    `get_meter()` lazily.
    """
    global _queue_depth_gauge, _jobs_counter, _job_duration_histogram
    global _chunk_embed_duration_histogram
    _queue_depth_gauge = None
    _jobs_counter = None
    _job_duration_histogram = None
    _chunk_embed_duration_histogram = None
    _registered_queues.clear()
