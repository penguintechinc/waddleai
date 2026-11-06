"""Observed generation throughput per locally hosted deployment (spec §4.7, AM6 daily recompute).

Each timed local request records one ``local_generation_samples`` row (output tokens and
wall-clock generation ms: first token to last for streams, total for non-stream). The AUTO price
recompute reads the p50 tokens/s over a rolling window. Samples that cannot be a real decode
measurement (too few tokens, too short, estimated counts) are never recorded.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta
from typing import Any

__all__ = [
    "MAX_GENERATION_MS",
    "MIN_GENERATION_MS",
    "MIN_OUTPUT_TOKENS",
    "observed_tokens_per_s",
    "prune_generation_samples",
    "record_generation_sample",
]

logger = logging.getLogger(__name__)

MIN_OUTPUT_TOKENS = 8  # a 1-token reply measures latency, not decode speed
MIN_GENERATION_MS = 50  # a single buffered chunk (first == last token) is not a measurement
MAX_GENERATION_MS = 3_600_000
_MAX_SAMPLES_READ = 20_000  # bound the read; the newest samples win


def _valid_int(value: Any) -> bool:
    """A non-bool int (engine/connector supplied, hence untrusted)."""
    return isinstance(value, int) and not isinstance(value, bool)


def record_generation_sample(
    db: Any, kind: int, deployment_id: int, output_tokens: Any, generation_ms: Any
) -> bool:
    """Insert one sample when it is a plausible decode measurement; True when recorded."""
    if not (_valid_int(output_tokens) and _valid_int(generation_ms)):
        return False
    if output_tokens < MIN_OUTPUT_TOKENS or not (
        MIN_GENERATION_MS <= generation_ms <= MAX_GENERATION_MS
    ):
        return False
    if not hasattr(db, "local_generation_samples"):
        return False
    db.local_generation_samples.insert(
        local_deployment_kind=kind,
        local_deployment_id=deployment_id,
        output_tokens=output_tokens,
        generation_ms=generation_ms,
        created_at=datetime.utcnow(),
    )
    return True


def observed_tokens_per_s(
    db: Any, kind: int, deployment_id: int, window_hours: int = 24
) -> float | None:
    """p50 output tokens/s over the last ``window_hours``; ``None`` when there are no samples."""
    if window_hours <= 0:
        raise ValueError("window_hours must be positive")
    if not hasattr(db, "local_generation_samples"):
        return None
    table = db.local_generation_samples
    since = datetime.utcnow() - timedelta(hours=window_hours)
    query = (
        (table.local_deployment_kind == kind)
        & (table.local_deployment_id == deployment_id)
        & (table.created_at >= since)
    )
    rows = db(query).select(orderby=~table.id, limitby=(0, _MAX_SAMPLES_READ))
    rates = [
        r.output_tokens * 1000.0 / r.generation_ms
        for r in rows
        if r.output_tokens and r.generation_ms and r.generation_ms > 0
    ]
    return float(statistics.median(rates)) if rates else None


def prune_generation_samples(db: Any, older_than_hours: int = 72) -> int:
    """Delete samples older than ``older_than_hours``; returns how many were removed."""
    if older_than_hours <= 0:
        raise ValueError("older_than_hours must be positive")
    if not hasattr(db, "local_generation_samples"):
        return 0
    table = db.local_generation_samples
    cutoff = datetime.utcnow() - timedelta(hours=older_than_hours)
    return int(db(table.created_at < cutoff).delete())
