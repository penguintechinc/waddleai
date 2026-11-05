# vLLM on Ray GPU Fleet — Plan D, part 1c: deploy wizard (Task D4; continuation of `...-D1-webui.md`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Context:** Tasks D1–D3 are in `...-D1-webui.md`; D5 and D4b are in `...-D1-webui-part2.md`. This file holds Task D4 only, moved here to keep each file under 25,000 characters. Global Constraints and Review Focus in `...-D1-webui.md` apply unchanged.

### Task D4: Deploy wizard (6 steps)

**Files:**
- Create: `services/webui/src/pages/GpuFleetDeployWizard.jsx`, `services/webui/src/pages/GpuFleetDeployWizard.css`
- Create: `services/webui/src/components/gpu/wizard/wizardState.js` (pure reducer, validators, body builder)
- Create: `services/webui/src/components/gpu/wizard/Step1PickModel.jsx`, `Step2FitCheck.jsx`, `Step3Advanced.jsx`, `Step4Chargeback.jsx` (thin wrapper over the shared `components/gpu/ChargebackStep.jsx`), `Step5Routing.jsx`, `Step6Review.jsx`, and `services/webui/src/components/gpu/ChargebackStep.jsx` (AM10: shared pricing step; props `{kind, priceMode, pricesInput, pricesOutput, comparisonModel, suggestion, onChange}`; reused by the Ollama form in D4b)
- Create: `services/webui/src/components/gpu/wizard/explain.js` (`Explain` constant: `tensorParallel`, `pipelineParallel`, `maxContext`, `allowMixedClass`, `kvHeadroom`, per quantization one-liners from `QUANT_LABEL`)
- Modify: `services/webui/src/App.jsx` (add `/gpu-fleet/deploy`)
- Test: `services/webui/src/test/wizardState.test.jsx`, `services/webui/src/test/GpuFleetDeployWizard.test.jsx`

**Step coverage:** Step 5 (`Step5Routing.jsx`) and Step 6 (`Step6Review.jsx`) are exercised by the `routing default on` and `submit body` tests in Step 2 below; both files are listed under Create above.

**Interfaces:**
- Consumes: D1 `fitCheck`, `useFitCheck`, `useCreateDeployment`, `usePriceSuggestion`, `useModelSearch`, `QUANT_LABEL`.
- Produces: `wizardReducer`, `initialWizardState()`, `validateStep(n, state)`, `buildCreateBody(state)` in `wizardState.js`; default export `GpuFleetDeployWizard`.

State fields: `{step, modelSource:'registry'|'hf', modelRef (registry name or HF repo id string, sent as `model_ref`; AM7/AM20), modelName, paramsB|null, dtype, quantization, vendor, gpuClass, allowMixedClass, maxModelLen, layout:{tensorParallel,pipelineParallel,replicas}|null, priceMode (1 AUTO default | 2 MANUAL), pricesInput, pricesOutput, comparisonModel, exposeViaProxy, preferLocal:{match,fallbackModel}|null, fit:{status:'idle'|'loading'|'ok'|'error', result}}`. `dtype` default `bf16`. `quantization` default `0` (NONE). Defaults for `layout` come from the recommended layout in the fit response, never a guessed value.

- [ ] **Step 1: Write failing reducer/validator tests**

`wizardState.test.jsx`:
- `step 1 requires model`: `validateStep(1, {...init, modelRef: ''})` → `{ok:false, errors:['Choose a model.']}`.
- `step 1 unknown params blocks Next` (Review Focus 6): `paramsB: null` and `modelSource:'hf'` → `ok:false`, errors include `Enter the parameter count in billions (for example 12 or 70). It is not guessed.`; `paramsB: null` must not be replaced by any default (`initialWizardState().paramsB === null`).
- `step 1 params must be positive`: `paramsB: 0` and `-3` → `ok:false`.
- `step 2 requires fits`: `fit.status: 'ok', result.fits: false` → `ok:false`, errors `['This model does not fit. Change the settings or the model.']`.
- `step 2 fit error blocks`: `fit.status: 'error'` → `ok:false` (Review Focus 3); a previous `fits: true` result is cleared by the reducer action `FIT_REQUEST` (assert `state.fit.result === null`).
- `step 4 price required (manual only)`: `priceMode: 2` with `pricesInput: ''` or `'0'` → `ok:false` (`must be greater than 0`); `priceMode: 1` (AUTO) with empty prices → `ok:true`; reducer `SET` of `priceMode` keeps other fields.
- `buildCreateBody` exact shape: given a complete valid MANUAL state (`priceMode: 2`), `Object.keys(body).sort()` equals `['allow_mixed_class','comparison_model','dtype','expose_via_proxy','max_model_len','model_ref','pipeline_parallel','prefer_local','price_input_per_1m','price_mode','price_output_per_1m','quantization','replicas_max','replicas_min','served_model_name','tensor_parallel','vendor','gpu_class','name']`, `body.price_mode === 2`, `body.price_input_per_1m === '4.50'` (string). AUTO state (`priceMode: 1`): `price_mode === 1` and no `price_input_per_1m`/`price_output_per_1m` keys.

- [ ] **Step 2: Write failing component tests**

`GpuFleetDeployWizard.test.jsx` (mock `fitCheck`, `createDeployment`, `getPriceSuggestion`):
- `engine choice`: default is vLLM on Ray; selecting Ollama disables multi-node layouts and shows the verbatim copy `Ollama runs single-node; use llama.cpp RPC or vLLM on Ray for multi-node.`
- `six steps in order`: headings "1. Pick model" … "6. Review & deploy" appear as a stepper.
- `fit check runs live on change`: change params to `12` → after 400 ms (fake timers) `fitCheck` called once with `{params_b: 12, quantization: 0, dtype: 'bf16', max_model_len: …, allow_mixed_class: false, …}`.
- `shows recommended sentence verbatim`: fit mock `{fits:true, layouts:[{recommended:true, explanation:'Fits on 1 node using 2× RTX 4090 (46 GB of 48 GB). Recommended.'}], refusal:null}` → that exact string rendered.
- `refusal verbatim and Deploy disabled` (spec §4.4, Review Focus 2): mock `{fits:false, layouts:[], refusal:'Does not fit in any pool. Options: use 4-bit quantization (needs 68 GB), or span 3 nodes of RTX 4090.'}` → exact text rendered; the "Deploy" button is `disabled` on step 6 and the Next button on step 2 is disabled.
- `null refusal fallback` (Review Focus 2): `{fits:false, refusal:null}` → text `The fit check did not explain why. Contact your administrator.`
- `Advanced quantization limited to matrix`: vendor Intel, `allowed_quantizations:[0]` → only "Full precision" option is enabled; others absent or disabled.
- `TP explanation shown`: Advanced panel contains the `Explain.tensorParallel` string.
- `model search uses API`: typing `qwen` in step 1 calls `searchModels` with `q: 'qwen'` and renders the returned `name`s.
- `allow-mixed-class warning`: checking the box shows exact text `Mixed GPU models in one deployment can run slower and may run out of memory on the smaller card.`
- `tco breakdown renders` (AM6, spec §4.7): mock `getPriceSuggestion` returns `{comparison_model, tco:{gpu_hourly_usd, node_hourly_usd, tokens_per_s_used, source:'estimate', breakdown:{card_amortization, power, ecosystem_share}}, price_input_per_1m, price_output_per_1m}` → Step 4 shows card amortization, power, ecosystem share, assumed throughput tokens/s, `estimate`/`observed` source label, and both suggested prices; price inputs hidden.
- `manual toggle sends price_mode 2`: click "Set price manually" → price inputs appear (prefilled from suggestion); after Deploy the `createDeployment` body has `price_mode: 2` and the typed prices; without toggling the body has `price_mode: 1` and no price keys.
- `chargeback copy exact`: Step 4 shows exactly `Teams using this model are charged this virtual price against their token budget.`; manual price input prefilled from `getPriceSuggestion` `{comparison_model:'gpt-4o-mini', price_input_per_1m:'4.50'}`, and the `Compare against` select shows `gpt-4o-mini`.
- `routing default on`: Step 5 "Make available via proxy" checkbox is checked by default; "Prefer this model for…" is unchecked and the fallback select is hidden until checked.
- `submit body`: on step 6 click "Deploy" → `createDeployment` called once with `buildCreateBody` output (exact equality to the expected object in the test).

- [ ] **Step 3: Run to verify fail**

Run: `cd services/webui && npx vitest run src/test/wizardState src/test/GpuFleetDeployWizard`
Expected: FAIL, module not found.

- [ ] **Step 4: Implement**

- `wizardState.js`: pure reducer with actions `SET`, `FIT_REQUEST`, `FIT_OK`, `FIT_ERROR`, `GOTO`; `validateStep` per the tests; `buildCreateBody` builds the exact key set above (`model_ref` from `modelName`, `comparison_model` from `comparisonModel`).
- Engine choice (AM12, AM14), first control on step 1: radio `vLLM on Ray (recommended)` (default, multi-node), `llama.cpp RPC` (multi-node; RPC specifics in Plan F F7, not repeated here), `Ollama` (single node only, with the copy above). State field `engine` in `{vllm_ray, llamacpp, ollama}`; `buildCreateBody` routes to the matching create path; Step 4 uses the shared `ChargebackStep` for all three kinds (AM14).
- Step 1 picker uses `useModelSearch({q, limit})` → `GET /api/v1/fleet/vllm/models?q=&limit=`; selecting a row sets `modelRef` and `modelName` to its `name` (AM7: the registry name or HF repo id string, never the UUID) and `paramsB` to its `params_b` (`null` keeps the input required). `public_id` is used only to key and select rows in the list (AM20).
- Quantization options = the fit response's `allowed_quantizations`.
- Step 4 (Chargeback) renders the shared `ChargebackStep` (AM10): default AUTO shows the `TcoResult` breakdown (card amortization, power, ecosystem share, assumed tokens/s, source observed|estimate, suggested input/output per 1M); "Set price manually" toggle (`priceMode` 1↔2) reveals the price inputs. `buildCreateBody` sends `price_mode` always, prices only when 2. `Compare against` select bound to `comparisonModel`, prefilled from the suggestion's `comparison_model`.
- `useFitCheck` is called with the state slice, debounced 400 ms; `FIT_REQUEST` dispatches first so stale results never persist.
- `explain.js` is the only home of `Explain` strings.

- [ ] **Step 5: Run to verify pass**

Run: `cd services/webui && npx vitest run --coverage src/test/wizardState src/test/GpuFleetDeployWizard`
Expected: all PASS; new-file coverage ≥90%.

- [ ] **Step 6: Commit**

```bash
git add services/webui/src/pages/GpuFleetDeployWizard.* services/webui/src/components/gpu/wizard services/webui/src/App.jsx services/webui/src/test
git commit -m "feat(webui): six-step vLLM deploy wizard with live fit check"  # + the executing session's attribution trailer
```
