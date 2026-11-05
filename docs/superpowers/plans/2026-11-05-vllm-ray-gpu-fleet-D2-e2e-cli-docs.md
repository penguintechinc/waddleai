# vLLM on Ray GPU Fleet — Plan D: UI, CLI, E2E, Docs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the flag-gated "GPU Fleet" admin UI (overview, deployments, 6-step deploy wizard, backend settings), its Playwright e2e, the `waddleai fleet vllm` CLI subcommands, and the admin guide with screenshots, all consuming Plan C's management API.

**Architecture:** Plain JSX React pages in `services/webui/src/` (the webui has no TypeScript; DTO types are JSDoc `@typedef`s), data via TanStack Query hooks over a typed axios client, all routes and nav links gated by a server-reported flag status. Pure logic (label maps, wizard reducer/validators, layout labels) lives in importable modules so it is unit-tested without rendering. CLI adds clap subcommands and thin `ApiClient` methods to the existing `waddleai` binary.

**Tech Stack:** React 18.3.1, react-router-dom 7.18.2, @tanstack/react-query 5.101.4, axios 1.20.0, Vite 8, Vitest 4.1.12 + @testing-library/react 15 (jsdom), ESLint 8 (`npm run lint`); Playwright (`tests/e2e`); Rust, clap 4.6.6, reqwest 0.13.4, wiremock 0.6.5; MkDocs site in `docs/docs-site/`.

**Spec:** `docs/superpowers/specs/2026-11-05-vllm-ray-gpu-fleet-design.md` (§5.1 UI, §5.3 CLI, §7 tests/docs). Master plan: `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-00-master.md`. Depends on Plan C (API). Branch `feature/vllm-ray-d` stacked on `feature/vllm-ray-c`, worktree under `.worktrees/`.

## Global Constraints

- Both flags gate every new route, nav link, and API call: `waddleai.fleet_v2` AND `waddleai.fleet-vllm-ray`; default OFF; unseen → OFF.
- The UI never recomputes server-owned numbers (fit, savings, chargeback, prices). It displays API values.
- Copy for admins is plain language; no bare "TP", "PP", "dtype", "quantization" without the explanation string from the API or the `Explain` constant in Task D4.
- Every refusal shows the API's refusal text verbatim; the UI never rewrites it.
- Every new component/function has a 2–3 line JSDoc (`general.md`); no ASCII-art dividers.
- Coverage ≥90% on new webui files and Rust modules; `set -euo pipefail` in any script; zero examined = FAIL.
- Commit trailer on every commit:
  the executing session's attribution trailer
- Never commit to `release/v0.4.X`; never push unless the branch-backup rule applies.

## Review Focus

Inputs and failure modes the spec implies but no spec-named test covers. Each names the task that owns its test.

1. API returns a status or state int outside the enum (e.g. `9`) → UI renders "Unknown status (9)", never crashes or shows blank. (D1, D3)
2. Fit-check returns `fits: false` with `refusal: null` → Deploy stays disabled and the UI says "The fit check did not explain why. Contact your administrator." (D4)
3. Fit-check request fails (network/5xx) after a previous `fits: true` → Deploy becomes disabled; a stale "fits" is never shown. (D4)
4. Inventory empty (no GPUs) → overview empty state states what to do (label nodes / check device plugin). (D2)
5. Savings with zero tokens in the window → "No tokens served locally in the last 7 days", not "$0.00 saved" or `NaN`. (D2)
6. Parameter count unknown → numeric input is required; Next is blocked; no default value is inserted. (D4)
7. Scaling a deployment to zero or deleting a `SERVING` deployment → confirm copy names in-flight requests. (D3)

> **Split:** Interface Decisions, Task D5b, and Tasks D6, D7 and D9 are in this file. Tasks D1–D3 are in `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-D1-webui.md`; Task D4 is in `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-D1-webui-part3.md`; Tasks D4b and D5 are in `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-D1-webui-part2.md`; Task D8 is in `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-D2-e2e-cli-docs-part2.md`.

## Interface Decisions (resolved here; sub-plans use these names)

**Flag status endpoint (provided by Plan C):** `GET /api/v1/fleet/vllm/flags` → `{"fleet_v2": bool, "fleet_vllm_ray": bool}`, scope `fleet:admin`. Server resolves flags; the webui has no PostHog client. `useVllmRayEnabled()` is true only when both are true.

**Plan C DTO additions used by the UI (provided by Plan C):**
- `POST /api/v1/fleet/vllm/fit-check` response adds `allowed_quantizations: number[]` (the `QUANT_MATRIX[vendor]` values for the requested pool).
- `POST /api/v1/fleet/vllm/deployments` body adds `expose_via_proxy: bool` (default true) and `prefer_local: {match: object, fallback_model: string} | null`.
- `GET /api/v1/fleet/vllm/models?q=&limit=` (provided by Plan C) → model_registry rows for the org (`public_id`, `name`, `params_b|null`, `origin`, `min_vram_gb`); wizard step 1 model search.
- Deployment DTO field names are taken from spec §6.1 in snake_case. Plan C's `openapi/v1.yaml` is authoritative; Task D1 step 1 reconciles names against it before any hook is written.

**Shared JS interfaces (`services/webui/src/api/vllmFleet.js`, Task D1):**

```js
/** @typedef {{id:number,public_id:string,name:string,slug:string,vendor:number,gpu_class:string,
 *   nodes_used:number,gpus_per_node:number,tensor_parallel:number,pipeline_parallel:number,
 *   replicas_min:number,replicas_max:number,replicas:number,status:number,status_detail:string|null,
 *   served_model_name:string,max_model_len:number,quantization:number,price_input_per_1m:string,
 *   price_output_per_1m:string,p50_latency_ms:number|null,tokens_per_min:number|null}} VllmDeployment */
export function listDeployments(params)            // GET /api/v1/fleet/vllm/deployments
export function getDeployment(publicId)            // GET .../deployments/<uuid>  (includes events)
export function createDeployment(body)             // POST .../deployments
export function patchDeployment(publicId, patch)   // PATCH; patch: {replicas_min?,replicas_max?,stopped?}
export function deleteDeployment(publicId)         // DELETE
export function fitCheck(body)                     // POST .../fit-check
export function getInventory(params)               // GET /api/v1/fleet/gpu-inventory
export function getSavings({period, kind})         // GET /api/v1/fleet/savings?period=7d|30d&kind=  (AM10)
export function getChargeback(params)              // GET /api/v1/fleet/chargeback?period=&team=&kind=&deployment=
export function getPriceSuggestion(params)         // GET /api/v1/fleet/price-suggestion?kind=1|2|3&...
export function searchModels(params)              // GET /api/v1/fleet/vllm/models?q=&limit=
export function getFlags()                         // GET /api/v1/fleet/vllm/flags
```

Label maps (`services/webui/src/api/vllmFleetLabels.js`, Task D1):

| Map | Values |
|---|---|
| `VENDOR_LABEL` | 1 "NVIDIA", 2 "AMD", 3 "Intel" |
| `DEPLOYMENT_STATUS_LABEL` | 1 "Queued", 2 "Downloading model", 3 "Starting engine", 4 "Warming up", 5 "Serving", 6 "Degraded — see details", 7 "Stopped", 8 "Failed" (**decision**; only "Serving" is fixed by spec) |
| `INVENTORY_STATE_LABEL` | 1 "Warm (running)", 2 "Cold (scaled to zero)", 3 "Unclassified — not schedulable", 4 "Mixed GPUs in one node — not supported yet" |
| `QUANT_LABEL` | 0 "Full precision (no quantization)", 1 "AWQ (4-bit, pre-quantized)", 2 "GPTQ (4-bit, pre-quantized)", 3 "FP8 (8-bit)", 4 "bitsandbytes (on-the-fly 4-bit, NVIDIA only)" |
| `unknownLabel(kind, n)` | returns `Unknown ${kind} (${n})` |

`formatAge(ms)` → "updated 30s ago" / "updated 4m ago" (`vllmFleetLabels.js`).

**Hooks (`services/webui/src/hooks/useVllmFleet.js`):** `useVllmRayEnabled()`, `useInventory(filters)` (staleTime 30_000, refetchInterval 30_000), `useDeployments(filters)` (staleTime 10_000, refetchInterval 15_000), `useDeployment(publicId)`, `useSavings({period, kind})` (staleTime 60_000), `useFitCheck(body, {enabled})` (mutation-like, debounced by the caller 400 ms), `usePriceSuggestion({kind, ...params})`, `useModelSearch(params)` (staleTime 60_000), mutations `useCreateDeployment`, `usePatchDeployment`, `useDeleteDeployment` (invalidate `['vllm','deployments']`).

**Query root:** `QueryClientProvider` is added once in `services/webui/src/main.jsx` (currently renders `<App />` at lines 19–23 with no provider; `@tanstack/react-query` is in `package.json` but unused).

**Routes (`services/webui/src/App.jsx`, inside the ProtectedRoute `<Routes>` at lines 38–49):** `/gpu-fleet` (Overview), `/gpu-fleet/deployments`, `/gpu-fleet/deploy` (wizard), `/gpu-fleet/deployments/:publicId` (detail drawer route optional — drawer only is enough), `/gpu-fleet/backend`. Nav link "GPU Fleet" in `services/webui/src/components/Header.jsx` nav (lines 24–28), rendered only when `useVllmRayEnabled()`.

**Components:** `components/gpu/NodeCard.jsx` (props `{node, gpus[]}`), `components/gpu/VramBar.jsx` (`{usedMb, totalMb}`), `components/gpu/SavingsCard.jsx` (`{period, onPeriodChange, kind, onKindChange}` uses `useSavings`), `components/gpu/StatusChip.jsx` (`{status}`), `components/gpu/StepTimeline.jsx` (`{events}`), `components/gpu/ConfirmAction.jsx` (`{title, body, confirmLabel, onConfirm, onCancel}`), `components/gpu/layoutLabel.js` (`layoutLabel({nodes_used, gpus_per_node, replicas})`: "1 node · 2 GPUs" when nodes_used=1; "3 nodes · 2 GPUs each" otherwise).

**Wizard state (`components/gpu/wizard/wizardState.js`, Task D4):** pure `initialWizardState()`, `wizardReducer(state, action)`, `validateStep(stepNo, state) → {ok, errors[]}`, `buildCreateBody(state)`. Steps are `1..6` in spec order.

---

### Task D5b: Nav link gated by flag

**Files:**
- Modify: `services/webui/src/components/Header.jsx:24-28` (add `<Link to="/gpu-fleet" className="nav-link">GPU Fleet</Link>` rendered only when `useVllmRayEnabled()` is true; add sub-links Deployments, Backend in a dropdown or as three links)
- Test: `services/webui/src/test/Header.test.jsx` (extend existing file)

- [ ] **Step 1: Add failing tests** to `Header.test.jsx`: with flag true → link "GPU Fleet" exists; with flag false → `queryByText('GPU Fleet')` is null. Mock `useVllmRayEnabled`.
- [ ] **Step 2: Run** `cd services/webui && npx vitest run src/test/Header` → FAIL.
- [ ] **Step 3: Implement** the conditional link.
- [ ] **Step 4: Run** `npx vitest run src/test/Header` → PASS (existing Header tests unchanged and passing).
- [ ] **Step 5: Commit** `feat(webui): flag-gated GPU Fleet nav link` with the trailer.


---

### Task D6: Playwright e2e and mock-data seeding

**Files:**
- Create: `tests/e2e/tests/gpu_fleet.spec.js`
- Create: `scripts/seed_gpu_fleet_mock.py`
- Modify: `Makefile:190-191` (replace the stub recipe line `@echo "No mock data seeding defined"` with `@echo "Seeding GPU fleet mock data..."` followed by `$(PY) scripts/seed_gpu_fleet_mock.py`). `PY` is defined at `Makefile:23`.

**Interfaces:**
- Consumes: the seeding script writes through the management ORM (`GpuInventory`, `VllmDeployment`, `VllmDeploymentEvent` from Plan B) against the dev DB; it is idempotent (`ON CONFLICT DO NOTHING` on `(fleet_backend_id, node_uid, gpu_index)` and `(org_id, name)`).
- Produces: 4 `gpu_inventory` rows (2 NVIDIA on one node: RTX 4090 ×2 and RTX 4070 ×1 on a second node; 1 AMD MI210 node; 1 Intel Arc A770 node), 2 deployments (one `SERVING`, one `STOPPED`) with events, and 1 `model_registry` row `qwen-12b-awq` (`params_b` 12) so the wizard model search returns a result.

- [ ] **Step 1: Write the failing seed tests**

`tests/unit/test_seed_gpu_fleet_mock.py`:
- `test_seed_twice_is_idempotent`: call `seed(db)` twice → both return `{"gpu_inventory": 4, "vllm_deployments": 2, "model_registry": 1}`; DB row counts are 4, 2, 1 (not doubled).
- `test_seed_zero_rows_exits_nonzero`: monkeypatch the writer to write nothing → `main()` raises `SystemExit` with code 1.

Run: `.venv/bin/python -m pytest tests/unit/test_seed_gpu_fleet_mock.py -v` (`make test-unit` takes no `PYTEST_ARGS`: its recipe hard-codes `tests/unit`, `Makefile:114-117`).
Expected: 2 failed, `ModuleNotFoundError` for `seed_gpu_fleet_mock`.

- [ ] **Step 2: Implement the seed script**

`scripts/seed_gpu_fleet_mock.py`: `seed(db) -> dict[str, int]` returns per-table row counts using the idempotent upserts in Interfaces above. `main()` calls `seed`, logs `seeded gpu_inventory=4 vllm_deployments=2 model_registry=1` through penguin logging (no `print`), and raises `SystemExit(1)` when the total row count is 0.

- [ ] **Step 3: Run the seed tests and the target**

Run: `.venv/bin/python -m pytest tests/unit/test_seed_gpu_fleet_mock.py -v` → 2 passed.
Run: `make seed-mock-data` → log line `seeded gpu_inventory=4 vllm_deployments=2 model_registry=1`, exit 0. Exit 1 = FAIL.

- [ ] **Step 4: Write the e2e spec** `tests/e2e/tests/gpu_fleet.spec.js`:
- `describe.skip` unless `process.env.WADDLEAI_VLLM_E2E === '1'` (explicit opt-in; the skip message names the env var). When the env var is set, flag-off is a **failure**, not a skip: `expect(flags.fleet_vllm_ray).toBe(true)`.
- Login via the existing `loginAsAdmin` helper pattern from `tests/e2e/tests/smoke.spec.js` (copy the helper; do not import from another spec).
- `overview renders inventory from seeded data`: goto `/gpu-fleet`; expect 3 vendor headings and `text=Unclassified` absent unless seeded; count node cards === 4 nodes.
- `wizard happy path to review`: goto `/gpu-fleet/deploy`; step 1 search `qwen` via `GET /api/v1/fleet/vllm/models` and choose the seeded model `qwen-12b-awq` (params 12); step 2 expect text `Fits on 1 node` (regex); Next through 3–5; step 6 expect the "Deploy" button enabled and the review shows the chosen layout label.
- `refusal path`: step 1 params `400`; step 2 expect text matching `/Does not fit in any pool/`; Deploy disabled on step 6 (navigate with Next only if enabled — assert Next disabled on step 2 instead).

- [ ] **Step 5: Run e2e**

Run: `cd tests/e2e && WADDLEAI_VLLM_E2E=1 npx playwright test tests/gpu_fleet.spec.js --reporter=list`
Expected: 3 passed, 0 skipped. Then `rm -rf /tmp/playwright-waddleai` (cleanup after every run, pass or fail; CI step with `if: always()`).

- [ ] **Step 6: Commit**

```bash
git add tests/e2e/tests/gpu_fleet.spec.js scripts/seed_gpu_fleet_mock.py tests/unit/test_seed_gpu_fleet_mock.py Makefile
git commit -m "test(e2e): GPU Fleet overview and wizard specs; seed gpu fleet mock data"  # + the executing session's attribution trailer
```

---

### Task D7: CLI `waddleai fleet vllm`

**Files:**
- Modify: `clients/waddleai-cli/src/cli.rs:97-101` (add `Vllm { action: VllmCommand }` and `Secrets { action: SecretsCommand }` variants to `FleetCommand`); `cli.rs:152-154` (dispatch arm); `cli.rs:262` area (add `cmd_fleet_vllm` dispatch call).
- Create: `clients/waddleai-cli/src/vllm.rs` (command implementations, table rendering, unit tests in `#[cfg(test)] mod tests`).
- Create: `clients/waddleai-cli/src/secrets.rs` (`SecretsCommand`, list and rotate, `#[cfg(test)]` module).
- Modify: `clients/waddleai-cli/src/lib.rs` (add `pub mod vllm;` and `pub mod secrets;`).
- Modify: `clients/waddleai-cli/src/api_client.rs` (add `patch_json` helper next to `post_json`; add `gpu_inventory`, `vllm_fit_check`, `vllm_list_deployments`, `vllm_create_deployment`, `vllm_patch_deployment`, `vllm_delete_deployment` near `fleet_status` at line 198).
- Modify: `clients/waddleai-cli/tests/cli_integration.rs` (one binary-level test).
- Modify: `clients/waddleai-cli/README.md` (command table).

**Decision:** the binary is `waddleai` (`Cargo.toml` `[[bin]]`), so commands are `waddleai fleet vllm …`, not `waddleai-cli …` as the spec's shorthand says.

**Interfaces:**
- Produces: `pub enum VllmCommand { Inventory{vendor:Option<String>, schedulable:Option<bool>, json:bool}, Fit{params_b:f64, quantization:u8, dtype:String, max_model_len:u32, allow_mixed_class:bool, json:bool}, Deploy{file:PathBuf, json:bool}, List{status:Option<u8>, json:bool}, Scale{public_id:String, min:u32, max:u32}, Stop{public_id:String}, Delete{public_id:String, yes:bool} }` in `vllm.rs`; `pub async fn run(api: &ApiClient, action: VllmCommand) -> Result<()>`.
- Produces (AM13, AM15): `pub enum SecretsCommand { List{json:bool}, Rotate{name:String, yes:bool} }` in `src/secrets.rs`; `VllmCommand::RotateToken{public_id:String, yes:bool}` in `src/vllm.rs`. Rotate first calls `GET /api/v1/fleet/secrets`; if the named secret has `source: external`, it prints `managed externally` to stderr and exits 2 with no POST. A server 409 with `managed externally` also exits 2.
- Consumes: Plan C endpoints via `ApiClient` methods; existing `CliError` (`src/error.rs`).

- [ ] **Step 1: Write failing tests** in `vllm.rs` (wiremock, pattern from `cli.rs` tests at line ~436 and `api_client.rs` tests at line ~278):
- `inventory_table_prints_vendor_and_class`: mock `GET /api/v1/fleet/gpu-inventory` returns 2 rows → stdout contains `NVIDIA` and `nvidia-rtx4090`.
- `inventory_json_is_passthrough`: `json: true` → stdout parses to the mocked JSON exactly.
- `inventory_filter_sent_as_query`: `vendor: Some("amd")` → request has `vendor=amd` query (wiremock `query_param`).
- `fit_refusal_exit_code_nonzero`: mock returns `{fits:false, refusal:"Does not fit…"}` → `run` returns `Err`, message contains the refusal text verbatim.
- `list_empty_prints_message`: `[]` → stdout `No deployments.`.
- `scale_sends_patch_exact_body`: `PATCH …/deployments/uuid-1` with body `{"replicas_min":0,"replicas_max":3}` (wiremock `body_json`).
- `delete_requires_yes_or_prompt`: `yes:false` in a non-TTY → `Err` containing `Pass --yes to delete without a prompt.`; `yes:true` → DELETE called once.
- `invalid_uuid_rejected_before_request`: `public_id:"nope"` → `Err`, zero requests received (`server.received_requests()` empty).
- `flag_off_403_message`: mock 403 with `{error:"feature not enabled"}` → error text includes `feature not enabled`.
- (in `src/secrets.rs`) `secrets_list_shows_source_and_fingerprint`: `GET /api/v1/fleet/secrets` → table with name, source, fingerprint; no value column.
- (in `src/secrets.rs`) `secrets_rotate_external_exits_2`: GET returns the secret with `source: external` → stderr contains `managed externally`, exit code 2, and `server.received_requests()` holds only the GET (zero POSTs).
- (in `src/secrets.rs`) `secrets_rotate_409_exits_2`: POST returns 409 `{reason:"managed externally"}` → stderr contains the reason, exit code 2.
- `rotate_token_requires_yes_or_prompt`: same contract as delete; `POST .../rotate-token` once with `--yes`.

Run: `cd clients/waddleai-cli && cargo test vllm::`
Expected: FAIL, `vllm` module not found / unresolved items.

- [ ] **Step 2: Implement** `vllm.rs`, `secrets.rs`, api_client methods, and the `FleetCommand::Vllm` and `FleetCommand::Secrets` variants with dispatch. Table output is hand-formatted with padded columns (no new crate). `--json` prints `serde_json::to_string_pretty` of the response.

- [ ] **Step 3: Run tests**

Run: `cd clients/waddleai-cli && cargo test vllm:: && cargo test --test cli_integration`
Expected: all PASS.

- [ ] **Step 4: Lint**

Run: `cd clients/waddleai-cli && cargo clippy --all-targets -- -D warnings && cargo fmt --check`
Expected: exit 0 (no output from fmt --check).

- [ ] **Step 5: Update README** `clients/waddleai-cli/README.md` with every command (including `fleet secrets list|rotate` and `fleet vllm rotate-token`) and one example each.

- [ ] **Step 6: Commit**

```bash
git add clients/waddleai-cli
git commit -m "feat(cli): waddleai fleet vllm subcommands"  # + the executing session's attribution trailer
```

---

### Task D8: Admin guide, README, docs-site, screenshots, OpenAPI link

Moved to `2026-11-05-vllm-ray-gpu-fleet-D2-e2e-cli-docs-part2.md` (size rule).

### Task D9: Gates — lint, unit, coverage, CLI, e2e

**Files:** none modified unless a gate fails. Gate results go in the PR description.

- [ ] **Step 1: Web lint**

Run: `cd services/webui && npm run lint`
Expected: exit 0; output has no errors and zero warnings (`--max-warnings 0` is set in package.json).

- [ ] **Step 2: Web tests with coverage**

Run: `cd services/webui && npm test`
Expected: all test files PASS; the coverage table lists every new file under `src/pages/GpuFleet*`, `src/components/gpu`, `src/api/vllmFleet*`, `src/hooks/useVllmFleet.js` at ≥90% lines and branches. Count of tests reported; a run with zero tests collected = FAIL.

- [ ] **Step 3: Web typecheck of the build**

Run: `cd services/webui && npx vite build`
Expected: build succeeds, exit 0.

- [ ] **Step 4: CLI**

Run: `cd clients/waddleai-cli && cargo clippy --all-targets -- -D warnings && cargo test`
Expected: exit 0; test count reported.

- [ ] **Step 5: Repo gates**

Run: `make lint && make test-unit`
Expected: exit 0; `pytest` reports the 90% coverage gate passing.

- [ ] **Step 6: Flag-on e2e** (see D6 step 5)

Run: `cd tests/e2e && WADDLEAI_VLLM_E2E=1 npx playwright test tests/gpu_fleet.spec.js --reporter=list && rm -rf /tmp/playwright-waddleai`
Expected: 3 passed, 0 skipped.

- [ ] **Step 7: Gate-can-fail check**

Temporarily break one label (`DEPLOYMENT_STATUS_LABEL[5] = 'X'`) and run `npx vitest run src/test/vllmFleetLabels`. Expected: FAIL. Revert and re-run: PASS. Record both results.

- [ ] **Step 8: Commit** — none (gates only). Report counts in the PR body.

---

## Open Questions (Not Determinable From the Repo)

| # | Question | Plan default |
|---|---|---|
| 4 | Webui is JSX, spec says TS types | JSDoc typedefs; no TS migration |
| 5 | `@tanstack/react-query` is installed but no `QueryClientProvider` exists | Add in `main.jsx` (D1) |
| 6 | No existing fleet backend page in webui | Create `/gpu-fleet/backend` (D5) |
| 7 | `make seed-mock-data` is a stub | Real seeder in D6 |
| 8 | Spec says `waddleai-cli`; binary is `waddleai` | `waddleai fleet vllm …` |
| 9 | Docs-site twin of `docs/operations/` pages | Twin + mkdocs nav in D8; verify the twin convention before writing |

Plan C's API must land in its branch before D1 step 1 can pass; D1 is blocked on C otherwise.
