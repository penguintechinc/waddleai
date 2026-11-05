# vLLM on Ray GPU Fleet — Plan D (D1 part 2): Tasks D4b and D5

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec/context:** same as `2026-11-05-vllm-ray-gpu-fleet-D1-webui.md` (Plan D header, Global Constraints, D1 hooks/API client). Master: `2026-11-05-vllm-ray-gpu-fleet-00-master.md`.

### Task D5: Backend settings panel (`vllm_ray` backend)

**Files:**
- Create: `services/webui/src/pages/GpuFleetBackend.jsx`, `services/webui/src/pages/GpuFleetBackend.css`
- Create: `services/webui/src/components/gpu/BackendVllmSettings.jsx`
- Modify: `services/webui/src/App.jsx` (add `/gpu-fleet/backend`)
- Test: `services/webui/src/test/BackendVllmSettings.test.jsx`

**Note:** no fleet-backend page exists in webui; this task creates it (update spec §5.1 wording in D8).

**Interfaces:**
- Consumes: management `GET /api/v1/fleet/backends/<int:backend_id>` (`services/management/app/api/v1/fleet.py:447`) and `GET /api/v1/fleet/backends/<int:backend_id>/health` (`fleet.py:564`), plus `getFlags`. Backend id is the one row with `type === 'vllm_ray'` from `GET /api/v1/fleet/backends` (`fleet.py:321`).
- Produces: `BackendVllmSettings({backend})` props; the `config` keys read are `ray_head_url`, `worker_groups[]` (`{name, gpu_class, replicas, max_replicas}`), `model_store` (`{used_gb, capacity_gb}`), `hot_cache` (`{enabled: bool}`), `tco`, and per-group `cost` (AM6/spec §6.2). Edits save via `PUT /api/v1/fleet/backends/<id>` with `{config}`.

- [ ] **Step 1: Write failing tests**

`BackendVllmSettings.test.jsx`:
- `ray status shown`: health `{status:'healthy', message:'Ray head reachable'}` → text `Ray cluster: healthy`.
- `unhealthy shows reason`: health `{status:'unhealthy', message:'Ray head unreachable'}` → text includes `Ray head unreachable`, plus the instruction `Check that the Ray head pod is running in the waddleai namespace.`
- `worker groups table`: two groups → rows showing `replicas / max_replicas`.
- `model store usage`: `{used_gb: 310, capacity_gb: 1000}` → text `310 GB of 1000 GB used`.
- `hot cache state`: `enabled:false` → text `NVMe hot cache: off (opt-in, see the admin guide)`.
- `no vllm_ray backend` → text `No vLLM backend is registered. Register one from the Fleet backends API.` (empty state).
- `flag off renders nothing`.
- `secrets panel`: `GET /api/v1/fleet/secrets` rows render name, source badge, fingerprint, rotated_at; no value text anywhere.
- `external secret disables rotate`: source `external` → badge `managed externally`, Rotate button disabled, tooltip `Managed externally (env or existingSecret). Rotate it in your secret store.`
- `rotate confirms then posts`: Rotate → confirm dialog `Rotating restarts Ray head and workers. Continue?`; confirm → `POST /api/v1/fleet/secrets/<name>/rotate` once; cancel → no request.
- `per-deployment rotate token`: each deployment row has `Rotate token` with confirm → `POST .../deployments/<uuid>/rotate-token`; 502 shows the reason and keeps the old fingerprint.
- `tco/cost edits saved` (AM6, AM9 snake_case): edit `tco.kwh_usd` to `0.25` and group `cost.card_usd` to `1800` → Save → `PUT /api/v1/fleet/backends/<id>` body `config.tco.kwh_usd === 0.25` and `config.worker_groups[0].cost.card_usd === 1800` (numbers, other keys preserved).

- [ ] **Step 2: Run to verify fail**

Run: `cd services/webui && npx vitest run src/test/BackendVllmSettings`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement**, per file:
  - `hooks/useVllmFleet.js`: add `useBackendVllm(backendId)` (`useQuery`, `staleTime: 30_000`) returning the health payload; its test is added to `BackendVllmSettings.test.jsx`.
  - `components/gpu/BackendVllmSettings.jsx`, sections in order:
    1. Ray status line (`Ray cluster: <status>` + message; unhealthy adds the instruction from Step 1).
    2. Worker groups table (`name`, `gpu_class`, `replicas / max_replicas`).
    3. Model store line (`used_gb of capacity_gb GB used`).
    4. Hot cache line (`NVMe hot cache: off|on`, opt-in copy).
    5. Secrets list from `GET /api/v1/fleet/secrets` (name, source badge, fingerprint, rotated_at; no value); Rotate per row confirms, then `POST /api/v1/fleet/secrets/<name>/rotate`; `source: external` shows `managed externally` and a disabled Rotate (AM13, AM15).
    6. Per-deployment `Rotate token` (confirm, then `POST .../deployments/<uuid>/rotate-token`); a 502 shows the reason and keeps the old fingerprint.
    7. Editable tco/cost form: `tco` keys `kwh_usd`, `lifecycle_years`, `utilization_pct`, `input_ratio`, `ram_usd_per_gb`, `nvme_usd_per_gb`, `nvme_pro_usd_per_gb`, `network_device_usd`; per-group `cost` keys `card_usd`, `tdp_watts`, `est_tokens_per_s`, `cpu_usd`, `ram_gb`, `nvme_gb`, `nvme_pro`, `network_devices`, `node_base_watts`, `gpus_per_node`, optional `node_base_usd` (snake_case, AM9). Numeric-validated; Save sends `PUT /api/v1/fleet/backends/<id>` with `{config}`, other keys preserved.
  - `pages/GpuFleetBackend.jsx`: loads the `vllm_ray` row from `GET /api/v1/fleet/backends`, renders `BackendVllmSettings` with it; empty state when none exists.
  - `pages/GpuFleetBackend.css`: layout only, no logic.
  - `App.jsx`: add the `/gpu-fleet/backend` route, gated by `useVllmRayEnabled()`.

- [ ] **Step 4: Run to verify pass**

Run: `cd services/webui && npx vitest run --coverage src/test/BackendVllmSettings`
Expected: PASS; coverage ≥90% on new files.

- [ ] **Step 5: Commit**

```bash
git add services/webui/src/pages/GpuFleetBackend.* services/webui/src/components/gpu/BackendVllmSettings.jsx services/webui/src/hooks/useVllmFleet.js services/webui/src/App.jsx services/webui/src/test
git commit -m "feat(webui): vllm_ray backend settings panel"  # + the executing session's attribution trailer
```

---

### Task D4b: Ollama deploy form reuses `ChargebackStep` (AM10)

**Files:**
- Modify: `services/webui/src/pages/OllamaDeployments.jsx` (the existing Ollama deployment form: create and edit paths gain a pricing section)
- Test: `services/webui/src/test/OllamaDeployments.pricing.test.jsx`

**Interfaces:**
- Consumes: `ChargebackStep` (D4, `services/webui/src/components/gpu/ChargebackStep.jsx`, props `{kind, priceMode, pricesInput, pricesOutput, comparisonModel, suggestion, onChange}`); `usePriceSuggestion({kind: 2, ...})` (D1 hook; `kind` 2 = Ollama); the existing Ollama API client functions already imported by `OllamaDeployments.jsx`.
- Produces: the Ollama form sends `price_mode`, `price_input_per_1m`, `price_output_per_1m`, `comparison_model` (C2b fields) and `gpu_config.node_selector` (`{"waddleai.penguintech.io/gpu-classes": <class>}`) plus `gpu_config.gpu_count`, taken from the form's existing GPU class and GPU count controls. Existing fields are unchanged; the pricing section is additive.
- Scope note: `services/webui/src` has no llama.cpp deployment form (`grep -ri llamacpp services/webui/src` is empty). llama.cpp pricing is API-only in this plan (C2b); a llama.cpp UI is an open item for the owner, not silently added here.

- [ ] **Step 1: Write the failing tests** (`OllamaDeployments.pricing.test.jsx`, mock `usePriceSuggestion` and the Ollama create call)
  - `ollama form renders the shared ChargebackStep`: the pricing section heading `Chargeback` is present on the create form.
  - `manual price sends price_mode 2`: toggle "Set price manually", enter `2.50` and `10.00`, submit → create body has `price_mode: 2`, `price_input_per_1m: '2.50'`, `price_output_per_1m: '10.00'`.
  - `manual zero price blocked`: `0` → submit disabled, message `must be greater than 0`.
  - `auto sends gpu_config fields and no client price`: default AUTO → body has `price_mode: 1`, no `price_input_per_1m`, and `gpu_config.node_selector["waddleai.penguintech.io/gpu-classes"]` equals the chosen class.
  - `regression: no pricing section edits leaves legacy body`: body keys equal the pre-existing key set plus only the `gpu_config` pricing sub-keys.

- [ ] **Step 2: Run to verify failure:** `npm --prefix services/webui test -- OllamaDeployments.pricing` → FAIL (section absent).

- [ ] **Step 3: Implement.** Import `ChargebackStep`; add the pricing section to the create and edit forms; map the form state to the body fields above.

- [ ] **Step 4: Run to verify pass:** `npm --prefix services/webui test -- OllamaDeployments` → all PASS, 0 failures (existing `OllamaDeployments.test.jsx` included).

- [ ] **Step 5: Commit**

```bash
git add services/webui/src/pages/OllamaDeployments.jsx services/webui/src/test/OllamaDeployments.pricing.test.jsx
git commit -m "feat(webui): virtual pricing on Ollama deployment form"  # + the executing session's attribution trailer
```
