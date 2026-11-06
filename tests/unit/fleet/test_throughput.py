"""Observed throughput: validation, exact p50, engine preference, principal cap, plausibility."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from shared.database.models import get_db
from shared.fleet.throughput import (
    MIN_PRINCIPALS,
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


def _sample(
    db: Any,
    tokens: int,
    ms: int,
    *,
    user: int = 1,
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


_NEXT_USER = [1000]


def _spread(db: Any, rates: list[int], *, source: int = TIMING_PROXY, **kw: Any) -> None:
    """One 1-second sample per rate (tok/s), each from a fresh user (so the cap never bites)."""
    for rate in rates:
        _NEXT_USER[0] += 1
        _sample(db, rate, 1000, user=_NEXT_USER[0], source=source, **kw)


def test_p50_is_the_exact_median_of_per_request_rates(db) -> None:
    """tokens/s per request, then the median: 100, 50, 200 -> 100 (not the mean 116.7)."""
    _spread(db, [100, 50, 200])
    assert observed_tokens_per_s(db, 1, 7) == 100.0
    _spread(db, [400])  # even count: median of 100 and 200
    assert observed_tokens_per_s(db, 1, 7) == 150.0


def test_none_without_samples_or_with_too_few_principals(db) -> None:
    """No samples, or fewer than MIN_PRINCIPALS distinct users -> None (keep the estimate)."""
    assert observed_tokens_per_s(db, 1, 7) is None
    for _ in range(30):
        _sample(db, 100, 1000, user=1)
        _sample(db, 100, 1000, user=2)
    assert MIN_PRINCIPALS == 3
    assert observed_tokens_per_s(db, 1, 7) is None
    _sample(db, 100, 1000, user=3)
    assert observed_tokens_per_s(db, 1, 7) == 100.0


def test_window_deployment_and_kind_scope(db) -> None:
    """Old samples, other deployments and other kinds never contribute."""
    _spread(db, [100, 100, 100])
    _spread(db, [999, 999, 999], age_h=30)  # outside the 24h window
    _spread(db, [999, 999, 999], dep=8)
    _spread(db, [999, 999, 999], kind=2)
    assert observed_tokens_per_s(db, 1, 7) == 100.0
    assert observed_tokens_per_s(db, 1, 7, window_hours=48) == 549.5


def test_rejects_non_positive_window(db) -> None:
    """A zero/negative window is a caller bug, not an empty result."""
    with pytest.raises(ValueError, match="window_hours"):
        observed_tokens_per_s(db, 1, 7, window_hours=0)


def test_engine_timed_samples_win_over_proxy_timed(db) -> None:
    """When any engine-timed sample exists, proxy-timed ones are ignored entirely."""
    _spread(db, [10, 10, 10, 10], source=TIMING_PROXY)
    _spread(db, [100, 100, 100], source=TIMING_ENGINE)
    assert observed_tokens_per_s(db, 1, 7) == 100.0


def test_proxy_timed_used_only_when_no_engine_samples(db) -> None:
    """With no engine timing at all, the proxy-measured samples drive the p50."""
    _spread(db, [40, 40, 40], source=TIMING_PROXY)
    assert observed_tokens_per_s(db, 1, 7) == 40.0


def test_one_key_flooding_cannot_move_the_p50_beyond_its_cap(db) -> None:
    """1000 low-rate samples from one user vs 9 honest users x 20 samples at 100 tok/s."""
    for _ in range(1000):
        _sample(db, 10, 1000, user=666)  # 10 tok/s, the attacker
    for user in range(1, 10):
        for _ in range(20):
            _sample(db, 100, 1000, user=user)  # 100 tok/s, honest
    # uncapped, the attacker (1000 of 1180) would pin the median at 10 tok/s
    assert observed_tokens_per_s(db, 1, 7) == 100.0


def test_implausible_rates_dropped_and_counted(db, metric_reader) -> None:
    """Rates >10x or <0.01x the GPU-class estimate are dropped and counted, not averaged in."""
    _spread(db, [100, 100, 100, 100])
    _spread(db, [5000, 5000])  # > 10 x 100
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
