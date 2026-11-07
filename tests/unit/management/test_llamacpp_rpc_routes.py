"""Plan F, F5: llama.cpp RPC topology on the llama.cpp deployment API (real sqlite DAL)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.management.app.services import fleet_secrets
from services.management.app.services.llamacpp_manager import LlamaCppManager
from shared.fleet.llamacpp_rpc import RPC_AUTH_REFUSAL
from shared.fleet.vllm_ray.slug import derive_slug
from tests.unit.management.vllm_harness import seed_backend, seed_gpus, seed_rate

URL = "/api/v1/llamacpp/deployments"
GIB65 = 65 * 2**30
IMAGE = "ghcr.io/penguintechinc/waddleai/llamacpp-rpc:cuda-0123456@sha256:" + "b" * 64
BODY = {
    "name": "qwen-rpc",
    "model_name": "m120",
    "model_url": "https://h.example/m.gguf",
    "model_filename": "m.gguf",
    "topology": 2,
    "gguf_bytes": GIB65,
    "comparison_model": "openai/gpt-4o",
}


def _body(**over):
    """The default RPC create body with overrides (``None`` drops a key)."""
    body = {**BODY, **over}
    return {k: v for k, v in body.items() if v is not None}


@pytest.fixture
def rpc(vllm_db, both_flags, fake_k8s, monkeypatch):
    """Two nodes x 2 RTX 4090 (24 GB), a rate row, a registry entry, image and auth env."""
    backend = seed_backend(vllm_db)
    seed_gpus(vllm_db, backend, node="node-a", count=2, vram_mb=24564)
    seed_gpus(vllm_db, backend, node="node-b", count=2, vram_mb=24564)
    seed_rate(vllm_db)
    vllm_db.model_registry.insert(
        name="m120", role="chat", license="mit", origin="o", params_b=120,
        public_id="00000000-0000-0000-0000-0000000000aa", is_utility=False,
    )  # fmt: skip
    vllm_db.commit()
    monkeypatch.setenv("WADDLEAI_LLAMACPP_RPC_IMAGE_CUDA", IMAGE)
    monkeypatch.setenv("WADDLEAI_FLEET_MUTUAL_AUTH", "true")
    monkeypatch.delenv("WADDLEAI_FLEET_NAMESPACE", raising=False)
    deploy = MagicMock()
    monkeypatch.setattr(LlamaCppManager, "deploy_rpc", deploy)
    return SimpleNamespace(db=vllm_db, backend=backend, deploy=deploy, k8s=fake_k8s)


@pytest.fixture
def org2_headers(mint_token):
    """Admin bearer for a second organization."""
    return {
        "Authorization": f"Bearer {mint_token(role='admin', user_id=30, org_id=2)}",
        "Content-Type": "application/json",
    }


async def _create(client, headers, **over):
    """Create an RPC deployment, asserting 201; returns the response body."""
    resp = await client.post(URL, headers=headers, json=_body(**over))
    assert resp.status_code == 201, await resp.get_json()
    return await resp.get_json()


def _row(rpc, dep_id):
    """The llamacpp_deployments row."""
    return rpc.db(rpc.db.llamacpp_deployments.id == dep_id).select().first()


async def test_create_flag_off_403(vllm_db, fake_k8s, client, auth_headers):
    """Without both GPU Fleet flags topology 2 is refused with the exact copy; no row."""
    resp = await client.post(URL, headers=auth_headers, json=_body())
    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "llama.cpp RPC requires the GPU Fleet flags"
    assert vllm_db(vllm_db.llamacpp_deployments.id > 0).count() == 0


@pytest.mark.parametrize("bad", [0, 3, -1, "x"])
async def test_create_invalid_topology_400(rpc, client, auth_headers, bad):
    """Only 1 and 2 are topologies; anything else is a 400 and creates nothing."""
    resp = await client.post(URL, headers=auth_headers, json=_body(topology=bad))
    assert resp.status_code == 400
    assert rpc.db(rpc.db.llamacpp_deployments.id > 0).count() == 0


@pytest.mark.parametrize("size", [None, 0, -5, "big"])
async def test_create_missing_or_invalid_gguf_bytes_422(rpc, client, auth_headers, size):
    """The GGUF size is required, positive and never guessed."""
    resp = await client.post(URL, headers=auth_headers, json=_body(gguf_bytes=size))
    assert resp.status_code in (422, 400)
    if resp.status_code == 422:
        assert "gguf_bytes" in (await resp.get_json())["error"]
    assert rpc.db(rpc.db.llamacpp_deployments.id > 0).count() == 0


async def test_create_auth_disabled_exact_text(rpc, client, auth_headers, monkeypatch):
    """Mutual auth off -> 422 with the spec string, no Kubernetes call, no row."""
    monkeypatch.setenv("WADDLEAI_FLEET_MUTUAL_AUTH", "false")
    resp = await client.post(URL, headers=auth_headers, json=_body())
    assert resp.status_code == 422 and (await resp.get_json())["error"] == RPC_AUTH_REFUSAL
    rpc.deploy.assert_not_called()
    assert rpc.db(rpc.db.llamacpp_deployments.id > 0).count() == 0


async def test_create_auth_env_unset_fails_closed(rpc, client, auth_headers, monkeypatch):
    """No chart env at all behaves like auth disabled."""
    monkeypatch.delenv("WADDLEAI_FLEET_MUTUAL_AUTH")
    resp = await client.post(URL, headers=auth_headers, json=_body())
    assert resp.status_code == 422 and (await resp.get_json())["error"] == RPC_AUTH_REFUSAL


async def test_create_cross_vendor_422(rpc, client, auth_headers):
    """NVIDIA + AMD inventory is refused before anything renders."""
    seed_gpus(rpc.db, rpc.backend, node="node-c", gpu_class="amd-mi300", vendor=2, count=2)
    resp = await client.post(URL, headers=auth_headers, json=_body())
    assert resp.status_code == 422
    assert "cannot mix GPU vendors" in (await resp.get_json())["error"]
    rpc.deploy.assert_not_called()


async def test_create_success_persists_layout_prices_keys_and_deploys(rpc, client, auth_headers):
    """201: row + layout columns, owner org, AUTO price, rate row, key link, one deploy call."""
    created = await _create(client, auth_headers)
    row = _row(rpc, created["deployment_id"])
    assert (row.topology, row.owner_org_id, row.status, row.n_gpu_layers) == (2, 1, "pending", 999)
    assert row.k8s_namespace == "waddleai" and row.gpu_count >= 1
    assert row.rpc_head_node_uid in {"uid-node-a", "uid-node-b"}
    assert len(row.rpc_workers) + row.gpu_count == len(row.tensor_split.split(","))
    assert all(w["gpu_count"] == 1 for w in row.rpc_workers)
    assert round(sum(float(x) for x in row.tensor_split.split(",")), 4) == 1.0
    assert created["price_mode"] == 1 and Decimal(created["price_output_per_1m"]) > 0
    assert created["explanation"].startswith("Fits across 2 nodes")
    rate = rpc.db(
        (rpc.db.token_conversion_rates.provider == "llamacpp")
        & (rpc.db.token_conversion_rates.model == f"deployment/{row.public_id}")
    ).select().first()  # fmt: skip
    assert rate is not None and rate.input_rate > 0
    slug = derive_slug(row.name, row.public_id)
    token = rpc.k8s.deployment_key(slug)
    assert token and fleet_secrets.fingerprint(token) == created["api_key_fingerprint"]
    link = rpc.db(rpc.db.connection_links.name == f"llamacpp-{row.public_id}").select().first()
    assert link.owner_org_id == 1 and link.enabled is False
    rpc.deploy.assert_called_once()
    args = rpc.deploy.call_args.args  # (self is the class attribute mock: no self)
    assert args[0].id == row.id and args[1].nodes_used == 2 and args[3] == IMAGE
    assert set(args[2]) == {"uid-node-a", "uid-node-b"}


async def test_create_never_leaks_the_token(rpc, client, auth_headers, caplog):
    """The minted token is in the Secret only: never in the response or any log line."""
    caplog.set_level("DEBUG")
    resp = await client.post(URL, headers=auth_headers, json=_body())
    text = await resp.get_data(as_text=True)
    token = next(iter(rpc.k8s.secrets[fleet_secrets.DEPLOYMENT_KEYS_SECRET].values()))
    assert resp.status_code == 201 and token not in text and token not in caplog.text


async def test_create_one_node_fits_422(rpc, client, auth_headers):
    """A model that fits one node is refused: use the single-node topology."""
    resp = await client.post(URL, headers=auth_headers, json=_body(gguf_bytes=8 * 2**30))
    assert resp.status_code == 422
    assert (await resp.get_json())["error"] == "Fits on one node. Use the single-node topology."
    assert rpc.db(rpc.db.llamacpp_deployments.id > 0).count() == 0


async def test_create_does_not_fit_422(rpc, client, auth_headers):
    """A model bigger than the inventory is refused with the numbers."""
    resp = await client.post(URL, headers=auth_headers, json=_body(gguf_bytes=400 * 2**30))
    assert resp.status_code == 422 and "Does not fit" in (await resp.get_json())["error"]


async def test_create_no_backend_or_inventory_422(
    vllm_db, both_flags, fake_k8s, client, auth_headers, monkeypatch
):
    """Without a fleet backend/inventory the collector message is returned."""
    monkeypatch.setenv("WADDLEAI_FLEET_MUTUAL_AUTH", "true")
    resp = await client.post(URL, headers=auth_headers, json=_body())
    assert resp.status_code == 422
    assert "inventory collector must run first" in (await resp.get_json())["error"]


async def test_create_other_orgs_inventory_is_invisible(rpc, client, org2_headers):
    """Org 2 never plans against org 1's GPUs, even though they would fit."""
    resp = await client.post(URL, headers=org2_headers, json=_body())
    assert resp.status_code == 422
    assert "inventory collector" in (await resp.get_json())["error"]
    rpc.deploy.assert_not_called()


async def test_create_image_not_configured_500_names_variable(
    rpc, client, auth_headers, monkeypatch
):
    """A missing vendor image is a 500 naming the variable; no row, no key, no upstream fallback."""
    monkeypatch.delenv("WADDLEAI_LLAMACPP_RPC_IMAGE_CUDA")
    resp = await client.post(URL, headers=auth_headers, json=_body())
    assert resp.status_code == 500
    assert "WADDLEAI_LLAMACPP_RPC_IMAGE_CUDA" in (await resp.get_json())["error"]
    assert rpc.db(rpc.db.llamacpp_deployments.id > 0).count() == 0
    assert rpc.k8s.secrets.get(fleet_secrets.DEPLOYMENT_KEYS_SECRET, {}) == {}


async def test_create_tag_only_image_refused(rpc, client, auth_headers, monkeypatch):
    """The image variable must carry a digest."""
    monkeypatch.setenv("WADDLEAI_LLAMACPP_RPC_IMAGE_CUDA", "ghcr.io/x/y:cuda")
    resp = await client.post(URL, headers=auth_headers, json=_body())
    assert resp.status_code == 500 and "digest-pinned" in (await resp.get_json())["error"]


async def test_create_deploy_failure_rolls_everything_back(rpc, client, auth_headers):
    """A Kubernetes failure -> 503, and row, key, link and rate row are all gone."""
    rpc.deploy.side_effect = RuntimeError("quota exceeded")
    resp = await client.post(URL, headers=auth_headers, json=_body())
    assert resp.status_code == 503 and "nothing was kept" in (await resp.get_json())["error"]
    assert rpc.db(rpc.db.llamacpp_deployments.id > 0).count() == 0
    assert rpc.db(rpc.db.connection_links.id > 0).count() == 0
    assert rpc.db(rpc.db.token_conversion_rates.provider == "llamacpp").count() == 0
    assert rpc.k8s.secrets[fleet_secrets.DEPLOYMENT_KEYS_SECRET] == {}


async def test_create_secret_write_failure_502_keeps_nothing(rpc, client, auth_headers):
    """If the Secret cannot be written nothing is deployed or kept."""
    rpc.k8s.fail_next_patch = True
    resp = await client.post(URL, headers=auth_headers, json=_body())
    assert resp.status_code == 502
    assert rpc.db(rpc.db.llamacpp_deployments.id > 0).count() == 0
    assert rpc.db(rpc.db.token_conversion_rates.provider == "llamacpp").count() == 0
    rpc.deploy.assert_not_called()


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"deployment_type": "remote"}, "Kubernetes nodes only"),
        ({"model_url": None}, "model_url and model_filename"),
        ({"model_filename": None}, "model_url and model_filename"),
        ({"k8s_namespace": "elsewhere"}, "fleet namespace"),
        ({"comparison_model": None}, "comparison_model is required"),
        ({"comparison_model": "openai/none"}, "No commercial rate"),
        ({"price_mode": 2}, "manual pricing needs"),
        ({"rpc_workers": []}, "non-empty list"),
        (
            {
                "rpc_workers": [
                    {"node_uid": "uid-node-a", "gpu_class": "nvidia-rtx4090", "gpu_count": 2}
                ]
            },
            "gpu_count must be 1",
        ),
        ({"rpc_workers": [{"gpu_class": "nvidia-rtx4090", "gpu_count": 1}]}, "needs node_uid"),
        ({"rpc_workers": ["x"]}, "must be objects"),
    ],
)
async def test_create_validation_refusals_leave_no_trace(rpc, client, auth_headers, over, message):
    """Every pre-flight refusal returns a plain-language 4xx and keeps no row/key/rate."""
    resp = await client.post(URL, headers=auth_headers, json=_body(**over))
    assert resp.status_code in (400, 422, 400)
    text = (await resp.get_json()).get("error", "")
    assert message in text or resp.status_code == 400
    assert rpc.db(rpc.db.llamacpp_deployments.id > 0).count() == 0
    assert rpc.k8s.secrets.get(fleet_secrets.DEPLOYMENT_KEYS_SECRET, {}) == {}
    rpc.deploy.assert_not_called()


async def test_create_mixed_class_needs_consent_and_manual_price(rpc, client, auth_headers):
    """Only a 4090+4070 mix fits: needs consent; AUTO price is refused, MANUAL is accepted."""
    rpc.db(rpc.db.gpu_inventory.node_name == "node-b").update(schedulable=False)
    seed_gpus(
        rpc.db, rpc.backend, node="node-c", gpu_class="nvidia-rtx4070", count=4, vram_mb=12288
    )
    refused = await client.post(URL, headers=auth_headers, json=_body())
    assert refused.status_code == 422 and "mixes GPU classes" in (await refused.get_json())["error"]
    auto = await client.post(URL, headers=auth_headers, json=_body(allow_mixed_class=True))
    assert auto.status_code == 422
    assert "one GPU class" in (await auto.get_json())["error"]
    assert rpc.db(rpc.db.llamacpp_deployments.id > 0).count() == 0
    manual = await _create(
        client, auth_headers, allow_mixed_class=True, price_mode=2,
        price_input_per_1m="1", price_output_per_1m="4",
    )  # fmt: skip
    row = _row(rpc, manual["deployment_id"])
    assert row.allow_mixed_class is True and "mixing" in manual["explanation"]
    classes = {w["gpu_class"] for w in row.rpc_workers} | set(row.node_selector.values())
    assert classes == {"nvidia-rtx4090", "nvidia-rtx4070"}


async def test_create_explicit_workers_restrict_the_gpu_set(rpc, client, auth_headers):
    """rpc_workers limit placement to the named GPUs; too few GPUs -> does not fit."""
    two = [{"node_uid": "uid-node-a", "gpu_class": "nvidia-rtx4090", "gpu_count": 1}] * 2
    resp = await client.post(URL, headers=auth_headers, json=_body(rpc_workers=two))
    assert resp.status_code == 422 and "Does not fit" in (await resp.get_json())["error"]
    four = [
        {"node_uid": f"uid-node-{n}", "gpu_class": "nvidia-rtx4090", "gpu_count": 1}
        for n in ("a", "a", "b", "b")
    ]
    ok = await client.post(URL, headers=auth_headers, json=_body(rpc_workers=four))
    assert ok.status_code == 201


async def test_create_manual_price_persists(rpc, client, auth_headers):
    """MANUAL pricing stores exactly the given prices."""
    created = await _create(
        client, auth_headers, price_mode=2, price_input_per_1m="2", price_output_per_1m="6"
    )
    assert created["price_mode"] == 2 and Decimal(created["price_output_per_1m"]) == Decimal("6")


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("?topology=2", 1),
        ("?topology=1", 1),
        ("", 2),
        ("?topology=2&status=running", 0),
        ("?topology=2&status=pending", 1),
        ("?status=pending", 2),
        ("?page=9", 0),
    ],
)
async def test_list_filters(rpc, client, auth_headers, query, expected):
    """topology/status filters and pagination narrow the list to the caller's org."""
    await _create(client, auth_headers)
    single = await client.post(
        URL, headers=auth_headers, json={"name": "solo", "model_name": "m120"}
    )
    assert single.status_code == 201
    resp = await client.get(URL + query, headers=auth_headers)
    assert resp.status_code == 200 and len((await resp.get_json())["deployments"]) == expected


async def test_list_invalid_topology_filter_400(rpc, client, auth_headers):
    """An unknown topology filter value is rejected, not ignored."""
    assert (await client.get(URL + "?topology=7", headers=auth_headers)).status_code == 400


async def test_list_is_scoped_to_the_callers_org(rpc, client, auth_headers, org2_headers):
    """Another org never sees this org's RPC layout (it names infrastructure nodes)."""
    await _create(client, auth_headers)
    other = await client.get(URL, headers=org2_headers)
    assert (await other.get_json())["deployments"] == []
    mine = (await (await client.get(URL, headers=auth_headers)).get_json())["deployments"]
    assert len(mine) == 1 and mine[0]["topology"] == 2 and mine[0]["rpc_workers"]
    assert mine[0]["allow_mixed_class"] is False and mine[0]["tensor_split"]


async def test_list_unowned_rows_only_for_system_org(
    rpc, client, auth_headers, mint_token, monkeypatch
):
    """Legacy unowned rows are hidden from org admins and shown to the system org."""
    rpc.db.llamacpp_deployments.insert(
        name="legacy", deployment_type="remote", status="stopped", model_name="m",
        public_id="11111111-1111-1111-1111-111111111111",
    )  # fmt: skip
    rpc.db.commit()
    assert (await (await client.get(URL, headers=auth_headers)).get_json())["deployments"] == []
    monkeypatch.setenv("WADDLEAI_SYSTEM_ORG_ID", "99")
    system = {"Authorization": f"Bearer {mint_token(role='admin', user_id=31, org_id=99)}"}
    names = [
        d["name"] for d in (await (await client.get(URL, headers=system)).get_json())["deployments"]
    ]
    assert names == ["legacy"]


async def test_patch_layout_change_while_running_409(rpc, client, auth_headers):
    """A running RPC deployment's layout cannot change; the message says what to do."""
    created = await _create(client, auth_headers)
    dep_id = created["deployment_id"]
    for status in ("running", "degraded", "deploying", "error"):
        rpc.db(rpc.db.llamacpp_deployments.id == dep_id).update(status=status)
        rpc.db.commit()
        resp = await client.patch(
            f"{URL}/{dep_id}", headers=auth_headers, json={"allow_mixed_class": True}
        )
        assert resp.status_code == 409
        assert (await resp.get_json())["error"] == "Stop the deployment before changing its layout."
        other = await client.patch(f"{URL}/{dep_id}", headers=auth_headers, json={"n_ctx": 1})
        assert other.status_code == 409 and "before modifying" in (await other.get_json())["error"]
    ok = await client.patch(
        f"{URL}/{dep_id}", headers=auth_headers,
        json={"price_mode": 2, "price_input_per_1m": "1", "price_output_per_1m": "3"},
    )  # fmt: skip
    assert ok.status_code == 200  # a price change never needs a stop


async def test_patch_stopped_replans_layout_and_reprices(rpc, client, auth_headers):
    """A stopped deployment can be re-planned: new split persisted and AUTO price recomputed."""
    created = await _create(client, auth_headers)
    dep_id = created["deployment_id"]
    rpc.db(rpc.db.llamacpp_deployments.id == dep_id).update(status="stopped")
    rpc.db.commit()
    before = Decimal(str(_row(rpc, dep_id).price_output_per_1m))
    resp = await client.patch(
        f"{URL}/{dep_id}",
        headers=auth_headers,
        json={"gguf_bytes": 40 * 2**30, "allow_mixed_class": False},
    )
    assert resp.status_code == 200, await resp.get_json()
    row = _row(rpc, dep_id)
    assert row.topology == 2 and Decimal(str(row.price_output_per_1m)) != before


@pytest.mark.parametrize(
    ("body", "status", "message"),
    [
        ({"topology": 9}, 400, "topology must be"),
        ({"topology": 2}, 422, "gguf_bytes"),
        ({"allow_mixed_class": "yes"}, 400, "true or false"),
        ({"gguf_bytes": 400 * 2**30}, 422, "Does not fit"),
        ({"gguf_bytes": 8 * 2**30}, 422, "Fits on one node"),
        ({"gpu_count": 4}, 422, "derived from the RPC layout"),
        ({"node_selector": {}}, 422, "derived from the RPC layout"),
        ({"gguf_bytes": GIB65, "rpc_workers": []}, 422, "non-empty list"),
    ],
)
async def test_patch_refusals(rpc, client, auth_headers, body, status, message):
    """Bad layout patches are refused with plain copy and change nothing."""
    created = await _create(client, auth_headers)
    dep_id = created["deployment_id"]
    rpc.db(rpc.db.llamacpp_deployments.id == dep_id).update(status="stopped")
    rpc.db.commit()
    snapshot = (_row(rpc, dep_id).rpc_workers, _row(rpc, dep_id).tensor_split)
    resp = await client.patch(f"{URL}/{dep_id}", headers=auth_headers, json=body)
    assert resp.status_code == status and message in (await resp.get_json())["error"]
    assert (_row(rpc, dep_id).rpc_workers, _row(rpc, dep_id).tensor_split) == snapshot


async def test_patch_to_single_node_resets_rpc_columns(rpc, client, auth_headers):
    """Topology 1 clears the worker list, split and head so the row is a plain deployment."""
    created = await _create(client, auth_headers)
    dep_id = created["deployment_id"]
    rpc.db(rpc.db.llamacpp_deployments.id == dep_id).update(status="stopped")
    rpc.db.commit()
    resp = await client.patch(f"{URL}/{dep_id}", headers=auth_headers, json={"topology": 1})
    assert resp.status_code == 200, await resp.get_json()
    row = _row(rpc, dep_id)
    assert (row.topology, row.rpc_workers, row.tensor_split, row.rpc_head_node_uid) == (
        1,
        None,
        None,
        None,
    )


async def test_patch_flags_off_403(rpc, client, auth_headers, monkeypatch):
    """Planning a multi-node layout requires the flags even on PATCH."""
    created = await _create(client, auth_headers)
    dep_id = created["deployment_id"]
    rpc.db(rpc.db.llamacpp_deployments.id == dep_id).update(status="stopped")
    rpc.db.commit()
    monkeypatch.delenv("WADDLEAI_FLAG_FLEET_VLLM_RAY")
    resp = await client.patch(f"{URL}/{dep_id}", headers=auth_headers, json={"gguf_bytes": GIB65})
    assert resp.status_code == 403


async def test_deploy_rpc_row_rotates_token_and_deploys(rpc, client, auth_headers):
    """Redeploying a stopped RPC row mints a new token (Secret + link) then deploys."""
    created = await _create(client, auth_headers)
    dep_id = created["deployment_id"]
    row = _row(rpc, dep_id)
    slug = derive_slug(row.name, row.public_id)
    old = rpc.k8s.deployment_key(slug)
    rpc.db(rpc.db.llamacpp_deployments.id == dep_id).update(status="stopped")
    rpc.db.commit()
    rpc.deploy.reset_mock()
    resp = await client.post(f"{URL}/{dep_id}/deploy", headers=auth_headers)
    assert resp.status_code == 200, await resp.get_json()
    new = rpc.k8s.deployment_key(slug)
    assert new and new != old
    rpc.deploy.assert_called_once()
    layout = rpc.deploy.call_args.args[1]
    assert layout.head_node_uid == row.rpc_head_node_uid and layout.nodes_used == 2
    assert [round(x, 4) for x in layout.tensor_split] == [
        float(x) for x in row.tensor_split.split(",")
    ]


@pytest.mark.parametrize("status", ["running", "deploying", "degraded", "error"])
async def test_deploy_rpc_row_already_live_409(rpc, client, auth_headers, status):
    """An already-deployed RPC row is never created twice."""
    dep_id = (await _create(client, auth_headers))["deployment_id"]
    rpc.db(rpc.db.llamacpp_deployments.id == dep_id).update(status=status)
    rpc.db.commit()
    rpc.deploy.reset_mock()
    resp = await client.post(f"{URL}/{dep_id}/deploy", headers=auth_headers)
    assert resp.status_code == 409
    rpc.deploy.assert_not_called()


async def test_deploy_rpc_row_node_left_inventory_409(rpc, client, auth_headers):
    """If a planned node left the inventory the redeploy asks for a re-plan, never guesses."""
    dep_id = (await _create(client, auth_headers))["deployment_id"]
    rpc.db(rpc.db.llamacpp_deployments.id == dep_id).update(status="stopped")
    rpc.db(rpc.db.gpu_inventory.id > 0).delete()
    rpc.db.commit()
    rpc.deploy.reset_mock()
    resp = await client.post(f"{URL}/{dep_id}/deploy", headers=auth_headers)
    assert resp.status_code == 409 and "left the GPU inventory" in (await resp.get_json())["error"]
    rpc.deploy.assert_not_called()


async def test_deploy_rpc_row_flags_off_403_and_failure_503(rpc, client, auth_headers, monkeypatch):
    """Flags off -> 403; a Kubernetes failure -> 503 with no detail leaked; refusal -> 422."""
    dep_id = (await _create(client, auth_headers))["deployment_id"]
    rpc.db(rpc.db.llamacpp_deployments.id == dep_id).update(status="stopped")
    rpc.db.commit()
    rpc.deploy.side_effect = RuntimeError("secret detail")
    failed = await client.post(f"{URL}/{dep_id}/deploy", headers=auth_headers)
    assert failed.status_code == 503 and "secret detail" not in await failed.get_data(as_text=True)
    from shared.fleet.llamacpp_rpc import RpcRefused

    rpc.deploy.side_effect = RpcRefused(RPC_AUTH_REFUSAL)
    refused = await client.post(f"{URL}/{dep_id}/deploy", headers=auth_headers)
    assert refused.status_code == 422
    monkeypatch.delenv("WADDLEAI_FLAG_FLEET_V2")
    assert (await client.post(f"{URL}/{dep_id}/deploy", headers=auth_headers)).status_code == 403


async def test_deploy_rpc_row_image_missing_500(rpc, client, auth_headers, monkeypatch):
    """A redeploy with no image configured is a 500 naming the variable."""
    dep_id = (await _create(client, auth_headers))["deployment_id"]
    rpc.db(rpc.db.llamacpp_deployments.id == dep_id).update(status="stopped")
    rpc.db.commit()
    monkeypatch.delenv("WADDLEAI_LLAMACPP_RPC_IMAGE_CUDA")
    resp = await client.post(f"{URL}/{dep_id}/deploy", headers=auth_headers)
    assert resp.status_code == 500 and "RPC_IMAGE_CUDA" in (await resp.get_json())["error"]


async def test_deploy_rpc_row_secret_failure_502(rpc, client, auth_headers):
    """If the new token cannot be written the redeploy stops before any Kubernetes call."""
    dep_id = (await _create(client, auth_headers))["deployment_id"]
    rpc.db(rpc.db.llamacpp_deployments.id == dep_id).update(status="stopped")
    rpc.db.commit()
    rpc.deploy.reset_mock()
    rpc.k8s.fail_next_patch = True
    resp = await client.post(f"{URL}/{dep_id}/deploy", headers=auth_headers)
    assert resp.status_code == 502
    rpc.deploy.assert_not_called()


async def test_deploy_rpc_row_recreates_missing_link(rpc, client, auth_headers):
    """A missing key link is re-provisioned (token + link) rather than failing."""
    dep_id = (await _create(client, auth_headers))["deployment_id"]
    row = _row(rpc, dep_id)
    rpc.db(rpc.db.llamacpp_deployments.id == dep_id).update(status="stopped")
    rpc.db(rpc.db.connection_links.name == f"llamacpp-{row.public_id}").delete()
    rpc.db.commit()
    resp = await client.post(f"{URL}/{dep_id}/deploy", headers=auth_headers)
    assert resp.status_code == 200
    assert rpc.db(rpc.db.connection_links.name == f"llamacpp-{row.public_id}").count() == 1


async def test_remove_dispatches_to_manager(rpc, client, auth_headers, monkeypatch):
    """The remove route reaches remove_daemonset (which routes RPC rows to remove_rpc)."""
    removed = MagicMock()
    monkeypatch.setattr(LlamaCppManager, "remove_daemonset", removed)
    dep_id = (await _create(client, auth_headers))["deployment_id"]
    resp = await client.post(f"{URL}/{dep_id}/remove", headers=auth_headers)
    assert resp.status_code == 200 and removed.call_args.kwargs == {"force": True}


@pytest.mark.parametrize("status", ["running", "deploying", "degraded", "error"])
async def test_delete_live_rpc_needs_force(rpc, client, auth_headers, status):
    """Any non-stopped RPC state owns cluster objects, so deleting needs force."""
    dep_id = (await _create(client, auth_headers))["deployment_id"]
    rpc.db(rpc.db.llamacpp_deployments.id == dep_id).update(status=status)
    rpc.db.commit()
    resp = await client.delete(f"{URL}/{dep_id}", headers=auth_headers)
    assert resp.status_code == 409 and _row(rpc, dep_id) is not None


async def test_delete_force_removes_objects_key_link_rate_and_row(
    rpc, client, auth_headers, monkeypatch
):
    """Force delete tears down the cluster objects, the bearer token, link, rate row and row."""
    removed = MagicMock()
    monkeypatch.setattr(LlamaCppManager, "remove_daemonset", removed)
    dep_id = (await _create(client, auth_headers))["deployment_id"]
    row = _row(rpc, dep_id)
    slug = derive_slug(row.name, row.public_id)
    rpc.db(rpc.db.llamacpp_deployments.id == dep_id).update(status="degraded")
    rpc.db.commit()
    resp = await client.delete(f"{URL}/{dep_id}?force=true", headers=auth_headers)
    assert resp.status_code == 200
    removed.assert_called_once()
    assert rpc.k8s.deployment_key(slug) is None
    assert _row(rpc, dep_id) is None
    assert rpc.db(rpc.db.connection_links.id > 0).count() == 0
    assert rpc.db(rpc.db.token_conversion_rates.provider == "llamacpp").count() == 0


async def test_delete_stopped_rpc_keeps_nothing_behind_and_secret_failure_502(
    rpc, client, auth_headers
):
    """A stopped RPC deployment deletes cleanly; a Secret failure keeps the row for a retry."""
    dep_id = (await _create(client, auth_headers))["deployment_id"]
    rpc.db(rpc.db.llamacpp_deployments.id == dep_id).update(status="stopped")
    rpc.db.commit()
    rpc.k8s.fail_next_patch = True
    failed = await client.delete(f"{URL}/{dep_id}", headers=auth_headers)
    assert failed.status_code == 502 and _row(rpc, dep_id) is not None
    assert (await client.delete(f"{URL}/{dep_id}", headers=auth_headers)).status_code == 200
    assert _row(rpc, dep_id) is None


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get", ""),
        ("patch", ""),
        ("delete", ""),
        ("post", "/deploy"),
        ("post", "/remove"),
        ("get", "/health"),
        ("get", "/export/k8s"),
    ],
)
async def test_cross_org_rpc_routes_404_and_change_nothing(
    rpc, client, auth_headers, org2_headers, method, suffix
):
    """Org 2 cannot see, change, deploy or remove org 1's RPC deployment."""
    dep_id = (await _create(client, auth_headers))["deployment_id"]
    rpc.deploy.reset_mock()
    before = _row(rpc, dep_id)
    resp = await getattr(client, method)(
        f"{URL}/{dep_id}{suffix}",
        headers=org2_headers,
        json={"gguf_bytes": GIB65, "allow_mixed_class": True},
    )
    assert resp.status_code == 404
    rpc.deploy.assert_not_called()
    after = _row(rpc, dep_id)
    assert after is not None and (after.rpc_workers, after.status) == (
        before.rpc_workers,
        before.status,
    )


async def test_cross_org_rotate_rpc_404(rpc, client, auth_headers, org2_headers):
    """The rotate route hides another org's RPC deployment and restarts nothing."""
    created = await _create(client, auth_headers)
    public_id = _row(rpc, created["deployment_id"]).public_id
    resp = await client.post(
        f"/api/v1/fleet/llamacpp/deployments/{public_id}/rotate-token", headers=org2_headers
    )
    assert resp.status_code == 404 and rpc.k8s.restart == {}


async def test_rotate_rpc_restarts_head_deployment_only(rpc, client, auth_headers):
    """AM18: Secret key first, then the head Deployment restarts; workers are untouched."""
    created = await _create(client, auth_headers)
    row = _row(rpc, created["deployment_id"])
    slug = derive_slug(row.name, row.public_id)
    old = rpc.k8s.deployment_key(slug)
    rpc.db(rpc.db.llamacpp_deployments.id == row.id).update(status="running")
    rpc.db.commit()  # a pending deployment has no head pod to roll
    resp = await client.post(
        f"/api/v1/fleet/llamacpp/deployments/{row.public_id}/rotate-token", headers=auth_headers
    )
    assert resp.status_code == 200
    new = rpc.k8s.deployment_key(slug)
    assert new != old and new not in await resp.get_data(as_text=True)
    assert rpc.k8s.restart == {f"Deployment/{row.k8s_daemonset_name}": 1}


async def test_export_rpc_manifests_yaml(rpc, client, auth_headers):
    """Export renders the head, workers, services and policies from the stored layout."""
    import yaml

    dep_id = (await _create(client, auth_headers))["deployment_id"]
    resp = await client.get(f"{URL}/{dep_id}/export/k8s", headers=auth_headers)
    assert resp.status_code == 200
    kinds = [d["kind"] for d in yaml.safe_load_all(await resp.get_data(as_text=True))]
    assert kinds.count("Deployment") == 1 and kinds.count("CiliumNetworkPolicy") == 2
    assert "StatefulSet" in kinds and kinds.count("Service") == 2


async def test_export_rpc_manifests_conflict_when_not_renderable(
    rpc, client, auth_headers, monkeypatch
):
    """No image or a node that left the inventory is a 409 with the reason."""
    dep_id = (await _create(client, auth_headers))["deployment_id"]
    monkeypatch.delenv("WADDLEAI_LLAMACPP_RPC_IMAGE_CUDA")
    assert (await client.get(f"{URL}/{dep_id}/export/k8s", headers=auth_headers)).status_code == 409


async def test_health_rpc_includes_worker_counts(rpc, client, auth_headers, monkeypatch):
    """RPC health adds status_message and worker counts (live when readable, else the layout)."""
    dep_id = (await _create(client, auth_headers))["deployment_id"]
    row = _row(rpc, dep_id)
    rpc.db(rpc.db.llamacpp_deployments.id == dep_id).update(
        endpoint_url="http://head:8080", status_message="serving across 4 GPUs"
    )
    rpc.db.commit()
    monkeypatch.setattr(
        "services.management.app.api.v1.llamacpp.requests.get",
        lambda *_a, **_k: SimpleNamespace(status_code=200),
    )
    observed = SimpleNamespace(workers_up=2, workers_total=3)
    monkeypatch.setattr(LlamaCppManager, "rpc_observe", lambda self, dep: observed)
    body = await (await client.get(f"{URL}/{dep_id}/health", headers=auth_headers)).get_json()
    assert body["status"] == "healthy" and (body["workers_up"], body["workers_total"]) == (2, 3)
    assert body["status_message"] == "serving across 4 GPUs"
    monkeypatch.setattr(
        LlamaCppManager, "rpc_observe", MagicMock(side_effect=RuntimeError("k8s down"))
    )
    degraded = await (await client.get(f"{URL}/{dep_id}/health", headers=auth_headers)).get_json()
    assert degraded["workers_up"] is None and degraded["workers_total"] == len(row.rpc_workers)


async def test_health_single_node_has_no_worker_fields(rpc, client, auth_headers, monkeypatch):
    """Single-node health is unchanged: the RPC fields are null."""
    single = await (
        await client.post(URL, headers=auth_headers, json={"name": "solo", "model_name": "m120"})
    ).get_json()
    rpc.db(rpc.db.llamacpp_deployments.id == single["deployment_id"]).update(
        endpoint_url="http://x:8080"
    )
    rpc.db.commit()
    monkeypatch.setattr(
        "services.management.app.api.v1.llamacpp.requests.get",
        lambda *_a, **_k: SimpleNamespace(status_code=503),
    )
    body = await (
        await client.get(f"{URL}/{single['deployment_id']}/health", headers=auth_headers)
    ).get_json()
    assert (
        body["status"] == "unhealthy"
        and body["workers_up"] is None
        and body["status_message"] is None
    )


async def test_unauthenticated_and_forbidden_rpc_requests(rpc, client, user_auth_headers):
    """No JWT -> 401; a plain user (no llama.cpp admin scope) -> 403, nothing created."""
    assert (await client.post(URL, json=_body())).status_code == 401
    assert (await client.post(URL, headers=user_auth_headers, json=_body())).status_code == 403
    assert rpc.db(rpc.db.llamacpp_deployments.id > 0).count() == 0


def test_uuid_helper_import_is_used():
    """Keep the uuid import honest for slug derivation fixtures."""
    assert uuid.UUID("12345678-1234-5678-1234-567812345678").hex


async def test_same_name_in_two_orgs_gets_distinct_kubernetes_objects(
    rpc, client, auth_headers, org2_headers
):
    """Regression (tenant collision): two orgs picking one name never share object names."""
    seed2 = seed_backend(rpc.db, org_id=2, name="gpu2")
    seed_gpus(rpc.db, seed2, org_id=2, node="node-y", count=2, vram_mb=24564)
    seed_gpus(rpc.db, seed2, org_id=2, node="node-z", count=2, vram_mb=24564)
    # distinct names (the column is unique) that sanitise to the SAME old name-only object name
    first = await _create(client, auth_headers, name="Qwen_RPC")
    second = await _create(client, org2_headers, name="qwen-rpc")
    one, two = _row(rpc, first["deployment_id"]), _row(rpc, second["deployment_id"])
    old = LlamaCppManager(rpc.db)._daemonset_name
    assert old(one.name) == old(two.name)  # the pre-fix collision
    assert one.k8s_daemonset_name != two.k8s_daemonset_name
    for row in (one, two):
        assert str(row.public_id).replace("-", "")[:12] in row.k8s_daemonset_name
    assert {call.args[0].k8s_daemonset_name for call in rpc.deploy.call_args_list} == {
        one.k8s_daemonset_name,
        two.k8s_daemonset_name,
    }


async def test_create_refuses_a_base_name_already_in_use(rpc, client, auth_headers, monkeypatch):
    """Regression (tenant collision): an existing row with the same object name is a 409."""
    monkeypatch.setattr(
        "services.management.app.api.v1.llamacpp.rpc_service.base_name_taken", lambda *_a: True
    )
    resp = await client.post(URL, headers=auth_headers, json=_body())
    assert resp.status_code == 409
    assert rpc.db(rpc.db.llamacpp_deployments.id > 0).count() == 0
    rpc.deploy.assert_not_called()


async def test_convert_to_rpc_rebases_the_object_name_and_refuses_collisions(
    rpc, client, auth_headers, monkeypatch
):
    """Regression (tenant collision): converting a name-only row moves it to a public-id name."""
    single = await (
        await client.post(URL, headers=auth_headers, json={"name": "solo", "model_name": "m120"})
    ).get_json()
    dep_id = single["deployment_id"]
    row = _row(rpc, dep_id)
    assert row.k8s_daemonset_name.startswith("waddleai-llamacpp-solo-")  # + public-id suffix
    path = "services.management.app.api.v1.llamacpp.rpc_service.base_name_taken"
    with monkeypatch.context() as taken:
        taken.setattr(path, lambda *_a: True)
        refused = await client.patch(f"{URL}/{dep_id}", headers=auth_headers, json=_convert())
    assert refused.status_code == 409
    assert _row(rpc, dep_id).topology == 1
    ok = await client.patch(f"{URL}/{dep_id}", headers=auth_headers, json=_convert())
    assert ok.status_code == 200, await ok.get_json()
    after = _row(rpc, dep_id)
    assert after.topology == 2
    assert str(after.public_id).replace("-", "")[:12] in after.k8s_daemonset_name


def _convert():
    """PATCH body converting a deployment to RPC."""
    return {
        "topology": 2,
        "gguf_bytes": GIB65,
        "model_url": BODY["model_url"],
        "model_filename": BODY["model_filename"],
        "comparison_model": "openai/gpt-4o",
    }


async def test_list_without_an_org_is_403_and_never_matches_unowned_rows(
    rpc, client, auth_headers, monkeypatch
):
    """Regression (list/visibility mismatch): an org-less token fails closed, never lists NULLs."""
    rpc.db.llamacpp_deployments.insert(
        name="legacy", deployment_type="remote", status="stopped", model_name="m",
        public_id="44444444-4444-4444-4444-444444444444",
    )  # fmt: skip
    rpc.db.commit()
    monkeypatch.setattr(
        "services.management.app.api.v1.llamacpp.g", SimpleNamespace(user={"organization_id": None})
    )
    resp = await client.get(URL, headers=auth_headers)
    assert resp.status_code == 403
    assert "legacy" not in await resp.get_data(as_text=True)


@pytest.mark.parametrize("status", ["running", "deploying", "degraded", "error"])
async def test_convert_refused_while_old_objects_may_be_live(rpc, client, auth_headers, status):
    """Regression: re-basing the object name of a live row would orphan its objects (409)."""
    single = await (
        await client.post(URL, headers=auth_headers, json={"name": "solo", "model_name": "m120"})
    ).get_json()
    dep_id = single["deployment_id"]
    original_name = _row(rpc, dep_id).k8s_daemonset_name
    rpc.db(rpc.db.llamacpp_deployments.id == dep_id).update(status=status)
    rpc.db.commit()
    price_only_blocked = await client.patch(
        f"{URL}/{dep_id}", headers=auth_headers, json=_convert()
    )
    assert price_only_blocked.status_code == 409
    row = _row(rpc, dep_id)
    assert row.topology == 1
    assert row.k8s_daemonset_name == original_name  # a refused convert must not re-base it
