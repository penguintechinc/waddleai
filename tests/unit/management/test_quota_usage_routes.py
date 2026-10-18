"""Usage report + rebuild: current-cycle default, tenant scoping, cache outage."""

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from services.management.app import extensions
from services.management.app.services import quota_pools as qp
from shared.quota import keys
from shared.quota.cycle import cycle_index, utc_today
from shared.quota.ledger import SCOPE_BUFFER, SCOPE_USER, LedgerEntry, QuotaLedger

BASE = "/api/v1/pooled-quota"
ANCHOR = utc_today() - timedelta(days=35)  # current cycle = 1


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Flag ON for every test in this module."""
    monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", "1")


@pytest.fixture
def seeded(team_world):
    """Org a enabled, cycle-1 anchor, ledger rows in cycles 1 and 0 (+ a foreign org row)."""
    w = team_world
    qp.set_org_pool(w.db, w.org["a"], cycle_anchor_date=ANCHOR, pool_credits=1000, enabled=True)
    ledger = QuotaLedger(w.db)
    ledger.add(
        w.org["a"],
        1,
        [LedgerEntry(SCOPE_USER, w.u["mia"], 30), LedgerEntry(SCOPE_BUFFER, w.t["a1"], 7)],
    )
    ledger.add(w.org["a"], 0, [LedgerEntry(SCOPE_USER, w.u["mia"], 99)])
    ledger.add(w.org["b"], 1, [LedgerEntry(SCOPE_USER, w.u["carl"], 5)])
    return w


async def test_usage_defaults_to_the_current_cycle_and_is_org_scoped(
    real_db_client, team_headers, seeded
):
    """Cycle 1 rows only, no foreign-org rows; ?cycle=0 returns the previous cycle."""
    h = team_headers("boss")
    body = await (await real_db_client.get(f"{BASE}/usage", headers=h)).get_json()
    assert body["cycle"] == cycle_index(ANCHOR, utc_today()) == 1
    assert {(e["scope_type"], e["credits_used"]) for e in body["entries"]} == {
        ("user", 30),
        ("team_buffer", 7),
    }
    prev = await (await real_db_client.get(f"{BASE}/usage?cycle=0", headers=h)).get_json()
    assert [(e["scope_id"], e["credits_used"]) for e in prev["entries"]] == [(seeded.u["mia"], 99)]


async def test_usage_without_a_configured_cycle_is_empty_not_an_error(
    real_db_client, team_headers, team_world
):
    """No anchor yet: cycle None and no entries."""
    body = await (
        await real_db_client.get(f"{BASE}/usage", headers=team_headers("boss"))
    ).get_json()
    assert (body["cycle"], body["entries"], body["total"]) == (None, [], 0)


async def test_rebuild_restores_counters_and_reports_the_count(
    real_db_client, team_headers, seeded, monkeypatch
):
    """POST /rebuild SET-NXs the current cycle's ledger rows into Valkey."""
    client = MagicMock()
    client.set.return_value = True
    monkeypatch.setattr(extensions, "redis_client", client)
    resp = await real_db_client.post(f"{BASE}/rebuild", headers=team_headers("boss"), json={})
    assert resp.status_code == 200
    assert await resp.get_json() == {"cycle": 1, "counters_restored": 2}
    assert {c.args[0] for c in client.set.call_args_list} == {
        keys.user_counter(seeded.u["mia"], 1),
        keys.buffer_counter(seeded.t["a1"], 1),
    }


async def test_rebuild_without_a_cache_client_is_503_and_without_a_cycle_409(
    real_db_client, team_headers, team_world, monkeypatch
):
    """No Valkey client -> 503; no configured cycle (and no explicit one) -> 409."""
    monkeypatch.setattr(extensions, "redis_client", None)
    h = team_headers("boss")
    assert (
        await real_db_client.post(f"{BASE}/rebuild", headers=h, json={"cycle": 1})
    ).status_code == 503
    monkeypatch.setattr(extensions, "redis_client", MagicMock())
    assert (await real_db_client.post(f"{BASE}/rebuild", headers=h, json={})).status_code == 409


async def test_team_view_reports_ledger_backed_usage(real_db_client, team_headers, seeded):
    """GET /teams/<id>/pooled-quota adds cycle and reservation/buffer usage."""
    team = seeded.db(seeded.db.teams.id == seeded.t["a1"]).select().first()
    qp.set_team_pool(seeded.db, seeded.org["a"], team, 100)
    url = f"/api/v1/teams/{seeded.t['a1']}/pooled-quota"
    body = await (await real_db_client.get(url, headers=team_headers("adam"))).get_json()
    assert (body["usage_cycle"], body["reservation_used_credits"], body["buffer_used_credits"]) == (
        1,
        30,
        7,
    )


async def test_scope_required(real_db_client, team_headers):
    """The org scope gates both routes."""
    assert (
        await real_db_client.get(f"{BASE}/usage", headers=team_headers("olive"))
    ).status_code == 403
    assert (
        await real_db_client.post(f"{BASE}/rebuild", headers=team_headers("olive"), json={})
    ).status_code == 403


async def test_flag_off_is_404_first(real_db_client, team_headers, monkeypatch):
    """Flag OFF (default) -> both routes 404 before any other check."""
    monkeypatch.delenv("WADDLEAI_FLAG_POOLED_QUOTA", raising=False)
    h = team_headers("boss")
    assert (await real_db_client.get(f"{BASE}/usage", headers=h)).status_code == 404
    assert (await real_db_client.post(f"{BASE}/rebuild", headers=h, json={})).status_code == 404


async def test_usage_entries_carry_ids_and_credits_only(real_db_client, team_headers, seeded):
    """No PII: exact key set per entry and per body."""
    body = await (
        await real_db_client.get(f"{BASE}/usage", headers=team_headers("boss"))
    ).get_json()
    assert set(body) == {"org_id", "cycle", "entries", "total", "pagination"}
    assert all(set(e) == {"scope_type", "scope_id", "credits_used"} for e in body["entries"])
