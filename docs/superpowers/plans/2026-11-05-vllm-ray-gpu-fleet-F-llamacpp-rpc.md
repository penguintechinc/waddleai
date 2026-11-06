# vLLM on Ray GPU Fleet — F: llama.cpp Multi-Node via RPC Implementation Plan (Part 1: F1–F2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the existing llama.cpp fleet backend serve one GGUF model across several GPU nodes (a llama-server head plus rpc-server worker pods), planned, priced, metered and routed like every other local deployment.

**Architecture:** A pure planner (`shared/fleet/llamacpp_rpc.py`) reuses Plan B's `Pool` and picks one-vendor layouts with VRAM-proportional `tensor_split`; `LlamaCppManager` renders a head Deployment, one StatefulSet per worker node behind a headless Service, and a per-deployment CiliumNetworkPolicy; RPC deployments use vendor images built from one llama.cpp commit.

**Tech Stack:** Python 3.13, Quart, SQLAlchemy + Alembic, penguin-dal calls already used by `LlamaCppManager`, kubernetes Python client, Cilium mutual auth (beta), llama.cpp pinned by commit (`GGML_RPC=ON`, `LLAMA_OPENSSL=ON`), Docker multi-stage builds.

**Spec:** `docs/superpowers/specs/2026-11-05-vllm-ray-gpu-fleet-design.md` §4.8, §4.4, §4.7, §6.2, §6.3, §7. Research: `docs/superpowers/specs/2026-11-05-vllm-ray-research-notes.md` §2-§3. Master: `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-00-master.md` (AM6, AM8, AM10, AM11, AM12, AM13 binding).

**Split:** this file: Global Constraints, Review Focus, Decisions, F1-F2. `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-F3-llamacpp-rpc-manifests.md` (F3). `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-F5-llamacpp-rpc-api.md` (F5). `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-F2-llamacpp-rpc.md` (F4, F6-F8, open items). Branch `feature/vllm-ray-f`, stacked on `feature/vllm-ray-c`, worktree under `.worktrees/`.

## Global Constraints

- Master Global Constraints apply verbatim: Python 3.13, penguin logging only, OTel logs+metrics+traces per new path, `@dataclass(slots=True)`, int enums, `public_id` at boundaries, `fleet:admin` scope, rootless + PSA restricted, digest pins, `quart-schema` DTOs, ≥90% coverage.
- RPC path requires `_vllm_ray_enabled(org_id)` (C1: `waddleai.fleet_v2` AND `waddleai.fleet-vllm-ray`, default OFF). Flag off + `topology=2` → 403 `llama.cpp RPC requires the GPU Fleet flags`.
- Units: VRAM in MB in planner and inventory; GB only in copy.
- Auth (AM11, AM13): RPC requires Cilium mutual auth (`fleet.security.mutualAuth` → env `WADDLEAI_FLEET_MUTUAL_AUTH`). Head enforces `--api-key-file` from a Secret; the key never appears in argv, logs or responses.
- Images: head and workers use the F4 image for one vendor (one llama.cpp commit). The upstream `LLAMACPP_IMAGE` digest is never used for RPC.
- Copy: every refusal says what to change, in plain language.

## Review Focus

1. Worker pod dies mid-generation → in-flight request fails via the proxy's upstream error; status `degraded` names the worker; no zero-token usage row. (F6 `test_derive_rpc_status_timeline` case "degraded", F6 `test_worker_pod_dies_marks_degraded_and_keeps_row`)
2. Head scheduled onto a node without planned GPUs → head pinned to the planned hostname with the planned GPU count; planner refuses zero head GPUs. (F3 `test_head_pinned_to_planned_node`, F2 `test_head_has_gpus_and_is_most_loaded`)
3. `tensor_split` sum ≠ 1.0 → planner sums to 1.0 at 4 dp for every fixture; manager refuses a split whose length ≠ device count. (F2 `test_tensor_split_sums_to_one`, F3 `test_tensor_split_length_mismatch_refused`)
4. Mixed vendors → refused before any object renders; API 422. (F2 `test_cross_vendor_refused`, F5 `test_create_cross_vendor_422`)
5. Auth disabled → planner refuses with `RPC_AUTH_REFUSAL`; manager makes no K8s call; worker port admits only head pods. (F2 `test_auth_disabled_refused_with_spec_string`, F3 `test_mutual_auth_disabled_no_k8s_calls`, F3 `test_worker_ingress_only_from_head`)

Also pinned: mixed-class refusal (F2 `test_mixed_class_refused_unless_allowed`), no-fit (F2 `test_no_fit_refused`).

---

## Decisions

- **Worker granularity:** one rpc-server pod per GPU (`gpu_count=1` per `rpc_workers` entry). rpc-server exposes one device per process (`-d`), so the device index is always `0` inside the pod.
- **Worker workload:** one StatefulSet per worker node (replicas = GPUs used there), pinned by `kubernetes.io/hostname`, all sharing one headless Service. A single StatefulSet cannot pin pods to different nodes.
- **Head:** a Deployment, `replicas: 1`, pinned to the planned head node.
- **Not LeaderWorkerSet:** not in `Chart.yaml` dependencies (OI-8). Rendering stays in Python in `LlamaCppManager`, like the existing DaemonSet.
- **Migration (AM17 conditional, applies to every migration step below):** if Plan B's `028` is unmerged at branch time, add the four F1 columns to `028` and create no `029` file; if Plan B's `028` is merged, create `029_llamacpp_rpc.py` with `down_revision` = `028`. Check: `git log --oneline release/v0.4.X -- services/management/alembic/versions/`.

---

## Task F1: Schema

**Files:**
- Create: `services/management/alembic/versions/029_llamacpp_rpc.py` (AM17 conditional: only when Plan B's `028` is merged; otherwise the columns go into `028` and this file is not created)
- Modify: `shared/fleet/enums.py` (append `LlamaCppTopology`)
- Modify: `services/management/app/models_sqlalchemy.py:387-420` (`LlamaCppDeployment`; insert after line 408 `gpu_count`)
- Test: `tests/unit/management/test_llamacpp_rpc_schema.py`

**Interfaces:**
- Consumes: `LlamaCppDeployment`; Plan B's enum module and migration chain (`028`, B2).
- Produces: `LlamaCppTopology(IntEnum): SINGLE_NODE = 1; RPC_MULTI_NODE = 2`; `LlamaCppDeployment.topology: int` (NOT NULL, default 1); `.rpc_workers: list[dict] | None` (JSON, one `{node_uid, gpu_class, gpu_count, tensor_split_share}` per worker pod); `.tensor_split: str | None` (VARCHAR(255), comma-joined 4-dp shares in device order); `.allow_mixed_class: bool` (NOT NULL, default False).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/management/test_llamacpp_rpc_schema.py
from sqlalchemy import inspect

from services.management.app.models_sqlalchemy import LlamaCppDeployment
from shared.fleet.enums import LlamaCppTopology

RPC_COLS = {"topology", "rpc_workers", "tensor_split", "allow_mixed_class"}


def test_topology_values():
    assert (LlamaCppTopology.SINGLE_NODE, LlamaCppTopology.RPC_MULTI_NODE) == (1, 2)


def test_orm_defaults(db_session):
    dep = LlamaCppDeployment(name="t-rpc", model_name="m", model_filename="m.gguf")
    db_session.add(dep)
    db_session.flush()
    assert (dep.topology, dep.allow_mixed_class, dep.rpc_workers, dep.tensor_split) == (1, False, None, None)


def test_migration_029_upgrade_and_downgrade(scratch_sqlite_engine, alembic_cfg):
    from alembic import command
    command.upgrade(alembic_cfg, "029_llamacpp_rpc")
    assert RPC_COLS <= {c["name"] for c in inspect(scratch_sqlite_engine).get_columns("llamacpp_deployments")}
    command.downgrade(alembic_cfg, "-1")
    assert not RPC_COLS & {c["name"] for c in inspect(scratch_sqlite_engine).get_columns("llamacpp_deployments")}
```

Fixtures `db_session`, `scratch_sqlite_engine`, `alembic_cfg` come from Plan B's B2 test conventions; reuse them. AM17: under fold-in, the migration test targets revision `028` instead of `029_llamacpp_rpc`; the assertions are unchanged.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/management/test_llamacpp_rpc_schema.py -v` → 3 FAILED with `ImportError: cannot import name 'LlamaCppTopology'`.

- [ ] **Step 3: Implement**

- `enums.py`: `LlamaCppTopology(IntEnum)` with the two members.
- `models_sqlalchemy.py` after line 408: `topology = Column(Integer, nullable=False, default=1, server_default="1")`, `rpc_workers = Column(JSON)`, `tensor_split = Column(String(255))`, `allow_mixed_class = Column(Boolean, nullable=False, default=False, server_default=sa.false())`.
- AM17 conditional: `029_llamacpp_rpc.py` with `down_revision` = Plan B's 028 revision id (verify in B2) when 028 is merged; under fold-in, add the same four columns inside `028`'s `upgrade()`/`downgrade()` instead. `upgrade()` adds the four columns inside `op.batch_alter_table("llamacpp_deployments")`; `downgrade()` drops them in the same batch.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/management/test_llamacpp_rpc_schema.py -v` → `3 passed`. Run `pytest tests/unit/management -k "models_match or schema" -q` (B1's ORM-vs-migration test, extended with the four columns) → 0 failures.

- [ ] **Step 5: Commit**

```bash
git add shared/fleet/enums.py services/management/app/models_sqlalchemy.py services/management/alembic/versions/029_llamacpp_rpc.py tests/unit/management/test_llamacpp_rpc_schema.py  # AM17: under fold-in, replace 029_llamacpp_rpc.py with services/management/alembic/versions/028_*.py
git commit -m "feat(llamacpp): topology, RPC worker, split and mixed-class schema" -m "<the executing session's attribution trailer>"
```

---

## Task F2: Layout planner

**Files:**
- Create: `shared/fleet/llamacpp_rpc.py` (pure; imports `Pool`, `estimate_weight_bytes` from `shared/fleet/vllm_ray/fit.py`)
- Test: `tests/unit/fleet/test_llamacpp_rpc.py`

**Interfaces:**
- Consumes: `Pool(vendor:int, gpu_class:str, nodes:list[tuple[str,int,int]])` where a node is `(node_uid, gpu_count, vram_free_mb_per_gpu)` (B4).
- Produces:

```python
MAX_RPC_GPUS: int = 8
RPC_KV_CTX_OVERHEAD_MB: int = 1536     # per GPU, runtime context (spec §4.4)
RPC_SLOWDOWN_COPY: str = "slower across nodes (expect 20–50% lower decode throughput)"
RPC_AUTH_REFUSAL: str = "enable Cilium mutual auth (SPIRE) to deploy llama.cpp across nodes"   # AM17 wording (llama.cpp RPC needs mutual auth only)

class RpcRefused(ValueError): ...      # str(exc) is admin-facing copy

@dataclass(slots=True, frozen=True)
class RpcGpu: node_uid: str; gpu_class: str; vram_free_mb: int

@dataclass(slots=True)
class RpcWorker: node_uid: str; gpu_class: str; vram_free_mb: int; tensor_split_share: float   # one rpc-server pod = one GPU

@dataclass(slots=True)
class RpcLayout:
    vendor: int; head_node_uid: str; head_gpu_class: str; head_gpus: int
    workers: list[RpcWorker]              # device order: workers first, head-local GPUs after
    tensor_split: list[float]             # len == len(workers) + head_gpus; sums to 1.0 at 4 dp
    vram_needed_mb: int; vram_available_mb: int; nodes_used: int; explanation: str

def plan_rpc_layout(*, weight_bytes: int, pools: list[Pool], allow_mixed_class: bool,
                    kv_headroom_pct: int = 20, mutual_auth_enabled: bool,
                    internal_tls_enabled: bool) -> RpcLayout
```

`internal_tls_enabled` is logged, never satisfies the link (rpc-server has no TLS; OI-1). `weight_bytes` comes from the caller's `gguf_bytes`; the planner never estimates weights from parameter counts.

**Approach:**
1. `mutual_auth_enabled` False → `RpcRefused(RPC_AUTH_REFUSAL)` first.
2. More than one vendor in `pools` → `RpcRefused("RPC layouts cannot mix GPU vendors ({names}). Choose one vendor's pool.")`. No pools → `RpcRefused("No GPU inventory for this vendor. The GPU inventory collector must run first.")`.
3. Expand to one `RpcGpu` per GPU, dropping `vram_free_mb == 0`.
4. `need_mb(k) = ceil(weight_bytes × (1 + kv_headroom_pct/100) / 2**20) + k × RPC_KV_CTX_OVERHEAD_MB`.
5. Candidate sets: one per GPU class; plus the cross-class union only when `allow_mixed_class`. Greedy by `(-vram_free_mb, node_uid)` until `sum(free) ≥ need_mb(k)`, capped at `MAX_RPC_GPUS`.
6. Rank fitting candidates by `(nodes_used, k, -spare_mb, class labels)`. A one-node winner returns `workers=[]` with explanation `Fits on one node. Use the single-node topology; RPC is not needed.`
7. Head = node with the most selected GPUs (tie: `node_uid` ascending). Workers = remaining GPUs ordered by `(node_uid, -vram_free_mb)`.
8. `tensor_split` in device order `[workers..., head locals...]`; `share = free/total` rounded to 4 dp, last element `round(1 - sum(prior), 4)`. Device order is verified in F4 spike S1.
9. Only a mixed set fits and `allow_mixed_class` is False → `RpcRefused("Layout mixes GPU classes {a} and {b}. Enable allow_mixed_class to split by VRAM across both.")`.
10. No fit → `RpcRefused("Does not fit: needs {need_gb:.1f} GB; the best one-vendor pool offers {avail_gb:.1f} GB across up to 8 GPUs. Options: a smaller quantization, or more GPUs.")`.
11. Explanation: `Fits across {nodes_used} nodes ({need_gb:.0f} GB of {avail_gb:.0f} GB). Layers split by VRAM. RPC is {RPC_SLOWDOWN_COPY}.`; mixed sets say `mixing {a} and {b} GPUs`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/fleet/test_llamacpp_rpc.py
import random

import pytest

from shared.fleet.llamacpp_rpc import RPC_AUTH_REFUSAL, RPC_SLOWDOWN_COPY, RpcRefused, plan_rpc_layout
from shared.fleet.vllm_ray.fit import Pool

NV, AMD = 1, 2
W120 = 65 * 2**30                       # 120B Q4 GGUF size fixture (bytes)
AUTH = dict(mutual_auth_enabled=True, internal_tls_enabled=False)


def plan(pools, **kw):
    return plan_rpc_layout(weight_bytes=kw.pop("w", W120), pools=pools,
                           allow_mixed_class=kw.pop("mixed", False), **AUTH, **kw)


@pytest.mark.parametrize("nodes, cls, want_nodes", [
    ([("n1", 2, 24000), ("n2", 2, 24000)], "nvidia-rtx4090", 2),   # 2 nodes
    ([("n1", 1, 32000), ("n2", 1, 32000), ("n3", 1, 32000)], "nvidia-rtx5090", 3),  # 3 nodes
])
def test_120b_q4_fits_across_nodes(nodes, cls, want_nodes):
    lay = plan([Pool(NV, cls, nodes)])
    assert lay.nodes_used == want_nodes and len(lay.workers) == want_nodes - 1 + (lay.head_gpus - 1)
    assert round(sum(lay.tensor_split), 4) == 1.0 and RPC_SLOWDOWN_COPY in lay.explanation


def test_no_fit_refused():
    with pytest.raises(RpcRefused, match="Does not fit"):
        plan([Pool(NV, "nvidia-rtx4090", [("n1", 1, 24000), ("n2", 1, 24000)])])


def test_mixed_class_refused_unless_allowed():
    pools = [Pool(NV, "nvidia-rtx4090", [("n1", 2, 24000)]), Pool(NV, "nvidia-rtx4070", [("n2", 2, 12000)])]
    with pytest.raises(RpcRefused, match="mixes GPU classes"):
        plan(pools)
    assert "mixing" in plan(pools, mixed=True).explanation


```

Same file, continued (same imports and constants):

```python
def test_cross_vendor_refused():
    with pytest.raises(RpcRefused, match="cannot mix GPU vendors"):
        plan([Pool(NV, "a", [("n1", 1, 1)]), Pool(AMD, "b", [("n2", 1, 1)])], mixed=True)


def test_auth_disabled_refused_with_spec_string():
    with pytest.raises(RpcRefused) as exc:
        plan_rpc_layout(weight_bytes=W120, pools=[Pool(NV, "nvidia-rtx4090", [("n1", 2, 24000)])],
                        allow_mixed_class=False, mutual_auth_enabled=False, internal_tls_enabled=True)
    assert str(exc.value) == RPC_AUTH_REFUSAL


def test_fits_on_one_node_has_no_workers():
    lay = plan([Pool(NV, "nvidia-rtx4090", [("n1", 2, 24000)])], w=8 * 2**30)
    assert lay.workers == [] and lay.explanation.startswith("Fits on one node")


def test_head_has_gpus_and_is_most_loaded():
    lay = plan([Pool(NV, "nvidia-rtx4090", [("n1", 1, 24000), ("n2", 3, 24000)])])
    assert lay.head_gpus >= 1 and lay.head_node_uid == "n2"


def test_tensor_split_sums_to_one():
    rng = random.Random(1)
    for _ in range(50):
        nodes = [(f"n{i}", rng.randint(1, 3), rng.choice([12000, 16000, 24000, 32000])) for i in range(4)]
        try:
            lay = plan([Pool(NV, "nvidia-rtx4090", nodes)])
        except RpcRefused:
            continue
        assert abs(sum(lay.tensor_split) - 1.0) < 1e-9 and all(s > 0 for s in lay.tensor_split)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/fleet/test_llamacpp_rpc.py -v` → collection error `ModuleNotFoundError: No module named 'shared.fleet.llamacpp_rpc'`.

- [ ] **Step 3: Implement** `shared/fleet/llamacpp_rpc.py` per the Approach list, one private function per step (`_expand`, `_need_mb`, `_candidates`, `_rank`, `_split`, `_explain`).

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/fleet/test_llamacpp_rpc.py -v` → all pass. Run `pytest tests/unit/fleet/test_llamacpp_rpc.py --cov=shared.fleet.llamacpp_rpc --cov-branch` → ≥ 95%.

- [ ] **Step 5: Commit**

```bash
git add shared/fleet/llamacpp_rpc.py tests/unit/fleet/test_llamacpp_rpc.py
git commit -m "feat(llamacpp): RPC layout planner with fail-closed auth" -m "<the executing session's attribution trailer>"
```

---

## Task F3: K8s rendering in `LlamaCppManager`

Moved to `2026-11-05-vllm-ray-gpu-fleet-F3-llamacpp-rpc-manifests.md` (size rule). Runs after F2 and before F4.

---
