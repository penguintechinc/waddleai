# vLLM on Ray GPU Fleet — B: Fleet Backend Implementation Plan (index; parts 1 of 6 to 6 of 6)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `vllm_ray` fleet backend core in `shared/fleet/vllm_ray/`: enums, schema, GPU inventory parsing, pure fit calculator, Ray Serve config builder, Ray REST client, the `VllmRayBackend` lifecycle, class-aware placement, and conformance coverage.

**Architecture:** Pure-Python modules (parsers, fit, serve config) sit under a thin I/O layer (`ray_client.py`) and the backend (`backend.py`) that persists state through the `penguin-dal`-style `db` handle used by the existing fleet backends. All GPU placement is expressed as Ray custom resources (`gpu_class:<c>`) and placement-group bundles; the backend never talks to vLLM directly.

**Tech Stack:** Python 3.13, `@dataclass(slots=True)`, SQLAlchemy + Alembic (schema only), `httpx` (Ray REST), pytest + `respx`/`httpx.MockTransport`, mypy strict, ruff.

**Spec:** `docs/superpowers/specs/2026-11-05-vllm-ray-gpu-fleet-design.md` (§2 anti-goals, §3, §4.2–4.6, §6.1, §7). Master: `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-00-master.md` (Cross-plan Interfaces are binding).

## Setup (not a task)

- Branch `feature/vllm-ray-b` off `release/v0.4.X` in `.worktrees/vllm-ray-b` (`.worktrees/` is already in `.gitignore`).
- Create a per-worktree venv: `uv venv -p 3.13 .venv && uv pip install --require-hashes -r requirements.txt`.
- Migration 027 (`services/management/alembic/versions/027_configuration_as_code.py`) is currently untracked. It must be committed on `release/v0.4.X` before Task B2 can be merged; B2 revises it.
- Commit prefix: `feat:` for every task (branch type `feature/`).

## Global Constraints

- Python 3.13; `@dataclass(slots=True)` for all new data types; penguin logging only (no `print`, no `logging.basicConfig`); OTel logs + metrics + traces on every new code path.
- Every new feature path checks BOTH flags: `waddleai.fleet_v2` and `waddleai.fleet-vllm-ray` (default OFF; unseen = OFF) via `shared.utils.feature_flags.is_feature_enabled(flag, distinct_id=str(org_id), default=False)`.
- API scope `fleet:admin` (`Permission.FLEET_ADMIN`, `shared/auth/permissions.py:97`). Plan B adds no endpoints; Plan C does.
- Entity identity: numeric BIGINT PK internal, `public_id` UUID external. Int enums for vendor, quantization, status, state. No string references.
- Units: VRAM in **MB** on `NodeInfo` and `gpu_inventory`; **GB** in `model_registry.min_vram`. 1 GB = 1024 MB (matches `shared/fleet/caps.py:40`, `_MB_PER_GB`).
- Ray and vLLM versions are pinned by Plan A; Plan B never hardcodes an image tag.
- No PRC-origin model or dependency. Placement deny-list (`shared/fleet/placement.py`, `_DENIED_ORIGINS`) applies unchanged.
- Tokens from credentials only; never in logs, args, or exception text.
- Commits: explicit file lists (never `git add -A`); never `git stash`; the commit message ends with the executing session's attribution trailer.
- Test fences ≤ 40 lines (names + key assertions); every `Expected:` names a count or message; plan steps carry decided values only (no mid-step corrections).
- Cross-plan owners: `FakeRayServeClient` created in B6; `build_authenticated_openai_app` in B5; `load_pricing_inputs` in B4d; `upsert_virtual_rate` imported from C6 (`shared/quota/virtual_price.py`), not defined in B.
- Copy for admins: every refusal states what to change, in plain language.

## Review Focus

Five spec-implied inputs with no other task's tests exercising them. Each line names the task that owns the test.

1. NVIDIA GPU whose product name matches no `nvidiaClassMap` pattern → `UNCLASSIFIED`, `schedulable=False`, excluded from pools and from fit, shown in inventory. **Owner: B3 (inventory tests) and B4 (`build_pools` exclusion test).**
2. Ray Serve reports `DEPLOY_FAILED` after `SERVING` (e.g. OOM on a long prompt) → status `DEGRADED` with the Serve message; endpoint no longer returned by `endpoints_for` so routing falls back. **Owner: B7 (reconcile test).** The `connection_links` health annotation is tested in C.
3. Streaming response without a final `usage` chunk → counted as `estimated=true` with a missing-usage counter, never silently zero. **Owner: C (connector test).** Not in B.
4. Deployment deleted while requests are in flight → Serve app removed, `endpoints_for` returns `[]` immediately, no orphaned endpoint. **Owner: B7 (delete test).** `connection_links` deletion and offers are tested in C.
5. Same deployment name in two orgs, and same `served_model_name` as a commercial model → unique per org only; `location=local` wins only via a routing rule. **Owner: C (API uniqueness + offers test).** B only enforces `(org_id, name)` and `(org_id, slug)` uniqueness at schema level (B2).

---

## Cross-task decisions (binding for B and for C/D)

| # | Decision | Reason |
|---|---|---|
| D1 | Package path is `shared/fleet/vllm_ray/` (not the single file named in spec §6.1). Master wins. | Module size and the per-concern split. |
| D2 | `gpu_class` in `placement_group_spec` accepts `str | list[str]` (one class per pipeline stage). A list is needed for `allow_mixed_class`. | Stage classes may differ when mixing is allowed. |
| D3 | `RayServeClient.delete_application(name, remaining)` takes the full remaining desired set. Ray's REST API has no per-app delete, so the backend always PUTs the full desired set. | Ray PUT replaces the whole config. Deleting one app means PUTting every other app. |
| D4 | `RayServeClient.put_applications(config)` takes `{"applications": [...]}` for the whole desired set. | Same reason as D3. |
| D5 | `run_inventory` uses the Ray Jobs API (`POST /api/jobs/`, poll status, read logs). Each worker prints one `WAGPU_JSON:` line. | Ray Jobs fans out to every alive GPU node without a second port or agent. No per-worker HTTP server. |
| D6 | `VllmRayBackend.__init__` accepts an extra keyword `canary: Callable[[str, str], Awaitable[bool]] | None = None` (endpoint_url, served_model) → healthy? | Canary must be injectable for tests. Default is an httpx `/v1/models` + one completion. |
| D7 | `FleetHealth` / `NodeInfo.node_id` for vLLM nodes is `"<node_name>/<gpu_class>"`; `node_uid` is the K8s node UID. One `NodeInfo` per (node, class). | A node can host two classes. `count_managed_nodes` dedupes on `node_uid`, so the node is still counted once. |
| D8 | FP8 eligibility is a class-name test, not a vendor-only test: `quant_allowed(vendor, gpu_class, quant)`. `QUANT_MATRIX` keeps vendor-level sets only. | Spec §4.5: FP8 is Ada-or-newer NVIDIA and MI300 AMD only. |
| D9 | Enums in `enums.py` are `IntEnum`, as master specifies. `BackendType` stays `StrEnum` (existing). | Master interfaces. |

---

## Task parts (each ≤25,000 chars per docs/CLAUDE.md file-size rule)

| Part | File | Tasks |
|---|---|---|
| 1 | `2026-11-05-vllm-ray-gpu-fleet-B-fleet-backend-part1.md` | Tasks B1–B2 (enums, BackendType, NodeInfo; migration 028 and ORM) |
| 2 | `2026-11-05-vllm-ray-gpu-fleet-B-fleet-backend-part2.md` | Tasks B3–B4 (GPU inventory parsers; fit calculator) |
| 3 | `2026-11-05-vllm-ray-gpu-fleet-B-fleet-backend-part3.md` | Tasks B5–B6 (Ray Serve config; Ray REST client) |
| 4 | `2026-11-05-vllm-ray-gpu-fleet-B-fleet-backend-part4.md` | Task B7 (VllmRayBackend lifecycle and reconcile) |
| 5 | `2026-11-05-vllm-ray-gpu-fleet-B-fleet-backend-part5.md` | Tasks B8–B10 (placement/caps, conformance, verification gate; open items) |
| 6 | `2026-11-05-vllm-ray-gpu-fleet-B-fleet-backend-part6.md` | Tasks B4b–B4d (TCO pricing AM6; GPU price reference AM8; shared local-kind pricing AM10). Execution order: B4 → B4c → B4b → B4d |

Execute tasks in part order. Each task is its own commit on `feature/vllm-ray-b`.
