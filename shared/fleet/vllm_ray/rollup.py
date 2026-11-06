"""Savings and chargeback rollups over ``token_usage`` for every locally hosted kind (AM10).

Cost per row comes from the unit-price SNAPSHOT stored on that row (never today's price), so a
price change never rewrites history. A row with no snapshot price is excluded and counted in
``rows_missing_price`` -- it is never costed as free. Money is ``Decimal`` throughout.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from shared.quota.virtual_price import LOCAL_DEPLOYMENT_TABLES, snapshot_price

__all__ = [
    "ChargebackResult",
    "ChargebackRow",
    "DeploymentSavings",
    "SavingsResult",
    "chargeback",
    "savings",
]

logger = logging.getLogger(__name__)

_MILLION = Decimal(1_000_000)
_ZERO = Decimal(0)


@dataclass(slots=True)
class DeploymentSavings:
    """Savings for one deployment; ``savings`` is None when its commercial price is unknown."""

    kind: int
    deployment_id: int
    comparison_model: str | None
    tokens_input: int
    tokens_output: int
    virtual_cost: Decimal
    commercial_equivalent_cost: Decimal | None
    savings: Decimal | None


@dataclass(slots=True)
class SavingsResult:
    """Totals plus per-deployment breakdown; savings may be negative."""

    tokens_input: int
    tokens_output: int
    virtual_cost: Decimal
    commercial_equivalent_cost: Decimal
    savings: Decimal
    by_deployment: list[DeploymentSavings]
    rows_examined: int
    rows_missing_price: int
    missing_commercial_models: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ChargebackRow:
    """One (team, deployment) line of the chargeback report."""

    team_id: int | None
    local_deployment_kind: int
    local_deployment_id: int
    tokens_input: int
    tokens_output: int
    request_count: int
    virtual_cost: Decimal


@dataclass(slots=True)
class ChargebackResult:
    """Chargeback lines plus the denominator (rows examined / skipped for no price)."""

    rows: list[ChargebackRow]
    total_virtual_cost: Decimal
    rows_examined: int
    rows_missing_price: int


def _usage_rows(db: Any, org_id: int, since: date, kind: int | None) -> list[Any]:
    """This org's local-kind ``token_usage`` rows on/after ``since`` (optionally one kind)."""
    table = db.token_usage
    query = (
        (table.organization_id == org_id)
        & (table.local_deployment_kind != None)  # noqa: E711
        & (table.date >= since)
    )
    if kind is not None:
        query = query & (table.local_deployment_kind == kind)
    return list(db(query).select())


def _row_cost(row: Any) -> Decimal | None:
    """Virtual cost of one row from its stored price snapshot; None when unpriced."""
    price_in = snapshot_price(getattr(row, "unit_price_input", None))
    price_out = snapshot_price(getattr(row, "unit_price_output", None))
    if price_in is None or price_out is None:
        return None
    tokens_in = max(0, int(row.tokens_input_total or 0))
    tokens_out = max(0, int(row.tokens_output_total or 0))
    return tokens_in * price_in / _MILLION + tokens_out * price_out / _MILLION


def _team_by_user(db: Any, org_id: int, user_ids: set[int]) -> dict[int, int]:
    """Each user's lowest team id in the org (a multi-team user is counted once there)."""
    if not user_ids or not hasattr(db, "team_members"):
        return {}
    table = db.team_members
    members = db(table.organization_id == org_id).select()
    lowest: dict[int, int] = {}
    for member in members:
        user_id, team_id = int(member.user_id), int(member.team_id)
        if user_id in user_ids and (user_id not in lowest or team_id < lowest[user_id]):
            lowest[user_id] = team_id
    return lowest


def chargeback(
    db: Any,
    org_id: int,
    since: date,
    team_id: int | None = None,
    deployment_id: int | None = None,
    kind: int | None = None,
) -> ChargebackResult:
    """Virtual cost per (team, deployment); ``deployment_id`` requires ``kind``."""
    if deployment_id is not None and kind is None:
        raise ValueError("deployment_id requires kind (deployment ids are kind-scoped)")
    rows = _usage_rows(db, org_id, since, kind)
    teams = _team_by_user(db, org_id, {int(r.user_id) for r in rows})
    grouped: dict[tuple[int | None, int, int], ChargebackRow] = {}
    missing = 0
    for row in rows:
        if deployment_id is not None and int(row.local_deployment_id) != deployment_id:
            continue
        row_team = teams.get(int(row.user_id))
        if team_id is not None and row_team != team_id:
            continue
        cost = _row_cost(row)
        if cost is None:
            missing += 1
            continue
        key = (row_team, int(row.local_deployment_kind), int(row.local_deployment_id))
        line = grouped.setdefault(key, ChargebackRow(key[0], key[1], key[2], 0, 0, 0, _ZERO))
        line.tokens_input += max(0, int(row.tokens_input_total or 0))
        line.tokens_output += max(0, int(row.tokens_output_total or 0))
        line.request_count += int(row.request_count or 0)
        line.virtual_cost += cost
    lines = sorted(
        grouped.values(),
        key=lambda r: (
            r.team_id is None,
            r.team_id or 0,
            r.local_deployment_kind,
            r.local_deployment_id,
        ),
    )
    examined = sum(
        1
        for r in rows
        if (deployment_id is None or int(r.local_deployment_id) == deployment_id)
        and (team_id is None or teams.get(int(r.user_id)) == team_id)
    )
    return ChargebackResult(
        rows=lines,
        total_virtual_cost=sum((r.virtual_cost for r in lines), _ZERO),
        rows_examined=examined,
        rows_missing_price=missing,
    )


def _comparison_models(db: Any, pairs: set[tuple[int, int]]) -> dict[tuple[int, int], str | None]:
    """``comparison_model`` for each (kind, deployment id) from its deployment table."""
    out: dict[tuple[int, int], str | None] = {}
    for kind, dep_id in pairs:
        table_name = LOCAL_DEPLOYMENT_TABLES.get(kind)
        out[(kind, dep_id)] = None
        if table_name is None or not hasattr(db, table_name):
            continue
        table = getattr(db, table_name)
        found = db(table.id == dep_id).select().first()
        value = getattr(found, "comparison_model", None) if found is not None else None
        out[(kind, dep_id)] = str(value) if value else None
    return out


def _commercial_rate(
    db: Any, comparison_model: str | None
) -> tuple[Decimal, Decimal, Decimal] | None:
    """``(input_rate, output_rate, base_cost)`` of an enabled ``provider/model`` rate row."""
    if not comparison_model or "/" not in comparison_model:
        return None
    provider, model = comparison_model.split("/", 1)
    table = db.token_conversion_rates
    found = (
        db(
            (table.provider == provider) & (table.model == model) & (table.enabled == True)  # noqa: E712
        )
        .select()
        .first()
    )
    if found is None or not found.input_rate or not found.output_rate:
        return None
    return (
        Decimal(str(found.input_rate)),
        Decimal(str(found.output_rate)),
        Decimal(str(found.base_cost_per_waddleai_token or "0.001")),
    )


def savings(db: Any, org_id: int, since: date, kind: int | None = None) -> SavingsResult:
    """What the local fleet saved versus each deployment's commercial comparison model.

    Commercial equivalent = the same tokens priced at the comparison model's conversion rate
    (tokens / rate = credits, x base USD per credit). Savings are computed apples-to-apples over
    deployments whose commercial price is known; the rest are reported in
    ``missing_commercial_models``.
    """
    rows = _usage_rows(db, org_id, since, kind)
    per: dict[tuple[int, int], list[Decimal | int]] = {}
    missing = 0
    for row in rows:
        cost = _row_cost(row)
        if cost is None:
            missing += 1
            continue
        key = (int(row.local_deployment_kind), int(row.local_deployment_id))
        acc = per.setdefault(key, [0, 0, _ZERO])
        acc[0] = int(acc[0]) + max(0, int(row.tokens_input_total or 0))
        acc[1] = int(acc[1]) + max(0, int(row.tokens_output_total or 0))
        acc[2] = Decimal(acc[2]) + cost
    comparisons = _comparison_models(db, set(per))
    by_deployment: list[DeploymentSavings] = []
    totals_in = totals_out = 0
    virtual_total = commercial_total = priced_virtual = _ZERO
    missing_models: list[str] = []
    for key in sorted(per):
        tin, tout, vcost = int(per[key][0]), int(per[key][1]), Decimal(per[key][2])
        comparison = comparisons[key]
        rate = _commercial_rate(db, comparison)
        commercial: Decimal | None = None
        if rate is None:
            missing_models.append(comparison or f"<unset:{key[0]}/{key[1]}>")
        else:
            rate_in, rate_out, base = rate
            commercial = (Decimal(tin) / rate_in + Decimal(tout) / rate_out) * base
            commercial_total += commercial
            priced_virtual += vcost
        by_deployment.append(
            DeploymentSavings(
                kind=key[0],
                deployment_id=key[1],
                comparison_model=comparison,
                tokens_input=tin,
                tokens_output=tout,
                virtual_cost=vcost,
                commercial_equivalent_cost=commercial,
                savings=None if commercial is None else commercial - vcost,
            )
        )
        totals_in += tin
        totals_out += tout
        virtual_total += vcost
    return SavingsResult(
        tokens_input=totals_in,
        tokens_output=totals_out,
        virtual_cost=virtual_total,
        commercial_equivalent_cost=commercial_total,
        savings=commercial_total - priced_virtual,
        by_deployment=by_deployment,
        rows_examined=len(rows),
        rows_missing_price=missing,
        missing_commercial_models=sorted(set(missing_models)),
    )
