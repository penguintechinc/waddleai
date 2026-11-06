"""Observed generation throughput per locally hosted deployment (spec §4.7, AM6 daily recompute).

Each timed local request records one ``local_generation_samples`` row. The AUTO price recompute
reads the p50 tokens/s over a rolling window, so the sample set is attacker-reachable input and
is hardened accordingly:

* timing prefers the ENGINE's own decode timer (``TIMING_ENGINE``); wall-clock samples
  (``TIMING_PROXY``) are measured from upstream chunk receipt with consumer stalls excluded, and
  are used only when no engine-timed sample exists;
* implausible rates (absurdly high/low versus the GPU-class estimate) are dropped and counted;
* no single user may contribute more than ~20% of the samples (and at most 50), and at least
  ``MIN_PRINCIPALS`` distinct users are required, so one principal cannot steer the p50.
"""

from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

__all__ = [
    "MAX_GENERATION_MS",
    "MAX_SAMPLES_PER_PRINCIPAL",
    "MIN_GENERATION_MS",
    "MIN_OUTPUT_TOKENS",
    "MIN_PRINCIPALS",
    "TIMING_ENGINE",
    "TIMING_PROXY",
    "observed_tokens_per_s",
    "prune_generation_samples",
    "record_generation_sample",
]

logger = logging.getLogger(__name__)

TIMING_ENGINE = 1
TIMING_PROXY = 2
MIN_OUTPUT_TOKENS = 8  # a 1-token reply measures latency, not decode speed
MIN_GENERATION_MS = 50  # a single buffered chunk (first == last token) is not a measurement
MAX_GENERATION_MS = 3_600_000
MAX_SAMPLES_PER_PRINCIPAL = 50
MAX_SHARE_PER_PRINCIPAL = 0.2
MIN_PRINCIPALS = 3  # fewer distinct users and one of them could set the median
ABSOLUTE_MAX_TOKENS_PER_S = 100_000.0
ABSOLUTE_MIN_TOKENS_PER_S = 0.01
_ESTIMATE_HIGH, _ESTIMATE_LOW = 10.0, 0.01
_MAX_SAMPLES_READ = 20_000  # bound the read; the newest samples win


def _valid_int(value: Any) -> bool:
    """A non-bool int (engine/connector supplied, hence untrusted)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _count_dropped(reason: str, n: int) -> None:
    """Count ignored samples; telemetry failure never breaks the recompute."""
    if n <= 0:
        return
    try:
        from shared.observability.vllm_metrics import throughput_sample_dropped_counter

        throughput_sample_dropped_counter().add(n, {"reason": reason})
    except Exception:  # noqa: BLE001
        logger.debug("dropped-sample counter emission failed", exc_info=True)


def record_generation_sample(
    db: Any,
    kind: int,
    deployment_id: int,
    output_tokens: Any,
    generation_ms: Any,
    timing_source: int = TIMING_PROXY,
    user_id: int | None = None,
) -> bool:
    """Insert one sample when it is a plausible decode measurement; True when recorded."""
    if not (_valid_int(output_tokens) and _valid_int(generation_ms)):
        return False
    if timing_source not in (TIMING_ENGINE, TIMING_PROXY):
        return False
    if output_tokens < MIN_OUTPUT_TOKENS:
        return False
    if not (MIN_GENERATION_MS <= generation_ms <= MAX_GENERATION_MS):
        return False
    if not hasattr(db, "local_generation_samples"):
        return False
    db.local_generation_samples.insert(
        local_deployment_kind=kind,
        local_deployment_id=deployment_id,
        output_tokens=output_tokens,
        generation_ms=generation_ms,
        timing_source=timing_source,
        user_id=user_id if _valid_int(user_id) else None,
        created_at=datetime.utcnow(),
    )
    return True


def _cap_per_principal(samples: list[tuple[int | None, float]]) -> list[float] | None:
    """Rates after capping each principal at <= 20% of the result (and <= 50 samples).

    ``samples`` are newest-first ``(user_id, rate)``. Returns None when fewer than
    ``MIN_PRINCIPALS`` distinct users contributed. Samples with no user are one anonymous
    principal. The cap is the largest c <= 50 with c <= 20% of ``sum(min(n_user, c))``.
    """
    by_user: dict[int | None, list[float]] = defaultdict(list)
    for user, rate in samples:
        by_user[user].append(rate)
    if len(by_user) < MIN_PRINCIPALS:
        return None
    cap = 1
    for candidate in range(MAX_SAMPLES_PER_PRINCIPAL, 0, -1):
        total = sum(min(len(v), candidate) for v in by_user.values())
        if candidate <= MAX_SHARE_PER_PRINCIPAL * total:
            cap = candidate
            break
    kept = [rate for rates in by_user.values() for rate in rates[:cap]]
    _count_dropped("principal_cap", len(samples) - len(kept))
    return kept


def observed_tokens_per_s(
    db: Any,
    kind: int,
    deployment_id: int,
    window_hours: int = 24,
    estimate_tokens_per_s: float | None = None,
) -> float | None:
    """p50 output tokens/s over the last ``window_hours``; ``None`` when not trustworthy.

    Engine-timed samples are used when any exist (proxy-timed ones only otherwise). Rates more
    than 10x or less than 0.01x ``estimate_tokens_per_s`` (the GPU-class estimate; absolute
    bounds apply when it is None) are dropped and counted, each user is capped, and fewer than
    ``MIN_PRINCIPALS`` distinct users yields None so the reconciler keeps the estimate.
    """
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
    low, high = ABSOLUTE_MIN_TOKENS_PER_S, ABSOLUTE_MAX_TOKENS_PER_S
    if estimate_tokens_per_s is not None and estimate_tokens_per_s > 0:
        low = max(low, estimate_tokens_per_s * _ESTIMATE_LOW)
        high = min(high, estimate_tokens_per_s * _ESTIMATE_HIGH)
    engine: list[tuple[int | None, float]] = []
    proxy: list[tuple[int | None, float]] = []
    absurd = 0
    for row in rows:
        if not row.output_tokens or not row.generation_ms or row.generation_ms <= 0:
            continue
        rate = row.output_tokens * 1000.0 / row.generation_ms
        if not (low <= rate <= high):
            absurd += 1
            continue
        bucket = engine if getattr(row, "timing_source", TIMING_PROXY) == TIMING_ENGINE else proxy
        bucket.append((getattr(row, "user_id", None), rate))
    _count_dropped("implausible_rate", absurd)
    kept = _cap_per_principal(engine) if engine else None
    if kept is None:
        kept = _cap_per_principal(proxy)
    return float(statistics.median(kept)) if kept else None


def prune_generation_samples(db: Any, older_than_hours: int = 72) -> int:
    """Delete samples older than ``older_than_hours``; returns how many were removed."""
    if older_than_hours <= 0:
        raise ValueError("older_than_hours must be positive")
    if not hasattr(db, "local_generation_samples"):
        return 0
    table = db.local_generation_samples
    cutoff = datetime.utcnow() - timedelta(hours=older_than_hours)
    return int(db(table.created_at < cutoff).delete())
