"""Observed generation throughput per locally hosted deployment (spec §4.7, AM6 daily recompute).

Each timed local request records one ``local_generation_samples`` row. The AUTO price recompute
reads the p50 tokens/s over a rolling window, so the sample set is attacker-reachable input and
is hardened accordingly:

* timing prefers the ENGINE's own decode timer (``TIMING_ENGINE``); wall-clock samples
  (``TIMING_PROXY``) are measured from upstream chunk receipt with consumer stalls excluded, and
  are used only when no engine-timed sample exists;
* implausible rates (absurdly high/low versus the GPU-class estimate) are dropped and counted;
* the per-user cap is applied INSIDE the read (a window function keeps each user's newest 50),
  so a flood from one key cannot fill the read window and crowd out everyone else; no user may
  then exceed 20% of the result; and at least ``MIN_PRINCIPALS`` (= 1 / max share) distinct users
  and ``MIN_SAMPLES`` samples are required, else the answer is None (keep the estimate).
"""

from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa

__all__ = [
    "MAX_GENERATION_MS",
    "MAX_SAMPLES_PER_PRINCIPAL",
    "MIN_GENERATION_MS",
    "MIN_OUTPUT_TOKENS",
    "MIN_PRINCIPALS",
    "MIN_SAMPLES",
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
MIN_PRINCIPALS = 5  # = 1 / MAX_SHARE_PER_PRINCIPAL: fewer users cannot satisfy the share cap
MIN_SAMPLES = 20  # after the per-user cap; fewer is noise, not a throughput measurement
ABSOLUTE_MAX_TOKENS_PER_S = 100_000.0
ABSOLUTE_MIN_TOKENS_PER_S = 0.01
_ESTIMATE_HIGH, _ESTIMATE_LOW = 10.0, 0.01
MAX_SAMPLES_READ = 20_000  # global bound AFTER the per-user cap inside the query


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
    """Rates after capping each principal at <= 20% of the result; None if not trustworthy.

    ``samples`` are newest-first ``(user_id, rate)`` already limited to 50 per user by the read.
    Samples with no user are one anonymous principal. The cap is the largest c <= 50 such that
    c <= 20% of ``sum(min(n_user, c))``; if no c satisfies that, or fewer than
    ``MIN_PRINCIPALS`` users / ``MIN_SAMPLES`` samples remain, the result is None (there is no
    weaker fallback that would let one principal dominate).
    """
    by_user: dict[int | None, list[float]] = defaultdict(list)
    for user, rate in samples:
        by_user[user].append(rate)
    if len(by_user) < MIN_PRINCIPALS:
        return None
    for cap in range(MAX_SAMPLES_PER_PRINCIPAL, 0, -1):
        total = sum(min(len(v), cap) for v in by_user.values())
        if cap <= MAX_SHARE_PER_PRINCIPAL * total:
            kept = [rate for rates in by_user.values() for rate in rates[:cap]]
            if len(kept) < MIN_SAMPLES:
                return None
            _count_dropped("principal_cap", len(samples) - len(kept))
            return kept
    return None


_SAMPLES = sa.table(
    "local_generation_samples",
    sa.column("id", sa.BigInteger),
    sa.column("local_deployment_kind", sa.Integer),
    sa.column("local_deployment_id", sa.BigInteger),
    sa.column("output_tokens", sa.Integer),
    sa.column("generation_ms", sa.Integer),
    sa.column("timing_source", sa.SmallInteger),
    sa.column("user_id", sa.BigInteger),
    sa.column("created_at", sa.DateTime),
)


def _read_capped(
    db: Any, kind: int, deployment_id: int, since: datetime
) -> list[tuple[int | None, float, int]]:
    """``(user_id, tokens/s, timing_source)`` newest-first, <= 50 newest rows PER USER.

    The per-user limit is a window function inside the query, so one user's flood can neither
    fill the read nor evict another user's samples. Implausible rows are filtered by the caller.
    """
    t = _SAMPLES.c
    rank = (
        sa.func.row_number()
        .over(partition_by=sa.func.coalesce(t.user_id, -1), order_by=t.id.desc())
        .label("rn")
    )
    inner = (
        sa.select(t.id, t.output_tokens, t.generation_ms, t.timing_source, t.user_id, rank)
        .where(
            (t.local_deployment_kind == kind)
            & (t.local_deployment_id == deployment_id)
            & (t.created_at >= since)
        )
        .subquery()
    )
    stmt = (
        sa.select(
            inner.c.output_tokens, inner.c.generation_ms, inner.c.timing_source, inner.c.user_id
        )
        .where(inner.c.rn <= MAX_SAMPLES_PER_PRINCIPAL)
        .order_by(inner.c.id.desc())
        .limit(MAX_SAMPLES_READ)
    )
    with db.engine.connect() as conn:
        rows = conn.execute(stmt).all()
    return [
        (user, tokens * 1000.0 / ms, int(source))
        for tokens, ms, source, user in rows
        if tokens and ms and ms > 0
    ]


def observed_tokens_per_s(
    db: Any,
    kind: int,
    deployment_id: int,
    window_hours: int = 24,
    estimate_tokens_per_s: float | None = None,
) -> float | None:
    """p50 output tokens/s over the last ``window_hours``; ``None`` when not trustworthy.

    Each user's newest 50 samples are read (cap applied in SQL). Rates more than 10x or less
    than 0.01x ``estimate_tokens_per_s`` (the GPU-class estimate; absolute bounds apply when it
    is None) are dropped and counted. Engine-timed samples answer only if THEY alone meet the
    min-principal/min-sample thresholds; otherwise proxy-timed ones are tried under the same
    thresholds; if neither qualifies the result is None and the reconciler keeps the estimate.
    """
    if window_hours <= 0:
        raise ValueError("window_hours must be positive")
    if not hasattr(db, "local_generation_samples") or not hasattr(db, "engine"):
        return None
    since = datetime.utcnow() - timedelta(hours=window_hours)
    low, high = ABSOLUTE_MIN_TOKENS_PER_S, ABSOLUTE_MAX_TOKENS_PER_S
    if estimate_tokens_per_s is not None and estimate_tokens_per_s > 0:
        low = max(low, estimate_tokens_per_s * _ESTIMATE_LOW)
        high = min(high, estimate_tokens_per_s * _ESTIMATE_HIGH)
    engine: list[tuple[int | None, float]] = []
    proxy: list[tuple[int | None, float]] = []
    absurd = 0
    for user, rate, source in _read_capped(db, kind, deployment_id, since):
        if not (low <= rate <= high):
            absurd += 1
            continue
        (engine if source == TIMING_ENGINE else proxy).append((user, rate))
    _count_dropped("implausible_rate", absurd)
    kept = _cap_per_principal(engine)
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
