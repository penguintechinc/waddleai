# vLLM on Ray GPU Fleet — F3: llama.cpp RPC K8s Rendering (Part 2 of the F plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Context:** Part of Plan F. Part 1 (F1 schema, F2 planner) is in `2026-11-05-vllm-ray-gpu-fleet-F-llamacpp-rpc.md`; F5 (API) is in `2026-11-05-vllm-ray-gpu-fleet-F5-llamacpp-rpc-api.md`; F4, F6–F8 and open items are in `2026-11-05-vllm-ray-gpu-fleet-F2-llamacpp-rpc.md`. Global Constraints and Review Focus in `2026-11-05-vllm-ray-gpu-fleet-F-llamacpp-rpc.md` apply unchanged. Branch `feature/vllm-ray-f`.

## Task F3: K8s rendering in `LlamaCppManager`

**Files:**
- Modify: `services/management/app/services/llamacpp_manager.py` — constants after line 38; `__init__` (line 104) reads `mutual_auth_enabled` from `config["mutual_auth_enabled"]`; add `export_rpc_manifests`, `deploy_rpc`, `get_k8s_custom_client` near `remove_daemonset` (ends line 343).
- Test: `tests/unit/management/test_llamacpp_rpc_manifests.py`

**Interfaces:**
- Consumes: `RpcLayout`, `RpcWorker`, `RpcRefused`, `RPC_AUTH_REFUSAL` (F2); the row (F1); `CURL_DOWNLOADER_IMAGE` (line 36); `get_k8s_apps_client` / `get_k8s_core_client` (lines 41-64).
- Produces:

```python
RPC_PORT: int = 50052
RPC_DEVICE_BY_VENDOR: dict[int, str] = {1: "CUDA0", 2: "ROCm0", 3: "SYCL0"}   # executor verifies against the pinned image: `rpc-server --help`
RPC_RESOURCE_BY_VENDOR: dict[int, str] = {2: "amd.com/gpu", 3: "gpu.intel.com/i915"}   # AMD/Intel device-plugin resources; executor verifies against the A1 class map
def resource_for(vendor: int, gpu_class: str) -> str   # NVIDIA: "nvidia.com/<short class>" from the A1 class map (nvidia-rtx4090 -> nvidia.com/rtx4090); else RPC_RESOURCE_BY_VENDOR

class LlamaCppManager:
    def export_rpc_manifests(self, deployment, layout: RpcLayout, hostnames: dict[str, str], image: str, n_ctx: int) -> list[dict]
    def deploy_rpc(self, deployment, layout: RpcLayout, hostnames: dict[str, str], image: str) -> None
```

**Objects.** `B = self._daemonset_name(deployment.name)`, `ns = deployment.k8s_namespace`.
- `Deployment` `B` (head, replicas 1): `nodeSelector {kubernetes.io/hostname: hostnames[head]}`; limits and requests `{resource_for(v, layout.head_gpu_class): head_gpus}`; labels `waddleai.penguintech.io/llamacpp-deployment: B`, `...llamacpp-role: head`. Init container `wait-workers` (`CURL_DOWNLOADER_IMAGE`, existing securityContext) loops `curl -sS --connect-timeout 2 telnet://<fqdn>:50052 </dev/null` per worker until exit 0 (OI-10). Main container args: `--model /models/<file> --n-gpu-layers 999 --ctx-size <n_ctx> --port 8080 --host 0.0.0.0 --rpc <csv fqdn:50052> --tensor-split <csv> --api-key-file /secrets/api-key`. Mounts `/models`, `/secrets` (Secret `B-apikey`, readOnly), `/tmp`.
- `StatefulSet` `B-rpc-<i>` per worker node (i = index in sorted node_uid order): replicas = workers on that node; `serviceName: B-rpc`; `nodeSelector` hostname; container args `["-H", "0.0.0.0", "-p", "50052", "-d", RPC_DEVICE_BY_VENDOR[v]]`; limit and request `{resource_for(v, worker.gpu_class): "1"}`; `tcpSocket` readiness on 50052; hardened securityContext as the existing manifest.
- `Service` `B-rpc` (`clusterIP: None`), selector `{deployment: B, role: worker}`, 50052/TCP.
- `Service` `B-svc` (existing naming), selector `{role: head}`, port 8080.
- `CiliumNetworkPolicy` `B-rpc-allow` (`cilium.io/v2`): endpoints `role=worker` ingress from `role=head, deployment=B` on 50052/TCP, `authentication: {mode: required}`; endpoints `role=head` ingress from proxy pods (labels copied from `waddleai-allow-fleet-ingress` in `k8s/helm/waddleai/templates/cilium-network-policy.yaml`) on 8080/TCP with `authentication.mode: required`; head egress to workers on 50052/TCP with the same auth mode.
- The Secret `B-apikey` is created by F5. The manager only references it.

**Refusals:** `mutual_auth_enabled` False → `RpcRefused(RPC_AUTH_REFUSAL)` before any K8s client is built. `len(tensor_split) != len(workers) + head_gpus` → `ValueError("tensor_split length X must equal device count Y")`.

**Deploy order:** headless Service → `B-svc` → worker StatefulSets → head Deployment → CNP. On any exception, delete what was created (log each delete failure), then re-raise. Row updates: `status="deploying"`, `endpoint_url=http://B-svc.<ns>:8080`, `rpc_workers`, `tensor_split`.

- [ ] **Step 1: Write the failing tests** (10 tests; fences split for size)

```python
# tests/unit/management/test_llamacpp_rpc_manifests.py
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from services.management.app.services.llamacpp_manager import LLAMACPP_IMAGE, LlamaCppManager
from shared.fleet.llamacpp_rpc import RPC_AUTH_REFUSAL, RpcLayout, RpcRefused, RpcWorker

IMG = "ghcr.io/penguintechinc/waddleai/llamacpp-rpc:cuda-0123456@sha256:" + "a" * 64
HOSTS = {"n1": "gpu-a1", "n2": "gpu-a2"}
MGR = "services.management.app.services.llamacpp_manager"


def layout() -> RpcLayout:
    """Head n1 with 2 RTX 4090s; two single-GPU workers on n2; split 0.25/0.25/0.5 in device order."""
    ws = [RpcWorker("n2", "nvidia-rtx4090", 23000, 0.25), RpcWorker("n2", "nvidia-rtx4090", 23000, 0.25)]
    return RpcLayout(1, "n1", "nvidia-rtx4090", 2, ws, [0.25, 0.25, 0.5], 80000, 92000, 2, "x")


@pytest.fixture
def mgr():
    return LlamaCppManager(db=MagicMock(), config={"mutual_auth_enabled": True})


@pytest.fixture
def dep():
    return SimpleNamespace(id=7, name="qwen-rpc", k8s_namespace="waddleai", model_filename="m.gguf",
                           model_url="https://h/m.gguf", n_ctx=8192, status="pending")


def kind(mgr, dep, k):
    return [d for d in mgr.export_rpc_manifests(dep, layout(), HOSTS, IMG, 8192) if d["kind"] == k]


```

Same file, continued (same imports and fixtures):

```python
def test_head_pinned_to_planned_node(mgr, dep):
    spec = kind(mgr, dep, "Deployment")[0]["spec"]["template"]["spec"]
    assert spec["nodeSelector"] == {"kubernetes.io/hostname": "gpu-a1"}
    assert spec["containers"][0]["resources"]["limits"]["nvidia.com/rtx4090"] == "2"


def test_head_args(mgr, dep):
    args = kind(mgr, dep, "Deployment")[0]["spec"]["template"]["spec"]["containers"][0]["args"]
    assert len(args[args.index("--rpc") + 1].split(",")) == 2
    assert args[args.index("--n-gpu-layers") + 1] == "999"
    assert args[args.index("--api-key-file") + 1] == "/secrets/api-key"
    assert args[args.index("--tensor-split") + 1] == "0.25,0.25,0.5"


def test_one_statefulset_per_worker_node(mgr, dep):
    sts = kind(mgr, dep, "StatefulSet")
    assert len(sts) == 1 and sts[0]["spec"]["replicas"] == 2
    assert sts[0]["spec"]["template"]["spec"]["nodeSelector"] == {"kubernetes.io/hostname": "gpu-a2"}


def test_worker_runs_rpc_server_hardened(mgr, dep):
    c = kind(mgr, dep, "StatefulSet")[0]["spec"]["template"]["spec"]["containers"][0]
    assert c["args"][:2] == ["-H", "0.0.0.0"] and c["args"][c["args"].index("-p") + 1] == "50052"
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["capabilities"] == {"drop": ["ALL"]}


```

Same file, continued (same imports and fixtures):

```python
def test_worker_ingress_only_from_head(mgr, dep):
    rule = kind(mgr, dep, "CiliumNetworkPolicy")[0]["spec"]["ingress"][0]
    assert rule["fromEndpoints"] == [{"matchLabels": {
        "waddleai.penguintech.io/llamacpp-deployment": mgr._daemonset_name(dep.name),
        "waddleai.penguintech.io/llamacpp-role": "head"}}]
    assert rule["authentication"] == {"mode": "required"}


def test_services_not_exposed(mgr, dep):
    for svc in kind(mgr, dep, "Service"):
        assert svc["spec"].get("type", "ClusterIP") == "ClusterIP"


def test_head_image_is_rpc_image(mgr, dep):
    img = kind(mgr, dep, "Deployment")[0]["spec"]["template"]["spec"]["containers"][0]["image"]
    assert img == IMG and img != LLAMACPP_IMAGE


```

Same file, continued (same imports and fixtures):

```python
def test_mutual_auth_disabled_no_k8s_calls(dep):
    m = LlamaCppManager(db=MagicMock(), config={"mutual_auth_enabled": False})
    with patch(f"{MGR}.get_k8s_apps_client") as apps:
        with pytest.raises(RpcRefused) as exc:
            m.deploy_rpc(dep, layout(), HOSTS, IMG)
    assert str(exc.value) == RPC_AUTH_REFUSAL and apps.call_count == 0


def test_tensor_split_length_mismatch_refused(mgr, dep):
    bad = layout()
    bad.tensor_split = bad.tensor_split[:-1]
    with pytest.raises(ValueError, match="tensor_split length"):
        mgr.export_rpc_manifests(dep, bad, HOSTS, IMG, 8192)


def test_deploy_rollback_on_failure(mgr, dep):
    apps, core, crd = MagicMock(), MagicMock(), MagicMock()
    apps.create_namespaced_stateful_set.side_effect = RuntimeError("quota")
    with patch(f"{MGR}.get_k8s_apps_client", return_value=apps), \
         patch(f"{MGR}.get_k8s_core_client", return_value=core), \
         patch(f"{MGR}.get_k8s_custom_client", return_value=crd):
        with pytest.raises(RuntimeError, match="quota"):
            mgr.deploy_rpc(dep, layout(), HOSTS, IMG)
    core.delete_namespaced_service.assert_called()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/management/test_llamacpp_rpc_manifests.py -v` → 10 FAILED with `AttributeError: 'LlamaCppManager' object has no attribute 'export_rpc_manifests'`.

- [ ] **Step 3: Implement** the constants, `export_rpc_manifests` (length check, auth check, objects above; reuse the existing hardened dicts from `export_k8s_manifest`), `deploy_rpc` (create order, rollback, row update), `get_k8s_custom_client` (`CustomObjectsApi`, same pattern as lines 41-64).

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/management/test_llamacpp_rpc_manifests.py -v` → `10 passed`. Run `pytest tests/unit/management/test_llamacpp_manager.py tests/unit/management/test_llamacpp_manager_hardening.py -q` → 0 failures (DaemonSet path unchanged).

- [ ] **Step 5: Commit**

```bash
git add services/management/app/services/llamacpp_manager.py tests/unit/management/test_llamacpp_rpc_manifests.py
git commit -m "feat(llamacpp): render RPC head, worker StatefulSets and per-deployment CNP" -m "<the executing session's attribution trailer>"
```

---

Part 2 (F4-F8, open items): `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-F2-llamacpp-rpc.md`.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/management/test_llamacpp_rpc_manifests.py -v` → 10 failed, `AttributeError: 'LlamaCppManager' object has no attribute 'export_rpc_manifests'` (or `deploy_rpc`).

- [ ] **Step 3: Implement**

- Constants and `resource_for` at module level near `LLAMACPP_IMAGE`.
- `LlamaCppManager.__init__` reads `mutual_auth_enabled` from `config`.
- `export_rpc_manifests` builds the objects in the Objects order above.
- `deploy_rpc` checks `mutual_auth_enabled` first, then runs the Deploy order with rollback.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/management/test_llamacpp_rpc_manifests.py -v` → `10 passed`, 0 failed.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/services/llamacpp_manager.py tests/unit/management/test_llamacpp_rpc_manifests.py
git commit -m "feat(llamacpp): RPC head, worker, service and CNP manifests" -m "<the executing session's attribution trailer>"
```
