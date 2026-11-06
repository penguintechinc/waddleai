# vLLM on Ray GPU Fleet — F5: llama.cpp RPC API (Part 3 of the F plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Context:** Part of Plan F. F1 and F2 are in `2026-11-05-vllm-ray-gpu-fleet-F-llamacpp-rpc.md`; F3 (K8s rendering) is in `2026-11-05-vllm-ray-gpu-fleet-F3-llamacpp-rpc-manifests.md`; F4, F6–F8 and open items are in `2026-11-05-vllm-ray-gpu-fleet-F2-llamacpp-rpc.md`. Global Constraints and Review Focus in `2026-11-05-vllm-ray-gpu-fleet-F-llamacpp-rpc.md` apply unchanged.

---

## Task F5: API (create/PATCH topology, key minting, pricing)

**Files:**
- Modify: `services/management/app/api/v1/llamacpp.py` — DTOs (lines 54-76 response, 86-101 request: add `topology: int = 1`, `allow_mixed_class: bool = False`, `rpc_workers: list[dict] | None = None`, `gguf_bytes: int | None = None`); `_deployment_to_dict` (line 207); create (252-302); PATCH (318-383).
- Create: `services/management/app/services/llamacpp_rpc_service.py` (orchestration; no Quart imports).
- Test: `tests/unit/management/test_llamacpp_rpc_routes.py`; fixtures appended to `tests/unit/management/conftest.py`.

**Interfaces:**
- Consumes: `plan_rpc_layout`, `RpcRefused`, `RpcLayout` (F2); `LlamaCppManager.deploy_rpc`, `.mutual_auth_enabled` (F3); `GpuInventory` (B2: `node_uid`, `node_name`, `vendor`, `gpu_class`, `vram_total_mb`, `vram_used_mb`, `schedulable`, `state`); `_vllm_ray_enabled` (C1); `sync_deployment_link` (C1-part2); `fleet_secrets.mint_token()` (C1-part3, `services/management/app/services/fleet_secrets.py`; OI-6 closed) for the key stored in `connection_links.api_key`; `apply_pricing` (B6, AM10); `TcoInputs`, `GpuClassCost`, `NodeClassCost` (B4b).
- Produces:
  - `pools_from_inventory(db, fleet_backend_id: int, org_id: int) -> list[Pool]`: schedulable, WARM rows; grouped by `(vendor, gpu_class)`; nodes `(node_uid, gpu_count, vram_total_mb - vram_used_mb)`.
  - `async def create_rpc_deployment(db, org_id, body: dict, *, mutual_auth_enabled: bool, manager) -> int`; `async def replan_rpc_deployment(db, dep, body, *, mutual_auth_enabled, manager) -> None`.

**Create (`topology == 2`), in order:** flag off → 403 `llama.cpp RPC requires the GPU Fleet flags`; `deployment_type != "kubernetes"` → 422 `RPC layouts run on Kubernetes nodes only.`; `topology` ∉ {1,2} → 400; `gguf_bytes` missing or < 1 → 422 `RPC layout needs the GGUF file size (gguf_bytes); it is not guessed.`; `rpc_workers` (each `node_uid`, `gpu_class`, `gpu_count == 1`) or auto-plan from `pools_from_inventory`; `RpcRefused` → 422 `str(exc)`; one-node result → 422 `Fits on one node. Use the single-node topology.`; persist row (`topology`, `rpc_workers`, `tensor_split` 4-dp CSV, `allow_mixed_class`, `status="pending"`); mint key with `fleet_secrets.mint_token()` and create Secret `B-apikey` (`stringData.api-key`) — value never in logs, responses or exception text; image from `WADDLEAI_LLAMACPP_RPC_IMAGE_<VENDOR>` (missing → 500 naming the variable; no upstream fallback); `deploy_rpc` via `asyncio.to_thread` with hostnames from `gpu_inventory.node_name`; `price_mode == AUTO_TCO` → `apply_pricing(db, LocalDeploymentKind.LLAMACPP, ...)` with distinct-node `nodes_used` (OI-2); `sync_deployment_link`; 201 `{deployment_id, message}`.

**Rotate token (AM18, AM20):** `POST /api/v1/llamacpp/deployments/<int:id>/rotate-token` (scope `fleet:admin`) → `async def rotate_rpc_token(db, dep, *, manager) -> None` in `llamacpp_rpc_service.py`. Order is fixed: (1) mint `fleet_secrets.mint_token()`; (2) **Secret update first**: `core.patch_namespaced_secret(B-apikey, body={"stringData": {"api-key": new}})`; (3) **then head rolling restart**: bump `spec.template.metadata.annotations["waddleai.penguintech.io/restart-generation"]` on the head Deployment. llama-server reads `--api-key-file` only at startup, so the head must restart; workers are not touched. Response 200 `{deployment_id, message: "Token rotated; head restarting"}`. The token never appears in logs, responses or exception text.

**PATCH:** `status == "running"` and `topology`/`rpc_workers`/`allow_mixed_class` changed → 409 `Stop the deployment before changing its layout.`; otherwise rerun the Create steps, rotating the Secret only when the layout changes.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/management/test_llamacpp_rpc_routes.py
import pytest
from unittest.mock import MagicMock

from shared.fleet.llamacpp_rpc import RPC_AUTH_REFUSAL

RPC = {"name": "qwen-rpc", "model_name": "qwen-120b", "model_filename": "m.gguf",
       "model_url": "https://h/m.gguf", "topology": 2, "gguf_bytes": 65 * 2**30}
URL = "/api/v1/llamacpp/deployments"

@pytest.mark.asyncio
async def test_create_flag_off_403(client_flags_off, auth):
    r = await client_flags_off.post(URL, json=RPC, headers=auth)
    assert r.status_code == 403 and "GPU Fleet flags" in (await r.get_json())["message"]

@pytest.mark.asyncio
async def test_create_missing_gguf_bytes_422(client_on, auth):
    r = await client_on.post(URL, json={k: v for k, v in RPC.items() if k != "gguf_bytes"}, headers=auth)
    assert r.status_code == 422 and "gguf_bytes" in (await r.get_json())["message"]

@pytest.mark.asyncio
async def test_create_auth_disabled_exact_text(client_on_no_mauth, auth, inventory_two_nodes):
    r = await client_on_no_mauth.post(URL, json=RPC, headers=auth)
    assert r.status_code == 422 and (await r.get_json())["message"] == RPC_AUTH_REFUSAL

@pytest.mark.asyncio
async def test_create_cross_vendor_422(client_on, auth, inventory_nvidia_and_amd):
    r = await client_on.post(URL, json=RPC, headers=auth)
    assert r.status_code == 422 and "cannot mix GPU vendors" in (await r.get_json())["message"]

```

Same file, continued (same imports and fixtures):

```python
@pytest.mark.parametrize("bad", [0, 3, "x"])
@pytest.mark.asyncio
async def test_create_invalid_topology_400(client_on, auth, bad):
    assert (await client_on.post(URL, json={**RPC, "topology": bad}, headers=auth)).status_code == 400

```

Same file, continued (same imports and fixtures):

```python
@pytest.mark.asyncio
async def test_create_mints_key_and_never_leaks_it(client_on, auth, inventory_two_nodes, core_mock, caplog):
    r = await client_on.post(URL, json=RPC, headers=auth)
    secret = core_mock.create_namespaced_secret.call_args.kwargs["body"]
    value = secret["stringData"]["api-key"]
    assert r.status_code == 201 and secret["metadata"]["name"].endswith("-apikey")
    assert value not in await r.get_text() and value not in caplog.text

```

Same file, continued (same imports and fixtures):

```python
@pytest.mark.parametrize("query, expected", [("?topology=2", 1), ("?topology=1", 1),
                                             ("?topology=2&status=running", 0), ("?page=9", 0)])
@pytest.mark.asyncio
async def test_list_filters(client_on, auth, seeded_rpc_and_single, query, expected):
    r = await client_on.get(URL + query, headers=auth)
    assert r.status_code == 200 and len((await r.get_json())["deployments"]) == expected

@pytest.mark.asyncio
async def test_patch_layout_change_while_running_409(client_on, auth, running_rpc):
    r = await client_on.patch(f"{URL}/{running_rpc.id}", json={"allow_mixed_class": True}, headers=auth)
    assert r.status_code == 409

```

Same file, continued (same imports and fixtures):

```python
def test_fake_llamacpp_rejects_unauthenticated(fake_llamacpp_server):
    # mirrors llama-server --api-key-file: 401 without the bearer, 200 with it
    import requests
    assert requests.get(f"{fake_llamacpp_server.url}/v1/models", timeout=5).status_code == 401
    ok = requests.get(f"{fake_llamacpp_server.url}/v1/models", timeout=5,
                      headers={"Authorization": f"Bearer {fake_llamacpp_server.key}"})
    assert ok.status_code == 200

```

Same file, continued (same imports and fixtures):

```python
@pytest.mark.asyncio
async def test_rotate_token_secret_then_head_restart_only(client_on, auth, running_rpc, core_mock, apps_mock):
    order = MagicMock()
    order.attach_mock(core_mock.patch_namespaced_secret, "secret")
    order.attach_mock(apps_mock.patch_namespaced_deployment, "head")
    r = await client_on.post(f"{URL}/{running_rpc.id}/rotate-token", headers=auth)
    new = core_mock.patch_namespaced_secret.call_args.kwargs["body"]["stringData"]["api-key"]
    assert r.status_code == 200 and [c[0] for c in order.mock_calls] == ["secret", "head"]
    assert new not in await r.get_text()
    apps_mock.patch_namespaced_stateful_set.assert_not_called()
    assert "waddleai.penguintech.io/restart-generation" in str(apps_mock.patch_namespaced_deployment.call_args)
```
- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/management/test_llamacpp_rpc_routes.py -v` → failures: 404 or `TypeError` on unknown `topology` field.

- [ ] **Step 3: Implement** `llamacpp_rpc_service.py` and the route changes per Create and PATCH behaviour.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/management/test_llamacpp_rpc_routes.py -v` → `15 passed`, 0 failed. Run `pytest tests/unit/management -k llamacpp -q` → 0 failures.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/llamacpp.py services/management/app/services/llamacpp_rpc_service.py tests/unit/management/test_llamacpp_rpc_routes.py tests/unit/management/conftest.py
git commit -m "feat(llamacpp): RPC topology in create/PATCH with planning, key minting and pricing" -m "<the executing session's attribution trailer>"
```

---
