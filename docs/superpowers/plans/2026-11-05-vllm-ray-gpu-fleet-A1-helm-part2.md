# vLLM on Ray GPU Fleet — Sub-plan A, part 2: Ray auth, internal TLS, key precedence (Task A2b; continuation of `...-A1-helm.md`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Context:** Tasks A1–A2 are in `2026-11-05-vllm-ray-gpu-fleet-A1-helm.md` (Task A2c, the worker-group permutation matrix, is at the end of this file) (Global Constraints, Version pins and Review Focus apply here unchanged). Run A2b after A2 and before A3. Values come from the A2 block (`fleet.vllmRay.rayInternalTls`, `fleet.vllmRay.tlsGeneration`, `fleet.security.*`).

**Spec:** `docs/superpowers/specs/2026-11-05-vllm-ray-gpu-fleet-design.md` §6.3 items 3 and 5. Amendments AM11, AM12, AM13, AM15 in `...-01-amendments.md`.

### Task A2b: Ray token Secret, internal TLS, external key precedence, restart hook

**Files:**
- Create: `k8s/helm/waddleai/templates/fleet-vllm-ray/secrets.yaml`
- Create: `k8s/helm/waddleai/templates/fleet-vllm-ray/tls.yaml`
- Modify: `k8s/helm/waddleai/templates/fleet-vllm-ray/raycluster.yaml` (env, mounts, pod-template annotation)
- Modify: `k8s/helm/waddleai/templates/management-deployment.yaml` (two chart-set env entries, gated on `fleet.vllmRay.enabled`)
- Test: `tests/helm/test_vllm_ray_auth_render.py`

**Interfaces:**
- Consumes: A2 values; cert-manager CRDs `cert-manager.io/v1` (`Issuer`, `Certificate`).
- Produces: Secret `waddleai-ray-auth` (key `token`); cert-manager objects `waddleai-ray-selfsigned` (Issuer), `waddleai-ray-ca` (CA Certificate), `waddleai-ray-ca-issuer` (Issuer), `waddleai-ray-tls` (Certificate, Secret keys `tls.crt`, `tls.key`, `ca.crt`); pod-template annotation `waddleai.penguintech.io/restart-generation` on head and every worker group. Plan C (C3 reconcile) consumes the Secret names and the annotation.
- **Key precedence (AM15), one rule for every chart-generated key:** env var > `fleet.security.<key>.existingSecret` > chart-generated. The chart generates only when neither is set.
  - Ray token: env `WADDLEAI_RAY_AUTH_TOKEN` sourced from `fleet.security.rayAuthToken.envSecret` (key `token`) > `fleet.security.rayAuthToken.existingSecret` (key `token`) > chart-generated `waddleai-ray-auth`.
  - Ray TLS: management-pod env `WADDLEAI_RAY_TLS_*_PATH` (not rendered by the chart) > `fleet.security.rayTls.existingSecret` (keys `tls.crt`, `tls.key`, `ca.crt`) > chart-generated `waddleai-ray-tls`.
  - Token encryption key: env `WADDLEAI_FLEET_TOKEN_ENCRYPTION_KEY` (passed to the management pod, falls back to the existing Fernet key used by `fleet.py` credentials) > `fleet.security.tokenEncryptionKey.existingSecret` (key `fernet`) > chart-generated Secret `waddleai-fleet-token-key` (`randAlphaNum 64`, kept by `lookup`).
  - `WADDLEAI_SPIFFE_TRUST_DOMAIN` is passed through from `fleet.security.spiffeTrustDomain` (default `penguintech.io`); it is not a secret.
  - Chart-set management env (AM15 fixed list, Step 3d): `WADDLEAI_FLEET_MUTUAL_AUTH` (from `fleet.security.mutualAuth`), `WADDLEAI_FLEET_RAY_INTERNAL_TLS` (from `fleet.vllmRay.rayInternalTls`).
- **Restart hook (AM12, AM13).** Ray reads certs and token once at startup. Renewal or rotation of `waddleai-ray-tls` or `waddleai-ray-auth` must bump `waddleai.penguintech.io/restart-generation` on the RayCluster pod templates, which makes KubeRay roll head and workers. Plan C's reconcile loop (C3) watches both Secrets and runs that same bump on change; the rotation endpoints (C2c) use it too. The chart only seeds the annotation from `fleet.vllmRay.restartGeneration`.
- **Lifetimes.** CA 8760h (1y), renewBefore 720h. Leaf 2160h (90d), renewBefore 720h. Recorded here, consumed by the restart hook.
- **SPIRE SVID path not rendered in this plan.** cert-manager is the chart default because SVIDs still need a pod restart on rotation (AM12). SPIRE mode is an alternative for a later plan, not a silent fallback.

- [ ] **Step 1: Write the failing tests**

```python
# tests/helm/test_vllm_ray_auth_render.py
import base64
import re
from pathlib import Path

import pytest

from tests.helm.conftest import find, find_optional, render

BASE = str(Path(__file__).parent / "fixtures" / "vllm-ray-base.yaml")
CHART = Path(__file__).parents[2] / "k8s" / "helm" / "waddleai"
EXT_TOKEN = {"fleet.security.rayAuthToken.existingSecret": "ops-ray-token"}

def _pod_templates(docs):
    rc = find(docs, "RayCluster", "waddleai-inference")["spec"]
    return [rc["headGroupSpec"]["template"]] + [g["template"] for g in rc["workerGroupSpecs"]]

def test_token_secret_generated_48_alnum():
    token = base64.b64decode(find(render(BASE), "Secret", "waddleai-ray-auth")["data"]["token"]).decode()
    assert len(token) == 48 and re.fullmatch(r"[A-Za-z0-9]+", token)

def test_token_secret_upgrade_uses_lookup():
    tpl = (CHART / "templates/fleet-vllm-ray/secrets.yaml").read_text()
    assert 'lookup "v1" "Secret"' in tpl and "randAlphaNum 48" in tpl

def test_external_token_wins_nothing_generated():
    docs = render(BASE, EXT_TOKEN)
    assert find_optional(docs, "Secret", "waddleai-ray-auth") is None
    for pod in _pod_templates(docs):
        vol = [v for v in pod["spec"]["volumes"] if v["name"] == "ray-auth"][0]
        assert vol["secret"]["secretName"] == "ops-ray-token"

def test_env_token_wins_over_existing_secret():
    docs = render(BASE, {**EXT_TOKEN, "fleet.security.rayAuthToken.envSecret": "ops-env-secret"})
    assert find_optional(docs, "Secret", "waddleai-ray-auth") is None
    env = _pod_templates(docs)[0]["spec"]["containers"][0]["env"]
    assert any(e["name"] == "WADDLEAI_RAY_AUTH_TOKEN" and e["valueFrom"]["secretKeyRef"]["name"] == "ops-env-secret" for e in env)
```

```python


def test_ray_auth_envs_and_mount_on_head_and_workers():
    for pod in _pod_templates(render(BASE)):
        c = pod["spec"]["containers"][0]
        env = {e["name"]: e.get("value") for e in c["env"]}
        assert env["RAY_AUTH_MODE"] == "token" and env["RAY_AUTH_TOKEN_PATH"] == "/etc/ray-auth/token"
        assert any(m["mountPath"] == "/etc/ray-auth" and m.get("readOnly") for m in c["volumeMounts"])


def test_tls_envs_on_every_pod_by_default():
    for pod in _pod_templates(render(BASE)):
        env = {e["name"]: e.get("value") for e in pod["spec"]["containers"][0]["env"]}
        assert env["RAY_USE_TLS"] == "1" and env["RAY_TLS_SERVER_CERT"] == "/etc/ray-tls/tls.crt"
        assert env["RAY_TLS_SERVER_KEY"] == "/etc/ray-tls/tls.key" and env["RAY_TLS_CA_CERT"] == "/etc/ray-tls/ca.crt"


def test_tls_absent_when_disabled():
    docs = render(BASE, {"fleet.vllmRay.rayInternalTls": "false"})
    assert "RAY_USE_TLS" not in str(find(docs, "RayCluster", "waddleai-inference"))
    assert find_optional(docs, "Certificate", "waddleai-ray-tls") is None


def test_external_tls_secret_wins_over_generation():
    docs = render(BASE, {"fleet.security.rayTls.existingSecret": "ops-ray-tls"})
    assert find_optional(docs, "Certificate", "waddleai-ray-tls") is None
    vol = [v for v in _pod_templates(docs)[0]["spec"]["volumes"] if v["name"] == "ray-tls"][0]
    assert vol["secret"]["secretName"] == "ops-ray-tls"


```

```python
def test_restart_generation_annotation_on_every_pod_template():
    for pod in _pod_templates(render(BASE, {"fleet.vllmRay.restartGeneration": "3"})):
        assert pod["metadata"]["annotations"]["waddleai.penguintech.io/restart-generation"] == "3"


def test_cert_manager_missing_fails_loud():
    with pytest.raises(AssertionError, match="cert-manager"):
        render(BASE, {"fleet.vllmRay.rayInternalTls": "true"})


def test_management_env_mutual_auth_and_internal_tls_flags():
    d = [d for d in render(BASE) if d["kind"] == "Deployment" and "management" in d["metadata"]["name"]][0]
    env = {e["name"]: e.get("value") for e in d["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["WADDLEAI_FLEET_MUTUAL_AUTH"] == "true" and env["WADDLEAI_FLEET_RAY_INTERNAL_TLS"] == "true"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/helm/test_vllm_ray_auth_render.py -v`
Expected: 10 failed, 1 passed (`test_tls_absent_when_disabled`, the disabled-path guard). `test_cert_manager_missing_fails_loud` needs no helper change: default helm capabilities lack `cert-manager.io/v1`.

- [ ] **Step 3: Implement** (one sub-step per file)

- [ ] **Step 3a: `k8s/helm/waddleai/templates/fleet-vllm-ray/secrets.yaml`**
- `secrets.yaml`: gated on `vllmRay.enabled` AND no `fleet.security.rayAuthToken.existingSecret`. Value = existing Secret's `token` if present (`lookup "v1" "Secret" .Release.Namespace "waddleai-ray-auth"`), else `randAlphaNum 48 | b64enc`. Annotation `helm.sh/resource-policy: keep`. Never reads or writes values.
- [ ] **Step 3b: `k8s/helm/waddleai/templates/fleet-vllm-ray/tls.yaml`**
- `tls.yaml`: gated on `vllmRay.enabled` AND `rayInternalTls` AND no `fleet.security.rayTls.existingSecret`. First line: `fail` unless `.Capabilities.APIVersions.Has "cert-manager.io/v1"`, message contains `cert-manager` and says to install cert-manager or set `rayInternalTls=false` (allowed only while `fleet.security.mutualAuth` is on). Then Issuer `waddleai-ray-selfsigned` (`selfSigned: {}`), CA Certificate `waddleai-ray-ca` (`isCA: true`, `duration: 8760h`, `renewBefore: 720h`, ECDSA 256), Issuer `waddleai-ray-ca-issuer` (`ca.secretName: waddleai-ray-ca`), leaf Certificate `waddleai-ray-tls` (`duration: 2160h`, `renewBefore: 720h`, usages `server auth`, `client auth`, dnsNames: `waddleai-inference-head-svc`, `waddleai-inference-head-svc.<ns>.svc`, `localhost`, `*.waddleai-inference.<ns>.svc.cluster.local`). One CA per cluster (AM13).
- [ ] **Step 3c: `k8s/helm/waddleai/templates/fleet-vllm-ray/raycluster.yaml`** (env, mounts, pod-template annotation)
- `raycluster.yaml`, added to every head and worker container: env `RAY_AUTH_MODE=token`, `RAY_AUTH_TOKEN_PATH=/etc/ray-auth/token`; when `rayInternalTls`: `RAY_USE_TLS=1`, `RAY_TLS_SERVER_CERT=/etc/ray-tls/tls.crt`, `RAY_TLS_SERVER_KEY=/etc/ray-tls/tls.key`, `RAY_TLS_CA_CERT=/etc/ray-tls/ca.crt`. Precedence env: `WADDLEAI_RAY_AUTH_TOKEN` is a container env sourced from `fleet.security.rayAuthToken.envSecret` (key `token`) when set. The chart does not render `WADDLEAI_RAY_TLS_*_PATH`; the management pod reads them only, and Ray pods always use the mounted paths. Never literals in the chart. Mounts: `ray-auth` at `/etc/ray-auth` (readOnly) and `ray-tls` at `/etc/ray-tls` (readOnly, when TLS). Volumes: `ray-auth` secret = `fleet.security.rayAuthToken.existingSecret` or `waddleai-ray-auth`, item `token` -> `token`, `defaultMode: 0440` (group-readable through `fsGroup: 1000`); `ray-tls` secret = `fleet.security.rayTls.existingSecret` or `waddleai-ray-tls`.
- Pod-template annotation `waddleai.penguintech.io/restart-generation: "{{ fleet.vllmRay.restartGeneration }}"` on head and every worker group.
- [ ] **Step 3d: `k8s/helm/waddleai/templates/management-deployment.yaml`** — in the management container `env`, add when `fleet.vllmRay.enabled`: `WADDLEAI_FLEET_MUTUAL_AUTH` (`{{ .Values.fleet.security.mutualAuth | quote }}`) and `WADDLEAI_FLEET_RAY_INTERNAL_TLS` (`{{ .Values.fleet.vllmRay.rayInternalTls | quote }}`). Disabled renders are unchanged.

- No `values.yaml` change in this task: the A2-storage-nvidia block already holds every key used here.

- Run: `pytest tests/helm/test_vllm_ray_auth_render.py -v`. Expected: 11 passed, 0 skipped.

- [ ] **Step 4: Live upgrade-invariance check (E1, not render)**

Helm `template` cannot run `lookup`. On dal2beta (validation cluster, AM21): `helm upgrade` twice with `fleet.vllmRay.enabled=true`, then `kubectl get secret waddleai-ray-auth -o jsonpath='{.data.token}' | sha256sum`. Hash must be identical across both upgrades. Record the hash pair in the PR. Expected: two identical hash lines. A skip is not a pass.

- [ ] **Step 5: Verify the disabled path is unchanged**

Run: `helm template waddleai k8s/helm/waddleai -f k8s/helm/waddleai/values-alpha.yaml | wc -l` before Step 3 and after it. Expected: the same integer both times.

- [ ] **Step 6: Commit**

```bash
git add k8s/helm/waddleai/templates/fleet-vllm-ray/secrets.yaml k8s/helm/waddleai/templates/fleet-vllm-ray/tls.yaml k8s/helm/waddleai/templates/fleet-vllm-ray/raycluster.yaml k8s/helm/waddleai/templates/management-deployment.yaml tests/helm/test_vllm_ray_auth_render.py
git commit -m "feat(helm): ray token secret, internal TLS and external key precedence for vllm-ray

+ the executing session's attribution trailer"
```

---

---

### Task A2c: Worker-group permutation render matrix (AM19d; extends Task A2)

**Files:**
- Modify: `tests/helm/test_vllm_ray_render.py` (add one parametrized test; `BASE` and `render`/`_rc` helpers from Task A2)

- [ ] **Step 1: Add the matrix test**

```python
import itertools

# name, vendor, resourceName, image — same values as test_three_vendor_groups_render_three_groups
GROUP_SPECS = {
    "nvidia": ("nvidia-rtx4090", "nvidia", "nvidia.com/rtx4090", "cuda"),
    "nvidia2": ("nvidia-rtx4070", "nvidia", "nvidia.com/rtx4070", "cuda"),
    "amd": ("amd-mi300", "amd", "amd.com/gpu", "rocm"),
    "intel": ("intel-b580", "intel", "gpu.intel.com/i915", "xpu"),
}
PERMUTATIONS = (
    [[k] for k in ("nvidia", "amd", "intel")]                 # nvidia-only, amd-only, intel-only
    + [list(p) for p in itertools.permutations(("nvidia", "amd", "intel"))]  # all three, every order
    + [["nvidia", "nvidia2"], ["nvidia2", "nvidia"]]           # mixed NVIDIA classes
)

@pytest.mark.parametrize("keys", PERMUTATIONS, ids=lambda ks: "-".join(ks))
def test_worker_group_permutations_render_exact_groups(keys):
    """Each permutation renders one group per slot, in order, each with only its own GPU resource."""
    overrides = {}
    for i, k in enumerate(keys):
        name, vendor, res, image = GROUP_SPECS[k]
        overrides.update({
            f"fleet.vllmRay.workerGroups[{i}].name": name,
            f"fleet.vllmRay.workerGroups[{i}].vendor": vendor,
            f"fleet.vllmRay.workerGroups[{i}].gpuClass": name,
            f"fleet.vllmRay.workerGroups[{i}].resourceName": res,
            f"fleet.vllmRay.workerGroups[{i}].gpusPerWorker": "1",
            f"fleet.vllmRay.workerGroups[{i}].vramMbPerNode": "24576",
            f"fleet.vllmRay.workerGroups[{i}].maxReplicas": "1",
            f"fleet.vllmRay.workerGroups[{i}].image": image,
        })
    groups = _rc(render(BASE, overrides))["spec"]["workerGroupSpecs"]
    assert [g["groupName"] for g in groups] == [GROUP_SPECS[k][0] for k in keys]
    all_gpu_keys = {v[2] for v in GROUP_SPECS.values()}
    for g, k in zip(groups, keys):
        limits = g["template"]["spec"]["containers"][0]["resources"]["limits"]
        assert GROUP_SPECS[k][2] in limits
        assert all_gpu_keys & set(limits) == {GROUP_SPECS[k][2]}
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/helm/test_vllm_ray_render.py -k permutations -v`
Expected: all parametrized cases FAIL before the A2 template is in place; after A2, 11 cases PASS (3 single-vendor + 6 orders + 2 mixed-class). Zero collected = FAIL.

- [ ] **Step 3: Verify the matrix ran** — `pytest ... -k permutations --co -q | tail -1` must report `11 tests collected`.

- [ ] **Step 4: Commit**

```bash
git add tests/helm/test_vllm_ray_render.py
git commit -m "test(helm): worker-group permutation render matrix for vllm-ray

+ the executing session's attribution trailer"
```

---
