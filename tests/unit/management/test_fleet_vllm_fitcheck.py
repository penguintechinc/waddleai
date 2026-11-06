"""C2: fit-check and price-suggestion endpoints."""

from __future__ import annotations

import pytest

from shared.fleet.vllm_ray.fit import QUANT_MATRIX
from tests.unit.management.vllm_harness import seed_backend, seed_gpus

FIT = "/api/v1/fleet/vllm/fit-check"
PRICE = "/api/v1/fleet/price-suggestion"


@pytest.fixture
def warm(vllm_db):
    """4 WARM RTX 4090s plus an UNCLASSIFIED and a COLD GPU on other nodes."""
    backend = seed_backend(vllm_db)
    seed_gpus(vllm_db, backend, node="warm", count=4)
    seed_gpus(vllm_db, backend, node="cold", count=2, state=2, schedulable=False)
    seed_gpus(vllm_db, backend, node="unk", gpu_class=None, count=2, state=3, schedulable=False)
    seed_gpus(vllm_db, backend, node="mixed", count=2, state=4, schedulable=False)
    return backend


async def test_fit_check_uses_only_warm_pools(client, warm, auth_headers, both_flags):
    """COLD, UNCLASSIFIED and MIXED_UNSUPPORTED GPUs never enter a pool."""
    resp = await client.post(FIT, headers=auth_headers, json={"params_b": 7})
    body = (await resp.get_json())["data"]
    assert body["fits"] is True
    assert {layout["gpu_class"] for layout in body["layouts"]} == {"nvidia-rtx4090"}
    assert max(layout["replicas"] * layout["tensor_parallel"] for layout in body["layouts"]) <= 4


async def test_fit_check_returns_refusal_text_with_200(client, warm, auth_headers, both_flags):
    """A model that fits nothing is 200 with fits=false and the refusal text."""
    resp = await client.post(FIT, headers=auth_headers, json={"params_b": 4000})
    assert resp.status_code == 200
    data = (await resp.get_json())["data"]
    assert data["fits"] is False and data["layouts"] == []
    assert data["refusal"]


async def test_fit_check_empty_inventory_refuses(client, vllm_db, auth_headers, both_flags):
    """No WARM GPUs: fits=false, a refusal, and no allowed quantizations."""
    data = (await (await client.post(FIT, headers=auth_headers, json={"params_b": 7})).get_json())[
        "data"
    ]
    assert data["fits"] is False and data["refusal"] and data["allowed_quantizations"] == []


async def test_fit_check_allowed_quantizations_vendor_and_union(
    client, vllm_db, auth_headers, both_flags
):
    """Allowed quantizations follow the vendor of the candidate pools."""
    backend = seed_backend(vllm_db)
    seed_gpus(vllm_db, backend, node="n", count=2)
    nvidia = (
        await (await client.post(FIT, headers=auth_headers, json={"params_b": 1})).get_json()
    )["data"]
    assert nvidia["allowed_quantizations"] == sorted(int(q) for q in QUANT_MATRIX[1])
    seed_gpus(vllm_db, backend, node="i", gpu_class="intel-arc-a770-16gb", count=2, vendor=3)
    intel = (
        await (
            await client.post(FIT, headers=auth_headers, json={"params_b": 1, "vendor": 3})
        ).get_json()
    )["data"]
    assert intel["allowed_quantizations"] == [0]
    union = (await (await client.post(FIT, headers=auth_headers, json={"params_b": 1})).get_json())[
        "data"
    ]
    assert union["allowed_quantizations"] == sorted(
        {int(q) for q in QUANT_MATRIX[1]} | {int(q) for q in QUANT_MATRIX[3]}
    )


async def test_fit_check_requires_model_ref_or_params_b_400(client, warm, auth_headers, both_flags):
    """Neither model_ref nor params_b: 400."""
    assert (await client.post(FIT, headers=auth_headers, json={})).status_code == 400


async def test_fit_check_unparseable_model_ref_422(client, warm, auth_headers, both_flags):
    """A model_ref with no size in its name and no params_b: 422 asking for the size."""
    resp = await client.post(FIT, headers=auth_headers, json={"model_ref": "org/mystery"})
    assert resp.status_code == 422
    assert "parameter count" in (await resp.get_json())["error"]


@pytest.mark.parametrize(
    "body", [{"params_b": -1}, {"params_b": 7, "dtype": "int4"}, {"params_b": 7, "vendor": 9}]
)
async def test_fit_check_invalid_body_400(client, warm, auth_headers, both_flags, body):
    """Invalid numeric/enum fields are 400."""
    assert (await client.post(FIT, headers=auth_headers, json=body)).status_code == 400


async def test_fit_check_parses_model_ref_and_has_no_side_effects(
    client, warm, auth_headers, both_flags, fake_backend, vllm_db
):
    """model_ref sizes the model; nothing is deployed or inserted."""
    resp = await client.post(FIT, headers=auth_headers, json={"model_ref": "org/Llama-7B"})
    assert resp.status_code == 200
    assert fake_backend.calls == []
    assert vllm_db(vllm_db.vllm_deployments.id > 0).count() == 0


async def test_fit_check_auth_matrix(client, warm, user_auth_headers, both_flags):
    """401 without a JWT, 403 without fleet:admin."""
    assert (await client.post(FIT, json={"params_b": 7})).status_code == 401
    assert (
        await client.post(FIT, headers=user_auth_headers, json={"params_b": 7})
    ).status_code == 403


async def test_price_suggestion_returns_tco_result_dto(client, warm, auth_headers, both_flags):
    """kind=1 returns the TcoResult fields; bad params 400; no fit 422; ValueError text 422."""
    q = "kind=1&params_b=7&vendor=1&gpu_class=nvidia-rtx4090&tp=1&pp=1&replicas=1"
    resp = await client.get(f"{PRICE}?{q}", headers=auth_headers)
    assert resp.status_code == 200
    data = (await resp.get_json())["data"]
    assert set(data) == {
        "hourly_cost_usd", "gpu_hourly_usd", "node_hourly_usd", "node_base_usd",
        "tokens_per_s_used", "source", "price_input_per_1m", "price_output_per_1m", "breakdown",
    }  # fmt: skip
    assert data["source"] == "estimate" and data["price_output_per_1m"] > 0
    alias = await client.get(f"/api/v1/fleet/vllm/price-suggestion?{q}", headers=auth_headers)
    assert alias.status_code == 200
    assert (await client.get(f"{PRICE}?kind=1", headers=auth_headers)).status_code == 400
    big = q.replace("params_b=7", "params_b=4000")
    nofit = await client.get(f"{PRICE}?{big}", headers=auth_headers)
    assert nofit.status_code == 422 and (await nofit.get_json())["error"]
    assert (await client.get(f"{PRICE}?kind=9&{q}", headers=auth_headers)).status_code == 400


async def test_price_suggestion_kwh_unset_is_422_with_message(
    client, vllm_db, auth_headers, both_flags
):
    """compute_tco's ValueError text is returned verbatim as a 422."""
    from tests.unit.management.vllm_harness import BACKEND_CONFIG

    backend = seed_backend(vllm_db, config={**BACKEND_CONFIG, "tco": {"kwh_usd": 0}})
    seed_gpus(vllm_db, backend, count=2)
    q = "params_b=7&vendor=1&gpu_class=nvidia-rtx4090&tp=1&pp=1&replicas=1"
    resp = await client.get(f"{PRICE}?{q}", headers=auth_headers)
    assert resp.status_code == 422 and "kwh_usd must be set" in (await resp.get_json())["error"]


async def test_price_suggestion_other_kinds_use_reference_class(
    client, vllm_db, auth_headers, both_flags
):
    """kind=2/3 price a single-node layout of `count` GPUs from the static reference."""
    seed_backend(vllm_db)
    ok = await client.get(
        f"{PRICE}?kind=2&params_b=7&gpu_class=nvidia-rtx4090&count=1", headers=auth_headers
    )
    assert ok.status_code == 200
    unknown = await client.get(
        f"{PRICE}?kind=3&params_b=7&gpu_class=not-a-gpu&count=1", headers=auth_headers
    )
    assert unknown.status_code == 422
    badcount = await client.get(
        f"{PRICE}?kind=3&params_b=7&gpu_class=nvidia-rtx4090&count=0", headers=auth_headers
    )
    assert badcount.status_code == 400
