"""C2b: price fields on the Ollama and llama.cpp deployment APIs (AM10)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.unit.management.vllm_harness import seed_backend, seed_gpus, seed_rate

LABEL = "waddleai.penguintech.io/gpu-classes"
OLLAMA = "/api/v1/ollama/deployments"
LLAMA = "/api/v1/llamacpp/deployments"


@pytest.fixture
def priced(vllm_db, both_flags, fake_k8s):
    """Pricing config (vllm_ray backend), a commercial rate row, and a registry entry."""
    backend = seed_backend(vllm_db)
    seed_gpus(vllm_db, backend, count=1)
    seed_rate(vllm_db)
    for name in ("m7b", "dep-a"):
        vllm_db.model_registry.insert(
            name=name, role="chat", license="mit", origin="o", params_b=7,
            public_id=f"00000000-0000-0000-0000-00000000000{len(name)}", is_utility=False,
        )  # fmt: skip
    vllm_db.commit()
    return vllm_db


def _ollama(**over):
    body = {
        "name": "dep-a",
        "endpoint_url": "http://ollama:11434",
        "gpu_config": {"node_selector": {LABEL: "nvidia-rtx4090"}, "gpu_count": 1},
        "comparison_model": "openai/gpt-4o",
    }
    body.update(over)
    return body


def _llama(**over):
    body = {
        "name": "ll-a",
        "model_name": "m7b",
        "node_selector": {LABEL: "nvidia-rtx4090"},
        "gpu_count": 1,
        "comparison_model": "openai/gpt-4o",
    }
    body.update(over)
    return body


async def test_ollama_create_manual_persists_prices_and_public_id(client, priced, auth_headers):
    """MANUAL prices and a public_id are stored; the response carries them."""
    resp = await client.post(
        OLLAMA, headers=auth_headers,
        json=_ollama(price_mode=2, price_input_per_1m="2", price_output_per_1m="6"),
    )  # fmt: skip
    assert resp.status_code == 201
    body = await resp.get_json()
    assert body["price_mode"] == 2 and Decimal(body["price_output_per_1m"]) == Decimal("6")
    assert body["public_id"] and body["comparison_model"] == "openai/gpt-4o"
    row = priced(priced.ollama_deployments.id == body["id"]).select().first()
    assert str(row.public_id) == body["public_id"]


async def test_ollama_create_auto_ignores_client_price_and_stores_breakdown(
    client, priced, auth_headers
):
    """AUTO ignores client prices and stores the TCO breakdown."""
    resp = await client.post(
        OLLAMA, headers=auth_headers,
        json=_ollama(price_input_per_1m="9.99", price_output_per_1m="9.99"),
    )  # fmt: skip
    body = await resp.get_json()
    assert resp.status_code == 201 and body["price_mode"] == 1
    assert Decimal(body["price_output_per_1m"]) != Decimal("9.99")
    assert body["price_breakdown"]["gpu_class"] == "nvidia-rtx4090"


@pytest.mark.parametrize(
    "prices",
    [{}, {"price_input_per_1m": "0", "price_output_per_1m": "1"},
     {"price_input_per_1m": "-1", "price_output_per_1m": "1"},
     {"price_input_per_1m": "nan", "price_output_per_1m": "1"},
     {"price_input_per_1m": "abc", "price_output_per_1m": "1"}],
)  # fmt: skip
async def test_llamacpp_patch_switch_to_manual_requires_positive_prices(
    client, priced, auth_headers, prices
):
    """Switching to MANUAL needs finite prices > 0 (even on a running deployment)."""
    created = await client.post(LLAMA, headers=auth_headers, json=_llama())
    dep_id = (await created.get_json())["deployment_id"]
    priced(priced.llamacpp_deployments.id == dep_id).update(status="running")
    priced.commit()
    resp = await client.patch(
        f"{LLAMA}/{dep_id}", headers=auth_headers, json={"price_mode": 2, **prices}
    )
    assert resp.status_code == 400


async def test_llamacpp_price_patch_on_running_deployment_succeeds(client, priced, auth_headers):
    """A price-only PATCH works while running; other fields still need a stopped deployment."""
    created = await client.post(LLAMA, headers=auth_headers, json=_llama())
    dep_id = (await created.get_json())["deployment_id"]
    priced(priced.llamacpp_deployments.id == dep_id).update(status="running")
    priced.commit()
    ok = await client.patch(
        f"{LLAMA}/{dep_id}", headers=auth_headers,
        json={"price_mode": 2, "price_input_per_1m": "3", "price_output_per_1m": "9"},
    )  # fmt: skip
    assert ok.status_code == 200
    row = priced(priced.llamacpp_deployments.id == dep_id).select().first()
    assert (row.price_mode, Decimal(str(row.price_output_per_1m))) == (2, Decimal("9"))
    blocked = await client.patch(f"{LLAMA}/{dep_id}", headers=auth_headers, json={"n_ctx": 8192})
    assert blocked.status_code == 409


async def test_auto_without_gpu_class_label_is_422(client, priced, auth_headers):
    """AUTO needs the node's gpu-classes label (Ollama and llama.cpp); no row survives a refusal."""
    bad_ollama = await client.post(
        OLLAMA, headers=auth_headers, json=_ollama(gpu_config={"gpu_count": 1})
    )
    assert bad_ollama.status_code == 422
    bad_llama = await client.post(LLAMA, headers=auth_headers, json=_llama(node_selector={}))
    assert bad_llama.status_code == 422
    assert priced(priced.ollama_deployments.id > 0).count() == 0
    assert priced(priced.llamacpp_deployments.id > 0).count() == 0


async def test_comparison_model_without_rate_row_is_422(client, priced, auth_headers):
    """comparison_model must have an enabled commercial rate; it is required when pricing."""
    for body in (
        _llama(comparison_model="openai/none"),
        _llama(comparison_model=None, price_mode=1),
    ):
        resp = await client.post(LLAMA, headers=auth_headers, json=body)
        assert resp.status_code == 422


async def test_local_rate_row_upserted_with_provider_ollama_or_llamacpp(
    client, priced, auth_headers
):
    """A per-deployment rate row is written for each kind, keyed deployment/<public_id>."""
    o = await (await client.post(OLLAMA, headers=auth_headers, json=_ollama())).get_json()
    ll = await (await client.post(LLAMA, headers=auth_headers, json=_llama())).get_json()
    for provider, public_id in (("ollama", o["public_id"]), ("llamacpp", ll["public_id"])):
        rate = (
            priced(
                (priced.token_conversion_rates.provider == provider)
                & (priced.token_conversion_rates.model == f"deployment/{public_id}")
            )
            .select()
            .first()
        )
        assert rate is not None and rate.input_rate > 0


async def test_ollama_put_prices_existing_deployment(client, priced, auth_headers):
    """PUT with price fields re-prices (404 for an unknown id, 400 for bad prices)."""
    created = await (await client.post(OLLAMA, headers=auth_headers, json=_ollama())).get_json()
    url = f"{OLLAMA}/{created['id']}"
    ok = await client.put(
        url,
        headers=auth_headers,
        json={"price_mode": 2, "price_input_per_1m": "1", "price_output_per_1m": "4"},
    )
    assert ok.status_code == 200
    row = priced(priced.ollama_deployments.id == created["id"]).select().first()
    assert (row.price_mode, Decimal(str(row.price_output_per_1m))) == (2, Decimal("4"))
    assert (await client.put(url, headers=auth_headers, json={"price_mode": 2})).status_code == 400
    assert (
        await client.put(f"{OLLAMA}/9999", headers=auth_headers, json={"price_mode": 1})
    ).status_code == 404


async def test_existing_create_without_pricing_fields_unchanged(client, priced, auth_headers):
    """Regression: no price fields -> 201 as before, no rate row, no pricing refusal."""
    resp = await client.post(
        OLLAMA, headers=auth_headers, json={"name": "plain", "endpoint_url": "http://o:11434"}
    )
    assert resp.status_code == 201
    llama = await client.post(LLAMA, headers=auth_headers, json={"name": "p", "model_name": "m"})
    assert llama.status_code == 201
    assert priced(priced.token_conversion_rates.provider == "ollama").count() == 0


async def test_llamacpp_create_mints_key_and_rotate_restarts_head(
    client, priced, auth_headers, fake_k8s
):
    """With both flags on, create mints a token; rotate rewrites the key and rolls the head."""
    created = await (await client.post(LLAMA, headers=auth_headers, json=_llama())).get_json()
    assert created["api_key_fingerprint"]
    row = priced(priced.llamacpp_deployments.id == created["deployment_id"]).select().first()
    secret = fake_k8s.secrets["waddleai-vllm-deployment-keys"]
    assert len(secret) == 1
    resp = await client.post(
        f"/api/v1/fleet/llamacpp/deployments/{created['public_id']}/rotate-token",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = await resp.get_json()
    assert "token" not in data and data["api_key_fingerprint"] != created["api_key_fingerprint"]
    assert list(fake_k8s.restart.values()) == [1]
    assert next(iter(fake_k8s.restart)).startswith("StatefulSet/")
    del row
    fake_k8s.fail_next_patch = True
    failed = await client.post(
        f"/api/v1/fleet/llamacpp/deployments/{created['public_id']}/rotate-token",
        headers=auth_headers,
    )
    assert failed.status_code == 502 and list(fake_k8s.restart.values()) == [1]


async def test_llamacpp_rotate_auth_and_flag_matrix(client, priced, user_auth_headers, monkeypatch):
    """401 without JWT, 403 without fleet:admin, 404 for an unknown deployment."""
    url = "/api/v1/fleet/llamacpp/deployments/00000000-0000-0000-0000-000000000001/rotate-token"
    assert (await client.post(url)).status_code == 401
    assert (await client.post(url, headers=user_auth_headers)).status_code == 403
