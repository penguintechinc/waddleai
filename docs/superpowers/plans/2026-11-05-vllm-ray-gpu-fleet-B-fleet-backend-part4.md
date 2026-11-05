# vLLM on Ray GPU Fleet — Plan B, part 4 of 6: Task B7 (VllmRayBackend lifecycle and reconcile)

> Part of `2026-11-05-vllm-ray-gpu-fleet-B-fleet-backend.md` (index). Global Constraints, Review Focus, and decisions D1–D9 are in the index and bind every task here. Tasks, Files, Interfaces, and steps below are unchanged from the index plan.

### Task B7: VllmRayBackend lifecycle and reconcile

**Files:**
- Create: `shared/fleet/vllm_ray/backend.py`
- Consumes (created in B6, not modified here): `tests/conformance/_fake_ray.py`
- Create: `tests/unit/fleet/vllm_ray/test_backend.py`

**Interfaces:**
- Consumes: `RayServeClient` and `FakeRayServeClient` (B6); `build_serve_app`, `placement_group_spec` (B5); `collect`, `upsert_inventory`, `GpuRecord` (B3); `fit_check`, `Pool`, `FitResult` (B4); enums (B1); ORM (B2); `InferenceFleetBackend`, `BackendType`, `ManagementScope`, `Endpoint`, `FleetHealth`, `ModelPlacement`, `NodeInfo`, `ProvisionSpec` (`shared/fleet/base.py`); `register` (`shared/fleet/registry.py`).
- Produces:
  - `@register(BackendType.VLLM_RAY) class VllmRayBackend(InferenceFleetBackend)`; `type = BackendType.VLLM_RAY`; `management_scope = ManagementScope.FULL_LIFECYCLE`
  - `def __init__(self, db: Any, config: dict[str, Any], credentials: dict[str, str] | None, client: RayServeClient | None = None, *, canary: Callable[[str, str], Awaitable[bool]] | None = None)` (D6)
  - ABC methods: `provision(spec)`, `deprovision(node_id)`, `health()`, `list_nodes()`, `place_model(model, constraints)`, `endpoints_for(model)`
  - `async def deploy(self, dep) -> None`; `async def scale(self, dep, replicas_min: int, replicas_max: int) -> None`; `async def stop(self, dep) -> None`; `async def delete(self, dep) -> None`; `async def reconcile(self, dep) -> int`; `async def collect_inventory(self) -> list[GpuRecord]`

`config` keys: `head_url` (required; `ValueError` if absent), `serve_base_url` (required; the route base, e.g. `http://waddleai-inference-head-svc:8000`), `model_store_path` (default `"/models"`), `class_map` (default `{}`), `kv_headroom_pct` (default `20`), `nccl` (dict, default from Helm-provided env; keys per B5), `allow_mixed_class` is per deployment, not config. `credentials` may contain `ray_token`. `hf_token` is ignored here (Plan C/A).

**Defaults file (AM9).** `__init__` first loads the JSON object at env `WADDLEAI_VLLM_RAY_BACKEND_CONFIG` (the chart-mounted ConfigMap `waddleai-vllm-ray-fleet-config`; snake_case keys such as `tco.*`, `worker_groups[].cost.*`) as DEFAULTS. Env unset → defaults `{}`. Env set but file missing, non-JSON, or not a JSON object → `RuntimeError("vLLM Ray config file unreadable")`. The `config` argument (the `fleet_backends.config` row) is merged on top: row wins per top-level key; `tco` and `worker_groups` are replaced whole, never deep-merged.

Status mapping (`reconcile`). `desired` = `dep.status`; Serve status `s`:

| Serve `s` | Current `dep.status` | New status | Notes |
|---|---|---|---|
| `DEPLOYING` / `NOT_STARTED` | any non-SERVING, non-DEGRADED | `STARTING` | |
| `DEPLOYING` / `NOT_STARTED` | SERVING or DEGRADED | unchanged | scaling, not a failure |
| `RUNNING` | PENDING, PULLING, STARTING | `WARMING`, then canary | canary passes → `SERVING` |
| `RUNNING` | WARMING | canary | pass → `SERVING`; fail → stays WARMING, message "canary failed" |
| `RUNNING` | SERVING | `SERVING` | |
| `RUNNING` | DEGRADED | canary | pass → `SERVING`, message "recovered" |
| `DEPLOY_FAILED` | SERVING | `DEGRADED`, `status_detail = Serve message` | Review Focus 2 |
| `DEPLOY_FAILED` | any other | `FAILED`, `status_detail = Serve message` | |
| `NOT_FOUND` | STOPPED | unchanged | |
| `NOT_FOUND` | other | `FAILED`, detail `"Serve application missing"` | |

- Every change of `dep.status` appends a `vllm_deployment_events` row (`state`, `message`, `ts`) and sets `updated_at`.
- Histogram `waddleai_vllm_model_load_seconds` observed on the transition to `SERVING` (value = seconds since `dep.created_at` or since the STARTING event). Recorded via the OTel meter `shared.utils` helper used elsewhere in fleet code; test asserts it is called once.
- `endpoints_for(model)` returns `Endpoint(url=dep.endpoint_url, node_id=dep.serve_app_name, loaded_models=[model], healthy=True)` for every row with `served_model_name == model`, `status == SERVING`, `endpoint_url` set. Any other status (including DEGRADED) → excluded.

`deploy(dep)`:
1. Model path = `f"{model_store_path}/{dep.slug}"` (weights are pulled by the existing HF downloader, Plan C; if the path is absent the deployment is FAILED with `"weights not in model store"`).
2. Set `dep.status = STARTING`, event row. Set `dep.endpoint_url = f"{serve_base_url}/d/{slug}/v1"`.
3. Build full desired set: every row of this backend with status not in (`STOPPED` with `deleted` marker) — i.e. all non-deleted rows; rows with `status == STOPPED` contribute min_replicas 0 (B5). Call `client.put_applications({"applications": [...]})`.
4. On `RayClientError`: `FAILED`, `status_detail = str(exc)`, event, re-raise as `RuntimeError` with plain message `"Ray Serve rejected the deployment; see deployment events."`

`scale`: update `replicas_min/max`, then full PUT. `stop`: `status = STOPPED`, full PUT (min_replicas → 0, app kept). `delete`: `status = STOPPED`, `endpoint_url = None`, full PUT with this row excluded, then `client.delete_application` (D3: the same full-set PUT is the delete; call `delete_application(name, remaining)` for the removal). Event `"deleted"`. The DB row itself is removed by Plan C's API layer after `delete` returns. Review Focus 4: after `delete`, `endpoints_for` returns `[]` immediately because `endpoint_url` is None and status is STOPPED.

`place_model(model, constraints)`: if `constraints.get("lazy")` → look up a SERVING row with `served_model_name == model`; found → `ModelPlacement(model, node_id=row.serve_app_name, "placed")`; else `RuntimeError("No GPU deployment is serving {model!r}. Deploy it from the GPU Fleet page.")`. Not lazy → same lookup. (Placement of new models is done by the wizard in Plan C, not here.)

`list_nodes`: rows from `gpu_inventory` for this backend, grouped by `(node_uid, gpu_class)` (D7). `vram_total_mb` = sum of `vram_total_mb` of the group; `vram_free_mb` = sum of `(total - used)` over `schedulable=True` rows; `healthy` = any row with `last_seen` within 180 s and state WARM. `gpu_vendor` = row vendor; `gpu_class` = class; `loaded_models` = served names of SERVING rows whose `gpu_class` equals the group's class and `vendor` matches (approximation; documented in docstring). `kind="k8s"`, `node_id = f"{node_name}/{gpu_class}"`.

`health`: `client.get_application` is not used; health = `FleetHealth(backend_id=self.fleet_backend_id, healthy=<any node healthy>, node_count=len(nodes), detail={"deployments": {status_name: count}})`. Health never raises: a `RayClientError` returns `healthy=False` with `detail={"error": "ray head unreachable"}`.

`provision(spec)`: `spec.constraints["deployment_public_ids"]` list → for each, `deploy(row)`; return `list_nodes()`. `deprovision(node_id)`: `PermissionError("vLLM deployments are stopped or deleted per deployment; deprovision is unsupported")`.

`collect_inventory`: `records = await InventoryCollector(self.fleet_backend_id, org_id).collect(self.client)`; `upsert_inventory(...)`; if `records` empty → log WARNING and return `[]`. `org_id` from `config["org_id"]`.

Canary default (when `canary is None`): `GET {url}/models` expects 200 with the served model id in `data[].id`; then `POST {url}/chat/completions` with `{"model": served, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4}` expects 200. Lives in `backend.py` as `_default_canary`. Tests always inject `canary`.

- [ ] **Step 1: Write the failing tests** (`test_backend.py`). Fixtures: `db` = `FakeDAL` (tests/conformance/_fake_dal.py) with `vllm_deployments`, `vllm_deployment_events`, `gpu_inventory` tables; `client = FakeRayServeClient()`; `canary = AsyncMock(return_value=True)`; `backend = VllmRayBackend(db, config=CFG, credentials=None, client=client, canary=canary)`; `dep` fixture: a row with `served_model_name="devstral-small"`, `slug="devstral-small"`, `serve_app_name="vllm-devstral-small"`, `status=PENDING`, `replicas_min=0`, `replicas_max=1`, etc.

Tests (names and assertions):

```python
# AM9 defaults file: env WADDLEAI_VLLM_RAY_BACKEND_CONFIG -> tmp JSON
def test_init_loads_configmap_defaults_row_wins(): ...  # row head_url wins; untouched defaults (e.g. tco.kwh_usd) survive
def test_init_env_unset_uses_row_only(): ...  # no env -> only row keys present
def test_init_unreadable_defaults_file_raises_runtime_error(): ...  # non-JSON or non-object -> RuntimeError("vLLM Ray config file unreadable")
# deploy and link auth
async def test_deploy_puts_full_desired_set_and_sets_starting(): ...  # status STARTING; "vllm-devstral-small" in client.apps; endpoint_url "http://serve:8000/d/devstral-small/v1"; last event STARTING
async def test_deploy_failure_marks_failed_with_detail(): ...  # client.fail_next_put=RayClientError("HTTP 500: boom") -> RuntimeError "Ray Serve rejected the deployment"; FAILED; "HTTP 500" in status_detail
async def test_deploy_refuses_multi_node_without_link_auth(): ...  # pp=2, mutual_auth and internal_tls False -> LinkAuthRefused(LINK_AUTH_REFUSAL); FAILED; client.puts == []
async def test_deploy_multi_node_allowed_with_either_link_auth(): ...  # parametrized: mutual_auth only / internal_tls only -> one PUT each
async def test_deploy_single_node_needs_no_link_auth(): ...  # pp=1, both False -> one PUT
async def test_place_model_applies_same_link_auth_refusal(): ...  # pp=2, both False -> LinkAuthRefused before any PUT
# reconcile (Serve status table)
async def test_reconcile_running_warms_then_canary_serves(): ...  # RUNNING from STARTING -> SERVING; canary awaited once with (endpoint_url, "devstral-small")
async def test_reconcile_canary_failure_stays_warming(): ...  # canary False -> WARMING; "canary" in status_detail
async def test_reconcile_deploy_failed_after_serving_is_degraded_with_message(): ...  # Review Focus 2: SERVING + DEPLOY_FAILED "CUDA out of memory..." -> DEGRADED; status_detail == message; last event DEGRADED
async def test_degraded_endpoint_is_not_offered(): ...  # Review Focus 2: endpoints_for == []
async def test_serving_endpoint_is_offered(): ...  # one Endpoint, healthy True, url == endpoint_url
async def test_reconcile_not_found_when_not_stopped_is_failed(): ...  # NOT_FOUND from STARTING -> FAILED, detail "Serve application missing"
async def test_reconcile_stopped_not_found_is_unchanged(): ...  # STOPPED + NOT_FOUND -> STOPPED
async def test_reconcile_appends_event_only_on_change(): ...  # two identical reconciles -> exactly one event row
async def test_observes_model_load_histogram_on_serving(): ...  # patched histogram called exactly once on SERVING
# delete, stop, scale, place
async def test_delete_removes_serve_app_and_endpoint_immediately(): ...  # Review Focus 4: app gone from client.apps; endpoint_url None; endpoints_for == []
async def test_delete_keeps_other_deployments_in_put(): ...  # remaining app names still in client.apps after delete
async def test_stop_keeps_app_with_min_replicas_zero(): ...  # STOPPED; app kept; autoscaling min_replicas 0
async def test_scale_updates_replicas_and_reputs(): ...  # max_replicas in last PUT == new replicas_max
async def test_place_model_lazy_without_serving_deployment_raises(): ...  # RuntimeError "Deploy it from the GPU Fleet page"
# nodes, health, inventory, teardown
async def test_list_nodes_groups_by_node_and_class(): ...  # 2 classes on one node -> 2 NodeInfo, same node_uid, vram_free_mb = sum
async def test_list_nodes_stale_rows_unhealthy(): ...  # last_seen older than 180 s -> healthy False
async def test_health_reports_unreachable_ray_without_raising(): ...  # client.fail_next_get -> healthy False, detail {"error": "ray head unreachable"}
async def test_collect_inventory_upserts_and_empty_payload_returns_empty(): ...  # rows upserted; empty payloads -> []
async def test_deprovision_unsupported(): ...  # PermissionError
def test_no_token_in_logs(caplog): ...  # credentials {"ray_token": "SECRET123"}: "SECRET123" not in caplog.text
async def test_idle_teardown_sweep_never_deprovisions_vllm_ray_nodes(): ...  # AM19d: IdleTeardownController.sweep makes zero deprovision calls for vllm_ray nodes
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/fleet/vllm_ray/test_backend.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'shared.fleet.vllm_ray.backend'`

- [ ] **Step 3: Implement `backend.py`**

- `_transition(dep, new_status, message)`: no-op if unchanged; else set status, status_detail, updated_at, and append an event (via `asyncio.to_thread` DB write).
- Use `asyncio.to_thread` around every `db(...)` call, matching `shared/fleet/registry.py` `build_backends_for_org`.
- `_full_desired_set(exclude=None)`: all rows of this backend (`db.vllm_deployments.fleet_backend_id == self.fleet_backend_id`), excluding `exclude`, each mapped with `build_serve_app(row, f"{model_store_path}/{row.slug}", self._nccl_env())`; `_nccl_env()` returns `config["nccl"]`.
- Every PUT goes through `_put_full(desired_rows)`.
- Link auth (AM11, spec §6.3 layer 1): `config['link_auth'] = {'mutual_auth': bool, 'internal_tls': bool}` (snake_case; rendered by A2's `fleet-backend-config.yaml` from `fleet.security.mutualAuth` and `fleet.vllmRay.rayInternalTls`; defaults true/true). `deploy` and `place_model` check it before any PUT: if `dep.pipeline_parallel > 1` and neither flag is true, `raise LinkAuthRefused(LINK_AUTH_REFUSAL)` with `LINK_AUTH_REFUSAL = "enable Cilium mutual auth (SPIRE) or Ray internal TLS to deploy across nodes"` (spec §6.3 wording). `deploy` also sets the row to FAILED with that detail. Single-node (pp=1) is never refused by this check.
- `reconcile` implements the mapping table. The canary is awaited only on RUNNING paths.
- Logging: `import logging`; `logger = logging.getLogger(__name__)`, the same pattern as `shared/fleet/registry.py:20` and `shared/fleet/placement.py:43`. No penguin logging helper is imported by any module under `shared/fleet/`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/fleet/vllm_ray/test_backend.py -v --cov=shared.fleet.vllm_ray.backend --cov-branch --cov-report=term-missing`
Expected: summary `31 passed, 0 failed` (30 tests; the parametrized multi-node test runs twice); coverage ≥ 90%.

Run: `mypy --strict shared/fleet/vllm_ray/backend.py`
Expected: `Success: no issues found`.

- [ ] **Step 5: Commit**

```bash
git add shared/fleet/vllm_ray/backend.py tests/unit/fleet/vllm_ray/test_backend.py
git commit -m "feat: add VllmRayBackend lifecycle and Serve status reconcile" -m "<the executing session's attribution trailer>"
```
