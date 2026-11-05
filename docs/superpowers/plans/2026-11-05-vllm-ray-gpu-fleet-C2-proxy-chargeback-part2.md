# vLLM on Ray GPU Fleet — Plan C, part 2b: savings/chargeback rollups and verification gate (Tasks C8–C9; continuation of `...-C2-proxy-chargeback.md`)

> Tasks C4–C6 are in `2026-11-05-vllm-ray-gpu-fleet-C2-proxy-chargeback.md`; Task C7 is in `2026-11-05-vllm-ray-gpu-fleet-C2-proxy-chargeback-part1b.md`. This file holds C8–C9, the C2-auth matrix (end of file) and the open items; interfaces and decisions there apply unchanged. Commit steps: explicit files (no `git add -A`), never `git stash`, + the executing session's attribution trailer.

### Task C8: Savings/chargeback rollup (all local kinds, AM10) and metrics

**Files:**
- Create: `shared/fleet/vllm_ray/rollup.py` (savings and chargeback queries)
- Modify: `shared/observability/vllm_metrics.py` (C4 created it), `services/management/app/services/vllm_reconciler.py` (emit gauges after refresh), `services/management/app/api/v1/fleet_vllm.py` (placement outcomes; the savings and chargeback routes switch from their C2 501 answers to the rollup calls), Plan B `shared/fleet/vllm_ray/backend.py` (`reconcile`: route its load-time observation through `record_model_load_seconds`)
- Test: `tests/unit/fleet/test_vllm_rollup.py`, `tests/unit/test_vllm_metrics.py`; drop C2's `xfail` markers in `tests/unit/management/test_fleet_vllm_rollups.py`

**Interfaces:**
- Consumes: `token_usage` (+ Plan B columns), `vllm_deployments`, `ollama_deployments`, `llamacpp_deployments` (`comparison_model`, `public_id`, pricing columns; AM10), `team_members` (`models_sqlalchemy.py:122`), `token_conversion_rates` (AM1/AM2), `GpuRecord`, `DeploymentStatus`, `LocalDeploymentKind`.
- Produces:
  - `def savings(db, org_id: int, since: date, kind: int | None = None) -> SavingsResult`; `def chargeback(db, org_id: int, since: date, team_id: int | None, deployment_id: int | None, kind: int | None = None) -> ChargebackResult`; `SavingsResult(tokens_input, tokens_output, virtual_cost, commercial_equivalent_cost, savings: Decimal, by_deployment: list, rows_examined, rows_missing_price: int)`; `ChargebackRow(team_id: int | None, local_deployment_kind: int, local_deployment_id: int, tokens_input, tokens_output, request_count, virtual_cost: Decimal)`; `by_deployment` keyed by `(kind, id)`; `ChargebackResult(rows, total_virtual_cost, rows_examined, rows_missing_price)`
  - `vllm_metrics`: `record_inventory(records: list[GpuRecord])` (gauges `waddleai_gpu_vram_total_mb`/`waddleai_gpu_vram_used_mb`, labels `vendor` nvidia|amd|intel, `gpu_class`, `node`, summed per label set); `record_deployment_counts(counts: dict[int, int])` (gauge `waddleai_vllm_deployments{status}`, zero when absent); `record_placement(outcome: str)` (counter `waddleai_vllm_placement_decisions_total{outcome}`, outcomes `placed|denied_origin|denied_quantization|denied_cap|no_fit`, else `ValueError`); `record_model_load_seconds(seconds: float)` (histogram `waddleai_vllm_model_load_seconds`)

**Decisions:** rows = `token_usage` with `organization_id = org`, `date >= since`, `local_deployment_kind IS NOT NULL` (+ filters: `kind` keeps one kind; `deployment_id` requires `kind`, else `ValueError`); virtual cost per row = `in*unit_price_input/1e6 + out*unit_price_output/1e6` from the stored snapshot; NULL snapshot prices → excluded, counted in `rows_missing_price`, never free; commercial equivalent = Σ tokens × the `comparison_model` rates from `comparison_rates` (C2, `shared/fleet/vllm_pricing.py`; AM1, AM2); a `comparison_model` with no enabled commercial rate row → reported missing; savings = commercial − virtual (may be negative); a multi-team user goes to the lowest `team_id`, a teamless user to `None`; Python aggregation, `Decimal` throughout. Gauges: sync `Meter.create_gauge` if the pinned `opentelemetry-api` has it (≥1.23), else an observable gauge over a snapshot dict. Recorders swallow exporter errors.

- [ ] **Step 1: Write the failing tests**

```python
# test_vllm_rollup.py   (fake DAL rows)
test_chargeback_groups_team_and_deployment_exact_decimals   # 2 teams x 2 deployments, hand-computed
test_chargeback_filters_team_deployment_org_and_period
test_chargeback_multi_team_user_lowest_team_once_teamless_none
test_chargeback_null_price_row_excluded_and_counted         # rows_missing_price == 1
test_chargeback_empty_period_zero_rows
test_savings_commercial_minus_virtual_total_and_per_deployment
test_savings_negative_and_missing_commercial_price_reported
test_rollup_covers_ollama_and_llamacpp_rows_and_kind_filter   # AM10
test_chargeback_deployment_without_kind_rejected               # AM10
# test_vllm_metrics.py   (metric_reader fixture)
test_inventory_gauges_sum_per_vendor_class_node    # exact values + label sets
test_deployment_counts_point_per_status_including_zero
test_vllm_deployments_gauge_labelled_by_status_zero_for_absent   # AM19d: waddleai_vllm_deployments{status} exact per-status value from vllm_deployments rows; absent statuses report 0
test_placement_counter_outcomes_and_unknown_raises
test_model_load_histogram_one_point_sum_12_5
test_reconcile_serving_transition_emits_one_load_histogram_point   # real VllmRayBackend + fake client
test_create_endpoint_emits_placement_outcomes      # placed/denied_origin/denied_cap/no_fit
test_recorders_never_raise_when_meter_broken
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/fleet/test_vllm_rollup.py tests/unit/test_vllm_metrics.py tests/unit/management/test_fleet_vllm_rollups.py -v`
Expected: 17 failed or errored, 0 passed; the `xfail`-marked rollup tests report `xfailed`.

- [ ] **Step 3: Implement**
  - `shared/fleet/vllm_ray/rollup.py` (create): `savings`, `chargeback`, `SavingsResult`, `ChargebackRow`, `ChargebackResult`.
  - `shared/observability/vllm_metrics.py`: `record_inventory`, `record_deployment_counts`, `record_placement`, `record_model_load_seconds`; recorders swallow exporter errors.
  - `services/management/app/services/vllm_reconciler.py`: call `record_inventory` and `record_deployment_counts` after each refresh.
  - `services/management/app/api/v1/fleet_vllm.py`: call `record_placement` once per create outcome; the savings and chargeback routes call `rollup.savings` / `rollup.chargeback` instead of returning 501.
  - `shared/fleet/vllm_ray/backend.py` (`reconcile`): `record_model_load_seconds` on the SERVING transition.
  - `tests/unit/management/test_fleet_vllm_rollups.py`: remove the `xfail` markers; add the DTO field-set, `7d|30d` and invalid-400 tests.

- [ ] **Step 4: Run to verify pass:** `pytest tests/unit/fleet tests/unit/test_vllm_metrics.py tests/unit/management -q` → Expected: 0 failed, 0 `xfailed`; passed count printed.

- [ ] **Step 5: Commit**

```bash
git add shared/fleet/vllm_ray/rollup.py shared/fleet/vllm_ray/backend.py shared/observability/vllm_metrics.py services/management/app/services/vllm_reconciler.py services/management/app/api/v1/fleet_vllm.py tests/unit/fleet/test_vllm_rollup.py tests/unit/test_vllm_metrics.py tests/unit/management/test_fleet_vllm_rollups.py
git commit -m "feat(fleet): savings/chargeback rollups and vLLM fleet metrics"
```

---

### Task C9: Verification gate

**Files:** none (evidence for the PR description).

- [ ] **Step 1: Unit suite.** `make test-unit 2>&1 | tee /tmp/claude-1000/-home-penguin-code-waddleai/<session-id>/scratchpad/c9-unit.log; echo "exit=${PIPESTATUS[0]}"` → `exit=0`, `fail_under = 90` met, collected count printed and stated in the PR, zero `xfailed`. `<session-id>` = the executing session's id, written out literally before running.
- [ ] **Step 2: Lint, types, OpenAPI.** Run `make lint` (exit 0); `mypy --strict` over every module named in Step 3 → `Success: no issues found`; `make generate-openapi && git diff --exit-code openapi/v1.yaml && make openapi-lint` → exit 0.
- [ ] **Step 3: Per-module coverage.** Run `pytest tests/unit -q --cov-branch --cov-fail-under=90 --cov-report=term-missing --cov=services.management.app.api.v1.fleet_vllm --cov=services.management.app.services.vllm_reconciler --cov=shared.routing.providers --cov=shared.routing.prefer_local --cov=shared.quota.virtual_price --cov=shared.fleet.vllm_pricing --cov=shared.fleet.vllm_ray.rollup --cov=shared.fleet.vllm_ray.model_size --cov=shared.observability.vllm_metrics --cov=shared.fleet.pricing`. Expected: exit 0; the report shows ≥ 90% on each of: `fleet_vllm`, `vllm_reconciler`, `routing.providers`, `routing.prefer_local`, `quota.virtual_price`, `fleet.vllm_pricing`, `fleet.vllm_ray.rollup`, `fleet.vllm_ray.model_size`, `observability.vllm_metrics`, `fleet.pricing` (a module absent from the report = FAIL).
- [ ] **Step 4: Security and flag gates.** `make test-security` (exit 0); `pytest tests/unit -k "second_flag or flag_off" -q` prints a count including `test_every_route_404_when_second_flag_off`.
- [ ] **Step 5: Prove a gate can fail.** break `upsert_virtual_rate` (e.g. `input_rate` × 2); `pytest tests/unit/quota/test_virtual_price.py -q` must exit non-zero; revert the mutation.

---

## Open items (owner)

- Prefer-local hand-off (`stages.py:1185-1210`) is traced and proven by C7's end-to-end test (AM5); C7 is not done until that test passes.
- Rate semantics: resolved — rates are divisors (LLM tokens per credit) per the corrected AM2 formula in the master plan (`input_rate = base_cost_per_waddleai_token × 1e6 / price_input_per_1m`).

---

### Task C2-auth: Auth matrix over every new management endpoint (AM19d)

**Files:**
- Create: `tests/unit/management/test_fleet_vllm_auth_matrix.py`

- [ ] **Step 1: Write the parametrized matrix.** Routes are generated from `app.url_map` at collection time, so a new endpoint is covered without editing the test:

```python
NEW_PREFIXES = ("/api/v1/fleet/vllm", "/api/v1/fleet/llamacpp", "/api/v1/fleet/ollama",
                "/api/v1/fleet/models", "/api/v1/fleet/gpu-inventory", "/api/v1/fleet/savings",
                "/api/v1/fleet/chargeback")
NEW_ROUTES = [(re.sub(r"<[^>]+>", "1", r.rule), m) for r in app.url_map.iter_rules()
              if r.rule.startswith(NEW_PREFIXES) for m in r.methods - {"HEAD", "OPTIONS"}]
assert NEW_ROUTES, "zero routes examined = FAIL"

@pytest.mark.parametrize("path,method", NEW_ROUTES)
async def test_new_route_401_without_jwt(client, path, method):
    resp = await client.open(path, method=method)
    assert resp.status_code == 401

@pytest.mark.parametrize("path,method", NEW_ROUTES)
async def test_new_route_403_without_fleet_admin(client, path, method, jwt_without_fleet_admin):
    resp = await client.open(path, method=method, headers={"Authorization": f"Bearer {jwt_without_fleet_admin}"})
    assert resp.status_code == 403
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/management/test_fleet_vllm_auth_matrix.py -v`
Expected: 2 × len(NEW_ROUTES) cases; a route missing `require_scope` fails its 403 case (the gate can fail). Zero collected = FAIL (the assert above).

- [ ] **Step 3: Implement** — verify each route carries `require_scope(Permission.FLEET_ADMIN)`; add it where missing in `fleet_vllm.py` (C2, C8, C3b weights DELETE), `llamacpp.py` (C2b, C2c), `ollama.py` (C2b) and `fleet.py` (C2c secrets routes). Step 2's failing cases name the routes.

- [ ] **Step 4: Run to verify pass:** same command → Expected: 0 failed; passed count = 2 × len(NEW_ROUTES).

- [ ] **Step 5: Commit**

```bash
git add tests/unit/management/test_fleet_vllm_auth_matrix.py services/management/app/api/v1/fleet_vllm.py services/management/app/api/v1/llamacpp.py services/management/app/api/v1/ollama.py services/management/app/api/v1/fleet.py
git commit -m "test(fleet): 401/403 auth matrix over every vLLM/llama.cpp/Ollama management route"
```

---
