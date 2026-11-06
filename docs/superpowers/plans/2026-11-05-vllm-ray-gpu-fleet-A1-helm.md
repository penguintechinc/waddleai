# vLLM on Ray GPU Fleet — Sub-plan A: Infrastructure (Helm, images, CI, Tetragon) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the chart, images, network/runtime policy and CI wiring so that `fleet.vllmRay.enabled=true` renders a KubeRay `RayCluster` with one worker group per GPU vendor+class, a shared model store and NVIDIA per-model resource names, all PSA/Cilium/Tetragon-compliant, and nothing new renders when disabled.

**Architecture:** Helm-only changes under `k8s/helm/waddleai/` (new `templates/fleet-vllm-ray/` directory, new `fleet.vllmRay` values block, pinned KubeRay subchart), three rootless vendor images under `images/vllm-ray-{cuda,rocm,xpu}/`, a CI matrix job in `.github/workflows/docker-build.yml`, and Makefile targets. No application (Python/React) code is touched; the Python `VllmRayBackend` (Plan B) consumes the RayCluster this plan renders.

**Tech Stack:** Helm 3 (`helm template` render tests via pytest in `tests/helm/`), KubeRay operator chart, Ray Serve LLM + vLLM (pinned), Cilium `CiliumNetworkPolicy` v2 (`endPort`), Tetragon `TracingPolicyNamespaced`, Docker buildx, hadolint, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-11-05-vllm-ray-gpu-fleet-design.md` (§4.1, §4.2, §4.5 step 6, §4.8, §6.2, §6.3, §6.4, §7, §8). Master plan: `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-00-master.md`.

## Execution setup

- Branch and worktree (once, before A1): `git -C /home/penguin/code/waddleai worktree add .worktrees/vllm-ray-a -b feature/vllm-ray-a release/v0.4.X`. `.worktrees/` is already in `.gitignore`.
- One commit per task. Every commit message ends with the executing session's attribution trailer.
- Never commit to `release/v0.4.X`. Never push unless asked (branch backups follow `using-git-worktrees`).
- Render tests run with `pytest tests/helm -v` inside the worktree. They SKIP (not pass) if `helm` is absent. A skipped run is NOT green; report the skip count.

## Global Constraints

- Every dependency and image pinned to an exact version AND digest (`@sha256:`). Mutable tags (`latest`, floating tags) are forbidden in the new values. The `pinning-dependency-digests` skill is the lookup procedure.
- Rootless: `USER 1000` in every new image; `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault` on every new pod spec.
- The single `hostPath` exception is opt-in and tagged `# STATEFUL EXCEPTION (approved)` in the template (A4).
- No secrets in values, images or templates. HF tokens are out of scope for Plan A.
- Every new template is gated on `fleet.vllmRay.enabled` (default `false`). Disabled means zero new objects rendered.
- Ray version (`fleet.vllmRay.ray.version`) is identical across the head image, all worker images and every `ray[...]` pin. Python minor is identical across all four images.
- Amd64-first: CI builds `linux/amd64` only for the new images (owner directive, memory `amd64_first_arch_priority`).
- Helm values root is `fleet.vllmRay.*` per spec §6.2, plus the fields this plan adds (listed in the values block below) so Plan B reads the same names.

## Version pins (candidates; executor MUST verify before writing values)

Web research on 2026-11-05 was inconclusive. Every value below is a candidate. The executor fills the verified value and records the source in the commit body.

| Component | Candidate | Source / conflict | Verify with |
|---|---|---|---|
| KubeRay operator chart | `1.6.2` | Chainguard mirror lists 1.6.2; Hopsworks pins 1.4.0 (downstream, not authoritative) | `helm repo add kuberay https://ray-project.github.io/kuberay-helm/ && helm search repo kuberay/kuberay-operator --versions` |
| Ray (`ray.version`) | `2.61.0` (minimum; AM11/AM12) | Ray token auth is default-on from 2.61.0, and no pin below it is allowed. Candidate 2.55.0 is REPLACED. ray-llm tag must match; executor re-verifies the tag and digest | Docker Hub tag list for `rayproject/ray-llm` and `rayproject/ray`; `pip index versions ray` |
| Python minor, ALL images | `3.12` | ray-llm candidate tag is `py312`; spec §4.8 said `py311`, superseded by the tag that exists | Confirm the chosen base's python minor; head and all workers must match |
| vLLM in CUDA image (bundled in ray-llm) | `0.31.0` | One source (dated Oct 2026); PyPI not directly confirmed | `pip index versions vllm`; read the vLLM version inside the pinned ray-llm image |
| rocm/vllm base | candidate tag `rocm7.12.0_vllm_0.16.0_*` | Docker Hub shows ROCm 7.12.0 / vLLM 0.16.0 builds; its `latest` is a 2025 build, so NOT `latest` | hub.docker.com/r/rocm/vllm/tags; pick the tag whose vLLM matches the CUDA image (A7 risk) |
| Intel base | `intel/oneapi-basekit:2026.0.x` (candidate) | `vllm-xpu-kernels` PyPI pages require oneAPI 2026.0 | Docker Hub `intel/oneapi-basekit` tags |
| vLLM XPU kernels | `vllm-xpu-kernels==0.1.16.1` (candidate) | PyPI release list | pypi.org/project/vllm-xpu-kernels |

Digest command for each base image (executor runs it and pastes the digest into the values block and the Dockerfile `FROM` line):

```bash
docker buildx imagetools inspect <repo>:<tag> --format '{{json .Manifest}}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["digest"])'
```

Subchart checksum (A1): `sha256sum k8s/helm/waddleai/charts/kuberay-operator-<version>.tgz`.

## Review Focus

Five input classes the spec implies but no task's happy-path test exercises. Each is pinned to the task that owns it.

1. `fleet.vllmRay.enabled=false` (default) renders zero new objects (RayCluster, PVC, CNP, ConfigMap, Tetragon component, hostPath). Owned by A2; re-asserted in A3 to A6.
2. `hotCache.enabled=true` must never emit a hostPath that the namespace PSA rejects at admission: the namespace goes to enforce=baseline, or a PolicyException scoped to Ray worker pods is rendered when `hotCache.kyvernoPolicyException=true`. Owned by A4.
3. Renaming active (vllmRay enabled and `nvidiaClassMap` non-empty) with an Ollama or llama.cpp deployment lacking `nvidiaClass` fails the render. It must never emit a plain `nvidia.com/gpu` request that a renamed node cannot satisfy. Owned by A3.
4. A worker group whose `gpuClass` is not in `nvidiaClassMap` (NVIDIA vendor) or with `gpusPerWorker < 1` fails the render. Owned by A2.
5. Ray version or Python minor mismatch between head and worker images fails the render (tag check) and the image build (build-time assert). Owned by A2 (render) and A7 (build).


Split: sibling plans `A2-storage-nvidia` (Tasks A3-A4) and `A3-netsec-images` (Tasks A5-A8). Values block (needed by A2) moved to `A2-storage-nvidia`.

## Decisions made in this plan (deviations from spec text, with reasons)

- **D1. Ray network policy is separate; `topology.fleetPorts` is NOT changed.** Spec §6.2 says to add 8000/8265/6379/10001 to `topology.fleetPorts`. That list drives `waddleai-allow-fleet-ingress` on the Ollama and llama.cpp endpoints and is also read by the Python reconciler through `CILIUM_TOPOLOGY`. Adding Ray ports there would open them on Ollama pods. Ray gets its own `waddleai-vllm-ray-*` policies (A5).
- **D2. Worker port range is pinned.** Workers start with `--min-worker-port=10002 --max-worker-port=10199` (values `fleet.vllmRay.ports.workerMin/workerMax`), so one Cilium `endPort` range covers them instead of all ephemeral ports.
- **D3. `vramMbPerNode` added per worker group.** Spec §4.1 requires `vram_mb` as a Ray custom resource, but the §6.2 field list omits it. Added as `workerGroups[].vramMbPerNode` (int, required).
- **D4. Node selection by vendor label; class by resource request.** Spec §4.2's `gpu-classes` node label is a comma list and cannot be matched by `nodeSelector`. Worker `nodeSelector` = `waddleai.penguintech.io/gpu-vendor` plus `waddleai.penguintech.io/ai-node`. Class is enforced by the `nvidia.com/<class>` (or AMD/Intel) resource request.
- **D5. Hot-cache PSA exemption cannot be pod-scoped through PSA.** PSA exemptions are per namespace, user or runtimeClass (AdmissionConfiguration), and the namespace is always the product name (`deploying-app` rule). Decision (owner-approved opt-in): when `fleet.vllmRay.hotCache.enabled=true` the chart sets the product namespace label `pod-security.kubernetes.io/enforce=baseline` (keeping `warn`/`audit` at `restricted`) via the chart's namespace/PSA template, and documents this as `# STATEFUL EXCEPTION (approved)`; if Kyverno is detected (values `fleet.vllmRay.hotCache.kyvernoPolicyException=true`) the chart renders a PolicyException scoped to the Ray worker pods instead and leaves enforce=restricted. Render tests cover both.
- **D6. Worker pods reuse existing selector labels.** Worker pods carry `waddleai.selectorLabels` plus `app.kubernetes.io/component: ray-worker`, so the existing Tetragon exec-allowlist template and PSA labelling apply without new selector logic.

## Tasks

Task A2b (Ray token Secret, internal TLS, restart hook, external key precedence; AM11/AM13/AM15) is in `2026-11-05-vllm-ray-gpu-fleet-A1-helm-part2.md`, run after A2.

### Task A1: Pin and vendor the KubeRay operator subchart

**Files:**
- Modify: `k8s/helm/waddleai/Chart.yaml` (append to `dependencies:`, after the `intel-gpu-plugin` entry near line 46)
- Create: `k8s/helm/waddleai/charts/kuberay-operator-<version>.tgz` (vendored with `helm pull`)
- Modify: `k8s/helm/waddleai/Chart.lock` (regenerated by `helm dependency update`)
- Modify: `k8s/helm/waddleai/values.yaml` (top-level `kuberay-operator:` block)
- Test: `tests/helm/test_vllm_ray_subchart_render.py`

**Interfaces:**
- Consumes: none.
- Produces: dependency `kuberay-operator` with condition `fleet.vllmRay.enabled`; CRD `ray.io/v1` `RayCluster` installed from the subchart's `crds/`.

- [ ] **Step 1: Write the failing test**

```python
# tests/helm/test_vllm_ray_subchart_render.py
"""KubeRay subchart is pinned, vendored, and gated on fleet.vllmRay.enabled (A1)."""
import hashlib

from tests.helm.conftest import CHART_DIR, render

KUBERAY_VERSION = "1.6.2"                       # candidate; executor sets verified value
KUBERAY_TGZ_SHA256 = "<sha256: computed by executor via sha256sum k8s/helm/waddleai/charts/kuberay-operator-1.6.2.tgz>"


def test_kuberay_tgz_is_vendored_and_matches_pinned_digest():
    """Archive exists and its sha256 equals the recorded pin. A gate that can fail."""
    tgz = CHART_DIR / "charts" / f"kuberay-operator-{KUBERAY_VERSION}.tgz"
    assert tgz.exists(), f"missing vendored subchart {tgz}"
    assert hashlib.sha256(tgz.read_bytes()).hexdigest() == KUBERAY_TGZ_SHA256


def test_kuberay_operator_absent_when_disabled():
    """Default (vllmRay disabled): no kuberay-operator Deployment rendered."""
    docs = render("values-alpha.yaml")
    assert not [d for d in docs if d.get("metadata", {}).get("name", "").startswith("kuberay-operator")]


def test_kuberay_operator_rendered_when_enabled():
    """Enabling vllmRay renders exactly one operator Deployment from the pinned chart version."""
    docs = render("values-alpha.yaml", {"fleet.vllmRay.enabled": "true"})
    deploys = [d for d in docs if d["kind"] == "Deployment"
               and d["metadata"]["name"].startswith("kuberay-operator")]
    assert len(deploys) == 1
    assert deploys[0]["metadata"]["labels"]["helm.sh/chart"] == f"kuberay-operator-{KUBERAY_VERSION}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/helm/test_vllm_ray_subchart_render.py -v`
Expected: 2 failed, 1 passed (`test_kuberay_operator_absent_when_disabled` passes on the disabled path), 0 skipped. `helm` must be installed; a skip is not a pass.

- [ ] **Step 3: Vendor the subchart and record the pin**

```bash
helm repo add kuberay https://ray-project.github.io/kuberay-helm/ && helm repo update kuberay
helm pull kuberay/kuberay-operator --version 1.6.2 -d k8s/helm/waddleai/charts/
sha256sum k8s/helm/waddleai/charts/kuberay-operator-1.6.2.tgz
```
Expected: the tgz exists and the sha256 is printed. Paste it into `KUBERAY_TGZ_SHA256`. If `helm search repo kuberay/kuberay-operator --versions` does not list 1.6.2, stop and report; do not substitute a version.

- [ ] **Step 4: Wire the dependency, then run the tests**

- [ ] **Step 4a: `k8s/helm/waddleai/Chart.yaml`** — append under `dependencies:` (after the `intel-gpu-plugin` entry):

```yaml
  # KubeRay operator, vendored and pinned. Enable via fleet.vllmRay.enabled=true.
  - name: kuberay-operator
    version: "1.6.2"
    repository: "https://ray-project.github.io/kuberay-helm/"
    condition: fleet.vllmRay.enabled
```

- [ ] **Step 4b: `k8s/helm/waddleai/values.yaml`** — add a top-level `kuberay-operator:` block that watches only its own namespace; comment that its CRDs ship in the subchart's `crds/`.

- [ ] **Step 4c: `k8s/helm/waddleai/Chart.lock`** — run `helm dependency update k8s/helm/waddleai`, then confirm the Chart.lock digest matches the vendored tgz. Run: `pytest tests/helm/test_vllm_ray_subchart_render.py -v`. Expected: 3 passed, 0 skipped.

- [ ] **Step 5: Commit**

```bash
git add k8s/helm/waddleai/Chart.yaml k8s/helm/waddleai/Chart.lock k8s/helm/waddleai/charts/kuberay-operator-1.6.2.tgz k8s/helm/waddleai/values.yaml tests/helm/test_vllm_ray_subchart_render.py
git commit -m "feat(helm): vendor pinned KubeRay operator subchart behind fleet.vllmRay.enabled

+ the executing session's attribution trailer"
```

---

### Task A2: RayCluster `waddleai-inference` from values

**Files:**
- Create: `k8s/helm/waddleai/templates/fleet-vllm-ray/raycluster.yaml`
- Create: `k8s/helm/waddleai/templates/fleet-vllm-ray/deployment-keys-secret.yaml`
- Modify: `k8s/helm/waddleai/values.yaml` (insert the A2-storage-nvidia values block inside `fleet:`)
- Create: `tests/helm/fixtures/vllm-ray-base.yaml` (enabled, one NVIDIA group, `ray.version`, image tags from the version table, fixed test digest `sha256:` + 64 zeros, render-only)
- Modify: `tests/helm/conftest.py` (add `find_optional(docs, kind, name)`: returns `None` when no object matches; `find` keeps asserting exactly one)
- Test: `tests/helm/test_vllm_ray_render.py`

**Interfaces:**
- Consumes: A1 (`ray.io/v1` CRD via subchart); values block above.
- Produces: `RayCluster` with `metadata.name: waddleai-inference` in `.Values.namespace`. Labels: `ray.io/cluster: waddleai-inference`. Head pods: `ray.io/node-type: head`. Worker pods: `ray.io/node-type: worker`, `ray.io/group: <workerGroups[].name>`, `app.kubernetes.io/component: ray-worker`. Head Service address `waddleai-inference-head-svc` on 8265 and 8000 is consumed by Plan B.
- **Key convention (AM9).** Helm values stay camelCase (`fleet.vllmRay.tco.*`, `workerGroups[].cost.*`). Everything past the chart boundary is snake_case: the rendered ConfigMap JSON `waddleai-vllm-ray-fleet-config` (`tco.*`, `worker_groups[].cost.*`, rendered by `fleet-backend-config.yaml` in `A2-storage-nvidia`) and the Plan B backend config it seeds.

- [ ] **Step 1: Write the failing tests**

```python
# tests/helm/test_vllm_ray_render.py
from pathlib import Path

import pytest

from tests.helm.conftest import find, find_optional, render

BASE = str(Path(__file__).parent / "fixtures" / "vllm-ray-base.yaml")

def _rc(docs):
    return find(docs, "RayCluster", "waddleai-inference")

def _env(container):
    return {e["name"]: e.get("value") for e in container.get("env", [])}

def _group(i, name, vendor, res, image, vram):
    p = f"fleet.vllmRay.workerGroups[{i}]."
    return {p + "name": name, p + "vendor": vendor, p + "gpuClass": name, p + "resourceName": res,
            p + "gpusPerWorker": "1", p + "vramMbPerNode": vram, p + "maxReplicas": "1", p + "image": image}

def test_disabled_renders_no_raycluster():
    assert find_optional(render("values-alpha.yaml"), "RayCluster", "waddleai-inference") is None

def test_one_nvidia_group_renders_class_limit():
    """minReplicas 0, maxReplicas 2; limit nvidia.com/rtx4090 = gpusPerWorker."""
    wg = _rc(render(BASE))["spec"]["workerGroupSpecs"]
    assert len(wg) == 1 and wg[0]["minReplicas"] == 0 and wg[0]["maxReplicas"] == 2
    assert wg[0]["template"]["spec"]["containers"][0]["resources"]["limits"]["nvidia.com/rtx4090"] == 1

def test_three_vendor_groups_render_in_order():
    docs = render(BASE, {**_group(1, "amd-mi300", "amd", "amd.com/gpu", "rocm", "196608"),
                         **_group(2, "intel-b580", "intel", "gpu.intel.com/i915", "xpu", "12288")})
    groups = _rc(docs)["spec"]["workerGroupSpecs"]
    assert [g["groupName"] for g in groups] == ["nvidia-rtx4090", "amd-mi300", "intel-b580"]
    assert "amd.com/gpu" in groups[1]["template"]["spec"]["containers"][0]["resources"]["limits"]
```

```python


def test_worker_advertises_vendor_class_vram_resources():
    res = _rc(render(BASE))["spec"]["workerGroupSpecs"][0]["rayStartParams"]["resources"]
    assert "gpu_vendor:nvidia" in res and "gpu_class:nvidia-rtx4090" in res and "vram_mb" in res


def test_nccl_and_hf_env_on_every_worker():
    for g in _rc(render(BASE, {"fleet.vllmRay.nccl.ifname": "eth0"}))["spec"]["workerGroupSpecs"]:
        env = _env(g["template"]["spec"]["containers"][0])
        assert env["NCCL_SOCKET_NTHREADS"] == "4" and env["NCCL_NSOCKS_PERTHREAD"] == "4"
        assert env["NCCL_IB_DISABLE"] == "1" and env["NCCL_SOCKET_IFNAME"] == "eth0"
        assert env["HF_HOME"] == "/models"


def test_worker_security_context_rootless():
    spec = _rc(render(BASE))["spec"]["workerGroupSpecs"][0]["template"]["spec"]
    sc = spec["containers"][0]["securityContext"]
    assert sc["runAsNonRoot"] is True and sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"] and spec["securityContext"]["runAsUser"] == 1000
    assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"


def test_head_no_gpu_and_avoids_ai_nodes():
    head = _rc(render(BASE))["spec"]["headGroupSpec"]
    assert "nvidia.com" not in str(head["template"]["spec"]["containers"][0].get("resources", {}))
    assert head["rayStartParams"]["num-gpus"] == "0"
    assert "waddleai.penguintech.io/ai-node" in str(head["template"]["spec"]["affinity"])
```

```python


def test_head_ports_gcs_dashboard_client_serve():
    head = _rc(render(BASE))["spec"]["headGroupSpec"]
    ports = {p["name"]: p["containerPort"] for p in head["template"]["spec"]["containers"][0]["ports"]}
    assert ports == {"gcs": 6379, "dashboard": 8265, "client": 10001, "serve": 8000}


def test_no_hostpath_by_default():
    assert "hostPath" not in str(_rc(render(BASE)))


def test_ray_version_identical_across_images():
    rc = _rc(render(BASE))["spec"]
    images = [rc["headGroupSpec"]["template"]["spec"]["containers"][0]["image"]] + [
        g["template"]["spec"]["containers"][0]["image"] for g in rc["workerGroupSpecs"]]
    assert all("2.61.0" in i for i in images), images


def test_invalid_gpus_per_worker_fails_render():
    with pytest.raises(AssertionError, match="gpusPerWorker"):
        render(BASE, {"fleet.vllmRay.workerGroups[0].gpusPerWorker": "0"})


def test_empty_worker_groups_fails_render():
    with pytest.raises(AssertionError, match="workerGroups"):
        render(BASE, {"fleet.vllmRay.workerGroups": "[]"})


def test_deployment_keys_mount_on_every_worker_empty_secret():
    docs = render(BASE)
    for g in _rc(docs)["spec"]["workerGroupSpecs"]:
        spec = g["template"]["spec"]
        assert {"name": "deployment-keys", "secret": {"secretName": "waddleai-vllm-deployment-keys", "optional": True}} in spec["volumes"]
        assert {"name": "deployment-keys", "mountPath": "/secrets/deployment-keys/", "readOnly": True} in spec["containers"][0]["volumeMounts"]
    assert not find(docs, "Secret", "waddleai-vllm-deployment-keys").get("data")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/helm/test_vllm_ray_render.py -v`
Expected: collection error `cannot import name 'find_optional'` (0 tests run) until Step 3c lands. After Step 3c and before Step 3d: 12 failed, 1 passed (`test_disabled_renders_no_raycluster`). The two `pytest.raises(..., match=...)` tests need the template's `fail` messages to contain `gpusPerWorker` and `workerGroups`; those words are part of the contract.

- [ ] **Step 3: Implement** (one sub-step per file)

- [ ] **Step 3a: `k8s/helm/waddleai/values.yaml`** — insert the `A2-storage-nvidia` values block inside `fleet:`.

- [ ] **Step 3b: `tests/helm/fixtures/vllm-ray-base.yaml`** — create with `fleet.vllmRay.enabled: true`, `ray.version: "2.61.0"`, one worker group `{name: nvidia-rtx4090, vendor: nvidia, gpuClass: nvidia-rtx4090, resourceName: nvidia.com/rtx4090, gpusPerWorker: 1, vramMbPerNode: 98304, minReplicas: 0, maxReplicas: 2, image: cuda}`, and `nvidiaClassMap: [{pattern: "*RTX 4090*", name: rtx4090}]`. Image tags come from the version table; every digest is the fixed test value `sha256:` + 64 zeros (render-only, never pulled).

- [ ] **Step 3c: `tests/helm/conftest.py`** — add `find_optional` (see Files block).

- [ ] **Step 3d: `k8s/helm/waddleai/templates/fleet-vllm-ray/raycluster.yaml`** — the bullets below apply to this file.
- `templates/fleet-vllm-ray/raycluster.yaml`, gated on `.Values.fleet.vllmRay.enabled`. Validation at top, using Helm `fail` with messages that contain `workerGroups` (empty list), `gpusPerWorker` (below 1) and `gpuClass` (NVIDIA group class not present in `nvidiaClassMap`).
- Shape, decisions only: `apiVersion: ray.io/v1`, `kind: RayCluster`, `metadata.name: waddleai-inference`, `spec.rayVersion` from `ray.version`, `spec.enableInTreeAutoscaling: true`, `spec.autoscalerOptions.idleTimeoutSeconds`.
- `headGroupSpec`: `serviceType: ClusterIP`; `rayStartParams`: `dashboard-host 0.0.0.0`, `dashboard-port`, `port 6379`, `ray-client-server-port 10001`, `num-cpus 0`, `num-gpus 0`, `include-dashboard true`; pod spec from `head.*` values plus the affinity default.
- One `workerGroupSpecs[]` per entry: `groupName` = `workerGroups[].name`, `minReplicas`, `maxReplicas`; `rayStartParams.resources` is a JSON string containing `"gpu_vendor:<v>": 1`, `"gpu_class:<c>": 1`, `"vram_mb": <vramMbPerNode>`; `rayStartParams.min-worker-port` and `max-worker-port` from D2. Pod spec: `nodeSelector` from values plus `waddleai.penguintech.io/gpu-vendor: <vendor>` (D4), tolerations from values, pod `securityContext` (`runAsUser: 1000`, `runAsNonRoot: true`, `fsGroup: 1000`, `seccompProfile: RuntimeDefault`), container `securityContext` (`allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `readOnlyRootFilesystem: true`), `resources.limits[<resourceName>]: gpusPerWorker`.
- Env: the three NCCL vars (`NCCL_SOCKET_NTHREADS=4`, `NCCL_NSOCKS_PERTHREAD=4`, `NCCL_IB_DISABLE=1`), `NCCL_SOCKET_IFNAME` only when `nccl.ifname` is non-empty, `HF_HOME=/models`.
- Volumes: `/tmp` and `/dev/shm` as emptyDir, `/dev/shm` with `medium: Memory`. `/models` from the model-store PVC (A4). AM16: `deployment-keys` Secret mounted read-only at `/secrets/deployment-keys/` on every worker.
- Pod labels: `waddleai.selectorLabels`, `app.kubernetes.io/component: ray-worker`, `ray.io/group`.
- Image reference: `repository@sha256:digest`. The digest is mandatory: `fail` when it is empty. Render-time `fail` when the head or any worker tag does not contain `ray.version`.

- [ ] **Step 3e: `k8s/helm/waddleai/templates/fleet-vllm-ray/deployment-keys-secret.yaml`**
- `deployment-keys-secret.yaml` (gated `vllmRay.enabled`): Secret `waddleai-vllm-deployment-keys`, `Opaque`, `helm.sh/resource-policy: keep`, no data; `lookup` re-emits existing `data` on upgrade (only C2c writes keys).
- Run: `pytest tests/helm/test_vllm_ray_render.py -v`. Expected: 13 passed, 0 skipped.

- [ ] **Step 4: Verify the disabled path is unchanged**

Run `helm template waddleai k8s/helm/waddleai -f k8s/helm/waddleai/values-alpha.yaml | wc -l` before Step 3 and after it. Expected: the same integer both times (the disabled path adds nothing).

- [ ] **Step 5: Commit**

```bash
git add k8s/helm/waddleai/templates/fleet-vllm-ray/raycluster.yaml k8s/helm/waddleai/templates/fleet-vllm-ray/deployment-keys-secret.yaml k8s/helm/waddleai/values.yaml tests/helm/fixtures/vllm-ray-base.yaml tests/helm/test_vllm_ray_render.py tests/helm/conftest.py
git commit -m "feat(helm): render KubeRay RayCluster waddleai-inference from fleet.vllmRay values

+ the executing session's attribution trailer"
```

---
