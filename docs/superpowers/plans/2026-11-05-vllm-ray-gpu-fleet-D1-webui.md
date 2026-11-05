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

> **Split:** tasks D1–D3 are in this file; Task D4 is in `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-D1-webui-part3.md`; Tasks D4b and D5 are in `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-D1-webui-part2.md`; Interface Decisions, Task D5b, Tasks D6–D9 and Open Questions are in `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-D2-e2e-cli-docs.md`.

### Task D1: API client, label maps, flag and data hooks

**Files:**
- Create: `services/webui/src/api/vllmFleet.js`
- Create: `services/webui/src/api/vllmFleetLabels.js`
- Create: `services/webui/src/hooks/useVllmFleet.js`
- Modify: `services/webui/src/main.jsx:19-23` (wrap `<App />` in `QueryClientProvider`; create one `QueryClient` at module scope)
- Test: `services/webui/src/test/vllmFleetApi.test.jsx`, `services/webui/src/test/vllmFleetLabels.test.jsx`, `services/webui/src/test/useVllmRayEnabled.test.jsx`

**Interfaces:**
- Consumes: Plan C endpoints (Interface Decisions above); axios global defaults already set in `main.jsx:12-14`.
- Produces: every export listed under "Shared JS interfaces" and "Hooks" above.

- [ ] **Step 1: Reconcile DTO names with Plan C's OpenAPI**

Run: `grep -n "/api/v1/fleet/" openapi/v1.yaml`
Expected: exactly these five paths are present: `/api/v1/fleet/gpu-inventory`, `/api/v1/fleet/savings`, `/api/v1/fleet/vllm/deployments`, `/api/v1/fleet/vllm/flags`, `/api/v1/fleet/vllm/models`. Every field named in the JSDoc typedefs of this plan appears in the matching C1 schema. A missing path or field is a Plan C defect: fix it in C1 (`openapi/v1.yaml`), then re-run this step. The typedefs in this plan are not edited to match.

- [ ] **Step 2: Write failing label tests**

`vllmFleetLabels.test.jsx` asserts:
- `DEPLOYMENT_STATUS_LABEL` has exactly the keys `1..8` (`Object.keys(...).length === 8`).
- `DEPLOYMENT_STATUS_LABEL[5] === 'Serving'`.
- `INVENTORY_STATE_LABEL[4] === 'Mixed GPUs in one node — not supported yet'`.
- `unknownLabel('deployment status', 9) === 'Unknown deployment status (9)'`.
- `formatAge(30_000) === 'updated 30s ago'`; `formatAge(240_000) === 'updated 4m ago'`.

- [ ] **Step 3: Write failing client tests (Review Focus 1)**

`vllmFleetApi.test.jsx` uses `vi.mock('axios')` and asserts:
- `listDeployments({status: 5, page: 2})` calls `axios.get('/api/v1/fleet/vllm/deployments', {params: {status: 5, page: 2}})`.
- `patchDeployment('uuid-1', {replicas_max: 3})` calls `axios.patch('/api/v1/fleet/vllm/deployments/uuid-1', {replicas_max: 3})`.
- `deleteDeployment('uuid-1')` calls `axios.delete('/api/v1/fleet/vllm/deployments/uuid-1')`.
- `getSavings({period: '7d'})` calls `axios.get('/api/v1/fleet/savings', {params: {period: '7d'}})`; with a kind it adds `kind` (1|2|3), omitted for all local kinds (AM10; `period` matches the C1 contract, which replaces the old `days` param).
- `searchModels({q: 'qwen', limit: 5})` calls `axios.get('/api/v1/fleet/vllm/models', {params: {q: 'qwen', limit: 5}})`.
- A rejected request rethrows an `Error` whose `message` is `response.data.error` when present, else `'Request failed'`.

- [ ] **Step 4: Write failing flag hook test**

`useVllmRayEnabled.test.jsx` (render with a `QueryClientProvider` wrapper, mock `getFlags`):
- `{fleet_v2: true, fleet_vllm_ray: true}` → `enabled === true`.
- `{fleet_v2: true, fleet_vllm_ray: false}` → `false`.
- `{fleet_v2: false, fleet_vllm_ray: true}` → `false`.
- request error → `false` (fail closed, no crash).

- [ ] **Step 5: Run tests to verify they fail**

Run: `cd services/webui && npx vitest run src/test/vllmFleet src/test/useVllmRayEnabled`
Expected: FAIL with "Failed to resolve import" for the new modules.

- [ ] **Step 6: Implement the client, labels, and hooks**

- `vllmFleetLabels.js`: the maps and `unknownLabel`, `formatAge` (no network).
- `vllmFleet.js`: one function per endpoint, using the axios instance defaults (no per-call headers). `request` helper maps errors as in Step 3.
- `useVllmFleet.js`: hooks per Interface Decisions. `useVllmRayEnabled` = `useQuery({queryKey:['vllm','flags'], queryFn: getFlags, staleTime: 60_000, retry: false})` and returns `data?.fleet_v2 === true && data?.fleet_vllm_ray === true` (fail closed on error).
- `main.jsx`: `const queryClient = new QueryClient()`; wrap `<App />` in `<QueryClientProvider client={queryClient}>`.

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd services/webui && npx vitest run src/test/vllmFleet src/test/useVllmRayEnabled`
Expected: all new tests PASS; `npm run lint` clean for the new files.

- [ ] **Step 8: Commit**

```bash
git add services/webui/src/api services/webui/src/hooks/useVllmFleet.js services/webui/src/main.jsx services/webui/src/test
git commit -m "feat(webui): vLLM fleet API client, label maps, flag hook"  # + the executing session's attribution trailer
```

---

### Task D2: GPU Fleet Overview page (nodes + savings)

**Files:**
- Create: `services/webui/src/pages/GpuFleetOverview.jsx`, `services/webui/src/pages/GpuFleetOverview.css`
- Create: `services/webui/src/components/gpu/NodeCard.jsx`, `services/webui/src/components/gpu/VramBar.jsx`, `services/webui/src/components/gpu/SavingsCard.jsx`
- Modify: `services/webui/src/App.jsx:40-48` (add `/gpu-fleet` route, gated by `useVllmRayEnabled()`; not rendered when off)
- Test: `services/webui/src/test/GpuFleetOverview.test.jsx`

**Interfaces:**
- Consumes: D1 `useInventory`, `useSavings`, `useVllmRayEnabled`, `VENDOR_LABEL`, `INVENTORY_STATE_LABEL`, `formatAge`.
- Produces: `GpuFleetOverview` default export (no props); `NodeCard`, `VramBar`, `SavingsCard` as in Interface Decisions.

Node grouping: group inventory rows by `vendor` then `gpu_class`; within a group, by `node_name`. Each group is a section headed `${VENDOR_LABEL[vendor]} · ${gpu_class}`.

- [ ] **Step 1: Write failing tests**

`GpuFleetOverview.test.jsx` (mock hooks via `vi.mock('../hooks/useVllmFleet')`):
- `renders three vendor sections`: inventory with NVIDIA ×2 (RTX 4090, RTX 4070 on different nodes), AMD ×1, Intel ×1 → `getByRole('heading', {name: 'NVIDIA · nvidia-rtx4090'})`, `…'AMD · amd-mi210'`, `…'Intel · intel-arc-a770'` all present.
- `unclassified badge text exact`: a row with `state: 3` shows the text `Unclassified — not schedulable` (exact string, `getByText`).
- `VramBar shows used/free`: row `vram_total_mb: 24576, vram_used_mb: 6144` → text `6.0 GB used of 24.0 GB`.
- `savings math from API values`: mock `useSavings` → `{local_tokens: 1200000, commercial_equivalent_cost: '18.00', virtual_cost: '6.00', est_saved: '12.00'}`; assert text `$12.00 saved` and that `$6.00` is shown; assert no client-side recompute (mock returns `est_saved: '99.99'` → text `$99.99 saved`).
- `zero tokens` (Review Focus 5): `local_tokens: 0` → text `No tokens served locally in the last 7 days`; `queryByText(/NaN|\$0\.00 saved/)` is null.
- `empty inventory` (Review Focus 4): `[]` → text `No GPUs reported yet. Label your AI nodes, then check that the NVIDIA device plugin is running.`
- `updated age shown`: `updated_at` 30 s ago → text `updated 30s ago`.
- `flag off renders nothing`: `useVllmRayEnabled` false → `container` is empty.

- [ ] **Step 2: Run to verify fail**

Run: `cd services/webui && npx vitest run src/test/GpuFleetOverview`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement**

Pages render from hooks only; `SavingsCard` has a 7/30-day segmented control and a kind filter (All local kinds by default · vLLM · Ollama · llama.cpp, AM10) calling `useSavings({period, kind})`, with `kind` omitted for All; amounts are formatted with `Intl.NumberFormat` only, no arithmetic. `VramBar` converts MB to GB with `/1024` and one decimal place (display only).

- [ ] **Step 4: Run to verify pass**

Run: `cd services/webui && npx vitest run src/test/GpuFleetOverview && npx vitest run --coverage src/test/GpuFleetOverview`
Expected: all PASS; coverage for the new files ≥90% lines and branches.

- [ ] **Step 5: Commit**

```bash
git add services/webui/src/pages/GpuFleetOverview.* services/webui/src/components/gpu services/webui/src/App.jsx services/webui/src/test/GpuFleetOverview.test.jsx
git commit -m "feat(webui): GPU Fleet overview page with node cards and savings"  # + the executing session's attribution trailer
```

---

### Task D3: Deployments page (table, status timeline, actions)

**Files:**
- Create: `services/webui/src/pages/GpuFleetDeployments.jsx`, `services/webui/src/pages/GpuFleetDeployments.css`
- Create: `services/webui/src/components/gpu/StatusChip.jsx`, `services/webui/src/components/gpu/StepTimeline.jsx`, `services/webui/src/components/gpu/ConfirmAction.jsx`, `services/webui/src/components/gpu/layoutLabel.js`
- Modify: `services/webui/src/App.jsx` (add `/gpu-fleet/deployments`)
- Test: `services/webui/src/test/GpuFleetDeployments.test.jsx`, `services/webui/src/test/layoutLabel.test.jsx`

**Interfaces:**
- Consumes: D1 `useDeployments`, `useDeployment`, `usePatchDeployment`, `useDeleteDeployment`, `DEPLOYMENT_STATUS_LABEL`, `unknownLabel`.
- Produces: `GpuFleetDeployments` (default export), `StatusChip({status})`, `StepTimeline({events})`, `ConfirmAction`, `layoutLabel(d)`.

Filters: status select (All + 8 statuses), vendor select, free-text model search; the filters are sent as query params (`status`, `vendor`, `model`).

`StatusChip` CSS class is derived from the status int (`chip-serving`, `chip-failed`, …); unknown ints get `chip-unknown` and label `unknownLabel('deployment status', n)`.

- [ ] **Step 1: Write failing tests**

`layoutLabel.test.jsx`:
- `layoutLabel({nodes_used:1, gpus_per_node:2})` → `'1 node · 2 GPUs'`.
- `layoutLabel({nodes_used:3, gpus_per_node:2})` → `'3 nodes · 2 GPUs each'`.
- `layoutLabel({nodes_used:1, gpus_per_node:1})` → `'1 node · 1 GPU'` (singular).

`GpuFleetDeployments.test.jsx`:
- `status labels complete`: for each `n` in `1..8`, a row with `status: n` renders text `DEPLOYMENT_STATUS_LABEL[n]` (use `it.each`).
- `unknown status`: row with `status: 9` renders `Unknown deployment status (9)` and does not throw (Review Focus 1).
- `delete confirm`: click Delete on a row → dialog with text `Delete deployment "qwen-12b"?`; `expect(deleteDeployment).not.toHaveBeenCalled()` until the confirm button is clicked; then called once with the row's `public_id`.
- `delete while SERVING names in-flight requests` (Review Focus 7): `status: 5` → dialog body contains `Requests in progress will fail`.
- `scale PATCH payload`: change "Max replicas" to `3` and save → `patch` called with `{replicas_max: 3}` exactly (no other keys).
- `stop/resume`: `status: 5` shows "Stop"; `status: 7` shows "Resume" → PATCH `{stopped: true}` / `{stopped: false}`.
- `layout column` uses `layoutLabel`: row with `nodes_used:3, gpus_per_node:2` shows `3 nodes · 2 GPUs each`.
- `timeline drawer`: clicking the status chip opens `StepTimeline` listing each event `message` in `ts` order.

- [ ] **Step 2: Run to verify fail**

Run: `cd services/webui && npx vitest run src/test/GpuFleetDeployments src/test/layoutLabel`
Expected: FAIL, modules not found.

- [ ] **Step 3: Implement**

Table columns: Model · Pool · Layout · Replicas · Status · p50 latency · Tokens/min · Actions. p50 and tokens/min show "—" when `null` (never `0`). Actions: Scale (inline number input, min 0), Stop/Resume, Delete, "Logs" link to `/gpu-fleet/deployments/:publicId`. `ConfirmAction` is a modal with Cancel as the default focus.

- [ ] **Step 4: Run to verify pass**

Run: `cd services/webui && npx vitest run --coverage src/test/GpuFleetDeployments src/test/layoutLabel`
Expected: all PASS; new-file coverage ≥90%.

- [ ] **Step 5: Commit**

```bash
git add services/webui/src/pages/GpuFleetDeployments.* services/webui/src/components/gpu services/webui/src/App.jsx services/webui/src/test
git commit -m "feat(webui): GPU Fleet deployments table with timeline and actions"  # + the executing session's attribution trailer
```

---

### Task D4: Deploy wizard (6 steps)

Moved to `2026-11-05-vllm-ray-gpu-fleet-D1-webui-part3.md` (size rule). Run after D3; engine choice (AM12, AM14) is in that file.
