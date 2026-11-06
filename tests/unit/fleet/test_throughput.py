"""Observed throughput: validation, exact p50, thresholds, per-user cap inside the read."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from shared.database.models import get_db
from shared.fleet import throughput
from shared.fleet.throughput import (
    MIN_PRINCIPALS,
    MIN_SAMPLES,
    TIMING_ENGINE,
    TIMING_PROXY,
    observed_tokens_per_s,
    prune_generation_samples,
    record_generation_sample,
)
from tests.unit.fixtures.otel import points


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
) -> None:
    """Insert a sample directly, aged ``age_h`` hours."""
    db.local_generation_samples.insert(
        local_deployment_kind=kind,
        local_deployment_id=dep,
        output_tokens=tokens,
        generation_ms=ms,
        timing_source=source,
        user_id=user,
        created_at=datetime.utcnow() - timedelta(hours=age_h),
    )


def _crowd(db: Any, rate: int, *, users: int = 5, per_user: int = 4, **kw: Any) -> None:
    """``users`` fresh users x ``per_user`` samples at ``rate`` tok/s (>= the thresholds)."""
    for _ in range(users):
        _NEXT_USER[0] += 1
        for _i in range(per_user):
            _sample(db, rate, 1000, user=_NEXT_USER[0], **kw)


def test_thresholds_match_the_share_cap() -> None:
    """MIN_PRINCIPALS == 1 / max share, so the 20% cap is always satisfiable at the minimum."""
    assert MIN_PRINCIPALS >= 5 and MIN_SAMPLES >= MIN_PRINCIPALS


def test_p50_is_the_exact_median_of_per_request_rates(db) -> None:
    """tokens/s per request, then the median over the capped set."""
    _crowd(db, 100)  # 20 samples at 100
    assert observed_tokens_per_s(db, 1, 7) == 100.0
    _crowd(db, 50)  # 20 at 50 -> even split: median of 50 and 100
    assert observed_tokens_per_s(db, 1, 7) == 75.0


def test_none_without_samples_principals_or_enough_samples(db) -> None:
    """No samples, < MIN_PRINCIPALS users, or < MIN_SAMPLES samples -> None (keep estimate)."""
    assert observed_tokens_per_s(db, 1, 7) is None
    _crowd(db, 100, users=MIN_PRINCIPALS - 1, per_user=50)  # many samples, too few users
    assert observed_tokens_per_s(db, 1, 7) is None
    _crowd(db, 100, users=MIN_PRINCIPALS, per_user=1)  # 9 users now but let a fresh dep test below
    other = 99
    for _ in range(MIN_PRINCIPALS):
        _NEXT_USER[0] += 1
        _sample(db, 100, 1000, user=_NEXT_USER[0], dep=other)
    assert observed_tokens_per_s(db, 1, other) is None  # 5 users, 5 samples: below MIN_SAMPLES
    _crowd(db, 100, users=MIN_PRINCIPALS, per_user=4, dep=other)
    assert observed_tokens_per_s(db, 1, other) == 100.0


def test_window_deployment_and_kind_scope(db) -> None:
    """Old samples, other deployments and other kinds never contribute."""
    _crowd(db, 100)
    _crowd(db, 999, age_h=30)  # outside the 24h window
    _crowd(db, 999, dep=8)
    _crowd(db, 999, kind=2)
    assert observed_tokens_per_s(db, 1, 7) == 100.0
    assert observed_tokens_per_s(db, 1, 7, window_hours=48) == 549.5


def test_rejects_non_positive_window(db) -> None:
    """A zero/negative window is a caller bug, not an empty result."""
    with pytest.raises(ValueError, match="window_hours"):
        observed_tokens_per_s(db, 1, 7, window_hours=0)


def test_engine_samples_override_proxy_only_when_they_meet_the_thresholds(db) -> None:
    """Enough engine-timed samples answer alone; too few engine ones fall back to proxy."""
    _crowd(db, 10, source=TIMING_PROXY)
    _crowd(db, 100, source=TIMING_ENGINE)
    assert observed_tokens_per_s(db, 1, 7) == 100.0  # engine qualifies -> wins
    for _ in range(MIN_PRINCIPALS - 1):  # a second deployment: engine below the user threshold
        _NEXT_USER[0] += 1
        for _i in range(10):
            _sample(db, 100, 1000, user=_NEXT_USER[0], source=TIMING_ENGINE, dep=8)
    _crowd(db, 40, source=TIMING_PROXY, dep=8)
    assert observed_tokens_per_s(db, 1, 8) == 40.0  # 4 engine users < 5 -> proxy used


def test_one_key_flooding_cannot_move_the_p50_beyond_its_cap(db) -> None:
    """1000 low-rate samples from one user vs 9 honest users x 20 samples at 100 tok/s."""
    for _ in range(1000):
        _sample(db, 10, 1000, user=666)  # 10 tok/s, the attacker
    for user in range(1, 10):
        for _ in range(20):
            _sample(db, 100, 1000, user=user)  # 100 tok/s, honest
    assert observed_tokens_per_s(db, 1, 7) == 100.0


def test_flood_cannot_fill_the_global_read_window(db, monkeypatch) -> None:
    """The per-user cap is applied inside the query: a newer flood cannot evict older users.

    The global read limit is shrunk to 100 rows; honest samples are OLDER than 500 attacker
    rows. A read that took the newest 100 rows first would see only the attacker.
    """
    monkeypatch.setattr(throughput, "MAX_SAMPLES_READ", 100)
    _crowd(db, 100, users=9, per_user=5)  # honest, inserted first (older)
    for _ in range(500):
        _sample(db, 10, 1000, user=666)  # attacker floods afterwards
    assert observed_tokens_per_s(db, 1, 7) == 100.0


def test_anonymous_samples_are_a_single_capped_principal(db) -> None:
    """Samples without a user are one principal: 500 anonymous rows count as one user."""
    for _ in range(500):
        _sample(db, 10, 1000, user=None)
    _crowd(db, 100, users=9, per_user=5)
    assert observed_tokens_per_s(db, 1, 7) == 100.0


def test_implausible_rates_dropped_and_counted(db, metric_reader) -> None:
    """Rates >10x or <0.01x the GPU-class estimate are dropped and counted, not averaged in."""
    _crowd(db, 100)
    _crowd(db, 5000, users=2, per_user=1)  # > 10 x 100
    _sample(db, 8, 3_600_000, user=900)  # ~0.002 tok/s < 0.01 x estimate
    assert observed_tokens_per_s(db, 1, 7, estimate_tokens_per_s=100.0) == 100.0
    dropped = points(metric_reader, "waddleai_local_throughput_samples_dropped_total")
    assert sum(p.value for p in dropped) == 3
    assert {p.attributes["reason"] for p in dropped} == {"implausible_rate"}


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
    assert observed_tokens_per_s(bare, 1, 7) is None
    assert prune_generation_samples(bare) == 0
