"""Observed AGGREGATE throughput: bucketing, busy-bucket p90, caps, thresholds, drops."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from shared.database.models import get_db
from shared.fleet import throughput
from shared.fleet.throughput import (
    BUCKET_S,
    MIN_BUSY_BUCKETS,
    MIN_PRINCIPALS,
    TIMING_ENGINE,
    TIMING_PROXY,
    observed_aggregate_tokens_per_s,
    prune_generation_samples,
    record_generation_sample,
)

pytestmark = pytest.mark.usefixtures("restore_obs_meter_cache")


@pytest.fixture
def db(tmp_path: Path) -> Any:
    """A throwaway sqlite penguin_dal DB with the proxy schema."""
    return get_db(db_uri=f"sqlite:///{tmp_path / 'tp.db'}")


_NEXT_USER = [1000]


def _sample(
    db: Any,
    tokens: int,
    ms: int,
    *,
    user: int | None = 1,
    source: int = TIMING_PROXY,
    kind: int = 1,
    dep: int = 7,
    age_h: float = 0,
    at: datetime | None = None,
) -> None:
    """Insert a sample directly, aged ``age_h`` hours (or completed at ``at``)."""
    db.local_generation_samples.insert(
        local_deployment_kind=kind,
        local_deployment_id=dep,
        output_tokens=tokens,
        generation_ms=ms,
        timing_source=source,
        user_id=user,
        created_at=at or datetime.utcnow() - timedelta(hours=age_h),
    )


def _bucket_time(k: int, i: int = 0, per: int = 1) -> datetime:
    """Completion time inside the k-th bucket back from now; i of per spreads rows within it."""
    base = (int(time.time()) // BUCKET_S - 1 - k) * BUCKET_S
    return datetime.utcfromtimestamp(base + (i + 0.5) * BUCKET_S / per)


def _traffic(
    db: Any,
    *,
    users: int,
    buckets: int,
    per_user: int = 30,
    tokens: int = 200,
    ms: int = 10_000,
    first_user: int = 1,
    start: int = 0,
    kind: int = 1,
    dep: int = 7,
    source: int = TIMING_PROXY,
) -> None:
    """``users`` streams, each completing ``per_user`` requests of ``tokens`` per bucket."""
    rows = [
        _row(kind, dep, tokens, ms, source, u, _bucket_time(k, i, per_user))
        for k in range(start, start + buckets)
        for u in range(first_user, first_user + users)
        for i in range(per_user)
    ]
    _bulk(db, rows)


def _row(
    kind: int, dep: int, tokens: int, ms: int, source: int, user: int, at: datetime
) -> dict[str, Any]:
    """One sample row for a bulk insert."""
    return {
        "local_deployment_kind": kind,
        "local_deployment_id": dep,
        "output_tokens": tokens,
        "generation_ms": ms,
        "timing_source": source,
        "user_id": user,
        "created_at": at,
    }


def _bulk(db: Any, rows: list[dict[str, Any]]) -> None:
    """One executemany insert (thousands of single autocommits are far too slow on sqlite)."""
    with db.engine.begin() as conn:
        conn.execute(throughput._SAMPLES.insert(), rows)


def test_thresholds() -> None:
    """The principal floor matches the 20% share cap; busy-bucket floor is 6 (30 minutes)."""
    assert MIN_PRINCIPALS >= 5 and MIN_BUSY_BUCKETS == 6


def test_ten_concurrent_streams_aggregate_not_per_request(db) -> None:
    """10 streams x 20 tok/s each, continuously, are ~200 tok/s aggregate -- not 20."""
    _traffic(db, users=10, buckets=6)  # 200 tokens / 10 s = 20 tok/s per request
    # 10 users x 30 requests x 200 tokens = 60_000 tokens per 300 s bucket
    assert observed_aggregate_tokens_per_s(db, 1, 7) == pytest.approx(200.0)


def test_idle_buckets_do_not_dilute(db) -> None:
    """Gaps between busy buckets (idle time) leave the sustained busy rate unchanged."""
    _traffic(db, users=10, buckets=3, start=0)
    _traffic(db, users=10, buckets=3, start=10)  # buckets 3..9 idle
    assert observed_aggregate_tokens_per_s(db, 1, 7) == pytest.approx(200.0)


def test_p90_of_busy_buckets(db) -> None:
    """The answer is the p90 of busy buckets: one outlier bucket does not set the price."""
    for k in range(10):  # 10 busy buckets: nine at 100 tok/s, one at 200 tok/s
        _traffic(db, users=10, buckets=1, per_user=15 if k else 30, start=k)
    assert observed_aggregate_tokens_per_s(db, 1, 7) == pytest.approx(100.0)
    _traffic(db, users=10, buckets=1, per_user=30, start=10)  # 2 of 11 at 200 -> p90 = 200
    assert observed_aggregate_tokens_per_s(db, 1, 7) == pytest.approx(200.0)


def test_single_principal_flooding_a_bucket_is_capped(db) -> None:
    """A principal with 90% of a bucket's tokens counts for at most 20% of the capped total."""
    rows = [
        _row(1, 7, 200, 10_000, TIMING_PROXY, u, _bucket_time(k, i, n))
        for k in range(6)
        for u in range(1, 11)  # nine ordinary users at 10 requests, user 10 floods
        for n in [10 if u < 10 else 900]
        for i in range(n)
    ]
    _bulk(db, rows)
    # ordinary users hold 2000 tokens each (under the cap); the flooder is cut to the c with
    # c = 0.2 * (18000 + c) = 4500 tokens
    got = observed_aggregate_tokens_per_s(db, 1, 7)
    assert got is not None
    assert got == pytest.approx((9 * 2000 + 4500) / BUCKET_S)
    assert got < (9 * 2000 + 900 * 200) / BUCKET_S / 2


def test_bucket_dominated_by_fewer_than_five_principals_does_not_count(db) -> None:
    """Four principals cannot satisfy the 20% cap: nothing is measured -> None."""
    _traffic(db, users=4, buckets=8)
    assert observed_aggregate_tokens_per_s(db, 1, 7) is None


def test_none_without_enough_principals_or_busy_buckets(db) -> None:
    """No samples, too few busy buckets, or too few principals overall -> None."""
    assert observed_aggregate_tokens_per_s(db, 1, 7) is None
    _traffic(db, users=10, buckets=MIN_BUSY_BUCKETS - 1)
    assert observed_aggregate_tokens_per_s(db, 1, 7) is None
    _traffic(db, users=10, buckets=1, start=MIN_BUSY_BUCKETS - 1)
    assert observed_aggregate_tokens_per_s(db, 1, 7) == pytest.approx(200.0)
    with pytest.raises(ValueError, match="window_hours"):
        observed_aggregate_tokens_per_s(db, 1, 7, window_hours=0)


def test_window_deployment_and_kind_scope(db) -> None:
    """Other deployments, other kinds and samples outside the window are ignored."""
    _traffic(db, users=10, buckets=6, dep=8)
    _traffic(db, users=10, buckets=6, kind=2)
    assert observed_aggregate_tokens_per_s(db, 1, 7) is None
    _traffic(db, users=10, buckets=6)
    assert observed_aggregate_tokens_per_s(db, 1, 7, window_hours=1) == pytest.approx(200.0)
    for _ in range(3):
        _sample(db, 200, 10_000, user=99, age_h=30)
    assert observed_aggregate_tokens_per_s(db, 1, 7, window_hours=1) == pytest.approx(200.0)


def test_engine_samples_win_only_when_they_qualify(db) -> None:
    """Engine-timed samples answer alone if they qualify; else proxy-timed ones are used."""
    _traffic(db, users=10, buckets=6, source=TIMING_ENGINE)
    _traffic(db, users=10, buckets=6, per_user=15, source=TIMING_PROXY, first_user=100)
    assert observed_aggregate_tokens_per_s(db, 1, 7) == pytest.approx(200.0)  # engine wins
    _traffic(db, users=10, buckets=6, source=TIMING_ENGINE, dep=8, per_user=3)
    _traffic(db, users=4, buckets=2, source=TIMING_ENGINE, dep=8, first_user=50)
    _traffic(db, users=10, buckets=6, per_user=15, source=TIMING_PROXY, dep=8, first_user=100)
    assert observed_aggregate_tokens_per_s(db, 1, 8) == pytest.approx(100.0)


def test_absurd_samples_and_aggregate_are_dropped_and_counted(db, monkeypatch) -> None:
    """Absurd per-request rates are dropped; an aggregate beyond 10x the estimate is refused."""
    counted: list[tuple[str, int]] = []
    monkeypatch.setattr(
        throughput, "_count_dropped", lambda reason, n: counted.append((reason, n)) if n else None
    )
    _traffic(db, users=10, buckets=6)  # 200 tok/s aggregate
    _bulk(db, [_row(1, 7, 8, 3_600_000, TIMING_PROXY, 900, _bucket_time(0))])  # ~0.002 tok/s
    assert observed_aggregate_tokens_per_s(db, 1, 7, estimate_aggregate_tokens_per_s=100.0) == (
        pytest.approx(200.0)
    )
    assert counted == [("implausible_rate", 1)]
    counted.clear()
    assert observed_aggregate_tokens_per_s(db, 1, 7, estimate_aggregate_tokens_per_s=10.0) is None
    assert counted[-1] == ("implausible_aggregate", 1)


def test_observed_never_lowers_throughput_below_the_estimate(db) -> None:
    """Demand is not capacity: an aggregate at or under the estimate is ignored (None).

    throughput_for_pricing = max(estimate, observed): a 5-principal trickle (or any
    sub-estimate load) can never raise the price above the estimate-based price, while a
    genuinely saturated deployment above the estimate is reported (and so lowers the price).
    """
    _traffic(db, users=5, buckets=6, per_user=6)  # trickle: 5 x 6 x 200 / 300 s = 20 tok/s
    assert observed_aggregate_tokens_per_s(db, 1, 7, estimate_aggregate_tokens_per_s=100.0) is None
    assert observed_aggregate_tokens_per_s(db, 1, 7, estimate_aggregate_tokens_per_s=20.0) is None
    assert observed_aggregate_tokens_per_s(db, 1, 7, estimate_aggregate_tokens_per_s=10.0) == (
        pytest.approx(20.0)
    )
    _traffic(db, users=10, buckets=6, dep=8)  # saturated: 200 tok/s
    assert observed_aggregate_tokens_per_s(db, 1, 8, estimate_aggregate_tokens_per_s=100.0) == (
        pytest.approx(200.0)
    )


def test_flood_cannot_evict_other_principals_from_the_aggregate(db) -> None:
    """One user's 100k rows cannot evict 5 other users' contributions (SQL-side aggregation)."""
    rows = [
        _row(1, 7, 200, 10_000, TIMING_PROXY, u, _bucket_time(k, i, 30))
        for k in range(6)
        for u in range(1, 6)  # five ordinary users, 30 requests per bucket
        for i in range(30)
    ]
    rows += [
        _row(1, 7, 1, 1000, TIMING_PROXY, 99, _bucket_time(i % 6, i, 100_000))
        for i in range(100_000)  # the flooder: 100k newest rows (below MIN_OUTPUT_TOKENS-ish)
    ]
    _bulk(db, rows)
    # ordinary users: 6000 tokens/bucket each; the flooder's ~16.7k tokens are cut to the c with
    # c = 0.2 * (30000 + c) = 7500
    got = observed_aggregate_tokens_per_s(db, 1, 7)
    assert got is not None and got == pytest.approx((5 * 6000 + 7500) / BUCKET_S, rel=0.02)


def test_dropped_counter_emits_the_named_metric(metric_reader) -> None:
    """The drop counter is waddleai_local_throughput_samples_dropped_total{reason}."""
    from shared.observability.vllm_metrics import throughput_sample_dropped_counter
    from tests.unit.fixtures.otel import points

    throughput_sample_dropped_counter().add(3, {"reason": "implausible_rate"})
    (pt,) = points(metric_reader, "waddleai_local_throughput_samples_dropped_total")
    assert pt.value == 3 and pt.attributes["reason"] == "implausible_rate"


@pytest.mark.parametrize(
    ("tokens", "ms"),
    [
        (7, 1000),
        (100, 49),
        (100, 3_600_001),
        (True, 1000),
        (100, True),
        ("100", 1000),
        (100, None),
        (-5, 1000),
        (100, -1000),
        (100.5, 1000),
    ],
)
def test_implausible_samples_are_not_recorded(db, tokens: Any, ms: Any) -> None:
    """Too few tokens, too short/long, bool/str/float/negative values are all refused."""
    assert record_generation_sample(db, 1, 7, tokens, ms) is False
    assert list(db(db.local_generation_samples.id > 0).select()) == []


def test_plausible_sample_recorded_with_source_and_user(db) -> None:
    """A real decode measurement is stored with its timing source and user."""
    assert record_generation_sample(db, 1, 7, 64, 800, TIMING_ENGINE, 42) is True
    (row,) = list(db(db.local_generation_samples.id > 0).select())
    assert (row.timing_source, row.user_id, row.output_tokens) == (TIMING_ENGINE, 42, 64)
    assert record_generation_sample(db, 1, 7, 64, 800, 9, 42) is False  # unknown source


def test_prune_removes_only_old_samples(db) -> None:
    """Pruning deletes samples older than the retention and reports how many."""
    _sample(db, 100, 1000, age_h=100)
    _sample(db, 100, 1000, age_h=80)
    _sample(db, 100, 1000, age_h=1)
    assert prune_generation_samples(db, older_than_hours=72) == 2
    assert len(list(db(db.local_generation_samples.id > 0).select())) == 1
    with pytest.raises(ValueError, match="older_than_hours"):
        prune_generation_samples(db, older_than_hours=0)


def test_db_without_samples_table_is_inert() -> None:
    """A DB handle without the migration-030 table never raises: nothing recorded, no p50."""
    bare = object()
    assert record_generation_sample(bare, 1, 7, 64, 800) is False
    assert observed_aggregate_tokens_per_s(bare, 1, 7) is None
    assert prune_generation_samples(bare) == 0
