# vLLM on Ray GPU Fleet — Plan C, part 1 of 2: management API and lifecycle glue (Tasks C1–C2, C2b; C3 in `...-C1-api-part2.md`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the `vllm_ray` fleet backend through the flag-gated management API (`fleet:admin`), and keep `connection_links`, local offers and prefer-local rules in step with deployment status.

**Architecture:** `fleet.py` accepts the new backend type behind a second flag; `fleet_vllm.py` serves the vLLM routes as `@validate_response` DTOs; a leader-guarded reconciler drives Plan B's `VllmRayBackend.reconcile` and mirrors `SERVING` state into `connection_links`. Part 2 (`...-C2-proxy-chargeback.md`): proxy connector, routing consolidation, chargeback, prefer-local, metrics, gate.

**Tech Stack:** Python 3.13, Quart + quart-schema, penguin-dal, SQLAlchemy models (Plan B), pytest, spectral.

**Spec:** `docs/superpowers/specs/2026-11-05-vllm-ray-gpu-fleet-design.md` (§4.5 step 5, §4.6, §4.7, §5.2, §7). Master: `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-00-master.md` (Cross-plan Interfaces are binding). Plan B: `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-B-fleet-backend.md` (+ `-part1..5`). Branch `feature/vllm-ray-c` (stacked on B), worktree in `.worktrees/`.

## Global Constraints

- Python 3.13, Quart, `@dataclass(slots=True)`, penguin logging only; OTel on every new path (instruments: part 2 C8).
- Both flags gate every new path: `waddleai.fleet_v2` AND `waddleai.fleet-vllm-ray` (default OFF; unseen OFF). Env override for tests: `WADDLEAI_FLAG_FLEET_V2=1`, `WADDLEAI_FLAG_FLEET_VLLM_RAY=1` (`shared/utils/feature_flags.py:127`). Flag-off → 404 `{"status":"error","error":"not_found"}`.
- `@require_auth` + `@require_scope(Permission.FLEET_ADMIN)`; org from `g.user["organization_id"]`; cross-org 403, missing 404 (`fleet.py:305-322`).
- UUID `public_id` at the API boundary (never the numeric PK); int enums on the wire.
- Explicit dataclass DTOs under `@validate_response`; lists bounded by `PageRequest.from_request()`; invalid filter → 400 (`_validation_error`).
- Runtime DB access via penguin-dal `db(...)` inside `asyncio.to_thread` (as `fleet.py:325-350`).
- Coverage ≥90% on new modules; gates must be able to fail; zero examined = FAIL.
- Refusals say what to change in plain language; Plan B's explanation strings pass through verbatim.
- Commit steps: explicit files (no `git add -A`), never `git stash`, + the executing session's attribution trailer. Never commit to `release/v0.4.X`.

## Review Focus

1. Delete while requests are in flight → Serve app, `connection_links` row and local `model_configs` entry all removed, no orphan offers. (C3; offers side part 2 C5)
2. Same name in two orgs OK, twice in one org → 409; a same-named commercial model never lets the vLLM destination win implicitly. (C2, C3; offers side C5)
3. `DEGRADED` keeps the `connection_links` row; the offer reads unavailable. (C3; offers side C5)
4. PATCH price affects new requests only; zero/negative/NaN price → 400. (C2)
5. Fit-check that fits nothing → 200 `fits=false` + refusal; create → 422 with the same text; unclassified GPUs never in pools. (C2)

---

### Task C1: `vllm_ray` in the existing fleet backends API

**Files:**
- Modify: `services/management/app/api/v1/fleet.py:60` (add `FLEET_VLLM_RAY_FLAG` after `FLEET_V2_FLAG`), `:80-90` (`_VALID_TYPES`), `:252-253` (add `_vllm_ray_enabled` after `_fleet_v2_enabled`), `:352-375` (`create_fleet_backend`: gate + config validation)
- Test: `tests/unit/management/test_fleet_api.py` (add `class TestVllmRayBackendType`, reuse `_enable_flag`, `make_mock_backend`, `_entitled` helpers at lines 17-55)

**Interfaces:**
- Consumes: `BackendType.VLLM_RAY` (Plan B1).
- Produces: `FLEET_VLLM_RAY_FLAG: str = "waddleai.fleet-vllm-ray"`; `def _vllm_ray_enabled(org_id: int) -> bool` (True only when `_fleet_v2_enabled(org_id)` AND the new flag); `_VALID_TYPES` contains `"vllm_ray"`. Default `management_scope` for `vllm_ray` stays `full_lifecycle` (existing expression at `:366-370` already yields it — pinned by a test, not changed).

- [ ] **Step 1: Write the failing tests** in `TestVllmRayBackendType` (helpers `_vllm_payload(**o)` = `{"name":"gpu","type":"vllm_ray","config":{"head_url":"http://head:8265","serve_base_url":"http://head:8000"}}`, `_enable_vllm(monkeypatch)` sets `WADDLEAI_FLAG_FLEET_VLLM_RAY=1`):

```python
test_vllm_ray_requires_second_flag          # fleet_v2 only -> POST 404
test_vllm_ray_creates_with_both_flags       # 201; data.management_scope == "full_lifecycle"
test_vllm_ray_is_not_pro_gated              # _entitled(False) -> 201; check_feature not called
test_vllm_ray_requires_head_and_serve_urls  # parametrize {}, head only, serve only -> 400 naming both keys
test_vllm_ray_enabled_needs_both_flags      # 4 combos; only (1,1) True
test_vllm_ray_in_valid_types
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/management/test_fleet_api.py -k VllmRayBackendType -v`
Expected: 11 failed (collected cases of the six tests in Step 1).

- [ ] **Step 3: Implement** the constant, `_vllm_ray_enabled`, the `_VALID_TYPES` member, and in `create_fleet_backend`: after the existing `_fleet_v2_enabled` 404 check, `if data.type == "vllm_ray" and not _vllm_ray_enabled(org_id): return 404 not_found`; after `config` is resolved, `if backend_type == "vllm_ray"` and `head_url`/`serve_base_url` missing or not `http(s)://` → `_validation_error("config.head_url and config.serve_base_url are required for vllm_ray (http or https URLs)")`. `_PRO_GATED_TYPES` unchanged.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/management/test_fleet_api.py -v`
Expected: 11 passed, 0 failed; existing tests in the file unchanged.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/fleet.py tests/unit/management/test_fleet_api.py
git commit -m "feat(fleet): accept vllm_ray backend type behind waddleai.fleet-vllm-ray"
```

---

### Task C2: `fleet_vllm.py` blueprint

Per-deployment token minting, rotation routes and cluster secret endpoints (AM11, AM13, AM15): Task C2c in `2026-11-05-vllm-ray-gpu-fleet-C1-api-part3.md`, run after C2b.

**Files:**
- Create: `services/management/app/api/v1/fleet_vllm.py`, `shared/fleet/vllm_ray/model_size.py`, `shared/fleet/vllm_pricing.py`
- Modify: `services/management/app/api/v1/__init__.py:34-40` (add `fleet_vllm` to the import list), `services/management/app/models_sqlalchemy.py` (B2 columns only: the `ModelRegistry` fields the models list reads; no migration), `openapi/v1.yaml` (regenerate, do not hand-edit)
- Test: `tests/unit/management/test_fleet_vllm_inventory.py`, `test_fleet_vllm_fitcheck.py`, `test_fleet_vllm_deployments.py`, `test_fleet_vllm_rollups.py`, `tests/unit/fleet/test_vllm_pricing.py`

**Interfaces:**
- Consumes (Plan B, exact): `VllmRayBackend` (`deploy/scale/stop/delete/reconcile`), `fit_check`, `Pool`, `FitResult`, `Layout`, `build_pools`, `QUANT_MATRIX`, `GpuRecord`, the five enums, tables `gpu_inventory`/`vllm_deployments`/`vllm_deployment_events`; existing `build_backend` (`registry.py:88`), `is_denied_origin` (`placement.py:94`), `CapEnforcer.enforce_model_cap` + `CapExceededError` (`caps.py:43,158`), B2 columns (AM1: `expose_via_proxy`, `prefer_local`, `comparison_model`, `ModelRegistry.public_id/params_b`), and from `fleet.py`: `_vllm_ray_enabled`, `_now`, `_validation_error`, `_get_org_scoped_backend`.
- Produces:
  - Routes on `api_v1_bp` under `/fleet` (style of `fleet.py:323`; URL prefix `/api/v1/fleet`).
  - `def parse_params_b(model_ref: str) -> float | None` — regex `(?<![\d.])(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])` on the last path segment.
  - `shared/fleet/vllm_pricing.py`: `def comparison_rates(db, comparison_model: str | None) -> dict | None` (enabled commercial rate row, savings only; None when absent); `def tco_price(backend_cfg: dict, layout: Layout, params_b: float, observed_tokens_per_s: float | None) -> TcoResult` (inputs via B4d `load_pricing_inputs(config: dict)`, calls `compute_tco` from `shared/fleet/vllm_ray/tco.py`, Plan B B4b).
  - No pricing env vars: the discount knob `WADDLEAI_VLLM_PRICE_DISCOUNT_PCT` is deleted (AM6); no per-class map or USD-per-credit vars (AM2). TCO inputs live in the backend config row (editable via `PUT /fleet/backends/<id>`).

**Decisions:**

- Price suggestion (AM6, AM10, spec §4.7): `GET /api/v1/fleet/price-suggestion?kind=1|2|3&model_ref=|params_b=&vendor=&gpu_class=&tp=&pp=&replicas=&count=` (`kind` defaults to 1 = vLLM; `GET /vllm/price-suggestion` stays an alias with `kind=1`). `kind=1` runs `fit_check` on this org's WARM pools for that layout, then `tco_price` (observed throughput if the model already has an observation, else estimate) and returns the `TcoResult` DTO (`hourly_cost_usd, gpu_hourly_usd, node_hourly_usd, node_base_usd, tokens_per_s_used, source, price_input_per_1m, price_output_per_1m, breakdown`). Missing/invalid params 400; no fit 422 with the `FitResult.refusal`; `compute_tco` `ValueError` (e.g. `kwh_usd must be set`, unknown GPU class) → 422 with its message verbatim. For `kind=2|3` there is no Ray placement, so no `fit_check`: `gpu_class` must exist in the static reference (AM8) and `count` (GPUs, default 1) defines a single-node layout (`tp=count`, `pp=1`, `replicas=1`). Observed throughput is the same kind's rolling 24h p50. `comparison_model` is kept only for savings.
- `GET /vllm/models` (B2 columns, AM1/AM4): DTO `{name, public_id: str (never null), params_b: float | None (stored; null → UI asks the admin), origin, min_vram_gb}`; `?q=` case-insensitive substring on `name`; `?limit=` 1–100 (default 25), non-integer → 400; ordered by `name`.
- Create body: `name` (1-255), `model_ref` (1-512), `served_model_name` (default last path segment of `model_ref`), `fleet_backend_id: int` (org-scoped via `_get_org_scoped_backend`; the backends API already exposes the int id), `vendor`, `gpu_class`, `allow_mixed_class=False`, `quantization`, `dtype` (`bfloat16|float16|float32`), `tensor_parallel`, `pipeline_parallel`, `replicas_min` (≥0), `replicas_max` (≥ min), `max_model_len` (ints ≥1), `price_mode` (int, default 1 = AUTO_TCO; 2 = MANUAL; else 400), `price_input_per_1m`/`price_output_per_1m` (decimal strings; MANUAL: required, finite, `> 0`, else 400; AUTO: ignored if sent), `comparison_model` (required; savings baseline), `params_b: float | None`, `expose_via_proxy=True`, `prefer_local: {"fallback_model": str, "match": dict} | None`.
- Create order, first failure wins: flags 404 → body/enum 400 → backend ownership 403/404 → deny-list (`is_denied_origin(registry origin or HF repo owner)`) 403 `"This model's publisher is on the blocked-origin list, so it cannot be deployed."` → `QUANT_MATRIX[vendor]` 422 naming allowed options → `comparison_model` without a rate row 422 → `CapEnforcer.enforce_model_cap` 403 with upgrade text → `fit_check` on this backend's WARM pools, `fits=False` → 422 with `FitResult.refusal` verbatim (`None` → `"The fit check did not explain why. Contact your administrator."`) → `(org_id,name)` pre-check 409 → AUTO: `tco_price` on the fit layout sets `price_input_per_1m`/`price_output_per_1m`, `price_breakdown` (TcoResult breakdown) and `observed_tokens_per_s=None` (`ValueError` → 422); never trusts client prices → insert (`status=PENDING`, `price_mode`, `slug` = lowercase `[a-z0-9-]` of name, ≤64; `serve_app_name=f"d-{slug}"`) → `await backend.deploy(dep)` (raises → row `FAILED` + `status_detail`, 502) → 201. `params_b` = body value, else `parse_params_b(model_ref)`, else 422 `"Can't infer model size from the name; enter the parameter count in billions."`.
- No migration in this plan: `expose_via_proxy`, `prefer_local` (JSON) and `comparison_model` are B2 columns (AM1). The prefer-local rule id lives in `prefer_local.rule_id` (set by C3).
- PATCH (all optional): `replicas_min`/`replicas_max` → `backend.scale`; `action: "stop" | "resume"` → `backend.stop` / `backend.deploy` (`stop` on STOPPED → 409; `resume` on STOPPED/FAILED re-deploys); `price_mode` and `price_*` (manual override at any time: switching to MANUAL requires prices `> 0`, else 400; switching back to AUTO recomputes via `tco_price` and refreshes `price_breakdown`) update the row (rate-row upsert is C6, part 2) — the link sync (Task C3) propagates them, so only later requests are charged the new price.
- DELETE: `backend.delete(dep)` → `fleet_secrets.remove_key(slug)` (C2c) → `unlink_deployment(db, dep)` (C3) → row delete → 200 `{"deleted": true}`.
- Events `GET .../<uuid>/events`: `ts` desc, bounded page, `{ts, state, message}`.
- `GET /vllm/flags` → `{"fleet_v2": bool, "fleet_vllm_ray": bool}`; any `fleet:admin` caller, never 404.
- `GET /gpu-inventory` filters `vendor` (int in `GpuVendor`), `gpu_class`, `node` (matches `node_name`), `schedulable` (`true|false`); invalid → 400; order `node_name, gpu_index`; UNCLASSIFIED rows listed with `schedulable=false`.
- Fit-check body: `model_ref | params_b` (≥1 required), `quantization`, `dtype`, `max_model_len`, `vendor?`, `gpu_class?`, `allow_mixed_class=False`; `pools = build_pools(records)` from this org's `WARM` rows filtered by vendor/class. Response = `FitResult` DTO + `allowed_quantizations` (`sorted(QUANT_MATRIX[vendor])`, or the union over vendors in the pools; `[]` with no WARM GPUs).
- `GET /api/v1/fleet/savings?period=7d|30d&kind=` (default `7d`, else 400; `kind` optional 1|2|3, absent = all local kinds) and `GET /api/v1/fleet/chargeback?period=&team=&kind=&deployment=` (`kind` required when `deployment` is given; `deployment` = that kind's `public_id` UUID, 404 unknown/foreign; `kind` outside 1|2|3 → 400) call `rollup.savings(db, org_id, since, kind)` / `rollup.chargeback(db, org_id, since, team_id, deployment_id, kind)` from `shared/fleet/vllm_ray/rollup.py` (created by C8). Until C8 both routes answer 501 `{"status":"error","error":"not_implemented"}` and their tests are `xfail(strict=True, reason="C8")`; C8 removes the markers. `/vllm/savings` and `/vllm/chargeback` remain as aliases pinned to `kind=1`. AM10.

- [ ] **Step 1: Write the failing tests.** Reuse `client`, `app_mock_db`, `admin_auth_headers`, `user_auth_headers`, `make_select_result` (`tests/unit/management/conftest.py`); add `make_inventory_row`/`make_deployment_row` factories and `_both_flags(monkeypatch)`. Test names (assertion in the name unless noted):

```python
# test_fleet_vllm_inventory.py
test_flags_endpoint_reports_each_flag_independently   # 4 combos -> exact booleans, always 200; user token 403, none 401
test_gpu_inventory_flag_off_is_404
test_gpu_inventory_filters_vendor_class_node_schedulable   # parametrized; assert query conditions
test_gpu_inventory_empty_result                       # 200, data == [], meta.total == 0
test_gpu_inventory_invalid_filter_values_400          # vendor=9, vendor=abc, schedulable=maybe
test_gpu_inventory_pagination_meta                    # page=2&limit=1 -> meta.page==2, meta.limit==1
test_gpu_inventory_query_always_org_scoped
test_gpu_inventory_unclassified_gpu_listed_unschedulable   # Review Focus 5
test_models_search_q_and_limit               # q, clamp, bad limit 400, params_b from column (null allowed), public_id non-null UUID
# test_fleet_vllm_fitcheck.py
test_fit_check_uses_only_warm_pools                   # COLD/UNCLASSIFIED/MIXED_UNSUPPORTED excluded
test_fit_check_returns_refusal_text_with_200          # Review Focus 5
test_fit_check_allowed_quantizations_vendor_and_union # NVIDIA -> sorted(QUANT_MATRIX[1]); INTEL -> [0]; none -> union
test_fit_check_requires_model_ref_or_params_b_400
test_fit_check_unparseable_model_ref_422
test_fit_check_no_side_effects                        # backend.deploy / vllm_deployments.insert never called
test_price_suggestion_returns_tco_result_dto   # fields == TcoResult; 400 missing params; 422 no-fit refusal; 422 ValueError text
```

```python
# test_fleet_vllm_deployments.py
test_create_happy_path_201_pending_deploy_called_once
test_create_denied_origin_403_before_fit_or_deploy
test_create_quant_not_in_matrix_422
test_create_free_cap_exceeded_403_upgrade_text
test_create_does_not_fit_422_with_refusal_verbatim    # + refusal None -> fallback text
test_create_manual_requires_positive_price   # price_mode=2; parametrized: missing, 0, -1, "nan", "abc" -> 400   (Review Focus 4)
test_create_auto_ignores_client_price   # price_mode default; client 9.99 ignored; stored == tco_price output; price_breakdown persisted
test_create_invalid_price_mode_400
test_create_expose_and_prefer_local_persisted   # expose_via_proxy default true; prefer_local stored
test_create_comparison_model_without_rate_422   # comparison_model required and must have a rate row
test_create_same_name_same_org_409
test_create_same_name_different_org_ok                # Review Focus 2
test_create_served_name_equal_to_commercial_model_ok  # no model_configs write at create
test_create_backend_foreign_org_403
test_create_backend_deploy_error_marks_failed_502
test_list_filters_status_vendor_model_pagination_empty_org_scoped_and_invalid_400
test_detail_has_timeline_404_unknown_403_foreign_org
test_patch_replicas_calls_scale / test_patch_replicas_max_below_min_400
test_patch_stop_then_resume / test_patch_stop_on_stopped_409
test_patch_price_updates_row_only_validates_positive  # backend untouched (Review Focus 4)
test_patch_switch_to_manual_with_prices_works   # price_mode=2 + prices persisted; without prices 400
test_patch_back_to_auto_recomputes
test_delete_orders_backend_delete_then_remove_key_then_unlink_then_row   # mock manager call order
test_events_ordered_desc_paginated_org_scoped
test_every_route_404_when_second_flag_off             # parametrized over all paths
test_dtos_have_exact_field_sets                       # response keys == dataclass fields
# test_fleet_vllm_rollups.py: period/team/deployment validation (400/404) -- xfail(strict) until C8
# tests/unit/fleet/test_vllm_pricing.py
test_parse_params_b_table      # 24B->24.0, 0.5b->0.5, 70B-Instruct->70.0, "org/model"->None, llama-3.1-8b-instruct->8.0
test_comparison_rates_default_first_enabled_commercial   # no param; disabled and local rows ignored; None when absent
test_tco_price_builds_inputs_from_backend_config   # keys -> TcoInputs/GpuClassCost; ValueError passes through
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/management/test_fleet_vllm_*.py tests/unit/fleet/test_vllm_pricing.py -v`
Expected: collection error (ModuleNotFoundError: fleet_vllm, model_size, vllm_pricing).

- [ ] **Step 3: Implement** `model_size.py`, `vllm_pricing.py`, DTO dataclasses (wire names = field names, `{status, data, meta}` wrappers like `FleetBackendListResponse`), helpers `_gate(org_id)` (404 unless `_vllm_ray_enabled`), `_load_inventory(db, org_id, backend_id=None) -> list[GpuRecord]`, `_pools(records, vendor, gpu_class)`, then the routes per the decisions. `CapEnforcer` takes `_get_license_client()`. Register the module in `api/v1/__init__.py`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/management/test_fleet_vllm_*.py tests/unit/fleet/test_vllm_pricing.py -v`
Expected: 0 failed; the C8-marked rollup tests report `xfailed` (count printed).

- [ ] **Step 5: Regenerate and lint the OpenAPI spec**

Run: `make generate-openapi && make openapi-lint; echo "exit=$?"`
Expected: `exit=0`; `grep -c "/api/v1/fleet/vllm\|/api/v1/fleet/gpu-inventory" openapi/v1.yaml` ≥ 8 (print the count).

- [ ] **Step 6: Commit**

```bash
git add services/management/app/api/v1/fleet_vllm.py services/management/app/api/v1/__init__.py services/management/app/models_sqlalchemy.py shared/fleet/vllm_ray/model_size.py shared/fleet/vllm_pricing.py openapi/v1.yaml tests/unit/management/test_fleet_vllm_inventory.py tests/unit/management/test_fleet_vllm_fitcheck.py tests/unit/management/test_fleet_vllm_deployments.py tests/unit/management/test_fleet_vllm_rollups.py tests/unit/fleet/test_vllm_pricing.py
git commit -m "feat(fleet): vLLM fleet management API (inventory, fit-check, deployments, price suggestion)"
```

---

### Task C2b: Ollama and llama.cpp price fields (AM10)

**Files:**
- Modify: `services/management/app/api/v1/ollama.py` (create `POST /ollama/deployments` at `:284`; update `PUT /ollama/deployments/<id>` at `:342`), `services/management/app/api/v1/llamacpp.py` (create `POST /llamacpp/deployments` at `:252`; update `PATCH /llamacpp/deployments/<id>` at `:318`)
- Test: `tests/unit/management/test_local_pricing_api.py`

**Interfaces:**
- Consumes: `apply_pricing`, `gpu_set_for`, `LocalDeploymentKind`, `load_pricing_inputs(config: dict) -> tuple[TcoInputs, dict[str, GpuClassCost]]` (B4d, `shared/fleet/pricing.py`; consumed, not redefined here); `comparison_rates` (C2, `shared/fleet/vllm_pricing.py`).
- Produces: create and update accept `price_mode` (int, default 1; else 400), `price_input_per_1m` / `price_output_per_1m` (decimal strings, MANUAL requires finite `> 0`, AUTO ignores client prices), `comparison_model` (required when pricing; no rate row → 422, as vLLM), and the GPU fields the pricing reads: Ollama `gpu_config.node_selector` and `gpu_config.gpu_count` (int ≥ 1); llama.cpp `node_selector` and `gpu_count`. The `node_selector` must carry `waddleai.penguintech.io/gpu-classes`. Responses add `public_id`, `price_mode`, `price_input_per_1m`, `price_output_per_1m`, `price_breakdown`, `comparison_model`.
- Create and update call `apply_pricing(db, kind, row, inputs, costs, observed_tokens_per_s=None)` after the row write, in the same transaction. `ValueError` → 422 with its message verbatim. Existing paths and status codes are unchanged for requests without pricing fields.

- [ ] **Step 1: Write the failing tests** (`test_local_pricing_api.py`, fake DAL, `kind` 2 and 3)
  - `test_ollama_create_manual_persists_prices_and_public_id`
  - `test_ollama_create_auto_ignores_client_price_and_stores_breakdown`
  - `test_llamacpp_patch_switch_to_manual_requires_positive_prices` (parametrize missing, `0`, `-1`, `"nan"`, `"abc"` → 400)
  - `test_auto_without_gpu_class_label_is_422`
  - `test_comparison_model_without_rate_row_is_422`
  - `test_local_rate_row_upserted_with_provider_ollama_or_llamacpp`
  - `test_existing_create_without_pricing_fields_unchanged` (regression: no price fields → 201 as before)

- [ ] **Step 2: Run to verify failure** `pytest tests/unit/management/test_local_pricing_api.py -v` → Expected: 11 failed (7 tests; the patch test runs 5 cases).

- [ ] **Step 3: Implement** the body parsing (shared helper `parse_price_fields(body)` in `services/management/app/api/v1/_pricing_body.py`, reused by both files, same 400 rules as C1), the `node_selector`/`gpu_count` validation, and the `apply_pricing` calls.

- [ ] **Step 4: Run to verify pass** `pytest tests/unit/management/test_local_pricing_api.py tests/unit/management -k "ollama or llamacpp" -q` → Expected: 11 passed, 0 failed in the new file; the `-k` run reports 0 failed.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/ollama.py services/management/app/api/v1/llamacpp.py services/management/app/api/v1/_pricing_body.py tests/unit/management/test_local_pricing_api.py
git commit -m "feat(fleet): price fields for ollama and llama.cpp deployments"
```

---

Task C3: see `2026-11-05-vllm-ray-gpu-fleet-C1-api-part2.md` (execute right after C2).

---


## Open items (owner; none block C1–C3)

- `model_configs` has no `org_id`: a SERVING vLLM local offer is visible to every org's routing; connection links are isolated by `owner_org_id` (AM3). Open for owner.
