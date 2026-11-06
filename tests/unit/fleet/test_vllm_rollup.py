"""Savings/chargeback rollups over token_usage (real sqlite penguin_dal, exact decimals)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from shared.database.models import get_db
from shared.fleet.vllm_ray.rollup import chargeback, savings

TODAY = date.today()

pytestmark = pytest.mark.usefixtures("restore_obs_meter_cache")


@pytest.fixture
def db(tmp_path: Path) -> Any:
    """A throwaway sqlite penguin_dal DB with the proxy schema."""
    return get_db(db_uri=f"sqlite:///{tmp_path / 'rollup.db'}")


def _usage(
    db: Any,
    *,
    org: int = 1,
    user: int = 1,
    kind: int | None = 1,
    dep: int | None = 10,
    tin: int = 1_000_000,
    tout: int = 1_000_000,
    price_in: str | None = "2.0",
    price_out: str | None = "4.0",
    day: date = TODAY,
    requests: int = 1,
) -> None:
    """Insert one token_usage rollup row."""
    db.token_usage.insert(
        api_key_id=user,
        user_id=user,
        organization_id=org,
        date=day,
        tokens_input_total=tin,
        tokens_output_total=tout,
        request_count=requests,
        local_deployment_kind=kind,
        local_deployment_id=dep,
        unit_price_input=price_in,
        unit_price_output=price_out,
    )


def _member(db: Any, user: int, team: int, org: int = 1) -> None:
    """Put ``user`` on ``team``."""
    db.team_members.insert(team_id=team, user_id=user, organization_id=org, team_role="member")


def _deployment(db: Any, table: str, comparison: str | None) -> int:
    """Insert a deployment row with a comparison model; returns its id."""
    return int(
        getattr(db, table).insert(
            public_id="p",
            price_input_per_1m="2.0",
            price_output_per_1m="4.0",
            comparison_model=comparison,
        )
    )


def _rate(
    db: Any, provider: str, model: str, rate_in: float, rate_out: float, base: float = 0.001
) -> None:
    """Insert a commercial conversion-rate row."""
    db.token_conversion_rates.insert(
        provider=provider,
        model=model,
        input_rate=rate_in,
        output_rate=rate_out,
        base_cost_per_waddleai_token=base,
        enabled=True,
    )


def test_chargeback_groups_team_and_deployment_exact_decimals(db) -> None:
    """2 teams x 2 deployments; every line is hand-computed from the stored snapshots."""
    _member(db, 1, 5)
    _member(db, 2, 6)
    # team5/dep10: 1M in @2.0 + 1M out @4.0 = 6.0 ; team5/dep11: 500k in @1.5 + 0 out = 0.75
    _usage(db, user=1, dep=10)
    _usage(db, user=1, dep=11, tin=500_000, tout=0, price_in="1.5", price_out="3.0")
    # team6/dep10: 2M in @2.0 + 250k out @4.0 = 4.0 + 1.0 = 5.0 ; team6/dep11: 1M out @3.0 = 3.0
    _usage(db, user=2, dep=10, tin=2_000_000, tout=250_000)
    _usage(db, user=2, dep=11, tin=0, tout=1_000_000, price_in="1.5", price_out="3.0")
    result = chargeback(db, 1, TODAY - timedelta(days=1))
    cost = {(r.team_id, r.local_deployment_id): r.virtual_cost for r in result.rows}
    assert cost == {
        (5, 10): Decimal("6.0"),
        (5, 11): Decimal("0.75"),
        (6, 10): Decimal("5.0"),
        (6, 11): Decimal("3.0"),
    }
    assert result.total_virtual_cost == Decimal("14.75")
    assert (result.rows_examined, result.rows_missing_price) == (4, 0)


def test_chargeback_filters_team_deployment_org_and_period(db) -> None:
    """team/deployment/org/period filters each narrow the report."""
    _member(db, 1, 5)
    _member(db, 2, 6)
    _usage(db, user=1, dep=10)
    _usage(db, user=2, dep=11)
    _usage(db, user=1, dep=10, org=2)  # other org
    _usage(db, user=1, dep=10, day=TODAY - timedelta(days=40))  # outside the period
    since = TODAY - timedelta(days=30)
    assert [(r.team_id, r.local_deployment_id) for r in chargeback(db, 1, since).rows] == [
        (5, 10),
        (6, 11),
    ]
    assert [r.team_id for r in chargeback(db, 1, since, team_id=6).rows] == [6]
    assert [
        r.local_deployment_id for r in chargeback(db, 1, since, deployment_id=10, kind=1).rows
    ] == [10]
    assert chargeback(db, 3, since).rows == []


def test_chargeback_multi_team_user_lowest_team_once_teamless_none(db) -> None:
    """A user on two teams is billed to the lowest team id, once; a teamless user to None."""
    _member(db, 1, 9)
    _member(db, 1, 4)
    _usage(db, user=1)
    _usage(db, user=2, dep=11)
    result = chargeback(db, 1, TODAY - timedelta(days=1))
    assert [(r.team_id, r.local_deployment_id) for r in result.rows] == [(4, 10), (None, 11)]
    assert sum(r.request_count for r in result.rows) == 2


def test_chargeback_null_price_row_excluded_and_counted(db) -> None:
    """A row without a price snapshot is never costed as free: excluded, counted."""
    _usage(db)
    _usage(db, dep=12, price_in=None, price_out=None)
    result = chargeback(db, 1, TODAY - timedelta(days=1))
    assert [r.local_deployment_id for r in result.rows] == [10]
    assert (result.rows_examined, result.rows_missing_price) == (2, 1)


def test_chargeback_empty_period_zero_rows(db) -> None:
    """No usage -> no rows, zero total, zero denominator reported honestly."""
    result = chargeback(db, 1, TODAY)
    assert (result.rows, result.total_virtual_cost, result.rows_examined) == ([], Decimal(0), 0)


def test_chargeback_deployment_without_kind_rejected(db) -> None:
    """Deployment ids are per kind: deployment_id alone is ambiguous (AM10)."""
    with pytest.raises(ValueError, match="requires kind"):
        chargeback(db, 1, TODAY, deployment_id=10)


def test_savings_commercial_minus_virtual_total_and_per_deployment(db) -> None:
    """Savings = tokens at the comparison model's rate minus tokens at the snapshot price."""
    dep_a = _deployment(db, "vllm_deployments", "openai/gpt-4o")
    dep_b = _deployment(db, "vllm_deployments", "openai/gpt-4o")
    # gpt-4o: 100 in-tokens/credit, 50 out-tokens/credit, $0.001/credit
    _rate(db, "openai", "gpt-4o", 100.0, 50.0)
    _usage(db, dep=dep_a, tin=1_000_000, tout=1_000_000)  # virtual 2.0 + 4.0 = 6.0
    _usage(db, dep=dep_b, tin=500_000, tout=0)  # virtual 1.0
    result = savings(db, 1, TODAY - timedelta(days=1))
    # commercial A = (1e6/100 + 1e6/50) * 0.001 = 30 ; B = 5000*0.001 = 5
    by = {d.deployment_id: d for d in result.by_deployment}
    assert by[dep_a].commercial_equivalent_cost == Decimal("30.000")
    assert by[dep_a].savings == Decimal("24.000")
    assert by[dep_b].savings == Decimal("4.000")
    assert (result.virtual_cost, result.commercial_equivalent_cost) == (
        Decimal("7.0"),
        Decimal("35.000"),
    )
    assert result.savings == Decimal("28.000")
    assert (result.tokens_input, result.tokens_output) == (1_500_000, 1_000_000)


def test_savings_negative_and_missing_commercial_price_reported(db) -> None:
    """Local dearer than commercial -> negative savings; unknown comparison -> reported missing."""
    dear = _deployment(db, "vllm_deployments", "openai/cheap")
    unknown = _deployment(db, "vllm_deployments", "nobody/none")
    _rate(db, "openai", "cheap", 1_000_000.0, 1_000_000.0)  # ~free commercial
    _usage(db, dep=dear)
    _usage(db, dep=unknown)
    result = savings(db, 1, TODAY - timedelta(days=1))
    by = {d.deployment_id: d for d in result.by_deployment}
    dear_savings = by[dear].savings
    assert dear_savings is not None and dear_savings < 0
    assert by[unknown].savings is None and by[unknown].commercial_equivalent_cost is None
    assert result.missing_commercial_models == ["nobody/none"]
    assert result.savings == by[dear].savings  # unknown deployment is not counted as savings


def test_rollup_covers_ollama_and_llamacpp_rows_and_kind_filter(db) -> None:
    """AM10: all three kinds roll up; ``kind`` narrows to one."""
    _usage(db, kind=1, dep=1)
    _usage(db, kind=2, dep=1)
    _usage(db, kind=3, dep=1)
    _usage(db, kind=None, dep=None, price_in=None, price_out=None)  # commercial: never included
    since = TODAY - timedelta(days=1)
    assert sorted(r.local_deployment_kind for r in chargeback(db, 1, since).rows) == [1, 2, 3]
    assert [r.local_deployment_kind for r in chargeback(db, 1, since, kind=2).rows] == [2]
    assert sorted({d.kind for d in savings(db, 1, since).by_deployment}) == [1, 2, 3]
    assert [d.kind for d in savings(db, 1, since, kind=3).by_deployment] == [3]
