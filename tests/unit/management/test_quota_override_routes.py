"""Override cap routes: validation, tenancy, response shape, invalidation."""

import importlib
from unittest.mock import AsyncMock

import pytest

ROUTES = importlib.import_module("services.management.app.api.v1.quota_pools")
URL = "/api/v1/pooled-quota/overrides"


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


async def test_create_list_delete_round_trip(real_db_client, team_headers, team_world, inval):
    """POST 201, GET shows exact DTO fields, DELETE 200 then 404; writes invalidate the org."""
    w, h = team_world, team_headers("boss")
    created = await real_db_client.post(
        URL, headers=h, json={"user_id": w.u["mia"], "cap_credits": 50}
    )
    assert created.status_code == 201
    oid = (await created.get_json())["id"]
    inval.assert_awaited_with(org_id=w.org["a"])
    listing = await (await real_db_client.get(URL, headers=h)).get_json()
    assert listing["total"] == 1
    assert listing["overrides"] == [
        {"id": oid, "user_id": w.u["mia"], "model": None, "provider": None, "cap_credits": 50}
    ]
    inval.reset_mock()
    assert (await real_db_client.delete(f"{URL}/{oid}", headers=h)).status_code == 200
    inval.assert_awaited_with(org_id=w.org["a"])
    assert (await real_db_client.delete(f"{URL}/{oid}", headers=h)).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"cap_credits": 10},                                  # no dimension
        {"model": "gpt-4o"},                                  # no cap
        {"model": "gpt-4o", "cap_credits": 0},                # cap must be >= 1
        {"model": "x" * 300, "cap_credits": 5},               # over-long
    ],
)  # fmt: skip
async def test_create_rejects_bad_input(real_db_client, team_headers, inval, payload):
    """Missing dimension/cap, zero cap, and over-long strings are 400 and never invalidate."""
    resp = await real_db_client.post(URL, headers=team_headers("boss"), json=payload)
    assert resp.status_code == 400
    inval.assert_not_awaited()


async def test_cross_tenant_user_is_404_and_other_orgs_overrides_invisible(
    real_db_client, team_headers, team_world, inval
):
    """A user of another org cannot be capped; org-a never lists or deletes org-b rows."""
    w = team_world
    resp = await real_db_client.post(
        URL, headers=team_headers("boss"), json={"user_id": w.u["carl"], "cap_credits": 5}
    )
    assert resp.status_code == 404
    await real_db_client.post(
        URL, headers=team_headers("boss"), json={"provider": "openai", "cap_credits": 5}
    )
    other = w.db.quota_override.insert(organization_id=w.org["b"], provider="x", cap_credits=1)
    listing = await (await real_db_client.get(URL, headers=team_headers("boss"))).get_json()
    assert [o["provider"] for o in listing["overrides"]] == ["openai"]
    assert (
        await real_db_client.delete(f"{URL}/{int(other)}", headers=team_headers("boss"))
    ).status_code == 404


async def test_member_cannot_manage_overrides(real_db_client, team_headers, inval):
    """The org scope is required; a team Owner without it is 403 on every verb, no invalidation."""
    h = team_headers("olive")
    assert (await real_db_client.get(URL, headers=h)).status_code == 403
    assert (
        await real_db_client.post(URL, headers=h, json={"model": "m", "cap_credits": 1})
    ).status_code == 403
    assert (await real_db_client.delete(f"{URL}/1", headers=h)).status_code == 403
    inval.assert_not_awaited()


async def test_flag_off_is_404_before_any_db_read(
    real_db_client, team_headers, team_world, inval, monkeypatch
):
    """Flag OFF -> 404 on all three verbs and nothing is written or invalidated."""
    monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", "0")
    h = team_headers("boss")
    assert (await real_db_client.get(URL, headers=h)).status_code == 404
    assert (
        await real_db_client.post(URL, headers=h, json={"model": "m", "cap_credits": 1})
    ).status_code == 404
    assert (await real_db_client.delete(f"{URL}/1", headers=h)).status_code == 404
    assert team_world.db(team_world.db.quota_override.id > 0).count() == 0
    inval.assert_not_awaited()


async def test_write_telemetry_uses_underscore_labels(
    real_db_client, team_headers, team_world, inval, monkeypatch
):
    """Create/delete emit _record_write with space-free kind labels."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ROUTES, "_record_write", lambda kind, outcome, **ids: calls.append((kind, outcome))
    )
    h = team_headers("boss")
    created = await real_db_client.post(URL, headers=h, json={"model": "gpt-4o", "cap_credits": 5})
    oid = (await created.get_json())["id"]
    await real_db_client.delete(f"{URL}/{oid}", headers=h)
    await real_db_client.post(URL, headers=h, json={"cap_credits": 5})
    assert ("override_create", "created") in calls
    assert ("override_delete", "deleted") in calls
    assert ("override_create", "rejected_400") in calls
    assert all(" " not in kind for kind, _ in calls)
