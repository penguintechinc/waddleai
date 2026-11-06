"""Observed AGGREGATE generation throughput per locally hosted deployment (spec 4.7, AM6).

Each timed local request records one ``local_generation_samples`` row. A request's own decode
rate (tokens/s of ONE stream) is NOT the deployment's throughput: vLLM batches many concurrent
requests, so pricing off a per-request rate would overstate cost per token by roughly the batch
factor. ``observed_aggregate_tokens_per_s`` therefore measures the sustained AGGREGATE output
rate the deployment delivered (compute_tco treats it as the deployment TOTAL):

* completed requests are bucketed into 5-minute buckets by completion time; a bucket's rate is
  ``sum(output tokens) / 300 s``; the answer is the p90 of the buckets that served traffic
  ("busy" buckets) so idle time does not dilute it;
* anti-manipulation: engine-timed samples are preferred (proxy-timed only when engine-timed ones
  alone do not qualify); each principal's tokens are capped at 20% of a bucket's tokens (a bucket
  dominated by fewer than 5 principals contributes nothing); at least ``MIN_PRINCIPALS`` distinct
  principals and ``MIN_BUSY_BUCKETS`` busy buckets are required, else None (keep the estimate);
* aggregation happens in SQL (``GROUP BY bucket, user, source``) so no row limit can be flooded;
* individually absurd per-request samples are dropped and counted; observed demand is NOT
  capacity, so ``max(estimate, observed)`` applies: an aggregate at or below the supplied
  estimate is ignored (None, the estimate stays), one above 10x the estimate is rejected.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa

__all__ = [
    "MAX_GENERATION_MS",
    "BUCKET_S",
    "MIN_BUSY_BUCKETS",
    "MIN_GENERATION_MS",
    "MIN_OUTPUT_TOKENS",
    "MIN_PRINCIPALS",
    "TIMING_ENGINE",
    "TIMING_PROXY",
    "observed_aggregate_tokens_per_s",
    "prune_generation_samples",
    "record_generation_sample",
]

logger = logging.getLogger(__name__)

TIMING_ENGINE = 1
TIMING_PROXY = 2
MIN_OUTPUT_TOKENS = 8  # a 1-token reply measures latency, not decode speed
MIN_GENERATION_MS = 50  # a single buffered chunk (first == last token) is not a measurement
MAX_GENERATION_MS = 3_600_000
BUCKET_S = 300
MIN_BUSY_BUCKETS = 6  # >= 30 minutes of sustained traffic inside the window
MAX_SHARE_PER_PRINCIPAL = 0.2
MIN_PRINCIPALS = 5  # = 1 / MAX_SHARE_PER_PRINCIPAL: fewer users cannot satisfy the share cap
ABSOLUTE_MAX_TOKENS_PER_S = 100_000.0
ABSOLUTE_MIN_TOKENS_PER_S = 0.01
_ESTIMATE_HIGH = 10.0
MAX_GROUPS = 100_000  # (bucket, user, source) groups; ~288 buckets x principals


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

# (bucket, user_id, timing_source, sane tokens, absurd-rate rows) -- one aggregated SQL group
_Group = tuple[int, int, int, int, int]


def _bucket_expr(db: Any) -> Any:
    """``floor(epoch(created_at) / 300)`` as a portable SQL expression (sqlite and postgres)."""
    t = _SAMPLES.c
    if db.engine.dialect.name == "sqlite":
        epoch = sa.cast(sa.func.strftime("%s", t.created_at), sa.BigInteger)
    else:
        epoch = sa.cast(sa.func.extract("epoch", t.created_at), sa.BigInteger)
    return epoch // BUCKET_S


def _read_groups(db: Any, kind: int, deployment_id: int, since: datetime) -> list[_Group]:
    """Aggregate the window in SQL: ``(bucket, user, source) -> SUM(tokens)``.

    Aggregating in the database means no row ``LIMIT`` exists that one principal's flood could
    use to evict everyone else; the group count is bounded by buckets x principals and the
    result is ordered by bucket (newest first), so if ``MAX_GROUPS`` ever truncates it drops the
    OLDEST bucket (discarded whole, never partially) regardless of who produced the rows.
    Rows whose own per-request rate is absurd are excluded from the sums and counted instead.
    """
    t = _SAMPLES.c
    bucket = _bucket_expr(db).label("bucket")
    user = sa.func.coalesce(t.user_id, -1).label("uid")
    rate_ok = (
        (t.generation_ms > 0)
        & (t.output_tokens * 1000 >= ABSOLUTE_MIN_TOKENS_PER_S * t.generation_ms)
        & (t.output_tokens * 1000 <= ABSOLUTE_MAX_TOKENS_PER_S * t.generation_ms)
    )
    stmt = (
        sa.select(
            bucket,
            user,
            t.timing_source,
            sa.func.sum(sa.case((rate_ok, t.output_tokens), else_=0)),
            sa.func.sum(sa.case((rate_ok, 0), else_=1)),
        )
        .where(
            (t.local_deployment_kind == kind)
            & (t.local_deployment_id == deployment_id)
            & (t.created_at >= since)
        )
        .group_by(bucket, user, t.timing_source)
        .order_by(bucket.desc(), user, t.timing_source)
        .limit(MAX_GROUPS)
    )
    with db.engine.connect() as conn:
        rows = [
            (int(b), int(u), int(s), int(tok or 0), int(bad or 0))
            for b, u, s, tok, bad in conn.execute(stmt)
        ]
    if len(rows) >= MAX_GROUPS:
        oldest = min(r[0] for r in rows)
        rows = [r for r in rows if r[0] != oldest]
    return rows


def _cap_bucket(by_user: dict[int, int]) -> float:
    """Total tokens of one bucket after capping each principal at 20% of the capped total.

    Solves ``c = 0.2 * sum(min(t_u, c))`` by monotone fixed-point iteration from the largest
    contribution. With fewer than 5 principals no cap can satisfy the share rule and the
    iteration collapses to 0 -- such a bucket is not counted as a sustained-throughput witness.
    """
    cap = float(max(by_user.values()))
    for _ in range(200):
        nxt = MAX_SHARE_PER_PRINCIPAL * sum(min(v, cap) for v in by_user.values())
        if abs(nxt - cap) < 1e-9:
            break
        cap = nxt
    if cap < 1e-6:
        return 0.0
    return float(sum(min(v, cap) for v in by_user.values()))


def _aggregate_rate(groups: list[_Group]) -> float | None:
    """p90 over busy 5-minute buckets of capped tokens / 300 s; None when under-evidenced."""
    buckets: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    principals: set[int] = set()
    for bucket, user, _source, tokens, _bad in groups:
        if tokens > 0:
            buckets[bucket][user] += tokens
            principals.add(user)
    if len(principals) < MIN_PRINCIPALS:
        return None
    rates = sorted(
        total / BUCKET_S for total in (_cap_bucket(users) for users in buckets.values()) if total
    )
    if len(rates) < MIN_BUSY_BUCKETS:
        return None
    return float(rates[min(len(rates) - 1, math.ceil(0.9 * len(rates)) - 1)])


def observed_aggregate_tokens_per_s(
    db: Any,
    kind: int,
    deployment_id: int,
    window_hours: int = 24,
    estimate_aggregate_tokens_per_s: float | None = None,
) -> float | None:
    """Sustained AGGREGATE output tokens/s of the whole deployment; None when not trustworthy.

    This is a deployment TOTAL across all concurrent requests (NOT a per-request decode rate),
    the quantity ``compute_tco`` expects. See the module docstring for the bucketing, the p90 of
    busy buckets, and the anti-manipulation rules; ``estimate_aggregate_tokens_per_s`` is the
    deployment-total GPU-class estimate: a result at or below it is ignored (None -- observed can
    only LOWER the price), one above 10x is rejected as implausible, and a missing estimate
    ignores the observation entirely.
    """
    if window_hours <= 0:
        raise ValueError("window_hours must be positive")
    if not hasattr(db, "local_generation_samples") or not hasattr(db, "engine"):
        return None
    since = datetime.utcnow() - timedelta(hours=window_hours)
    groups = _read_groups(db, kind, deployment_id, since)
    _count_dropped("implausible_rate", sum(g[4] for g in groups))
    result: float | None = None
    for source in (TIMING_ENGINE, TIMING_PROXY):
        result = _aggregate_rate([g for g in groups if g[2] == source])
        if result is not None:
            break
    if result is None:
        return None
    est = estimate_aggregate_tokens_per_s
    if est is None or est <= 0:
        # Owner rule: a missing/failed estimate => observed is ignored, never accepted unbounded.
        _count_dropped("estimate_unavailable", 1)
        return None
    if result > est * _ESTIMATE_HIGH:
        _count_dropped("implausible_aggregate", 1)
        return None
    if result <= est:
        return None  # demand is not capacity: observed may only LOWER the price
    return result


def prune_generation_samples(db: Any, older_than_hours: int = 72) -> int:
    """Delete samples older than ``older_than_hours``; returns how many were removed."""
    if older_than_hours <= 0:
        raise ValueError("older_than_hours must be positive")
    if not hasattr(db, "local_generation_samples"):
        return 0
    table = db.local_generation_samples
    cutoff = datetime.utcnow() - timedelta(hours=older_than_hours)
    return int(db(table.created_at < cutoff).delete())
