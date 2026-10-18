"""Org-layer pooled-quota routes: org pool + team pool (flag, scope, tenancy, shape)."""

import importlib
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from shared.auth.permissions import Permission
from shared.quota.cycle import cycle_bounds, cycle_index, utc_today

ROUTES = importlib.import_module("services.management.app.api.v1.quota_pools")
BASE = "/api/v1/pooled-quota"

ORG_POOL_FIELDS = {
    "org_id", "enabled", "cycle_anchor_date", "pool_credits", "allocated_to_teams",
    "current_cycle", "cycle_start", "cycle_end",
}  # fmt: skip


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Every test runs flag-ON unless it says otherwise."""
    monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", "1")


@pytest.fixture
def inval(monkeypatch):
    """Replace the proxy-cache invalidation with an awaitable mock to assert on."""
    mock = AsyncMock(return_value=1)
    monkeypatch.setattr(ROUTES, "invalidate_quota_config", mock)
    return mock


async def test_flag_off_is_404(real_db_client, team_headers, team_world, monkeypatch):
    """With waddleai.pooled_quota OFF the whole surface 404s (reads AND writes)."""
    monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", "0")
    h = team_headers("boss")
    assert (await real_db_client.get(f"{BASE}/org-pool", headers=h)).status_code == 404
    assert (
        await real_db_client.put(f"{BASE}/org-pool", headers=h, json={"pool_credits": 1})
    ).status_code == 404
    url = f"{BASE}/teams/{team_world.t['a1']}/pool"
    assert (await real_db_client.put(url, headers=h, json={"pool_credits": 1})).status_code == 404


async def test_flag_defaults_off_when_unset(real_db_client, team_headers, monkeypatch):
    """No env override and no PostHog -> OFF -> 404 (default OFF)."""
    monkeypatch.delenv("WADDLEAI_FLAG_POOLED_QUOTA", raising=False)
    resp = await real_db_client.get(f"{BASE}/org-pool", headers=team_headers("boss"))
    assert resp.status_code == 404


async def test_requires_auth_and_org_admin_scope(real_db_client, team_headers):
    """No token -> 401; a team Owner without the org scope -> 403."""
    assert (await real_db_client.get(f"{BASE}/org-pool")).status_code == 401
    assert (
        await real_db_client.get(f"{BASE}/org-pool", headers=team_headers("olive"))
    ).status_code == 403


async def test_unconfigured_org_reads_as_disabled_defaults(
    real_db_client, team_headers, team_world
):
    """No row yet: a fully-shaped, disabled, zero pool (and no row is created by a GET)."""
    resp = await real_db_client.get(f"{BASE}/org-pool", headers=team_headers("boss"))
    assert resp.status_code == 200
    body = await resp.get_json()
    assert set(body) == ORG_POOL_FIELDS
    assert body == {
        "org_id": team_world.org["a"], "enabled": False, "cycle_anchor_date": None,
        "pool_credits": 0, "allocated_to_teams": 0,
        "current_cycle": None, "cycle_start": None, "cycle_end": None,
    }  # fmt: skip


async def test_put_org_pool_then_get_reports_the_current_cycle(
    real_db_client, team_headers, team_world, inval
):
    """PUT sets anchor/pool/enabled; GET derives the cycle; the org cache blob is dropped."""
    anchor = utc_today() - timedelta(days=35)
    resp = await real_db_client.put(
        f"{BASE}/org-pool", headers=team_headers("boss"),
        json={"cycle_anchor_date": anchor.isoformat(), "pool_credits": 5000, "enabled": True},
    )  # fmt: skip
    assert resp.status_code == 200
    inval.assert_awaited_once_with(org_id=team_world.org["a"])
    body = await (
        await real_db_client.get(f"{BASE}/org-pool", headers=team_headers("boss"))
    ).get_json()
    start, end = cycle_bounds(anchor, cycle_index(anchor, utc_today()))
    assert set(body) == ORG_POOL_FIELDS
    assert (body["enabled"], body["pool_credits"], body["current_cycle"]) == (True, 5000, 1)
    assert (body["cycle_start"], body["cycle_end"]) == (start.isoformat(), end.isoformat())


async def test_put_org_pool_invalidates_only_after_the_write(
    real_db_client, team_headers, team_world, monkeypatch
):
    """The invalidation runs after the service write returned (DB already holds the new value)."""
    seen: list[int] = []

    async def _spy(*, org_id=None, team_id=None):
        row = team_world.db(team_world.db.quota_org_pool.org_id == org_id).select().first()
        seen.append(int(row.pool_credits) if row else -1)
        return 1

    monkeypatch.setattr(ROUTES, "invalidate_quota_config", _spy)
    resp = await real_db_client.put(
        f"{BASE}/org-pool", headers=team_headers("boss"), json={"pool_credits": 777}
    )
    assert resp.status_code == 200
    assert seen == [777]


async def test_failed_write_does_not_invalidate(real_db_client, team_headers, inval):
    """A rejected write (400) never drops the cache blob."""
    resp = await real_db_client.put(
        f"{BASE}/org-pool", headers=team_headers("boss"), json={"enabled": True}
    )
    assert resp.status_code == 400
    inval.assert_not_awaited()


@pytest.mark.parametrize(
    "payload", [{"cycle_anchor_date": "not-a-date"}, {"enabled": True}, {"pool_credits": -5}]
)
async def test_put_org_pool_rejects_bad_input(real_db_client, team_headers, payload):
    """Bad date, enabling without an anchor, and negative credits are all 400."""
    resp = await real_db_client.put(f"{BASE}/org-pool", headers=team_headers("boss"), json=payload)
    assert resp.status_code == 400


async def test_put_team_pool_allocates_and_invalidates_only_the_team(
    real_db_client, team_headers, team_world, inval
):
    """Allocation is bounded by the org pool (409) and drops only that team's blob."""
    w, h = team_world, team_headers("boss")
    await real_db_client.put(
        f"{BASE}/org-pool", headers=h,
        json={"cycle_anchor_date": "2026-01-01", "pool_credits": 1000, "enabled": True},
    )  # fmt: skip
    inval.reset_mock()
    url = f"{BASE}/teams/{w.t['a1']}/pool"
    assert (await real_db_client.put(url, headers=h, json={"pool_credits": 600})).status_code == 200
    inval.assert_awaited_once_with(team_id=w.t["a1"])
    assert (
        await real_db_client.put(
            f"{BASE}/teams/{w.t['a2']}/pool", headers=h, json={"pool_credits": 500}
        )
    ).status_code == 409
    assert (await real_db_client.put(url, headers=h, json={})).status_code == 400


async def test_team_pool_authorizes_the_specific_team(
    real_db_client, team_headers, team_world, monkeypatch
):
    """The route runs authorize_team for the PATH team with team:quota_admin (anti-IDOR)."""
    calls: list[tuple[int, Permission]] = []
    real = ROUTES.authorize_team

    async def _spy(database, team_id, permission):
        calls.append((team_id, permission))
        return await real(database, team_id, permission)

    monkeypatch.setattr(ROUTES, "authorize_team", _spy)
    url = f"{BASE}/teams/{team_world.t['a2']}/pool"
    await real_db_client.put(url, headers=team_headers("boss"), json={"pool_credits": 1})
    assert calls == [(team_world.t["a2"], Permission.TEAM_QUOTA_ADMIN)]


async def test_team_a_admin_cannot_edit_team_b_pool(
    real_db_client, team_headers, team_world, inval
):
    """A team-A owner/admin is refused on another team's pool (and on its own: org-layer op)."""
    for user in ("olive", "adam"):
        for team in ("a2", "a1"):
            resp = await real_db_client.put(
                f"{BASE}/teams/{team_world.t[team]}/pool",
                headers=team_headers(user),
                json={"pool_credits": 1},
            )
            assert resp.status_code == 403, (user, team)
    inval.assert_not_awaited()


async def test_team_authz_refusal_blocks_org_scoped_caller(
    real_db_client, team_headers, team_world, inval, monkeypatch
):
    """Even a caller holding the org scope is stopped when authorize_team refuses the team."""

    async def _refuse(*_a, **_k):
        return None, (ROUTES.jsonify({"error": "Insufficient permissions"}), 403)

    monkeypatch.setattr(ROUTES, "authorize_team", _refuse)
    url = f"{BASE}/teams/{team_world.t['a1']}/pool"
    assert (
        await real_db_client.put(url, headers=team_headers("boss"), json={"pool_credits": 1})
    ).status_code == 403
    inval.assert_not_awaited()


async def test_team_pool_is_tenant_scoped(real_db_client, team_headers, team_world):
    """Another tenant's team and a missing team are both 404 (never leaking ids)."""
    h = team_headers("boss")
    assert (
        await real_db_client.put(
            f"{BASE}/teams/{team_world.t['b1']}/pool", headers=h, json={"pool_credits": 1}
        )
    ).status_code == 404
    assert (
        await real_db_client.put(f"{BASE}/teams/999999/pool", headers=h, json={"pool_credits": 1})
    ).status_code == 404


async def test_writes_emit_telemetry(real_db_client, team_headers, caplog, inval):
    """A config write logs an INFO record (ids + credits only) via the penguin logging path."""
    import logging

    with caplog.at_level(logging.INFO):
        resp = await real_db_client.put(
            f"{BASE}/org-pool", headers=team_headers("boss"), json={"pool_credits": 5}
        )
    assert resp.status_code == 200
    assert any("quota: org pool updated" in r.getMessage() for r in caplog.records)
