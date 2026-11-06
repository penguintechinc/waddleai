"""GET /fleet/savings and /fleet/chargeback (+ /fleet/vllm/* aliases): auth, flag, DTO, filters."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from shared.fleet.vllm_ray.rollup import (
    ChargebackResult,
    ChargebackRow,
    DeploymentSavings,
    SavingsResult,
)

MODULE = "services.management.app.api.v1.fleet_chargeback"
PATHS = [
    "/api/v1/fleet/savings",
    "/api/v1/fleet/chargeback",
    "/api/v1/fleet/vllm/savings",
    "/api/v1/fleet/vllm/chargeback",
]


def _enable(monkeypatch: pytest.MonkeyPatch, on: bool = True) -> None:
    """Toggle waddleai.fleet_v2."""
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1" if on else "0")


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[tuple]]:
    """Replace the rollup queries with recorders that return fixed results."""
    calls: dict[str, list[tuple]] = {"savings": [], "chargeback": []}

    def fake_savings(_db: Any, org_id: int, since: date, kind: int | None = None) -> SavingsResult:
        calls["savings"].append((org_id, since, kind))
        return SavingsResult(
            tokens_input=10,
            tokens_output=5,
            virtual_cost=Decimal("1.5"),
            commercial_equivalent_cost=Decimal("4.0"),
            savings=Decimal("2.5"),
            by_deployment=[
                DeploymentSavings(
                    1, 7, "openai/gpt-4o", 10, 5, Decimal("1.5"), Decimal("4.0"), Decimal("2.5")
                )
            ],
            rows_examined=2,
            rows_missing_price=0,
            missing_commercial_models=[],
        )

    def fake_chargeback(
        _db: Any,
        org_id: int,
        since: date,
        team_id: int | None = None,
        deployment_id: int | None = None,
        kind: int | None = None,
    ) -> ChargebackResult:
        calls["chargeback"].append((org_id, since, team_id, deployment_id, kind))
        return ChargebackResult(
            rows=[ChargebackRow(3, 1, 7, 10, 5, 2, Decimal("1.5"))],
            total_virtual_cost=Decimal("1.5"),
            rows_examined=2,
            rows_missing_price=0,
        )

    monkeypatch.setattr(f"{MODULE}.savings", fake_savings)
    monkeypatch.setattr(f"{MODULE}.chargeback", fake_chargeback)
    monkeypatch.setattr(f"{MODULE}._db", lambda: object())
    return calls


@pytest.mark.parametrize("path", PATHS)
async def test_requires_jwt_and_fleet_admin_and_flag(
    client, app_mock_db, auth_headers, user_auth_headers, monkeypatch, spy, path
) -> None:
    """401 without JWT, 403 without fleet:admin, 404 with the flag off (never data)."""
    _enable(monkeypatch)
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=user_auth_headers)).status_code == 403
    _enable(monkeypatch, on=False)
    assert (await client.get(path, headers=auth_headers)).status_code == 404
    assert spy["savings"] == [] and spy["chargeback"] == []


async def test_savings_dto_field_set_exact(
    client, app_mock_db, auth_headers, monkeypatch, spy
) -> None:
    """The response carries exactly the declared fields: no extra data leaks (security.md)."""
    _enable(monkeypatch)
    resp = await client.get("/api/v1/fleet/savings", headers=auth_headers)
    assert resp.status_code == 200
    body = await resp.get_json()
    assert set(body) == {"status", "data", "meta"}
    assert set(body["data"]) == {
        "since",
        "tokens_input",
        "tokens_output",
        "virtual_cost",
        "commercial_equivalent_cost",
        "savings",
        "by_deployment",
        "rows_examined",
        "rows_missing_price",
        "missing_commercial_models",
    }
    assert set(body["data"]["by_deployment"][0]) == {
        "kind",
        "deployment_id",
        "comparison_model",
        "tokens_input",
        "tokens_output",
        "virtual_cost",
        "commercial_equivalent_cost",
        "savings",
    }
    assert body["data"]["savings"] == "2.5" and set(body["meta"]) == {"period", "timestamp"}


async def test_chargeback_dto_field_set_exact(
    client, app_mock_db, auth_headers, monkeypatch, spy
) -> None:
    """Chargeback rows expose exactly the declared fields."""
    _enable(monkeypatch)
    resp = await client.get("/api/v1/fleet/chargeback", headers=auth_headers)
    body = await resp.get_json()
    assert resp.status_code == 200
    assert set(body["data"]) == {
        "since",
        "rows",
        "total_virtual_cost",
        "rows_examined",
        "rows_missing_price",
    }
    assert set(body["data"]["rows"][0]) == {
        "team_id",
        "local_deployment_kind",
        "local_deployment_id",
        "tokens_input",
        "tokens_output",
        "request_count",
        "virtual_cost",
    }


@pytest.mark.parametrize(("period", "days"), [("7d", 7), ("30d", 30), (None, 30)])
async def test_period_selects_since_date_and_org_comes_from_jwt(
    client, app_mock_db, auth_headers, monkeypatch, spy, period, days
) -> None:
    """since=7d|30d (default 30d) maps to the window; the org is the JWT's, never a param."""
    _enable(monkeypatch)
    qs = "?org_id=999" + (f"&since={period}" if period else "")
    resp = await client.get("/api/v1/fleet/savings" + qs, headers=auth_headers)
    assert resp.status_code == 200
    org_id, since, _kind = spy["savings"][-1]
    assert org_id != 999 and since == date.today() - timedelta(days=days)


@pytest.mark.parametrize(
    "qs",
    [
        "since=1y",
        "since=",
        "kind=9",
        "kind=abc",
        "team_id=-1",
        "deployment_id=5",
        "deployment_id=x&kind=1",
    ],
)
async def test_invalid_parameters_are_400(
    client, app_mock_db, auth_headers, monkeypatch, spy, qs
) -> None:
    """Invalid period/kind/ids -> 400 with an error envelope, no query run."""
    _enable(monkeypatch)
    for path in ("/api/v1/fleet/savings", "/api/v1/fleet/chargeback"):
        resp = await client.get(f"{path}?{qs}", headers=auth_headers)
        assert resp.status_code == 400
        assert (await resp.get_json())["status"] == "error"
    assert spy["savings"] == [] and spy["chargeback"] == []


async def test_filters_and_vllm_alias_forces_kind(
    client, app_mock_db, auth_headers, monkeypatch, spy
) -> None:
    """team/deployment/kind filters pass through; the vllm alias pins kind=1."""
    _enable(monkeypatch)
    await client.get(
        "/api/v1/fleet/chargeback?team_id=3&deployment_id=7&kind=2", headers=auth_headers
    )
    assert spy["chargeback"][-1][2:] == (3, 7, 2)
    await client.get("/api/v1/fleet/vllm/chargeback?kind=3", headers=auth_headers)
    assert spy["chargeback"][-1][4] == 1  # alias ignores a conflicting kind
    await client.get("/api/v1/fleet/vllm/savings", headers=auth_headers)
    assert spy["savings"][-1][2] == 1
    await client.get("/api/v1/fleet/savings", headers=auth_headers)
    assert spy["savings"][-1][2] is None
