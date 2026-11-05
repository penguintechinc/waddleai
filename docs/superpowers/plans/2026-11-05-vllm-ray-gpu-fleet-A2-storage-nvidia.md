# vLLM on Ray GPU Fleet — Sub-plan A: Infrastructure (Helm, images, CI, Tetragon) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the chart, images, network/runtime policy and CI wiring so that `fleet.vllmRay.enabled=true` renders a KubeRay `RayCluster` with one worker group per GPU vendor+class, a shared model store and NVIDIA per-model resource names, all PSA/Cilium/Tetragon-compliant, and nothing new renders when disabled.

**Spec:** `docs/superpowers/specs/2026-11-05-vllm-ray-gpu-fleet-design.md` (§4.1, §4.2, §4.5 step 6, §4.8, §6.2, §6.3, §6.4, §7, §8). Master plan: `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-00-master.md`.

Split: sibling plans `A1-helm` (Tasks A1, A2), `A1-helm-part2` (Tasks A2b, A2c) and `A3-netsec-images` (Tasks A5-A8).

Global Constraints, Review Focus, Deviations and version table: see `A1-helm`.

## Values block (exact; added to `k8s/helm/waddleai/values.yaml`)

Insert inside the top-level `fleet:` mapping, immediately before the `# Secret management` comment (currently values.yaml line 704; locate with `grep -n '# Secret management' k8s/helm/waddleai/values.yaml`). These key names are the contract that Plan B and Plan C read.

```yaml
  vllmRay:
    enabled: false                      # gated; default OFF
    ray:
      version: "2.61.0"                 # >= 2.61.0 (AM12 token auth default-on); identical across head and every worker image
      pythonMinor: "3.12"               # head and all workers must match
    images:
      cuda:  {repository: ghcr.io/penguintechinc/waddleai/vllm-ray-cuda, tag: "<tag: CI tag from the image build job; digest is authoritative>", digest: "<sha256: computed by executor via docker buildx imagetools inspect ghcr.io/penguintechinc/waddleai/vllm-ray-cuda:<tag>>"}
      rocm:  {repository: ghcr.io/penguintechinc/waddleai/vllm-ray-rocm, tag: "<tag: executor selects per the ROCm rule in A3-netsec-images>", digest: "<sha256: computed by executor via docker buildx imagetools inspect ghcr.io/penguintechinc/waddleai/vllm-ray-rocm:<tag>>"}
      xpu:   {repository: ghcr.io/penguintechinc/waddleai/vllm-ray-xpu,  tag: "<tag: CI tag from the image build job; digest is authoritative>", digest: "<sha256: computed by executor via docker buildx imagetools inspect ghcr.io/penguintechinc/waddleai/vllm-ray-xpu:<tag>>"}
      head:  {repository: rayproject/ray, tag: "2.61.0-py312", digest: "<sha256: computed by executor via docker buildx imagetools inspect rayproject/ray:2.61.0-py312>"}   # CPU head, same Ray and Python minor
    head:
      resources: {requests: {cpu: "2", memory: "4Gi"}, limits: {cpu: "4", memory: "8Gi"}}
      nodeSelector: {}                  # override to a storage node until compute nodes land (spec §7)
      affinity:                         # default: never on AI nodes
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - {key: waddleai.penguintech.io/ai-node, operator: NotIn, values: ["true"]}
    tco:                                # AM6 / spec §4.7 owner defaults; handed to the fleet backend config
      kwhUsd: 0.18
      lifecycleYears: 10
      utilizationPct: 50
      inputRatio: 0.2
      ramUsdPerGb: 1
      nvmeUsdPerGb: 1
      nvmeProUsdPerGb: 2
      networkDeviceUsd: 400
    workerGroups: []                    # required when enabled; render fails if empty
    # workerGroups[] entry: {name, vendor (nvidia|amd|intel), gpuClass, resourceName,
    #   gpusPerWorker (>=1), vramMbPerNode (int), minReplicas (0), maxReplicas (>=1),
    #   nodeSelector {}, tolerations [], image (images key: cuda|rocm|xpu)}
    #   cost {cardUsd, tdpWatts, estTokensPerS, cpuUsd, ramGb, nvmeGb, nvmePro (bool),
    #     networkDevices, nodeBaseWatts, gpusPerNode, nodeBaseUsd (optional explicit override)}
    #   cardUsd and tdpWatts are optional: unset = reference fallback (B4d `load_pricing_inputs`).
    nvidiaClassMap: []                  # [{pattern: "*RTX 4090*", name: rtx4090}]; empty = no renaming
    modelStore: {storageClass: "", size: 500Gi, accessMode: ReadWriteMany}
    hotCache:
      enabled: false                    # opt-in; owner approved (spec §8.1)
      hostPath: /var/lib/waddleai/models
      kyvernoPolicyException: false     # true = PolicyException for ray-worker pods instead of enforce=baseline (D5)
    nccl:
      ifname: ""                        # empty = pod primary interface
    serve: {port: 8000}
    dashboard: {port: 8265}
    ports: {gcs: 6379, client: 10001, workerMin: 10002, workerMax: 10199}   # D2
    autoscaler: {idleTimeoutSeconds: 300}
    rayInternalTls: true                # AM11: Ray TLS envs + cert-manager certs (A2b); default on
    restartGeneration: 1                    # bumped on pod-template annotation waddleai.penguintech.io/restart-generation on cert renewal or rotation (A2b)
  security:                             # AM11/AM15; merge into existing fleet.security if present
    mutualAuth: true                    # Cilium mutual auth (SPIRE) on fleet CNPs (A3 A5)
    wireguard: true                     # PREREQUISITE: Cilium WireGuard encryption; documented, not rendered
    rayAuthToken: {existingSecret: "", envSecret: ""}  # AM15: env WADDLEAI_RAY_AUTH_TOKEN from the Secret named in envSecret (key token) wins; then existingSecret (key token); else chart-generated (A2b)
    rayTls: {existingSecret: ""}        # AM15: keys tls.crt, tls.key, ca.crt; else chart-generated (A2b)
    tokenEncryptionKey: {existingSecret: ""}  # AM15: key `fernet`; env WADDLEAI_FLEET_TOKEN_ENCRYPTION_KEY wins
```

### Task A3: NVIDIA per-class resource renaming

**Files:**
- Create: `k8s/helm/waddleai/templates/fleet-vllm-ray/nvidia-device-plugin-config.yaml`
- Create: `k8s/helm/waddleai/templates/fleet-vllm-ray/fleet-backend-config.yaml`
- Modify: `k8s/helm/waddleai/templates/_helpers.tpl` (GPU request lines at ~245, ~341, ~347, ~462, ~558, ~564)
- Modify: `k8s/helm/waddleai/templates/llamacpp-daemonset.yaml` (GPU request lines at ~131, ~137)
- Modify: `k8s/helm/waddleai/values.yaml` (`ollama.gpu.nvidiaClass`, `llamacpp.gpu.nvidiaClass`, default `""`; gpu-operator devicePlugin config passthrough)
- Modify: `k8s/helm/waddleai/templates/management-deployment.yaml` (`WADDLEAI_VLLM_RAY_BACKEND_CONFIG` file path plus ConfigMap volume mount)
- Modify: `tests/helm/fixtures/vllm-ray-base.yaml` (`cost` block on the worker group; `nvidia-gpu-operator.devicePlugin.config.name`)
- Test: `tests/helm/test_vllm_ray_nvidia_render.py`

**Interfaces:**
- Consumes: A2 values `fleet.vllmRay.nvidiaClassMap`, A2 group `gpuClass` names.
- Produces: ConfigMap `waddleai-nvidia-device-plugin-config` in the gpu-operator namespace (`nvidia-gpu-operator.namespace`, default `gpu-operator`), key `config.yaml`. Helper `waddleai.gpuResourceName` in `_helpers.tpl`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/helm/test_vllm_ray_nvidia_render.py
"""NVIDIA per-model resource renaming (spec §4.2, A3)."""
from pathlib import Path

import pytest
import yaml

from tests.helm.conftest import find, render

BASE = str(Path(__file__).parent / "fixtures" / "vllm-ray-base.yaml")
OLLAMA_ON = {"ollama.enabled": "true", "ollama.models[0]": "llama3:8b"}
DP_CM = "waddleai-nvidia-device-plugin-config"


def _dp_config(docs):
    return yaml.safe_load(find(docs, "ConfigMap", DP_CM)["data"]["config.yaml"])


def test_renaming_absent_by_default():
    docs = render("values-alpha.yaml")
    assert not [d for d in docs if d["kind"] == "ConfigMap" and DP_CM in d["metadata"]["name"]]


def test_configmap_lists_one_entry_per_class_map_item():
    cfg = _dp_config(render(BASE))
    assert cfg["version"] == "v1" and cfg["resources"]["gpus"] == [{"pattern": "*RTX 4090*", "name": "rtx4090"}]


def test_two_models_on_one_node_render_two_distinct_names():
    docs = render(BASE, {"fleet.vllmRay.nvidiaClassMap[1].pattern": "*RTX 4070*", "fleet.vllmRay.nvidiaClassMap[1].name": "rtx4070"})
    assert [g["name"] for g in _dp_config(docs)["resources"]["gpus"]] == ["rtx4090", "rtx4070"]


def test_gpu_operator_device_plugin_config_name_required():
    with pytest.raises(AssertionError, match="devicePlugin.config.name"):
        render(BASE, {"nvidia-gpu-operator.devicePlugin.config.name": ""})
```

```python
def test_ollama_requests_class_resource_when_class_set():
    docs = render(BASE, {**OLLAMA_ON, "ollama.gpu.nvidiaClass": "rtx4090"})
    limits = find(docs, "DaemonSet", "waddleai-ollama")["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
    assert "nvidia.com/rtx4090" in limits and "nvidia.com/gpu" not in limits


def test_ollama_keeps_plain_gpu_when_renaming_off():
    docs = render("values-alpha.yaml", {**OLLAMA_ON, "ollama.gpu.enabled": "true"})
    limits = find(docs, "DaemonSet", "waddleai-ollama")["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
    assert "nvidia.com/gpu" in limits


def test_renaming_active_without_class_fails_render():
    with pytest.raises(AssertionError, match="nvidiaClass"):
        render(BASE, OLLAMA_ON)


def test_llamacpp_requests_class_resource_when_class_set():
    docs = render(BASE, {"llamacpp.enabled": "true", "llamacpp.gpu.enabled": "true", "llamacpp.gpu.nvidiaClass": "rtx4090",
                         "llamacpp.models[0].name": "m", "llamacpp.models[0].url": "https://example.invalid/m.gguf",
                         "llamacpp.models[0].filename": "m.gguf"})
    sets = [d for d in docs if d["kind"] == "DaemonSet" and "llamacpp" in d["metadata"]["name"]]
    assert sets, "no llama.cpp DaemonSet rendered"
    assert all("nvidia.com/rtx4090" in d["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"] for d in sets)
```

- [ ] **Step 1b: Add the TCO/cost passthrough test (AM6)**

Append to `tests/helm/test_vllm_ray_nvidia_render.py`. Mechanism: the chart renders ConfigMap `waddleai-vllm-ray-fleet-config` (key `backend-config.json`) and the management Deployment reads it through env `WADDLEAI_VLLM_RAY_BACKEND_CONFIG` (file path to the mounted ConfigMap; Step 3d); Plan B seeds/refreshes the fleet backend config row from it.

```python
import json

TCO_DEFAULTS = {"kwh_usd": 0.18, "lifecycle_years": 10, "utilization_pct": 50, "input_ratio": 0.2,
                "ram_usd_per_gb": 1, "nvme_usd_per_gb": 1, "nvme_pro_usd_per_gb": 2, "network_device_usd": 400}
# AM9: Helm inputs are camelCase; the ConfigMap JSON the tests read back is snake_case.
COST_KEYS = {"card_usd", "tdp_watts", "est_tokens_per_s", "cpu_usd", "ram_gb", "nvme_gb",
             "nvme_pro", "network_devices", "node_base_watts", "gpus_per_node"}


def _backend_cfg(docs):
    return json.loads(find(docs, "ConfigMap", "waddleai-vllm-ray-fleet-config")["data"]["backend-config.json"])


def test_tco_defaults_and_cost_reach_backend_config():
    """regression: AM6 - tco defaults and workerGroups[].cost reach the backend config as snake_case (AM9)."""
    cfg = _backend_cfg(render(BASE))
    assert cfg["tco"] == TCO_DEFAULTS
    assert cfg["worker_groups"] and all(COST_KEYS <= set(g["cost"]) for g in cfg["worker_groups"])


def test_tco_overrides_and_optional_node_base_usd():
    cfg = _backend_cfg(render(BASE, {"fleet.vllmRay.tco.kwhUsd": "0.31", "fleet.vllmRay.workerGroups[0].cost.nodeBaseUsd": "4200"}))
    assert cfg["tco"]["kwh_usd"] == 0.31 and cfg["worker_groups"][0]["cost"]["node_base_usd"] == 4200
    assert "node_base_usd" not in _backend_cfg(render(BASE))["worker_groups"][0]["cost"]
```

```python
def test_management_mounts_backend_config_file():
    """AM20: env is a file path into a read-only ConfigMap volume mount, not configMapKeyRef."""
    mgmt = [d for d in render(BASE) if d["kind"] == "Deployment" and "management" in d["metadata"]["name"]][0]
    c = mgmt["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert env["WADDLEAI_VLLM_RAY_BACKEND_CONFIG"] == "/etc/waddleai/vllm-ray/backend-config.json"
    assert any(m["mountPath"] == "/etc/waddleai/vllm-ray" and m.get("readOnly") for m in c["volumeMounts"])


def test_no_backend_config_when_disabled():
    docs = render("values-alpha.yaml")
    assert not [d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "waddleai-vllm-ray-fleet-config"]
```

The `vllm-ray-base.yaml` fixture's worker groups must carry the full `cost` block (values listed in Step 3b); Step 3b adds it.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/helm/test_vllm_ray_nvidia_render.py -v`
Expected: 9 failed, 3 passed (regression guards: `test_renaming_absent_by_default`, `test_ollama_keeps_plain_gpu_when_renaming_off`, `test_no_backend_config_when_disabled`). `test_gpu_operator_device_plugin_config_name_required` asserts the key path confirmed in Step 3a and must never pass vacuously.

- [ ] **Step 3: Implement** (one sub-step per file)

- [ ] **Step 3a: `k8s/helm/waddleai/templates/fleet-vllm-ray/nvidia-device-plugin-config.yaml`**
- `nvidia-device-plugin-config.yaml`: gated on `vllmRay.enabled` AND non-empty `nvidiaClassMap`. ConfigMap `waddleai-nvidia-device-plugin-config` in `nvidia-gpu-operator.namespace`, key `config.yaml`: `version: v1`, `flags: {migStrategy: none}`, `resources.gpus` built from the map. Do not emit the default NVIDIA resource on AI nodes. E1 live check (not a render gate): confirm the pinned device plugin stops advertising `nvidia.com/gpu` in renamed mode; record the result in the PR. Render `fail` with a message containing `devicePlugin.config.name` when `nvidiaClassMap` is non-empty and `nvidia-gpu-operator.devicePlugin.config.name` is empty.
- Verify the gpu-operator key path: `helm show values k8s/helm/waddleai/charts/gpu-operator-v26.3.1.tgz | grep -n -A3 devicePlugin`. The output must contain a `devicePlugin` block with a `config.name` key; that path is the contract for `nvidia-gpu-operator.devicePlugin.config.name` (Step 3g).

- [ ] **Step 3b: `tests/helm/fixtures/vllm-ray-base.yaml`** — add the `cost` block to the worker group: `{cardUsd: 1800, tdpWatts: 450, estTokensPerS: 60, cpuUsd: 500, ramGb: 128, nvmeGb: 2000, nvmePro: true, networkDevices: 1, nodeBaseWatts: 150, gpusPerNode: 2}`; set `nvidia-gpu-operator.devicePlugin.config.name: waddleai-nvidia-device-plugin-config`.

- [ ] **Step 3c: `k8s/helm/waddleai/templates/fleet-vllm-ray/fleet-backend-config.yaml`**
- `fleet-backend-config.yaml`: gated on `vllmRay.enabled`. Also renders `link_auth: {mutual_auth: fleet.security.mutualAuth, internal_tls: fleet.vllmRay.rayInternalTls}` (AM11, Plan B B7). ConfigMap `waddleai-vllm-ray-fleet-config`, key `backend-config.json` = `toJson` of snake_case `{tco: {kwh_usd, lifecycle_years, utilization_pct, input_ratio, ram_usd_per_gb, nvme_usd_per_gb, nvme_pro_usd_per_gb, network_device_usd}, worker_groups: [{name, gpu_class, cost: {card_usd, tdp_watts, est_tokens_per_s, cpu_usd, ram_gb, nvme_gb, nvme_pro, network_devices, node_base_watts, gpus_per_node, node_base_usd (only when set)}}]}` (AM9; values read from the camelCase Helm keys); `cost.node_base_usd` only when set. The management Deployment mounts it per Step 3d. Fail render if `tco.kwhUsd <= 0`.
- [ ] **Step 3d: `k8s/helm/waddleai/templates/management-deployment.yaml`** — when `fleet.vllmRay.enabled`: ConfigMap volume `vllm-ray-config` from `waddleai-vllm-ray-fleet-config`, mounted read-only at `/etc/waddleai/vllm-ray/`; env `WADDLEAI_VLLM_RAY_BACKEND_CONFIG` set to the literal path `/etc/waddleai/vllm-ray/backend-config.json` (not `configMapKeyRef`).

- [ ] **Step 3e: `k8s/helm/waddleai/templates/_helpers.tpl`**
- `_helpers.tpl`: new define `waddleai.gpuResourceName`, called with `(dict "ctx" $ "class" <nvidiaClass> "type" <gpu.type>)`:
  - renaming active := `vllmRay.enabled` AND `nvidiaClassMap` non-empty AND type `nvidia`.
  - active and class empty: `fail` with a message containing `nvidiaClass`.
  - active and class set: `nvidia.com/<class>`.
  - inactive: `<type>.com/gpu`, which is today's behaviour.
  Replace the six `.com/gpu` lines in `_helpers.tpl` and the two in `llamacpp-daemonset.yaml` with calls to this helper. Values: Step 3g.
- [ ] **Step 3f: `k8s/helm/waddleai/templates/llamacpp-daemonset.yaml`** — replace the two `.com/gpu` request lines (~131, ~137) with `waddleai.gpuResourceName` calls.

- [ ] **Step 3g: `k8s/helm/waddleai/values.yaml`** — add `ollama.gpu.nvidiaClass` and `llamacpp.gpu.nvidiaClass` (default `""`); set `nvidia-gpu-operator.devicePlugin.config.name: ""` with a comment that it must be `waddleai-nvidia-device-plugin-config` when `nvidiaClassMap` is non-empty.

- Run: `pytest tests/helm/test_vllm_ray_nvidia_render.py tests/helm/test_ollama_render.py -v`. Expected: 12 passed, 0 skipped in the new file; `test_ollama_render.py` reports 0 failed.

- [ ] **Step 4: Commit**

```bash
git add k8s/helm/waddleai/templates/fleet-vllm-ray/nvidia-device-plugin-config.yaml k8s/helm/waddleai/templates/fleet-vllm-ray/fleet-backend-config.yaml k8s/helm/waddleai/templates/management-deployment.yaml k8s/helm/waddleai/templates/_helpers.tpl k8s/helm/waddleai/templates/llamacpp-daemonset.yaml k8s/helm/waddleai/values.yaml tests/helm/fixtures/vllm-ray-base.yaml tests/helm/test_vllm_ray_nvidia_render.py
git commit -m "feat(helm): per-model NVIDIA resource renaming for vllm-ray AI nodes

+ the executing session's attribution trailer"
```

---

### Task A4: Model store PVC (RWX) and opt-in NVMe hot cache

**Files:**
- Create: `k8s/helm/waddleai/templates/fleet-vllm-ray/model-store-pvc.yaml`
- Create: `k8s/helm/waddleai/templates/fleet-vllm-ray/psa-namespace.yaml` (namespace PSA label set; D5)
- Create: `k8s/helm/waddleai/templates/fleet-vllm-ray/kyverno-policyexception.yaml` (opt-in, D5 option b)
- Modify: `k8s/helm/waddleai/templates/fleet-vllm-ray/raycluster.yaml` (worker `/models` mount; conditional hot-cache volume with the STATEFUL comment)
- Test: `tests/helm/test_vllm_ray_storage_render.py`

**Interfaces:**
- Consumes: A2 RayCluster; values `fleet.vllmRay.modelStore.*`, `fleet.vllmRay.hotCache.enabled`, `fleet.vllmRay.hotCache.kyvernoPolicyException`.
- Produces: PVC `waddleai-vllm-ray-models` (RWX). Worker volume `models` mounted at `/models`. When `hotCache.enabled`: hostPath volume `hot-cache` mounted at `/models-cache`, worker pods only, plus the namespace PSA labels (enforce=baseline) or, when `kyvernoPolicyException` is true, a PolicyException for Ray worker pods with enforce=restricted.

- [ ] **Step 1: Write the failing tests**

```python
# tests/helm/test_vllm_ray_storage_render.py
"""Model store PVC, PSA namespace label and opt-in hot cache (spec §6.4, A4; decision D5)."""
from pathlib import Path

from tests.helm.conftest import find, find_optional, render

BASE = str(Path(__file__).parent / "fixtures" / "vllm-ray-base.yaml")
TEMPLATE = Path(__file__).resolve().parents[2] / "k8s/helm/waddleai/templates/fleet-vllm-ray/raycluster.yaml"
HOT = {"fleet.vllmRay.hotCache.enabled": "true"}


def _rc(docs):
    return find(docs, "RayCluster", "waddleai-inference")


def _ns_labels(docs):
    return find(docs, "Namespace", "waddleai")["metadata"]["labels"]


def test_model_store_pvc_rwx_size_and_class():
    docs = render(BASE, {"fleet.vllmRay.modelStore.storageClass": "nfs-storage", "fleet.vllmRay.modelStore.size": "500Gi"})
    pvc = find(docs, "PersistentVolumeClaim", "waddleai-vllm-ray-models")
    assert pvc["spec"]["accessModes"] == ["ReadWriteMany"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "500Gi"
    assert pvc["spec"]["storageClassName"] == "nfs-storage"


def test_workers_mount_model_store_at_models():
    for g in _rc(render(BASE))["spec"]["workerGroupSpecs"]:
        mounts = {m["mountPath"]: m["name"] for m in g["template"]["spec"]["containers"][0]["volumeMounts"]}
        assert mounts["/models"] == "models"


def test_default_no_hostpath_no_policyexception_enforce_restricted():
    docs = render(BASE)
    assert "hostPath" not in str(_rc(docs))
    assert find_optional(docs, "PolicyException", "waddleai-vllm-ray-hot-cache") is None
    assert _ns_labels(docs)["pod-security.kubernetes.io/enforce"] == "restricted"
```

```python


def test_hot_cache_hostpath_on_workers_only_and_baseline_label():
    docs = render(BASE, HOT)
    rc = _rc(docs)["spec"]
    hp = [v for g in rc["workerGroupSpecs"] for v in g["template"]["spec"]["volumes"] if "hostPath" in v]
    assert hp and hp[0]["hostPath"]["path"] == "/var/lib/waddleai/models"
    assert "hostPath" not in str(rc["headGroupSpec"])
    labels = _ns_labels(docs)
    assert labels["pod-security.kubernetes.io/enforce"] == "baseline"
    assert labels["pod-security.kubernetes.io/warn"] == "restricted" and labels["pod-security.kubernetes.io/audit"] == "restricted"


def test_hot_cache_template_carries_stateful_exception_comment():
    assert "# STATEFUL EXCEPTION (approved)" in TEMPLATE.read_text()


def test_hot_cache_kyverno_policyexception_scoped_to_ray_workers():
    docs = render(BASE, {**HOT, "fleet.vllmRay.hotCache.kyvernoPolicyException": "true"})
    pe = find(docs, "PolicyException", "waddleai-vllm-ray-hot-cache")
    assert pe["spec"]["match"]["any"][0]["resources"]["selector"]["matchLabels"]["app.kubernetes.io/component"] == "ray-worker"
    assert _ns_labels(docs)["pod-security.kubernetes.io/enforce"] == "restricted"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/helm/test_vllm_ray_storage_render.py -v`
Expected: 6 failed, 0 passed (no PVC, no namespace PSA labels, no hostPath gate, no PolicyException, no template comment).

- [ ] **Step 3: Implement**

- [ ] **Step 3a: `k8s/helm/waddleai/templates/fleet-vllm-ray/model-store-pvc.yaml`**
- `model-store-pvc.yaml`: gated on `vllmRay.enabled`. `accessModes` from `modelStore.accessMode`. `storageClassName` from `modelStore.storageClass`, key omitted when empty. Size from `modelStore.size`.
- [ ] **Step 3b: `k8s/helm/waddleai/templates/fleet-vllm-ray/psa-namespace.yaml`**
- `psa-namespace.yaml`: gated on `vllmRay.enabled`. Renders the release namespace (`Namespace`, `helm.sh/resource-policy: keep`) with PSA labels. `warn` and `audit` are always `restricted`. `enforce` is `baseline` when `hotCache.enabled` is true and `restricted` otherwise, including when `kyvernoPolicyException` is true.
- [ ] **Step 3c: `k8s/helm/waddleai/templates/fleet-vllm-ray/raycluster.yaml`**
- Worker template: `models` volume from PVC `waddleai-vllm-ray-models`, mounted at `/models`.
- Hot cache, when `hotCache.enabled` is true: in `raycluster.yaml`, on the worker pod template only, a `# STATEFUL EXCEPTION (approved)` comment directly above the `hot-cache` volume (`hostPath`, `path: /var/lib/waddleai/models`, `type: DirectoryOrCreate`), mounted at `/models-cache`. Head pod has no hostPath.
- [ ] **Step 3d: `k8s/helm/waddleai/templates/fleet-vllm-ray/kyverno-policyexception.yaml`**
- `kyverno-policyexception.yaml`: gated on `hotCache.enabled` AND `hotCache.kyvernoPolicyException`. Renders `PolicyException` (`kyverno.io/v2`) named `waddleai-vllm-ray-hot-cache` in the release namespace. `spec.exceptions[].policyName` lists the verified Kyverno host-path policy names `<policy names: executor lists via kubectl get clusterpolicies.kyverno.io -o name on dal2beta>`. `spec.match.any[].resources.selector.matchLabels` is `app.kubernetes.io/component: ray-worker`, so only Ray worker pods are exempt.
- Run: `pytest tests/helm/test_vllm_ray_storage_render.py -v`. Expected: 6 passed, 0 skipped.

- [ ] **Step 4: Commit**

```bash
git add k8s/helm/waddleai/templates/fleet-vllm-ray/model-store-pvc.yaml k8s/helm/waddleai/templates/fleet-vllm-ray/psa-namespace.yaml k8s/helm/waddleai/templates/fleet-vllm-ray/kyverno-policyexception.yaml k8s/helm/waddleai/templates/fleet-vllm-ray/raycluster.yaml tests/helm/test_vllm_ray_storage_render.py
git commit -m "feat(helm): vllm-ray model store PVC and opt-in NVMe hot cache with PSA baseline or PolicyException (D5)

+ the executing session's attribution trailer"
```

---
