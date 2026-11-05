# vLLM on Ray GPU Fleet — Sub-plan A: Infrastructure (Helm, images, CI, Tetragon) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the chart, images, network/runtime policy and CI wiring so that `fleet.vllmRay.enabled=true` renders a KubeRay `RayCluster` with one worker group per GPU vendor+class, a shared model store and NVIDIA per-model resource names, all PSA/Cilium/Tetragon-compliant, and nothing new renders when disabled.

**Spec:** `docs/superpowers/specs/2026-11-05-vllm-ray-gpu-fleet-design.md` (§4.1, §4.2, §4.5 step 6, §4.8, §6.2, §6.3, §6.4, §7, §8). Master plan: `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-00-master.md`.

Split: sibling plans `A1-helm` (Tasks A1, A2), `A1-helm-part2` (Tasks A2b, A2c) and `A2-storage-nvidia` (Tasks A3-A4). Task order in this file: A5, A7, A6, A8 (A6 runs after A7; IDs unchanged).

Global Constraints, Review Focus, Deviations and version table: see `A1-helm`.

### Task A5: Cilium policies for the Ray cluster

**Files:**
- Create: `k8s/helm/waddleai/templates/fleet-vllm-ray/cilium-policy.yaml`
- Test: `tests/helm/test_vllm_ray_network_render.py`

**Interfaces:**
- Consumes: A2 labels (`ray.io/cluster: waddleai-inference`, `ray.io/node-type: head|worker`); `cilium.topology.selectors.management` and `.aiproxy`; `fleet.vllmRay.ports.*`.
- Produces: `CiliumNetworkPolicy` `waddleai-vllm-ray-head` and `waddleai-vllm-ray-worker`. `topology.fleetPorts` is NOT changed (D1).

- [ ] **Step 1: Write the failing tests**

```python
# tests/helm/test_vllm_ray_network_render.py
"""Ray cluster network policy (spec §6.3, A5). Peers are asserted exactly."""
from pathlib import Path

from tests.helm.conftest import find, render

BASE = str(Path(__file__).parent / "fixtures" / "vllm-ray-base.yaml")
CILIUM = ["cilium.io/v2"]


def _ports(rule):
    return [p["port"] for tp in rule["toPorts"] for p in tp["ports"]]


def _cnp(name, api=CILIUM):
    return find(render(BASE, api_versions=api), "CiliumNetworkPolicy", name)


def test_head_ingress_is_exactly_management_aiproxy_and_workers():
    mgmt, proxy, workers = _cnp("waddleai-vllm-ray-head")["spec"]["ingress"]
    assert mgmt["fromEndpoints"][0]["matchLabels"]["app.kubernetes.io/component"] == "management"
    assert _ports(mgmt) == ["6379", "8265", "10001", "8000"]
    assert proxy["fromEndpoints"][0]["matchLabels"]["app.kubernetes.io/component"] == "proxy"
    assert _ports(proxy) == ["8000"]
    assert workers["fromEndpoints"][0]["matchLabels"]["ray.io/node-type"] == "worker"


def test_worker_ingress_only_from_head_and_workers_of_same_cluster():
    cnp = _cnp("waddleai-vllm-ray-worker")
    peers = [dict(fe["matchLabels"]) for r in cnp["spec"]["ingress"] for fe in r["fromEndpoints"]]
    assert peers and all(p.get("ray.io/cluster") == "waddleai-inference" for p in peers)
    assert {p["ray.io/node-type"] for p in peers} == {"head", "worker"}


def test_worker_port_range_uses_endport():
    cnp = _cnp("waddleai-vllm-ray-worker")
    ranges = [p for r in cnp["spec"]["ingress"] for tp in r["toPorts"] for p in tp["ports"] if "endPort" in p]
    assert ranges and ranges[0]["port"] == "10002" and ranges[0]["endPort"] == 10199
```

```python
def test_no_world_or_ingress_sources():
    for name in ("waddleai-vllm-ray-head", "waddleai-vllm-ray-worker"):
        text = str(_cnp(name))
        assert "world" not in text and "ingress" not in text.lower()


def test_fleet_ports_unchanged():
    fleet = _cnp("waddleai-allow-fleet-ingress", ["cilium.io/v2"])
    ports = [p for r in fleet["spec"]["ingress"] for p in _ports(r)]
    assert "8265" not in ports and "6379" not in ports


def test_mutual_auth_block_toggle():
    on = _cnp("waddleai-vllm-ray-head")["spec"]["ingress"]
    off = find(render(BASE, {"fleet.security.mutualAuth": "false"}, api_versions=CILIUM),
               "CiliumNetworkPolicy", "waddleai-vllm-ray-head")["spec"]["ingress"]
    assert all(r.get("authentication") == {"mode": "required"} for r in on)
    assert all("authentication" not in r for r in off)


def test_policies_skipped_without_cilium_crd():
    docs = render(BASE)
    assert not [d for d in docs if d["kind"] == "CiliumNetworkPolicy" and "vllm-ray" in d["metadata"]["name"]]
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/helm/test_vllm_ray_network_render.py -v`. Expected: 5 failed, 2 passed (`test_fleet_ports_unchanged` and `test_policies_skipped_without_cilium_crd` are regression guards that pass before the change).

- [ ] **Step 3: Implement**

- Guard: `vllmRay.enabled` AND `cilium.enabled` AND `cilium.networkPolicy.enabled` AND `.Capabilities.APIVersions.Has "cilium.io/v2"`, the same guard the external-fleet template uses.
- Head policy: `endpointSelector` matches `ray.io/cluster: waddleai-inference` and `ray.io/node-type: head`. Rule 1 from `topology.selectors.management` on TCP 6379, 8265, 10001, 8000. Rule 2 from `topology.selectors.aiproxy` on TCP 8000. Rule 3 from `{ray.io/cluster: waddleai-inference, ray.io/node-type: worker}` on TCP 6379, 10001, and the 10002 to 10199 `endPort` range.
- Worker policy: `endpointSelector` matches `ray.io/cluster: waddleai-inference` and `ray.io/node-type: worker`. Ingress from the head and from workers, both admitting the worker range with `endPort`. Add all-TCP between workers of the same cluster, because NCCL uses ephemeral ports. Record that decision in a template comment. No egress rules added; default-deny stays, since workers do not download from the internet (spec §4.5 step 2).
- **Mutual auth (AM11, spec §6.3 layer 1).** When `fleet.security.mutualAuth` is true, every rule in both policies carries `authentication: {mode: required}`; when false the key is absent. Test: `test_mutual_auth_block_toggle` (Step 1 fence).
- Assumption to verify in E1: the pinned Cilium version supports `endPort`.
- Run: `pytest tests/helm/test_vllm_ray_network_render.py -v`. Expected: 7 passed, 0 skipped.

- [ ] **Step 4: Commit**

```bash
git add k8s/helm/waddleai/templates/fleet-vllm-ray/cilium-policy.yaml tests/helm/test_vllm_ray_network_render.py
git commit -m "feat(helm): cilium policies for vllm-ray head and worker pods

+ the executing session's attribution trailer"
```

---

### Task A7: Three vendor images and CI build matrix

**Files:**
- Create: `images/vllm-ray-cuda/Dockerfile`
- Create: `images/vllm-ray-rocm/Dockerfile`, `images/vllm-ray-rocm/requirements.txt` (hash-pinned)
- Create: `images/vllm-ray-xpu/Dockerfile`, `images/vllm-ray-xpu/requirements.txt` (hash-pinned)
- Create: `tests/images/test_vllm_ray_images.sh`
- Modify: `.github/workflows/docker-build.yml` (new job `build-vllm-ray-images`, modelled on `build-ollama-image` at ~line 1171)
- Test: `tests/images/test_vllm_ray_images.sh` plus hadolint through `make lint`

**Interfaces:**
- Consumes: the version table above; A2 `images.{cuda,rocm,xpu,head}`.
- Produces: images with `ARG RAY_VERSION` and a build-time assert that `ray.__version__` equals it. `USER 1000`. No `ENTRYPOINT` override (KubeRay supplies the start command).

- [ ] **Step 1: Write the failing test**

```bash
#!/usr/bin/env bash
# tests/images/test_vllm_ray_images.sh: image contract gate (A7).
# Usage: test_vllm_ray_images.sh <image-ref> <ray-version> <py-minor>
# Fails loud on any mismatch. Zero checks run is a failure.
set -euo pipefail
img="$1"; ray_ver="$2"; py_minor="$3"

uid=$(docker run --rm --entrypoint /usr/bin/id "$img" -u)
[ "$uid" = "1000" ] || { echo "FAIL: uid=$uid want 1000"; exit 1; }

got=$(docker run --rm --entrypoint python3 "$img" -c 'import ray,sys;print(ray.__version__, "%d.%d" % sys.version_info[:2])')
[ "$got" = "$ray_ver $py_minor" ] || { echo "FAIL: got '$got' want '$ray_ver $py_minor'"; exit 1; }

for b in ray python3 bash nvidia-smi rocm-smi xpu-smi; do
  if docker run --rm --entrypoint sh "$img" -c "command -v $b" >/dev/null 2>&1; then
    echo "found $b"
  else
    echo "absent $b"   # vendor-specific tools may be absent; A6 allowlist uses only the found set
  fi
done
echo "PASS: $img uid=1000 ray=$ray_ver py=$py_minor"
```

- [ ] **Step 2: Run test to verify it fails.** Run: `bash tests/images/test_vllm_ray_images.sh waddleai/vllm-ray-cuda:dev 2.61.0 3.12`. Expected: non-zero exit from the first `docker run` (image not built or unpullable); no `PASS:` line printed.

- [ ] **Step 3: Implement** (one sub-step per file)

- [ ] **Step 3a: `images/vllm-ray-cuda/Dockerfile`**
- `vllm-ray-cuda/Dockerfile`: `ARG RAY_LLM_TAG=<tag: executor selects from Docker Hub rayproject/ray-llm, Ray 2.61.0, py312>` (build id is a candidate; executor re-verifies on Docker Hub, AM12), `FROM rayproject/ray-llm:${RAY_LLM_TAG}@<sha256: computed by executor via docker buildx imagetools inspect rayproject/ray-llm:${RAY_LLM_TAG}>`. `ARG RAY_VERSION=2.61.0`. `RUN python3 -c "import ray,os; assert ray.__version__==os.environ['RAY_VERSION']"` with `ENV RAY_VERSION` set from the ARG. `USER 1000`. No apt, no secrets.
- [ ] **Step 3b: `images/vllm-ray-rocm/Dockerfile`**
- `vllm-ray-rocm/Dockerfile`: `FROM rocm/vllm:<tag: executor selects per the ROCm rule below>@<sha256: computed by executor via docker buildx imagetools inspect rocm/vllm:<tag>>`. `COPY requirements.txt` and `pip install --no-cache-dir --require-hashes -r requirements.txt`, where requirements pin `ray[serve,llm]==2.61.0` with hashes. Same build-time Ray assert. `USER 1000`.
- **ROCm rule (AM20), record the outcome in the PR:** the ROCm base's bundled vLLM must match the Ray-LLM bundled vLLM minor (the CUDA image's vLLM, about 0.31.0). Choose the `rocm/vllm` tag whose vLLM minor matches. If no tag matches, build vLLM for ROCm from source at the pinned vLLM version. Never ship a silent mismatch.

- [ ] **Step 3c: `images/vllm-ray-rocm/requirements.txt`** — `ray[serve,llm]==2.61.0` and transitive pins, hash-pinned with `pip-compile --generate-hashes`; the hash lines are `<hashes: computed by executor via pip-compile --generate-hashes>`.

- [ ] **Step 3d: `images/vllm-ray-xpu/Dockerfile` and `images/vllm-ray-xpu/requirements.txt`**
- `vllm-ray-xpu/Dockerfile`: `FROM intel/oneapi-basekit:<tag: executor selects 2026.0.x>@<sha256: computed by executor via docker buildx imagetools inspect intel/oneapi-basekit:<tag>>`. Install `vllm-xpu-kernels==0.1.16.1`, the vLLM XPU wheel whose version the executor records in the PR, and `ray[serve,llm]==2.61.0` from `requirements.txt` with hashes. `USER 1000`. Single-GPU only; state this in the header (spec §2 non-goal).
- Python minor is 3.12 for all four images, including the head `rayproject/ray:2.61.0-py312`. Confirm each base's version with `docker run --rm --entrypoint python3 <base> -c 'import sys;print(sys.version)'`.
- **llama.cpp TLS (AM12).** Any llama.cpp image built by this plan or Plan F is compiled with `-DLLAMA_OPENSSL=ON` so `llama-server --ssl-cert-file/--ssl-key-file` works. The build asserts `ldd $(command -v llama-server) | grep -q libssl`.
- Hadolint must pass for all three (`make lint`). Each Dockerfile header records the digest command used.
- [ ] **Step 3e: build and test each image** (no file edit)
- Build and test each image:
  `docker buildx build --load --platform linux/amd64 -t waddleai/vllm-ray-<v>:dev images/vllm-ray-<v>`
  then `bash tests/images/test_vllm_ray_images.sh waddleai/vllm-ray-<v>:dev 2.61.0 3.12`.
  Record the `found` and `absent` output for each image. It feeds the A6 allowlist.
- [ ] **Step 3f: `.github/workflows/docker-build.yml`**
- CI: new job `build-vllm-ray-images` with `strategy.matrix.image: [cuda, rocm, xpu]`, `platforms: linux/amd64`, `push` only on non-PR events with the tier `push == 'true'`. On PRs, build with `load` and run the test script. Add a cleanup step with `if: always()`. Add a Trivy scan per image, as the existing ollama job does. The `images/**` path filter already matches.
- Run the three test invocations and `make lint`. Expected: three `PASS:` lines and `make lint` exit 0.

- [ ] **Step 4: Commit** (one commit: the images, the test script and the CI job are one deliverable)

```bash
git add images/vllm-ray-cuda/Dockerfile images/vllm-ray-rocm/Dockerfile images/vllm-ray-rocm/requirements.txt images/vllm-ray-xpu/Dockerfile images/vllm-ray-xpu/requirements.txt tests/images/test_vllm_ray_images.sh .github/workflows/docker-build.yml
git commit -m "feat(images): rootless vllm-ray cuda, rocm and xpu images and CI build matrix

+ the executing session's attribution trailer"
```

---

### Task A6: Tetragon exec allowlist for Ray worker pods (runs after A7)

**Files:**
- Modify: `k8s/helm/waddleai/values.yaml` (add `ray-worker` under `tetragon.exec.components`, near lines 1170–1200, same shape as existing entries: `enabled`, `mode`, `allowedExecs`)
- Test: `tests/helm/test_vllm_ray_tetragon_render.py`

**Interfaces:**
- Consumes: A2 worker pod labels; the existing `templates/tetragon-exec-policy.yaml` (no template change).
- Produces: `TracingPolicyNamespaced` `waddleai-exec-allowlist-ray-worker`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/helm/test_vllm_ray_tetragon_render.py
"""Tetragon exec allowlist for Ray worker pods (spec §6.3, A6)."""
from pathlib import Path

from tests.helm.conftest import find, render

BASE = str(Path(__file__).parent / "fixtures" / "vllm-ray-base.yaml")
TP_API = ["cilium.io/v1alpha1/TracingPolicyNamespaced"]
REQUIRED = {"/home/ray/anaconda3/bin/ray", "/home/ray/anaconda3/bin/python",
            "/usr/bin/nvidia-smi", "/usr/bin/rocm-smi", "/usr/bin/xpu-smi", "/bin/bash"}


def test_ray_worker_policy_is_enforced_with_required_binaries():
    """Sigkill allowlist containing ray, python, the three SMI tools and bash (KubeRay start command)."""
    tp = find(render(BASE, api_versions=TP_API), "TracingPolicyNamespaced", "waddleai-exec-allowlist-ray-worker")
    sel = tp["spec"]["kprobes"][0]["selectors"][0]
    assert sel["matchActions"] == [{"action": "Sigkill"}]
    allowed = set(sel["matchArgs"][0]["values"])
    assert REQUIRED <= allowed, REQUIRED - allowed


def test_policy_selects_only_ray_worker_component():
    """Pod selector targets component ray-worker only."""
    tp = find(render(BASE, api_versions=TP_API), "TracingPolicyNamespaced", "waddleai-exec-allowlist-ray-worker")
    assert tp["spec"]["podSelector"]["matchExpressions"][0]["values"] == ["ray-worker"]


def test_disabled_renders_no_ray_policy():
    """Default: no ray-worker TracingPolicy (review focus 1)."""
    docs = render("values-alpha.yaml", api_versions=TP_API)
    assert not [d for d in docs if d["kind"] == "TracingPolicyNamespaced" and "ray-worker" in d["metadata"]["name"]]


def test_worker_pods_carry_selector_labels():
    """Worker pod labels include the selector labels the policy matches (D6)."""
    labels = find(render(BASE), "RayCluster", "waddleai-inference")["spec"]["workerGroupSpecs"][0]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] == "waddleai"
    assert labels["app.kubernetes.io/component"] == "ray-worker"
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/helm/test_vllm_ray_tetragon_render.py -v`. Expected: 2 failed (`test_ray_worker_policy_is_enforced_with_required_binaries`, `test_policy_selects_only_ray_worker_component`), 2 passed (`test_disabled_renders_no_ray_policy` and `test_worker_pods_carry_selector_labels`, regression guards).

- [ ] **Step 3: Implement.** Add the `ray-worker` component with `enabled: true`, `mode: enforce`, and `allowedExecs` equal to the required set. The binary paths are image-specific. The A7 image step records `command -v ray python3 nvidia-smi rocm-smi xpu-smi bash` for each image, and the allowlist is the union of those results. Any path not confirmed by A7 is a blocker, not a guess. Run `pytest tests/helm/test_vllm_ray_tetragon_render.py -v`. Expected: 4 passed, 0 skipped.

- [ ] **Step 4: Commit**

```bash
git add k8s/helm/waddleai/values.yaml tests/helm/test_vllm_ray_tetragon_render.py
git commit -m "feat(helm): enforced tetragon exec allowlist for vllm-ray workers

+ the executing session's attribution trailer"
```

---

### Task A8: Makefile targets and test wiring

**Files:**
- Modify: `Makefile` (no `test-helm` or helm render target exists today; verified by grep)
- Test: the targets themselves

**Interfaces:**
- Consumes: A1 to A7.
- Produces: `helm-render-vllm-ray`, `test-helm`, `test-vllm-ray-images`.

- [ ] **Step 1: Write the failing check.** Run `make -n test-helm`. Expected: FAIL, "No rule to make target `test-helm`".

- [ ] **Step 2: Implement.** Each recipe uses `set -euo pipefail` and has no `|| true`.
  - `helm-render-vllm-ray`: `helm template waddleai k8s/helm/waddleai -f tests/helm/fixtures/vllm-ray-base.yaml > /dev/null`. Non-zero exit fails.
  - `test-helm`: `pytest tests/helm -v`, then fail if the summary contains any skipped test (`grep -q ' skipped' && exit 1`). A skipped render is not a pass.
  - `test-vllm-ray-images`: loops over `cuda rocm xpu` with `IMG_TAG ?= dev`, calling `tests/images/test_vllm_ray_images.sh` with the versions as make variables. Their defaults are the candidates from the version table.
Add the recipe line `@$(MAKE) test-helm` directly after the `@$(MAKE) test-unit` recipe line of the `test:` target (Makefile line 112).

- [ ] **Step 3: Verify.** Run `make helm-render-vllm-ray && make test-helm`. Expected: the render exits 0; pytest reports `<N> passed, 0 failed, 0 skipped` where N equals `pytest tests/helm --co -q | tail -1`; the grep gate exits 0. Then `make -n test-helm` must succeed.

- [ ] **Step 4: Commit**

```bash
git add Makefile
git commit -m "chore(make): add helm render, test-helm and vllm-ray image test targets

+ the executing session's attribution trailer"
```

---

## Self-review (completed)

- **Spec coverage:** §4.1 (A2), §4.2 renaming and co-tenancy (A3), §4.5 step 6 NCCL (A2), §4.8 images (A7), §6.2 values (values block, D1 to D3), §6.3 Cilium, Tetragon and no-ingress (A5, A6), §6.4 PVC and hot cache (A4), §7 Helm render tests and Tetragon (A2 to A6), CI matrix and Makefile (A7, A8). The §6.2 `topology.fleetPorts` addition is replaced on purpose by D1.
- **Names used consistently across tasks:** `waddleai-inference`, `fleet.vllmRay.*`, component `ray-worker`, `waddleai-vllm-ray-head` and `-worker`, `waddleai-exec-allowlist-ray-worker`, `waddleai-vllm-ray-models`, `waddleai-nvidia-device-plugin-config`.
- **Unresolved, to be reported and not guessed:** exact KubeRay version and sha256; Ray and ray-llm tag and digest (the Python minor is 3.12 where spec §4.8 said 3.11); the vLLM version inside ray-llm; the rocm/vllm tag whose vLLM matches; the intel/oneapi tag; the gpu-operator `devicePlugin.config.name` key path (confirmed in A2-storage-nvidia Step 3a); Cilium `endPort` support; the hot-cache PSA exemption mechanism (D5, decided); the Tetragon allowlist paths (from the A7 output).
- **Placeholders:** `<sha256: ...>` and `<tag: ...>` markers are deliberate. Before any commit that touches them, `grep -rn '<sha256:\|<tag:' k8s/helm/waddleai/values.yaml images/` must print nothing; the executor fills each from its named command.
