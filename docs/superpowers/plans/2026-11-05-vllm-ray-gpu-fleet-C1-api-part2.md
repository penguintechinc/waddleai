# vLLM on Ray GPU Fleet — Plan C, part 1b: lifecycle glue (Tasks C3 and C3b; continuation of `...-C1-api.md`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Context:** Tasks C1–C2 are in `2026-11-05-vllm-ray-gpu-fleet-C1-api.md` (Global Constraints, Review Focus and Architecture there apply here unchanged). This file holds Tasks C3 and C3b; it consumes `tco_price` and the `PriceMode` enum from C2/B4b.

**Spec:** `docs/superpowers/specs/2026-11-05-vllm-ray-gpu-fleet-design.md` (§4.6, §4.7). Branch `feature/vllm-ray-c`, worktree in `.worktrees/`. Commit steps: explicit files (no `git add -A`), never `git stash`, + the executing session's attribution trailer.

---

### Task C3: Lifecycle glue (reconcile loop, link and offer sync)

**Files:**
- Create: `services/management/app/services/vllm_reconciler.py`
- Modify: `services/management/app/__init__.py:193-202` (add a `before_serving` hook that starts the loop with `app.add_background_task`; shut down via `after_serving`), `shared/utils/llm_connectors.py` is NOT touched here
- Test: `tests/unit/management/test_vllm_reconciler.py`

**Interfaces:**
- Consumes: `VllmRayBackend.reconcile(dep) -> int`, `.collect_inventory()`, `build_backend`, `DeploymentStatus`, `insert_connection_link` (`shared/database/models.py:588`), `create_prefer_local_rule` (part 2 Task C7; injected via constructor, tests use a fake), `_vllm_ray_enabled`, `tco_price` (C2), `compute_tco` (B4b), `PriceMode`, `token_conversion_rates` (AM2).
- Produces:
  - `class LeaderGuard(Protocol): async def acquire(self) -> bool`; `PgAdvisoryLeaderGuard` (`pg_try_advisory_lock(7270011)`, held for process lifetime; non-Postgres/error → `False` + warning, never raises); `AlwaysLeader` for tests.
  - `class VllmReconciler: def __init__(self, db, *, guard: LeaderGuard, backend_factory=build_backend, prefer_local_fn=None, deployment_interval_s: float = 15.0, inventory_interval_s: float = 60.0, clock=time.monotonic, sleep=asyncio.sleep, usage_source=None, price_interval_s: float = 86400.0)` (`usage_source.p50_output_tps(db, kind, deployment_id, window_s) -> float | None` reads `token_usage` rows with `local_deployment_kind == kind AND local_deployment_id == deployment_id`); `async def tick(self) -> dict[str, int]` (keys `deployments_examined, status_changes, inventory_refreshes, links_upserted, links_removed, reconcile_errors, prices_recalculated`); `async def run_forever(self) -> None` (`tick` + `sleep(1)`, logs every exception, exits on `CancelledError`).
  - `async def sync_deployment_link(db, dep) -> int | None`; `async def unlink_deployment(db, dep) -> None` (also called by C2 delete).
  - `async def _recompute_auto_prices(db, backend, dep) -> TcoResult | None` (called from `tick`; returns `None` when `compute_tco` raises, price unchanged).
  - Link name `f"vllm-{dep.org_id}-{dep.slug}"` — the column is globally unique, so the org id is what lets two orgs reuse a name (Review Focus 2).

**Decisions:**
- `tick` is a no-op (zero counts) unless `guard.acquire()`. Backends: `fleet_backends` rows `type == "vllm_ray"`, `status != "disabled"`, org passes `_vllm_ray_enabled`. Per backend, built once per tick: `reconcile(dep)` for deployments in `PENDING..DEGRADED` not reconciled within `deployment_interval_s`; `collect_inventory()` when `inventory_interval_s` elapsed (first tick runs both). One failure never stops the rest (log `exc_info`, count `reconcile_errors`). `STOPPED`/`FAILED` are not reconciled.
- After `reconcile` returns `SERVING` and `expose_via_proxy` (B2 column): `sync_deployment_link` upserts the link (`owner_org_id=dep.org_id` (AM3), `provider="vllm"`, `endpoint_url=dep.endpoint_url` verbatim — Plan B builds `<head-svc>:8000/d/<slug>/v1`, `model_list=[served_model_name]`, `enabled=True`, `management_capabilities={"org_id", "local_deployment_kind": kind, "local_deployment_id": dep.id, "unit_price_input": str, "unit_price_output": str}` — how the proxy connector learns deployment and price, see part 2 C4/C6) and sets `connection_link_id`. Local offer: upsert `model_configs` (`model_name=served_model_name`, `preferred_providers=["vllm"]`, `context_length=max_model_len`) **only if no row with that name exists**; an existing row (e.g. a same-named commercial model) is untouched, so the vLLM destination is reachable only through a prefer-local rule (Review Focus 2). `prefer_local` set and `prefer_local.rule_id` absent → call `prefer_local_fn(db, org_id, dep, fallback_model, match)` once, persist the id as `prefer_local.rule_id`.
- `DEGRADED`: link kept and enabled; `management_capabilities.fleet_status = int(status)` is rewritten on every status change (part 2 C5 makes the connector non-routable unless `SERVING`). `FAILED`/`STOPPED`: link `enabled=False`, offer removed. Each tick re-syncs `management_capabilities` prices of SERVING deployments when they differ from the row.
- Daily AUTO price recompute (AM6/AM2/AM10, spec §4.7): in `tick`, for every local kind (vLLM `vllm_deployments`, Ollama `ollama_deployments`, llama.cpp `llamacpp_deployments`; one loop, `kind` = `LocalDeploymentKind`), for each `price_mode == AUTO_TCO` row in `SERVING` (Ollama/llama.cpp: their stored running state; the executor confirms the llama.cpp status enum), once per `price_interval_s` (tracked on the clock, first tick after serving counts): `observed_tokens_per_s = usage_source.p50_output_tps(db, kind, dep.id, 86400)` (rolling 24h p50 output tok/s; `None` → keep estimate, `source="estimate"`); the same `apply_pricing(db, kind, row, inputs, costs, observed_tokens_per_s)` call used by the API (B4d, AM10) computes via `compute_tco`, persists `price_*`, `price_breakdown`, `observed_tokens_per_s`, and upserts the `token_conversion_rates` row (`provider` = `vllm|ollama|llamacpp`, `model` = the kind's served name; rates are divisors per AM2); re-sync link capabilities; write a deployment event `"price recalculated"`. A `compute_tco` error is logged, counted in `reconcile_errors`, price unchanged. MANUAL deployments are never touched; usage rows keep their snapshot price.
- `unlink_deployment` (delete path) removes the link, the `model_configs` row only when `preferred_providers == ["vllm"]`, and the `rule_id` from `prefer_local`.

- [ ] **Step 1: Write the failing tests.** Fakes: `FakeBackend` (scripted `reconcile` returns, call log), in-memory DAL tables (`fleet_backends`, `vllm_deployments`, `connection_links`, `model_configs`), `FakeGuard`, fake `clock`/`sleep`:

```python
test_tick_not_leader_does_nothing              # counts all 0; backend_factory never called
test_tick_reconciles_each_nonterminal_once     # 3 live + STOPPED + FAILED -> 3 calls; deployments_examined == 3
test_tick_respects_deployment_interval         # +10s -> 0 calls; +16s -> calls
test_tick_inventory_every_60s                  # ticks at 0,15,30,45,60 -> collect_inventory called 2x
test_tick_skips_org_when_flags_off
test_one_failing_deployment_does_not_stop_others   # errors counted + logged
test_serving_with_expose_creates_link_and_offer    # exact link fields; model_configs ["vllm"]; connection_link_id set
test_serving_without_expose_creates_nothing
test_link_name_is_org_scoped                   # same slug, orgs 1+2 -> two rows   (Review Focus 2)
test_existing_commercial_model_config_untouched    # byte-identical   (Review Focus 2)
test_degraded_keeps_link_enabled               # Review Focus 3
test_failed_and_stopped_disable_link_and_remove_offer
test_prefer_local_rule_created_once_id_persisted   # two ticks -> fake called once
test_price_change_resyncs_link_capabilities
test_unlink_removes_link_offer_rule_only_if_owned  # ["openai","vllm"] row kept   (Review Focus 1)
test_delete_in_flight_leaves_no_orphans        # link count 0, vllm model_configs 0   (Review Focus 1)
test_auto_price_recomputed_daily_from_observed_p50   # fake usage source 40 tok/s; +86399s -> not recomputed; +86400s -> compute_tco got 40, price_* + observed_tokens_per_s stored, rate row input_rate == 0.001*1e6/price_input_per_1m (output likewise), event "price recalculated", prices_recalculated == 1
test_auto_price_recompute_without_observation_uses_estimate
test_manual_price_never_recomputed
test_price_recompute_error_keeps_old_price_counts_error
test_run_forever_survives_exceptions_and_cancels_cleanly
test_pg_guard_false_on_non_postgres_never_raises
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/management/test_vllm_reconciler.py -v`
Expected: collection error (ModuleNotFoundError: vllm_reconciler).

- [ ] **Step 3: Implement**
  - `services/management/app/services/vllm_reconciler.py` (create): `LeaderGuard`, `PgAdvisoryLeaderGuard`, `AlwaysLeader`, `VllmReconciler` (`tick`, `run_forever`), `sync_deployment_link`, `unlink_deployment`, `_recompute_auto_prices(db, backend, dep)` (usage p50 from `usage_source` → `tco_price` → `apply_pricing` persist → `upsert_virtual_rate` → event `"price recalculated"` → link re-sync). DAL calls in `asyncio.to_thread`.
  - `services/management/app/__init__.py` (modify, beside the Cilium hook): `before_serving` builds `VllmReconciler(db, guard=PgAdvisoryLeaderGuard(engine))` and starts `app.add_background_task(reconciler.run_forever)` unless env `WADDLEAI_VLLM_RECONCILER=0`; stored on `app.extensions["vllm_reconciler"]`; `after_serving` cancels it.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/management/test_vllm_reconciler.py tests/unit/management/test_fleet_vllm_deployments.py -v`
Expected: 22 passed, 0 failed in `test_vllm_reconciler.py`; `test_fleet_vllm_deployments.py` 0 failed (its delete test now exercises the real `unlink_deployment`).

- [ ] **Step 5: Commit**

```bash
git add services/management/app/services/vllm_reconciler.py services/management/app/__init__.py tests/unit/management/test_vllm_reconciler.py
git commit -m "feat(fleet): vLLM reconcile loop with connection_links and offer sync"
```

---

### Task C3b: Model store pull job and weights teardown (AM19a)

**Files:**
- Create: `services/management/app/services/model_store.py`
- Create: `services/management/app/services/model_store_job.py` (k8s Job manifest builder, pure function)
- Modify: `services/management/app/api/v1/fleet_vllm.py` (add `DELETE /api/v1/fleet/models/<model_ref>/weights`, `fleet:admin`)
- Test: `tests/unit/management/test_model_store.py`

**Interfaces:**
- Consumes: `shared/fleet/huggingface.py` digest-verifying downloader (Plan A/B; called inside the Job, never in the API process); `fleet_backends.credentials_ref` (decrypted via the existing Fernet helper in `fleet.py`); `vllm_deployment_events` (B2); the RWX model store PVC (A4); `DeploymentStatus.PULLING` (B1).
- Produces:
  - `async def ensure_weights(db, org_id: int, model_ref: str, deployment_id: int, kind: int) -> str  # returns store path "/models/<org_id>/<model_ref>"`
  - `class WeightsInUse(Exception)` (carries the referencing deployment ids and kinds)
  - `async def delete_weights(db, org_id: int, model_ref: str) -> None  # raises WeightsInUse while any vLLM, Ollama or llama.cpp deployment of this org references model_ref`
  - `def build_pull_job_manifest(*, name: str, namespace: str, image: str, model_ref: str, store_path: str, secret_name: str | None) -> dict` (rootless: `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`; `HF_TOKEN` only via `secretKeyRef` when `secret_name` is set; `ttlSecondsAfterFinished: 300`)

**Decisions:**
- One pull per cluster: `ensure_weights` checks for an existing digest marker at the store path first; present → return the path, no Job, no event (skip-if-present).
- Absent → create one Job per (cluster, model_ref); the row goes to `PULLING`. The Job's progress is written to `vllm_deployment_events` every 10 s as `{bytes_done, bytes_total}`; a `PULLING` row is never left without a progress event for more than 10 s.
- Gated repos: `credentials_ref` is decrypted in the API process, written to a short-lived Secret `waddleai-hf-<deployment_id>`, mounted as env `HF_TOKEN` in the Job, and deleted when the Job completes (or fails). The token is never logged, never in args, never in events.
- Refusal: `delete_weights` queries `vllm_deployments`, `ollama_deployments`, `llamacpp_deployments` for `model_ref` in `org_id`; any hit → `WeightsInUse` → endpoint 409 `{"reason": "weights in use", "deployments": [...]}`. No partial delete.
- Endpoint: 401 without JWT, 403 without `fleet:admin` (covered by the C2 auth matrix test).

- [ ] **Step 1: Write the failing tests** in `tests/unit/management/test_model_store.py` (fake DAL, fake Job client, fake clock, fake downloader):
  - `test_ensure_weights_skips_if_present`: digest marker exists at `/models/<org>/<ref>` → returns path; Job client call count 0; no `PULLING` event.
  - `test_pull_progress_events_every_10s`: fake clock advances 35 s over 4 poll ticks → exactly 3 progress events with `bytes_done` non-decreasing and `bytes_total` set; row status `PULLING`.
  - `test_gated_token_via_short_lived_secret`: `credentials_ref` set → Secret created with key `HF_TOKEN`, Job manifest env uses `secretKeyRef` (no literal value), Secret deleted after Job completes; assert the token string appears in no event row, log record, or manifest literal.
  - `test_delete_weights_refused_while_referenced`: one Ollama deployment references `model_ref` → `WeightsInUse` raised, no file removed, endpoint returns 409; after the reference is removed, delete succeeds (200).
- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/management/test_model_store.py -v`
Expected: 4 FAIL (ImportError / missing functions).

- [ ] **Step 3: Implement** `model_store.py`, `model_store_job.py`, and the DELETE route per Interfaces/Decisions. DAL calls in `asyncio.to_thread`. Use penguin logging only.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/management/test_model_store.py -v --cov=services.management.app.services.model_store --cov-branch --cov-report=term-missing`
Expected: 4 passed, 0 failed; coverage ≥ 90% on `model_store` (branch coverage printed).

Run: `mypy --strict services/management/app/services/model_store.py services/management/app/services/model_store_job.py`
Expected: `Success: no issues found`.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/services/model_store.py services/management/app/services/model_store_job.py services/management/app/api/v1/fleet_vllm.py tests/unit/management/test_model_store.py
git commit -m "feat(fleet): model store pull Job and weights teardown"
```

---
