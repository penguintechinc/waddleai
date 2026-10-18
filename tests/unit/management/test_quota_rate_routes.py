"""Conversion-rate admin routes: admin-only scope, validation, upsert, listing."""

import importlib

import pytest

ROUTES = importlib.import_module("services.management.app.api.v1.quota_pools")
URL = "/api/v1/pooled-quota/conversion-rates"


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Flag ON for every test in this module."""
    monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", "1")


async def test_put_upserts_by_provider_model_and_get_lists(real_db_client, team_headers):
    """Two PUTs of the same (provider, model) leave one row with the latest values."""
    h = team_headers("boss")
    first = await real_db_client.put(
        URL,
        headers=h,
        json={"provider": "openai", "model": "gpt-9", "input_rate": 5.0, "output_rate": 6.0},
    )
    assert first.status_code == 200
    second = await real_db_client.put(
        URL,
        headers=h,
        json={
            "provider": "openai",
            "model": "gpt-9",
            "input_rate": 7.0,
            "output_rate": 8.0,
            "enabled": False,
        },
    )
    assert (await first.get_json())["id"] == (await second.get_json())["id"]
    rows = (await (await real_db_client.get(URL, headers=h)).get_json())["rates"]
    (row,) = [r for r in rows if r["model"] == "gpt-9"]
    assert row == {
        "id": row["id"],
        "provider": "openai",
        "model": "gpt-9",
        "input_rate": 7.0,
        "output_rate": 8.0,
        "base_cost_per_waddleai_token": 0.001,
        "enabled": False,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"model": "m", "input_rate": 1.0, "output_rate": 1.0},
        {"provider": "p", "model": "m", "input_rate": 0, "output_rate": 1.0},
        {"provider": "p", "model": "m", "input_rate": -1.0, "output_rate": 1.0},
        {"provider": "p", "model": "m", "input_rate": 1.0, "output_rate": 0},
        {"provider": "p", "model": "m", "input_rate": 1e12, "output_rate": 1.0},
        {"provider": "p", "model": "m", "input_rate": 1.0},
        {"provider": "p" * 101, "model": "m", "input_rate": 1.0, "output_rate": 1.0},
        {
            "provider": "p",
            "model": "m",
            "input_rate": 1.0,
            "output_rate": 1.0,
            "base_cost_per_waddleai_token": -1.0,
        },
    ],
)
async def test_put_rejects_bad_input(real_db_client, team_headers, payload):
    """Zero/negative/absurd divisors, missing fields, over-long names and negative cost are 400."""
    assert (
        await real_db_client.put(URL, headers=team_headers("boss"), json=payload)
    ).status_code == 400


async def test_requires_the_admin_only_scope(real_db_client, team_headers):
    """A team Owner (no quota:admin) is 403 on both verbs; anonymous is 401."""
    body = {"provider": "p", "model": "m", "input_rate": 1.0, "output_rate": 1.0}
    assert (await real_db_client.get(URL, headers=team_headers("olive"))).status_code == 403
    assert (
        await real_db_client.put(URL, headers=team_headers("olive"), json=body)
    ).status_code == 403
    assert (await real_db_client.get(URL)).status_code == 401


async def test_flag_off_is_404(real_db_client, team_headers, monkeypatch):
    """With the pooled-quota flag OFF both verbs are 404 even for an admin."""
    monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", "0")
    h = team_headers("boss")
    body = {"provider": "p", "model": "m", "input_rate": 1.0, "output_rate": 1.0}
    assert (await real_db_client.get(URL, headers=h)).status_code == 404
    assert (await real_db_client.put(URL, headers=h, json=body)).status_code == 404


async def test_put_emits_telemetry(real_db_client, team_headers, monkeypatch):
    """A successful PUT records a rate_set write; a rejected one records rejected_400."""
    calls = []
    monkeypatch.setattr(
        ROUTES, "_record_write", lambda kind, outcome, **ids: calls.append((kind, outcome))
    )
    h = team_headers("boss")
    ok = {"provider": "p", "model": "m", "input_rate": 1.0, "output_rate": 1.0}
    assert (await real_db_client.put(URL, headers=h, json=ok)).status_code == 200
    assert (
        await real_db_client.put(URL, headers=h, json={**ok, "input_rate": 0})
    ).status_code == 400
    assert calls == [("rate_set", "updated"), ("rate_set", "rejected_400")]


async def test_get_paginates(real_db_client, team_headers):
    """The list is a bounded window: page_size=1 returns one row and the full total."""
    h = team_headers("boss")
    for m in ("a", "b"):
        await real_db_client.put(
            URL,
            headers=h,
            json={"provider": "zz", "model": m, "input_rate": 2.0, "output_rate": 2.0},
        )
    body = await (await real_db_client.get(f"{URL}?page=1&limit=1", headers=h)).get_json()
    assert len(body["rates"]) == 1
    assert body["total"] >= 2
