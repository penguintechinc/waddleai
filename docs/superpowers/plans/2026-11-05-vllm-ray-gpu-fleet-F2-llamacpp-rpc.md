# vLLM on Ray GPU Fleet — F: llama.cpp Multi-Node via RPC Implementation Plan (Part 4: F4, F6–F8, open items)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish llama.cpp RPC multi-node: image build, API topology, reconcile and status timeline, UI/CLI engine choice, and the gate.

**Architecture:** Part 1 (F1–F2) added schema and planner; F3 added rendering and F5 the API, each in its own file; this part adds vendor images (F4), reconciliation (F6), UI/CLI (F7) and gates (F8).

**Tech Stack:** Python 3.13, Quart, kubernetes client, Docker, React JSX (Plan D), Rust clap, Helm.

**Spec:** `docs/superpowers/specs/2026-11-05-vllm-ray-gpu-fleet-design.md` §4.8, §6.2, §6.3, §7. Part 1 (`2026-11-05-vllm-ray-gpu-fleet-F-llamacpp-rpc.md`), F3 (`2026-11-05-vllm-ray-gpu-fleet-F3-llamacpp-rpc-manifests.md`), F5 (`2026-11-05-vllm-ray-gpu-fleet-F5-llamacpp-rpc-api.md`) and master: see the headers there.

## Global Constraints

- Master Global Constraints and Part 1's flag, auth, image and unit rules apply to every task here.
- Flags: `_vllm_ray_enabled(org_id)`, default OFF.

## Review Focus

1. Worker dies mid-generation → `degraded`, row kept, no usage row (F6).
2. Head on a node without planned GPUs → pinned hostname and count (Part 1 F3).
3. `tensor_split` sum ≠ 1.0 → planner property test (Part 1 F2).
4. Mixed vendors → 422 before rendering (F5).
5. Auth disabled → `RPC_AUTH_REFUSAL`, no K8s calls (F5, F3).

---

## Task F4: RPC-capable images (one commit, three vendors)

**Files:**
- Create: `services/llamacpp-rpc/Dockerfile`; `tests/container/test_llamacpp_rpc_image.py`
- Modify: `.github/workflows/docker-build.yml` (add three matrix entries: `cuda`, `rocm`, `sycl`)
- Modify: `k8s/helm/waddleai/values.yaml` under `fleet:` (line 665): `llamacpp.images.{cuda,rocm,sycl}: {repository, digest}`

**Interfaces:**
- Produces: `ghcr.io/penguintechinc/waddleai/llamacpp-rpc:<vendor>-<commit7>@sha256:<digest>` per vendor; each contains `/usr/local/bin/llama-server` and `/usr/local/bin/rpc-server`, both from one commit.

**Decisions:** one build arg `LLAMACPP_COMMIT` for all targets (build asserts `git rev-parse HEAD`). Flags: `-DGGML_RPC=ON -DLLAMA_OPENSSL=ON -DCMAKE_BUILD_TYPE=Release` plus `-DGGML_CUDA=ON` / `-DGGML_HIP=ON` / `-DGGML_SYCL=ON` with icx/icpx. Bases digest-pinned via the `pinning-dependency-digests` skill (resolve, never invent). Runtime `USER 1000:1000`, label `org.opencontainers.image.revision`, hadolint clean. CI: beta push on `main` only; PRs build-only.

- [ ] **Step 1: Spike S1 (device names, flags, ordering)**

Run: `docker build --target runtime-cuda --build-arg LLAMACPP_COMMIT=<sha> -t llamacpp-rpc:cuda-spike services/llamacpp-rpc`, then `rpc-server --help` and `llama-server --help`.
Expected: `rpc-server --help` lists `-p` and `-d`; `llama-server --help` lists `--api-key-file` and `--ssl-cert-file`; the device names are `CUDA0` (cuda), `ROCm0` (rocm), `SYCL0` (sycl), equal to F3's `RPC_DEVICE_BY_VENDOR`. Record the output in `$SCRATCH/f4-s1/devices.txt` (`$SCRATCH` = the executor's scratchpad, per-agent namespace). A name mismatch blocks merge until F3's table is corrected.

- [ ] **Step 2: Write the failing container tests**

```python
# tests/container/test_llamacpp_rpc_image.py
import os
import subprocess

import pytest

VENDORS = ["cuda", "rocm", "sycl"]

def image(vendor: str) -> str:
    env = f"LLAMACPP_RPC_IMAGE_{vendor.upper()}"
    return os.environ.get(env) or pytest.fail(f"{env} is unset; the container gate cannot run without the built image")  # never skip

def run(img: str, *args: str, entrypoint: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "run", "--rm", "--entrypoint", entrypoint, img, *args],
                          capture_output=True, text=True, timeout=120, check=False)

@pytest.mark.parametrize("vendor", VENDORS)
def test_rpc_server_help(vendor):
    r = run(image(vendor), "--help", entrypoint="/usr/local/bin/rpc-server")
    assert r.returncode == 0 and "-p" in r.stdout and "-d" in r.stdout

@pytest.mark.parametrize("vendor", VENDORS)
def test_runs_as_uid_1000(vendor):
    r = run(image(vendor), "-u", entrypoint="id")
    assert r.returncode == 0 and r.stdout.strip() == "1000"

@pytest.mark.parametrize("vendor", VENDORS)
def test_llama_server_has_openssl_and_api_key(vendor):
    r = run(image(vendor), "--help", entrypoint="/usr/local/bin/llama-server")
    assert "--api-key-file" in r.stdout and "--ssl-cert-file" in r.stdout
```

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/container/test_llamacpp_rpc_image.py -v` with no image env → 9 FAILED (`LLAMACPP_RPC_IMAGE_<VENDOR> is unset`).

- [ ] **Step 4: Implement** the Dockerfile (three runtime targets from one builder ARG), the CI entries and values block. Run `hadolint services/llamacpp-rpc/Dockerfile` → exit 0.

- [ ] **Step 5: Run to verify pass**

Build the three targets; export `LLAMACPP_RPC_IMAGE_{CUDA,ROCM,SYCL}` and `LLAMACPP_COMMIT`; Run: `pytest tests/container/test_llamacpp_rpc_image.py -v` → `9 passed`.

- [ ] **Step 6: Commit**

```bash
git add services/llamacpp-rpc/Dockerfile tests/container/test_llamacpp_rpc_image.py k8s/helm/waddleai/values.yaml .github/workflows/docker-build.yml
git commit -m "build(llamacpp): rpc-capable llama.cpp images per vendor from one commit" -m "<the executing session's attribution trailer>"
```

---

## Task F5: API (create/PATCH topology, key minting, pricing, token rotation)

Moved to `2026-11-05-vllm-ray-gpu-fleet-F5-llamacpp-rpc-api.md` (size rule). Runs after F3 and before F6.

---

## Task F6: Reconcile, status timeline, metrics

**Files:**
- Create: `services/management/app/services/llamacpp_rpc_reconcile.py`
- Modify: `services/management/app/api/v1/llamacpp.py` health route (lines 489-523): add `status_message`, `workers_up`, `workers_total` for RPC rows.
- Modify: where C3 starts `VllmReconciler` (grep `VllmReconciler(`): start a sibling `LlamaCppRpcReconciler.run_forever()` under the same `LeaderGuard`.
- Test: `tests/unit/management/test_llamacpp_rpc_reconcile.py`

**Interfaces:**
- Produces: `derive_rpc_status(*, workers_total, workers_up, head_ready, was_running, head_reloading, failed_message, workers_down) -> RpcHealth` (pure; `RpcHealth(status, message, workers_total, workers_up, head_ready)`); `LlamaCppRpcReconciler(db, manager, *, guard, interval_s=15.0, clock, sleep)` with `async tick() -> dict[str,int]` (`deployments_examined, status_changes, head_restarts, reconcile_errors`) and `async run_forever()`.

**Timeline:** `pending` → `deploying` ("starting workers k/N") → `deploying` ("loading model") → `running` ("serving across N GPUs on M nodes") → `degraded` ("worker <pod> down (k/N up)") → `deploying` ("reloading model") → `running`. Kubernetes failure (ProgressDeadlineExceeded, CrashLoopBackOff) → `error`.

**Decisions:**
- Recovery: all workers back after `degraded` → bump the head pod-template annotation `waddleai.penguintech.io/restart-generation` (integer +1; AM20 restart mechanism) so the head reloads with the full `--rpc` list.
- Worker death mid-generation: the in-flight request fails via the proxy's upstream error path; the reconciler never deletes rows or writes usage.
- Load time: `waddleai_vllm_model_load_seconds{engine="llamacpp_rpc"}` from an in-process start time; after a restart, log "load time not recorded" and skip (never record 0).
- Gauge `waddleai_llamacpp_rpc_workers_up{deployment}` set each tick. `endpoints_for` (`llamacpp_manager.py:540`) returns the head only, since workers have no rows.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/management/test_llamacpp_rpc_reconcile.py
import pytest
from prometheus_client import REGISTRY

from services.management.app.services.llamacpp_rpc_reconcile import derive_rpc_status

# total, up, head_ready, was_running, reloading, failed, down, expect_status, expect_fragment
CASES = [
    (3, 0, False, False, False, None, ["w0", "w1", "w2"], "deploying", "starting workers 0/3"),
    (3, 3, False, False, False, None, [], "deploying", "loading model"),
    (3, 3, True, False, False, None, [], "running", "serving across 3 GPUs"),
    (3, 2, True, True, False, None, ["w1"], "degraded", "worker w1 down (2/3 up)"),
    (3, 3, False, True, True, None, [], "deploying", "reloading model"),
    (3, 2, False, False, False, "CrashLoopBackOff on w2", ["w2"], "error", "CrashLoopBackOff on w2"),
]

@pytest.mark.parametrize("c", CASES, ids=[c[7] for c in CASES])
def test_derive_rpc_status_timeline(c):
    total, up, ready, was_running, reloading, failed, down, status, fragment = c
    h = derive_rpc_status(workers_total=total, workers_up=up, head_ready=ready, was_running=was_running,
                          head_reloading=reloading, failed_message=failed, workers_down=down)
    assert h.status == status and fragment in h.message

```

Same file, continued (same imports and CASES):

```python
@pytest.mark.asyncio
async def test_worker_pod_dies_marks_degraded_and_keeps_row(reconciler, rpc_running_row, sts_mock):
    sts_mock.read_namespaced_stateful_set_status.return_value.status.ready_replicas = 2
    assert (await reconciler.tick())["status_changes"] == 1
    assert rpc_running_row.status == "degraded" and "down (2/3 up)" in rpc_running_row.status_message

@pytest.mark.asyncio
async def test_head_restart_on_recovery_and_gauge(reconciler, rpc_degraded_row, apps_mock, sts_mock):
    assert (await reconciler.tick())["head_restarts"] == 1
    annotations = apps_mock.patch_namespaced_deployment.call_args.kwargs["body"]["spec"]["template"]["metadata"]["annotations"]
    assert "waddleai.penguintech.io/restart-generation" in annotations
    assert REGISTRY.get_sample_value("waddleai_llamacpp_rpc_workers_up", {"deployment": rpc_degraded_row.name}) == 3.0

@pytest.mark.asyncio
async def test_endpoints_for_returns_head_only(manager, rpc_running_row):
    assert [e.url for e in await manager.endpoints_for(rpc_running_row.model_name)] == [rpc_running_row.endpoint_url]

@pytest.mark.asyncio
async def test_load_time_skipped_after_restart(reconciler, rpc_running_row, caplog):
    reconciler._started_at.clear()
    await reconciler.tick()
    assert "load time not recorded" in caplog.text
```

Fixtures (`reconciler`, `rpc_running_row`, `rpc_degraded_row`, `sts_mock`, `apps_mock`, `manager`) go in `tests/unit/management/conftest.py`; K8s clients are `MagicMock`s patched at `services.management.app.services.llamacpp_manager`.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/management/test_llamacpp_rpc_reconcile.py -v` → collection error `ModuleNotFoundError: ...llamacpp_rpc_reconcile`.

- [ ] **Step 3: Implement** `derive_rpc_status` (one branch per timeline row), then `tick` (read StatefulSet and head `ready_replicas`, write status and message, set the gauge, patch the head on recovery, observe the histogram on `deploying → running`); start the reconciler beside `VllmReconciler`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/management/test_llamacpp_rpc_reconcile.py -v` → `10 passed`, 0 failed.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/services/llamacpp_rpc_reconcile.py services/management/app/api/v1/llamacpp.py tests/unit/management/test_llamacpp_rpc_reconcile.py tests/unit/management/conftest.py
git commit -m "feat(llamacpp): RPC status timeline, worker gauge and head restart on recovery" -m "<the executing session's attribution trailer>"
```

---

## Task F7: UI "Engine" choice and CLI flag (small; reuses Plan D)

**Files:**
- Modify (Plan D): `services/webui/src/components/gpu/wizard/wizardState.js` (`engine: "vllm" | "llamacpp_rpc"`, default `"vllm"`; `ggufBytes`, `allowMixedClass`; `buildCreateBody` RPC branch posting `topology: 2` to `/api/v1/llamacpp/deployments`); `Step1PickModel.jsx` (Engine radio group, shown only when both flags are on); `Step2FitCheck.jsx` (RPC layout via `layoutLabel`); `Step3Advanced.jsx` (GGUF size in GB, sent as bytes; mixed-class switch).
- Create: `services/webui/src/components/gpu/wizard/engineCopy.js`.
- Modify: `clients/waddleai-cli/src/cli.rs` (`fleet llamacpp deploy`: `--topology {single,rpc}`, `--gguf-bytes <N>`, `--allow-mixed-class`); `clients/waddleai-cli/src/api_client.rs` (`llamacpp_deploy(body)`). Both already have working-tree changes; rebase onto merged C/D first.
- Test: `services/webui/src/test/engineCopy.test.jsx`; Rust `#[cfg(test)]` in `cli.rs`; Playwright `tests/e2e/gpu-fleet-engine.spec.js` (D2 harness).

**Interfaces:**
- Consumes: D1 wizard reducer/validators; D4 `ChargebackStep`; C1 flags endpoint.
- Produces: `ENGINE_COPY` with these exact strings: `vllm` label "vLLM on Ray", hint "Recommended. Multi-node tensor and pipeline parallelism on Ray."; `llamacpp_rpc` label "llama.cpp RPC", hint "slower across nodes, use when you need GGUF or llama.cpp-only features"; `ollama` label "Ollama", hint "Ollama runs single-node; use llama.cpp RPC or vLLM on Ray for multi-node" (informational chip, not selectable).

- [ ] **Step 1: Write the failing tests**

```javascript
// services/webui/src/test/engineCopy.test.jsx
import { ENGINE_COPY } from "../components/gpu/wizard/engineCopy.js";
import { buildCreateBody, initialWizardState, validateStep } from "../components/gpu/wizard/wizardState.js";

test("engine copy matches approved wording", () => {
  expect(ENGINE_COPY.llamacpp_rpc.hint).toBe("slower across nodes, use when you need GGUF or llama.cpp-only features");
  expect(ENGINE_COPY.ollama.hint).toBe("Ollama runs single-node; use llama.cpp RPC or vLLM on Ray for multi-node");
});

test("rpc body posts topology 2 with GGUF bytes; missing size blocks step 3", () => {
  const s = { ...initialWizardState(), engine: "llamacpp_rpc", ggufBytes: 65 * 2 ** 30, name: "q", modelFilename: "m.gguf", modelUrl: "https://h/m.gguf" };
  expect(buildCreateBody(s)).toMatchObject({ topology: 2, deployment_type: "kubernetes", gguf_bytes: 65 * 2 ** 30 });
  const r = validateStep(3, { ...s, ggufBytes: null });
  expect(r.ok).toBe(false);
  expect(r.errors.join(" ")).toMatch(/GGUF file size/);
});
```

Rust: `fleet_llamacpp_rpc_sets_topology_two` and `fleet_llamacpp_default_is_single`. Playwright (D2 harness): the Engine group shows three options, Ollama is not a radio, and "llama.cpp RPC" reveals the GGUF field; output to `/tmp/playwright-waddleai`, removed after the run and in CI with `if: always()`.

- [ ] **Step 2: Run to verify failure**

Run: `npm --prefix services/webui test -- engineCopy` → FAIL (`Cannot find module engineCopy.js`). Run: `cargo test -p waddleai-cli fleet_llamacpp` → FAIL (no `--topology` flag).

- [ ] **Step 3: Implement** the copy module, reducer fields, validator and body branches, the conditional sections in the three step components, the CLI flag and client method.

- [ ] **Step 4: Run to verify pass**

Run: `npm --prefix services/webui test -- engineCopy wizardState GpuFleetDeployWizard` → all pass. `cargo test -p waddleai-cli` → all pass. Playwright spec → all pass, then `rm -rf /tmp/playwright-waddleai`.

- [ ] **Step 5: Commit**

```bash
git add services/webui/src/components/gpu/wizard services/webui/src/test clients/waddleai-cli/src tests/e2e/gpu-fleet-engine.spec.js
git commit -m "feat(webui,cli): llama.cpp RPC engine choice in deploy wizard and CLI" -m "<the executing session's attribution trailer>"
```

---

## Task F8: Gate

**Files:**
- Create: `tests/helm/test_llamacpp_rpc_render.py`
- Modify: `k8s/helm/waddleai/templates/management-deployment.yaml`: env `WADDLEAI_FLEET_MUTUAL_AUTH` from `fleet.security.mutualAuth` (fixed env, AM15/AM20); `WADDLEAI_LLAMACPP_RPC_IMAGE_{CUDA,ROCM,SYCL}` from `fleet.llamacpp.images`.
- Modify: `openapi/v1.yaml` (regenerated).

- [ ] **Step 1: Failing Helm render test**

```python
# tests/helm/test_llamacpp_rpc_render.py
from tests.helm.conftest import find, render

def test_rpc_images_and_mutual_auth_env_rendered():
    digest = "sha256:" + "b" * 64
    docs = render({"fleet.security.mutualAuth": "false", "fleet.llamacpp.images.cuda.digest": digest})
    dep = find(docs, kind="Deployment", name_contains="management")[0]
    env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["WADDLEAI_FLEET_MUTUAL_AUTH"] == "false"
    assert env["WADDLEAI_LLAMACPP_RPC_IMAGE_CUDA"].endswith("@" + digest)
```

Run: `pytest tests/helm/test_llamacpp_rpc_render.py -v` → FAIL (env absent). Add the template env, rerun → `1 passed`.

- [ ] **Step 2: Unit gate with coverage**

Run: `make test-unit` → exit 0; branch coverage ≥ 90% on `shared` and `services/management/app`. Report collected count and per-module coverage.

- [ ] **Step 3: Lint, OpenAPI, types**

Run: `make generate-openapi && make openapi-lint && make lint` → exit 0. Run `mypy` on the three new modules → report the count; zero new errors.

- [ ] **Step 4: Helm and container gates**

Run: `pytest tests/helm -k "fleet or llamacpp" -v` → all pass. Run the F4 container gate with the built refs → `9 passed`.

- [ ] **Step 5: Prove the gate can fail**

Commit the Step 1 test alone as a WIP commit (`git add tests/helm/test_llamacpp_rpc_render.py && git commit -m "wip: failing RPC render test"`). Run it → must FAIL (env absent). Then add the template env, run again → `1 passed`. Record both results. No `git stash` (repo ban); the WIP commit is squashed away at merge.

- [ ] **Step 6: Commit**

```bash
git add tests/helm/test_llamacpp_rpc_render.py k8s/helm/waddleai/templates/management-deployment.yaml openapi/v1.yaml
git commit -m "test(llamacpp): Helm render gate for RPC images and mutual auth env" -m "<the executing session's attribution trailer>"
```

Real-path validation (master §E E10-E12, dal2beta) is not satisfied by F8; record counts in the release PR.

---

## Open Items (decide before merge)

| ID | Item | Plan default |
|---|---|---|
| OI-1 | AM11 allows internal TLS, but rpc-server has no TLS (research §2) | RPC refused unless mutual auth; owner confirms |
| OI-2 | B6 `apply_pricing` sets `nodes_used=1` for non-vLLM; wrong for RPC | Closed (AM17): B4d takes `nodes_used` from the layout; F5 passes the layout's distinct node count |
| OI-3 | Ollama multi-node: no native RPC upstream (research §1) | Ollama stays single-node, AM12 copy |
| OI-4 | Device names, resource names, split order unverified | Spike S1 + F8 log check |
| OI-5 | Migration 029 vs fold-in to 028 | AM17 conditional: fold into 028 if Plan B's 028 is unmerged, else 029 (Part 1 Decisions) |
| OI-6 | Key-minting helper | Closed: `fleet_secrets.mint_token()` (C1-part3); remove_key sequencing cited to C2c; F5 uses it |
| OI-7 | RPC planning needs `gpu_inventory` (Plan B collector) | Refused with collector message when empty |
| OI-8 | LeaderWorkerSet not vendored | StatefulSet per worker node |
| OI-9 | Weight size never guessed | 422 when `gguf_bytes` missing |
| OI-10 | curl telnet exit semantics in head init wait | Verify F8; else use a `/dev/tcp` shell image |
