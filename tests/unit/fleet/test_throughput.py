"""Observed throughput samples: validation, exact p50, window, pruning, per-deployment scope."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from shared.database.models import get_db
from shared.fleet.throughput import (
    observed_tokens_per_s,
    prune_generation_samples,
    record_generation_sample,
)


@pytest.fixture
def db(tmp_path: Path) -> Any:
    """A throwaway sqlite penguin_dal DB with the proxy schema."""
    return get_db(db_uri=f"sqlite:///{tmp_path / 'tp.db'}")


def _sample(
    db: Any, tokens: int, ms: int, *, kind: int = 1, dep: int = 7, age_h: float = 0
) -> None:
    """Insert a sample directly, aged ``age_h`` hours."""
    db.local_generation_samples.insert(
        local_deployment_kind=kind,
        local_deployment_id=dep,
        output_tokens=tokens,
        generation_ms=ms,
        created_at=datetime.utcnow() - timedelta(hours=age_h),
    )


def test_p50_is_the_exact_median_of_per_request_rates(db) -> None:
    """tokens/s per request, then the median: 100, 50, 200 -> 100 (not the mean 116.7)."""
    _sample(db, 100, 1000)  # 100 tok/s
    _sample(db, 100, 2000)  # 50 tok/s
    _sample(db, 200, 1000)  # 200 tok/s
    assert observed_tokens_per_s(db, 1, 7) == 100.0
    _sample(db, 400, 1000)  # 400 tok/s -> even count: median of 100 and 200
    assert observed_tokens_per_s(db, 1, 7) == 150.0


def test_none_when_there_are_no_samples(db) -> None:
    """No samples -> None (the reconciler keeps the estimate), never 0."""
    assert observed_tokens_per_s(db, 1, 7) is None


def test_window_and_deployment_and_kind_scope(db) -> None:
    """Old samples, other deployments and other kinds never contribute."""
    _sample(db, 100, 1000)  # 100 tok/s, in window
    _sample(db, 999, 1000, age_h=30)  # outside the 24h window
    _sample(db, 999, 1000, dep=8)  # other deployment
    _sample(db, 999, 1000, kind=2)  # other kind
    assert observed_tokens_per_s(db, 1, 7) == 100.0
    # the 30h-old 999 tok/s sample enters a 48h window: median of {100, 999} = 549.5
    assert observed_tokens_per_s(db, 1, 7, window_hours=48) == 549.5


def test_rejects_non_positive_window(db) -> None:
    """A zero/negative window is a caller bug, not an empty result."""
    with pytest.raises(ValueError, match="window_hours"):
        observed_tokens_per_s(db, 1, 7, window_hours=0)


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
    assert observed_tokens_per_s(db, 1, 7) is None


def test_plausible_sample_is_recorded(db) -> None:
    """A real decode measurement is stored and read back."""
    assert record_generation_sample(db, 1, 7, 64, 800) is True
    assert observed_tokens_per_s(db, 1, 7) == 80.0


def test_prune_removes_only_old_samples(db) -> None:
    """Pruning deletes samples older than the retention and reports how many."""
    _sample(db, 100, 1000, age_h=100)
    _sample(db, 100, 1000, age_h=80)
    _sample(db, 100, 1000, age_h=1)
    assert prune_generation_samples(db, older_than_hours=72) == 2
    assert observed_tokens_per_s(db, 1, 7, window_hours=500) == 100.0
    with pytest.raises(ValueError, match="older_than_hours"):
        prune_generation_samples(db, older_than_hours=0)


def test_db_without_samples_table_is_inert() -> None:
    """A DB handle without the migration-030 table never raises: nothing recorded, no p50."""
    bare = object()
    assert record_generation_sample(bare, 1, 7, 64, 800) is False
    assert observed_tokens_per_s(bare, 1, 7) is None
    assert prune_generation_samples(bare) == 0
