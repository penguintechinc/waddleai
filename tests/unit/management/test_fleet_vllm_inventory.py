"""C2: flags, GPU inventory and model-search endpoints of the vLLM fleet API."""

from __future__ import annotations

import uuid

import pytest

from tests.unit.management.vllm_harness import seed_backend, seed_gpus

INV = "/api/v1/fleet/gpu-inventory"
FLAGS = "/api/v1/fleet/vllm/flags"
MODELS = "/api/v1/fleet/vllm/models"


@pytest.mark.parametrize(("v2", "vllm"), [("0", "0"), ("1", "0"), ("0", "1"), ("1", "1")])
async def test_flags_endpoint_reports_each_flag_independently(
    client, vllm_db, auth_headers, monkeypatch, v2, vllm
):
    """Each flag is reported on its own; always 200 for a fleet admin."""
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", v2)
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_VLLM_RAY", vllm)
    resp = await client.get(FLAGS, headers=auth_headers)
    assert resp.status_code == 200
    assert (await resp.get_json())["data"] == {
        "fleet_v2": v2 == "1",
        "fleet_vllm_ray": vllm == "1",
    }


async def test_flags_auth_matrix(client, vllm_db, user_auth_headers):
    """No JWT: 401; a non-admin: 403."""
    assert (await client.get(FLAGS)).status_code == 401
    assert (await client.get(FLAGS, headers=user_auth_headers)).status_code == 403


@pytest.mark.parametrize("path", [INV, MODELS])
async def test_inventory_and_models_flag_off_is_404(
    client, vllm_db, auth_headers, monkeypatch, path
):
    """Both flags off: 404 not_found."""
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")  # only the first flag
    resp = await client.get(path, headers=auth_headers)
    assert resp.status_code == 404
    assert (await resp.get_json())["error"] == "not_found"


@pytest.fixture
def populated(vllm_db):
    """Two nodes (4090 x2 schedulable, A100 x1 cold) plus an org-2 GPU."""
    backend = seed_backend(vllm_db)
    seed_gpus(vllm_db, backend, node="n1", count=2)
    seed_gpus(
        vllm_db,
        backend,
        node="n2",
        gpu_class="nvidia-a100-80g",
        count=1,
        state=2,
        schedulable=False,
    )
    other = seed_backend(vllm_db, org_id=2, name="other")
    seed_gpus(vllm_db, other, org_id=2, node="foreign", count=3)
    return backend


@pytest.mark.parametrize(
    ("query", "count"),
    [
        ("vendor=1", 3),
        ("vendor=2", 0),
        ("gpu_class=nvidia-a100-80g", 1),
        ("node=n1", 2),
        ("schedulable=true", 2),
        ("schedulable=false", 1),
        ("node=n1&schedulable=false", 0),
    ],
)
async def test_gpu_inventory_filters(client, populated, auth_headers, both_flags, query, count):
    """Each filter (and a combination) narrows the org's rows exactly."""
    resp = await client.get(f"{INV}?{query}", headers=auth_headers)
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["meta"]["total"] == count
    assert len(body["data"]) == count


async def test_gpu_inventory_empty_result(client, vllm_db, auth_headers, both_flags):
    """No GPUs: 200 with an empty list and total 0."""
    body = await (await client.get(INV, headers=auth_headers)).get_json()
    assert body["data"] == [] and body["meta"]["total"] == 0


@pytest.mark.parametrize("query", ["vendor=9", "vendor=abc", "schedulable=maybe"])
async def test_gpu_inventory_invalid_filter_values_400(
    client, populated, auth_headers, both_flags, query
):
    """Invalid filter values are 400."""
    assert (await client.get(f"{INV}?{query}", headers=auth_headers)).status_code == 400


async def test_gpu_inventory_pagination_meta_and_order(client, populated, auth_headers, both_flags):
    """page=2&limit=1 returns the second row in node,index order with matching meta."""
    body = await (await client.get(f"{INV}?page=2&limit=1", headers=auth_headers)).get_json()
    assert body["meta"]["page"] == 2 and body["meta"]["limit"] == 1
    assert [(r["node_name"], r["gpu_index"]) for r in body["data"]] == [("n1", 1)]


async def test_gpu_inventory_query_always_org_scoped(client, populated, auth_headers, both_flags):
    """Org 2's GPUs never appear for an org 1 admin."""
    body = await (await client.get(f"{INV}?limit=100", headers=auth_headers)).get_json()
    assert "foreign" not in {r["node_name"] for r in body["data"]}
    assert body["meta"]["total"] == 3


async def test_gpu_inventory_unclassified_gpu_listed_unschedulable(
    client, vllm_db, auth_headers, both_flags
):
    """An UNCLASSIFIED GPU is listed with schedulable=false."""
    backend = seed_backend(vllm_db)
    seed_gpus(vllm_db, backend, gpu_class=None, count=1, state=3, schedulable=False)
    body = await (await client.get(INV, headers=auth_headers)).get_json()
    assert body["data"][0]["gpu_class"] is None and body["data"][0]["schedulable"] is False


async def test_gpu_inventory_exact_field_set(client, populated, auth_headers, both_flags):
    """DTO field set: no numeric id, no raw row."""
    row = (await (await client.get(INV, headers=auth_headers)).get_json())["data"][0]
    assert set(row) == {
        "fleet_backend_id", "node_name", "gpu_index", "vendor", "gpu_class", "product_name",
        "vram_total_mb", "vram_used_mb", "schedulable", "state", "last_seen",
    }  # fmt: skip


async def test_models_search_q_and_limit(client, vllm_db, auth_headers, both_flags):
    """Q is a case-insensitive substring; limit is bounded; params_b null allowed; public_id set."""
    for name, params in (("Llama-3-8B", 8), ("mistral-7b", None), ("llama-70b", 70)):
        vllm_db.model_registry.insert(
            name=name, role="chat", license="mit", origin="Meta", params_b=params,
            public_id=str(uuid.uuid4()), is_utility=False,
        )  # fmt: skip
    vllm_db.commit()
    body = await (await client.get(f"{MODELS}?q=LLAMA", headers=auth_headers)).get_json()
    assert [m["name"] for m in body["data"]] == ["Llama-3-8B", "llama-70b"]
    assert body["data"][0]["params_b"] == 8.0 and uuid.UUID(body["data"][0]["public_id"])
    one = await (await client.get(f"{MODELS}?limit=1", headers=auth_headers)).get_json()
    assert len(one["data"]) == 1
    nulls = await (await client.get(f"{MODELS}?q=mistral", headers=auth_headers)).get_json()
    assert nulls["data"][0]["params_b"] is None
    for bad in ("0", "101", "abc"):
        assert (await client.get(f"{MODELS}?limit={bad}", headers=auth_headers)).status_code == 400
