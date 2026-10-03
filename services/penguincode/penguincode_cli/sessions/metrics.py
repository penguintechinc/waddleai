"""OTel instruments for the shared chat-session store (O4-a High fix).

Two instruments, named exactly per the audit remediation spec:

* ``active_sessions`` -- a synchronous Gauge, set by the sweeper
  (`store.SessionSweeper`) to the store's current non-expired row count.
* ``session_store_duration_seconds`` -- a Histogram (unit seconds) of one
  `SessionStore` operation's latency, labeled ``op`` only (bounded,
  low-cardinality: ``create``/``get``/``update_state``/``delete``/
  ``sweep_expired``/``count_active`` -- never a session id or any other
  unbounded value).

Kept separate from `observability.otel`'s ``penguincode.store.duration``
(whose ``op_kind`` is a closed three-value set that does not include
sessions) rather than widening that enum -- these two names are the ones
the finding specifies, and the two histograms serve different consumers
(vector/graph/extraction vs. the session store).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

from penguincode_cli.observability.otel import get_meter

if TYPE_CHECKING:
    # `Gauge` (the synchronous instrument `create_gauge` returns) is still
    # exposed under its pre-stabilization private name in
    # opentelemetry-api 1.44.0 -- aliased here so the rest of this module
    # reads as if it were the public type it will become.
    from opentelemetry.metrics import Histogram
    from opentelemetry.metrics import _Gauge as Gauge

ACTIVE_SESSIONS_GAUGE_NAME: Final = "active_sessions"
SESSION_STORE_DURATION_HISTOGRAM_NAME: Final = "session_store_duration_seconds"

_active_sessions_gauge: Gauge | None = None
_session_store_duration_histogram: Histogram | None = None


def _gauge() -> Gauge:
    global _active_sessions_gauge
    if _active_sessions_gauge is None:
        _active_sessions_gauge = get_meter().create_gauge(
            ACTIVE_SESSIONS_GAUGE_NAME,
            unit="1",
            description="Current count of non-expired penguincode chat sessions",
        )
    return _active_sessions_gauge


def _histogram() -> Histogram:
    global _session_store_duration_histogram
    if _session_store_duration_histogram is None:
        _session_store_duration_histogram = get_meter().create_histogram(
            SESSION_STORE_DURATION_HISTOGRAM_NAME,
            unit="s",
            description="Latency of one chat-session store operation, by op",
        )
    return _session_store_duration_histogram


def record_active_sessions(count: int) -> None:
    """Set the `active_sessions` gauge to `count` (the sweeper's own view, unlabeled)."""
    _gauge().set(count)


@contextmanager
def timed_session_store_operation(op: str) -> Iterator[None]:
    """Time one `SessionStore` operation into `session_store_duration_seconds`.

    `op` MUST be a bounded method-name label (``create``, ``get``,
    ``update_state``, ``delete``, ``sweep_expired``, ``count_active``) --
    never a session id, tenant id, or other unbounded value. Records the
    duration whether or not the block raises; exceptions propagate
    unchanged.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        _histogram().record(time.perf_counter() - start, attributes={"op": op})


def reset_for_testing() -> None:
    """Drop cached instruments so a test can install its own meter provider."""
    global _active_sessions_gauge, _session_store_duration_histogram
    _active_sessions_gauge = None
    _session_store_duration_histogram = None


__all__ = [
    "ACTIVE_SESSIONS_GAUGE_NAME",
    "SESSION_STORE_DURATION_HISTOGRAM_NAME",
    "record_active_sessions",
    "reset_for_testing",
    "timed_session_store_operation",
]
