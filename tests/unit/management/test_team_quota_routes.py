"""Team-layer quota routes: live-membership authz, tenancy, invariants, invalidation."""

import importlib
from datetime import date
from unittest.mock import AsyncMock

import pytest

from services.management.app.services import quota_pools as qp

ROUTES = importlib.import_module("services.management.app.api.v1.team_quota")


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Flag ON for every test in this module."""
    monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", "1")


@pytest.fixture
def inval(monkeypatch):
    """Awaitable mock standing in for proxy-cache invalidation."""
    mock = AsyncMock(return_value=1)
    monkeypatch.setattr(ROUTES, "invalidate_quota_config", mock)
    return mock


@pytest.fixture
def pooled(team_world):
    """Org a with an enabled 1000-credit pool, team a1 allocated 100 with a 10-credit buffer."""
    w = team_world
    qp.set_org_pool(
        w.db, w.org["a"], cycle_anchor_date=date(2026, 1, 1), pool_credits=1000, enabled=True
    )
    team = w.db(w.db.teams.id == w.t["a1"]).select().first()
    qp.set_team_pool(w.db, w.org["a"], team, 100)
    qp.set_team_buffer(w.db, team, 10)
    return w


def _url(w, tail="", key="a1"):
    """Route URL for a team_world team."""
    return f"/api/v1/teams/{w.t[key]}/pooled-quota{tail}"


async def test_get_shape_for_viewer_and_unconfigured_team(real_db_client, team_headers, pooled):
    """Viewer can read; DTO has exactly the spec fields; unallocated team reads as zeros."""
    w = pooled
    body = await (await real_db_client.get(_url(w), headers=team_headers("vic"))).get_json()
    assert set(body) == {
        "team_id", "configured", "pool_credits", "shared_buffer_credits", "reserved_credits",
        "unallocated_credits", "reservations", "total", "pagination",
    }  # fmt: skip
    assert (
        body["configured"],
        body["pool_credits"],
        body["shared_buffer_credits"],
        body["unallocated_credits"],
    ) == (True, 100, 10, 90)
    empty = await (
        await real_db_client.get(_url(w, key="a2"), headers=team_headers("boss"))
    ).get_json()
    assert (empty["configured"], empty["pool_credits"], empty["unallocated_credits"]) == (
        False,
        0,
        0,
    )


async def test_authz_matrix(real_db_client, team_headers, pooled):
    """Nat (other team) 403, carl (other tenant) 404, flag-off 404, anonymous 401."""
    w = pooled
    assert (await real_db_client.get(_url(w), headers=team_headers("nat"))).status_code == 403
    assert (await real_db_client.get(_url(w), headers=team_headers("carl"))).status_code == 404
    assert (await real_db_client.get(_url(w))).status_code == 401


async def test_flag_off_is_404(real_db_client, team_headers, pooled, monkeypatch):
    """Flag OFF hides the team routes too."""
    monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", "0")
    assert (
        await real_db_client.get(_url(pooled), headers=team_headers("olive"))
    ).status_code == 404


async def test_buffer_edit_needs_team_admin_and_respects_the_pool(
    real_db_client, team_headers, pooled, inval
):
    """Admin/owner 200 (drops the team blob); member 403; over-pool 409; missing field 400."""
    w = pooled
    url = _url(w, "/buffer")
    assert (
        await real_db_client.put(
            url, headers=team_headers("adam"), json={"shared_buffer_credits": 20}
        )
    ).status_code == 200
    inval.assert_awaited_once_with(team_id=w.t["a1"])
    assert (
        await real_db_client.put(
            url, headers=team_headers("mia"), json={"shared_buffer_credits": 5}
        )
    ).status_code == 403
    assert (
        await real_db_client.put(
            url, headers=team_headers("olive"), json={"shared_buffer_credits": 101}
        )
    ).status_code == 409
    assert (
        await real_db_client.put(url, headers=team_headers("olive"), json={})
    ).status_code == 400


async def test_reservation_crud_and_membership(real_db_client, team_headers, pooled, inval):
    """PUT upserts (listed by GET), non-member 404, member forbidden, DELETE then 404."""
    w = pooled
    url = _url(w, f"/reservations/{w.u['mia']}")
    body = {"guaranteed_credits": 40, "daily_subcap": 15}
    assert (
        await real_db_client.put(url, headers=team_headers("adam"), json=body)
    ).status_code == 200
    got = await (await real_db_client.get(_url(w), headers=team_headers("adam"))).get_json()
    assert got["reservations"] == [
        {"user_id": w.u["mia"], "guaranteed_credits": 40, "daily_subcap": 15}
    ]
    assert got["reserved_credits"] == 40 and got["unallocated_credits"] == 50
    assert (
        await real_db_client.put(url, headers=team_headers("mia"), json=body)
    ).status_code == 403
    nat = _url(w, f"/reservations/{w.u['nat']}")
    assert (
        await real_db_client.put(nat, headers=team_headers("adam"), json=body)
    ).status_code == 404
    assert (
        await real_db_client.put(url, headers=team_headers("adam"), json={"daily_subcap": 3})
    ).status_code == 400
    assert (await real_db_client.delete(url, headers=team_headers("adam"))).status_code == 200
    assert (await real_db_client.delete(url, headers=team_headers("adam"))).status_code == 404


async def test_cross_team_admin_cannot_manage_other_team(
    real_db_client, team_headers, pooled, inval
):
    """regression: IDOR -- team-a2 member/admin on a1's path is 403 for every mutating route."""
    w = pooled
    body = {"guaranteed_credits": 5}
    url = _url(w, f"/reservations/{w.u['mia']}")
    assert (
        await real_db_client.put(url, headers=team_headers("nat"), json=body)
    ).status_code == 403
    assert (await real_db_client.delete(url, headers=team_headers("nat"))).status_code == 403
    assert (
        await real_db_client.put(
            _url(w, "/buffer"), headers=team_headers("nat"), json={"shared_buffer_credits": 1}
        )
    ).status_code == 403
    assert (
        await real_db_client.put(url, headers=team_headers("carl"), json=body)
    ).status_code == 404
    inval.assert_not_awaited()


async def test_reservation_cannot_be_hijacked_across_teams(
    real_db_client, team_headers, pooled, inval
):
    """regression: upsert key omits team_id; a2 admin must not move a1 user mia's reservation."""
    w = pooled
    team2 = w.db(w.db.teams.id == w.t["a2"]).select().first()
    qp.set_team_pool(w.db, w.org["a"], team2, 100)
    mine = _url(w, f"/reservations/{w.u['mia']}")
    ok = {"guaranteed_credits": 40, "daily_subcap": 15}
    assert (
        await real_db_client.put(mine, headers=team_headers("adam"), json=ok)
    ).status_code == 200
    inval.reset_mock()
    # org admin boss passes team authz on a2, but mia is not an a2 member.
    hijack = _url(w, f"/reservations/{w.u['mia']}", key="a2")
    assert (
        await real_db_client.put(
            hijack, headers=team_headers("boss"), json={"guaranteed_credits": 1}
        )
    ).status_code == 404
    inval.assert_not_awaited()
    row = w.db(w.db.quota_user_reservation.user_id == w.u["mia"]).select().first()
    assert (row.team_id, row.guaranteed_credits) == (w.t["a1"], 40)


async def test_buffer_invalidates_after_write_and_no_pii(
    real_db_client, team_headers, pooled, inval
):
    """Invalidation fires only after a successful write; response carries no usernames/emails."""
    w = pooled
    url = _url(w, f"/reservations/{w.u['mia']}")
    await real_db_client.put(url, headers=team_headers("adam"), json={"guaranteed_credits": 10})
    raw = await (await real_db_client.get(_url(w), headers=team_headers("adam"))).get_data(
        as_text=True
    )
    assert "mia" not in raw and "@" not in raw
    inval.reset_mock()
    over = await real_db_client.put(
        url, headers=team_headers("adam"), json={"guaranteed_credits": 999}
    )
    assert over.status_code == 409
    inval.assert_not_awaited()
