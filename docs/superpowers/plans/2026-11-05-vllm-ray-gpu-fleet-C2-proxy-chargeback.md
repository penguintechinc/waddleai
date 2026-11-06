# vLLM on Ray GPU Fleet — Plan C, part 2a of 2b: proxy connector, routing, chargeback, metrics (Tasks C4–C6; C7 in `...-C2-proxy-chargeback-part1b.md`; C8–C9 in `...-C2-proxy-chargeback-part2.md`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route to vLLM deployments through the proxy with real `usage` counts, debit quota at each deployment's virtual price with a per-row price snapshot, report savings/chargeback, emit fleet metrics.

**Architecture:** `VllmConnector` reports provider usage or a flagged estimate; one `LOCAL_PROVIDERS` constant; usage rows carry `local_deployment_kind` + `local_deployment_id` + price snapshots and the quota engine converts tokens to credits at the virtual price; prefer-local rules are `routing_rules_v2` rows consumed by the routing engine.

**Tech Stack:** Python 3.13, `openai` SDK, Quart, pydal, OpenTelemetry, pytest.

**Spec:** `docs/superpowers/specs/2026-11-05-vllm-ray-gpu-fleet-design.md` (§4.6, §4.7, §7). Master: `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-00-master.md`. Part 1 (C1–C3): `docs/superpowers/plans/2026-11-05-vllm-ray-gpu-fleet-C1-api.md`. Branch `feature/vllm-ray-c`.

## Global Constraints

- Python 3.13, `@dataclass(slots=True)`, penguin logging only; OTel on every new path; exporter failure never breaks a request.
- Money is `Decimal`; prices `NUMERIC(12,6)` per 1M tokens.
- No silent zero or fallback: missing usage/price is flagged, counted, logged. No PII in telemetry.
- Coverage ≥90% on new modules; zero examined = FAIL.
- Commit steps: explicit files (no `git add -A`), never `git stash`, + the executing session's attribution trailer; never commit to `release/v0.4.X`.

## Review Focus

1. Stream with no final `usage` chunk → `include_usage` forced; still missing → estimate, `estimated=true` on the stored row, `waddleai_vllm_usage_missing_total` +1, warning; never a silent zero. (C4, C6)
2. Deployment not `SERVING` or unhealthy → offer unavailable, connector non-routable; requests go commercial. (C5, C7)
3. Delete mid-request → no orphan offers; callers get the proxy's existing upstream-unavailable error. (C5; part 1 C3)
4. Same served name as a commercial model → vLLM only via an explicit prefer-local route. (C5, C7)
5. Price change mid-day → earlier rows keep the old snapshot; new usage opens a new row. (C6)

---

### Task C4: `VllmConnector`

**Files:**
- Modify: `shared/utils/llm_connectors.py` — `OpenAIConnector.chat_completion` (`:366-402`, `usage is None` branch), `stream_chat_completion` (`:440-476`), `_load_connectors` (`:2397-2420`: add `"management_capabilities": link.management_capabilities or {}` to `config` (with `owner_org_id`) and `elif link.provider == "vllm"`); new `VllmConnector` before `XAIConnector` (`:581`)
- Modify: `services/management/app/api/v1/providers.py:287` (`SUPPORTED_PROVIDERS["vllm"] = {"name": "vLLM (GPU fleet)", "models": [], "requires_api_key": True, "default_endpoint": ""}`)
- Create: `shared/observability/vllm_metrics.py` (C4: `usage_missing_counter()`, `reset_for_testing()`; C8 adds the rest), `tests/unit/fixtures/otel.py` (`metric_reader` fixture + `points(reader, name)`, from `tests/unit/management/test_management_observability.py:33-50,75`), `tests/unit/fixtures/fake_openai.py`
- Test: `tests/unit/test_vllm_connector.py`

**Interfaces:**
- Consumes: `OpenAIConnector`, `StreamChunk`, `ProviderServerError`, `get_meter` (`shared/observability/metrics.py:94`), `count_tokens` (`:515`).
- Produces:
  - Parent hooks (defaults keep OpenAI/xAI behavior): `async def _handle_missing_usage(self, model: str, messages: list[dict[str, str]], content: str) -> dict[str, Any]` (default raises the existing `ProviderServerError`); `def _stamp_usage(self, usage_info: dict) -> dict` (identity; applied to non-stream usage and the final stream chunk's); `async def _finalize_stream_usage(self, usage: dict | None, model: str, messages: list[dict[str, str]], text: str) -> dict | None` (returns `usage`; the stream loop accumulates `text`).
  - `class VllmConnector(OpenAIConnector)`: `provider_label = "vllm"`; SDK key is the connection link's per-deployment bearer (`api_key`, AM11); a missing key raises before any request (fail closed), "vllm-no-auth" is removed; llama.cpp connector same rule; from `config["management_capabilities"]`: `local_deployment_id`, `unit_price_input`/`unit_price_output` (`Decimal | None`), `fleet_status`; `is_routable` = `fleet_status is None or fleet_status == int(DeploymentStatus.SERVING)`; stream always sends `stream_options={"include_usage": True}`; `_stamp_usage` adds `provider="vllm"`, `estimated` (default False), `local_deployment_kind=1`, `local_deployment_id`, price strings; missing-usage hooks estimate via `count_tokens`, set `estimated=True`, `usage_missing_counter().add(1, {"reason": "stream" | "non_stream"})`, `logger.warning` (no content).
  - `usage_missing_counter()` — counter `waddleai_vllm_usage_missing_total`, unit `"1"`.

- [ ] **Step 1: Write the failing tests.** `fake_openai.py`: fixture `fake_openai_server` runs a Quart app under hypercorn on `127.0.0.1:0` serving `POST /v1/chat/completions`, `mode` in `json_usage|json_no_usage|sse_usage|sse_no_usage`, recording request bodies. Connector: `VllmConnector("vllm-1-x", {"endpoint_url": url, "api_key": None, "model_list": ["m"], "management_capabilities": {"local_deployment_id": 7, "unit_price_input": "0.5", "unit_price_output": "1.5", "fleet_status": 5}})`.

```python
test_non_stream_reads_usage_from_body     # counts from body; estimated False; stamped deployment 7, prices "0.5"/"1.5"; counter 0 points
test_non_stream_missing_usage_is_estimated_not_zero   # tokens > 0, estimated True, counter 1 {"reason":"non_stream"}, warning   (Review Focus 1)
test_stream_forces_include_usage          # caller passes {"include_usage": False} -> recorded body has True
test_stream_reads_usage_from_final_chunk  # real counts, estimated False
test_stream_without_usage_chunk_is_estimated_and_counted   # estimated True, output tokens > 0, counter 1 {"reason":"stream"}   (Review Focus 1)
test_openai_connector_missing_usage_still_raises   # regression
test_is_routable   # fleet_status 5 True, None True, 6 False, 3 False   (Review Focus 2)
test_factory_builds_vllm_connector_from_link       # provider "vllm" row -> attrs from management_capabilities
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_vllm_connector.py -v`
Expected: 10 failed, 1 passed (the OpenAI regression test).

- [ ] **Step 3: Implement** hooks (behavior-preserving), `VllmConnector`, metrics module, factory branch + config key, `SUPPORTED_PROVIDERS` entry.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/test_vllm_connector.py -v; pytest tests/unit -k "connector or openai" -q`
Expected: 11 passed, 0 failed (`test_is_routable` runs 4 cases); the `-k` run reports 0 failed.

- [ ] **Step 5: Commit**

```bash
git add shared/utils/llm_connectors.py services/management/app/api/v1/providers.py shared/observability/vllm_metrics.py tests/unit/fixtures/otel.py tests/unit/fixtures/fake_openai.py tests/unit/test_vllm_connector.py
git commit -m "feat(proxy): vllm connector with real usage and loud missing-usage estimate"
```

---

### Task C4b: Ollama and llama.cpp usage carries the local kind (AM10)

**Files:**
- Modify: `shared/utils/llm_connectors.py` — `_load_connectors` (`:2397-2420`) already passes `management_capabilities` for every link (C4), so Ollama and llama.cpp links receive it too; `OllamaConnector.chat_completion` builds `usage_info` at `:1618-1627` (`"provider": "ollama"` at `:1624`); `LlamaCppConnector.chat_completion` sets `usage["provider"] = "llamacpp"` at `:1890`; the streamed usage paths at `:1797-1807` (Ollama) and `:1945` (llama.cpp) stamp the same keys.
- Test: `tests/unit/test_local_usage_kind.py`

**Interfaces:**
- Consumes: `LocalDeploymentKind` (B1); `management_capabilities` `{local_deployment_kind, local_deployment_id}` written on the link by C2b and the C3 link sync.
- Produces: usage dicts from the Ollama and llama.cpp connectors include `local_deployment_kind` (int) and `local_deployment_id` (int | None) from `config["management_capabilities"]`. Absent capabilities → both `None` (commercial behavior unchanged). Helper `_local_usage_keys(config) -> dict[str, int | None]`.

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_local_usage_kind.py`):
  - `test_ollama_usage_stamps_kind_and_id_from_link`: capabilities `{local_deployment_kind: 2, local_deployment_id: 9}` → usage has `local_deployment_kind == 2`, `local_deployment_id == 9`.
  - `test_llamacpp_usage_stamps_kind_and_id_from_link`: capabilities kind 3, id 4 → usage has `local_deployment_kind == 3`, `local_deployment_id == 4`.
  - `test_stream_usage_stamps_kind`: final stream usage chunk of each connector carries the same two keys (kind 2 Ollama, kind 3 llama.cpp).
  - `test_link_without_capabilities_leaves_kind_none`: no `management_capabilities` → both keys `None`; `provider` unchanged.

- [ ] **Step 2: Run to verify failure:** `pytest tests/unit/test_local_usage_kind.py -v` → Expected: 4 failed (keys absent).

- [ ] **Step 3: Implement**
  - `shared/utils/llm_connectors.py`: add `_local_usage_keys(config)`; stamp its result into `OllamaConnector.chat_completion` usage (`:1618-1627`), `LlamaCppConnector.chat_completion` usage (`:1890`), and the streamed usage paths (Ollama `:1797-1807`, llama.cpp `:1945`).

- [ ] **Step 4: Run to verify pass:** `pytest tests/unit/test_local_usage_kind.py tests/unit/test_llm_connectors.py -q` → Expected: 4 passed, 0 failed; `tests/unit/test_llm_connectors.py` 0 failed.

- [ ] **Step 5: Commit**

```bash
git add shared/utils/llm_connectors.py tests/unit/test_local_usage_kind.py
git commit -m "feat(fleet): local deployment kind on ollama and llama.cpp usage"
```

---

### Task C5: One `LOCAL_PROVIDERS` constant; vLLM offers

**Files:**
- Create: `shared/routing/providers.py`
- Modify: `shared/routing/offers.py:35,83`; `proxy/apps/proxy_server/pipeline/stages.py:928`; `shared/utils/request_router.py:122-160`, `:367-408`, `:410-416` (`_get_available_providers`, `select_provider`, `_is_affinity_eligible`); `fleet_vllm.py` and `shared/fleet/vllm_pricing.py` (use `LOCAL_PROVIDERS`)
- Test: `tests/unit/routing/test_offers_vllm.py`, `tests/unit/test_request_router_vllm.py`

**Interfaces:**
- Consumes: `VllmConnector.is_routable` (C4); `PlacementEngine.annotate_offers` (`shared/fleet/placement.py:391`, local offers unavailable without a healthy endpoint) over Plan B's `endpoints_for` (only `SERVING` rows).
- Produces: `LOCAL_PROVIDERS: frozenset[str] = frozenset({"ollama", "llamacpp", "vllm"})`; `RequestRouter.select_provider(model, strategy=None, preferred_backend=None, destination: str | None = None)` — a `destination` inside the available set wins over strategy (never over breaker/availability filtering).

**Decisions:** `_get_available_providers(model, org_id)` skips any connector whose link has `owner_org_id` set and ≠ `org_id` (AM3; `offers.py` applies the same filter), and skips a `VllmConnector` when non-routable, or when a connector outside `LOCAL_PROVIDERS` also lists `model` (then reachable only via `destination`; Review Focus 4). `_is_affinity_eligible` includes `VllmConnector`. Both literals are deleted.

- [ ] **Step 1: Write the failing tests**

```python
test_local_providers_contents_disjoint_from_commercial   # LOCAL_PROVIDERS == {"ollama","llamacpp","vllm"}; disjoint from _COMMERCIAL_PROVIDERS (stages.py:715); offers.py and stages.py import it
test_vllm_only_model_config_is_local_offer               # preferred_providers == ["vllm"] -> offer kind "local"
test_mixed_model_config_stays_commercial                 # preferred_providers ["openai"] -> offer kind "commercial" (Review Focus 4)
test_serving_deployment_offer_available                  # SERVING row + real PlacementEngine -> offer available == True
test_degraded_deployment_offer_unavailable               # DEGRADED -> available == False; connection_links row kept (Review Focus 2)
test_stopped_failed_deleted_offer_unavailable            # STOPPED/FAILED -> available == False; deleted row -> offer absent (Review Focus 3)
test_router_skips_non_routable_vllm_connector            # fleet_status 6 -> selected provider == "commercial" (Review Focus 2)
test_router_vllm_not_implicit_when_commercial_serves_same_model   # no destination -> commercial chosen (Review Focus 4)
test_router_destination_selects_vllm_but_not_when_circuit_open_or_non_routable   # destination "vllm-1-x" -> vllm; open breaker or non-routable -> not vllm
test_affinity_eligible_includes_vllm                     # _is_affinity_eligible(VllmConnector) == True
test_foreign_org_vllm_link_never_offered                 # owner_org_id 2, request org 1 -> absent from offers and routing (AM3)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/routing/test_offers_vllm.py tests/unit/test_request_router_vllm.py -v`
Expected: 11 failed or errored, 0 passed.

- [ ] **Step 3: Implement**
  - `shared/routing/providers.py` (create): `LOCAL_PROVIDERS: frozenset[str] = frozenset({"ollama", "llamacpp", "vllm"})`.
  - `shared/routing/offers.py:35,83` and `proxy/apps/proxy_server/pipeline/stages.py:928`: import `LOCAL_PROVIDERS`; delete the literals.
  - `shared/utils/request_router.py:122-160` (`_get_available_providers`): skip links with `owner_org_id` set and ≠ `org_id`; skip a non-routable `VllmConnector`; skip vllm when a non-`LOCAL_PROVIDERS` connector also lists the model.
  - `shared/utils/request_router.py:367-408` (`select_provider`): add the `destination` argument; a destination inside the available set wins over strategy, never over breaker or availability filtering.
  - `shared/utils/request_router.py:410-416` (`_is_affinity_eligible`): include `VllmConnector`.
  - `services/management/app/api/v1/fleet_vllm.py`, `shared/fleet/vllm_pricing.py`: use `LOCAL_PROVIDERS`.

- [ ] **Step 4: Run to verify pass:** `pytest tests/unit/routing tests/unit/test_request_router_vllm.py tests/unit/fleet tests/unit/proxy -q` → Expected: 0 failed; the 11 new tests pass.

- [ ] **Step 5: Commit**

```bash
git add shared/routing/providers.py shared/routing/offers.py proxy/apps/proxy_server/pipeline/stages.py shared/utils/request_router.py services/management/app/api/v1/fleet_vllm.py shared/fleet/vllm_pricing.py tests/unit/routing/test_offers_vllm.py tests/unit/test_request_router_vllm.py
git commit -m "refactor(routing): single LOCAL_PROVIDERS constant; vllm-aware offers and router"
```

---

### Task C6: Virtual-price rate rows and chargeback snapshots (AM2)

**Files:**
- Create: `shared/quota/virtual_price.py`: `upsert_virtual_rate(db, provider, model, price_input, price_output)` (the single definition; B4d's `apply_pricing` imports it) and `usage_row_key(kind, deployment_id, price_in, price_out) -> tuple` (snapshot key, see Decisions)
- Modify: `services/management/app/api/v1/fleet_vllm.py` (call `apply_pricing` (B4d; it upserts the rate row) after the create insert and in the price PATCH path); `shared/database/models.py:356-373` (pydal `token_usage` fields; minimal `vllm_deployments` fields the sqlite test proves missing); `shared/utils/token_manager.py:171-184,202-260,262-330` (snapshot columns, per-price row split); `proxy/apps/proxy_server/main.py:2136,2564` (pass `local_deployment_kind`, `local_deployment_id`, `estimated`, prices from `usage`)
- Test: `tests/unit/quota/test_virtual_price.py`, `tests/unit/test_token_manager_vllm.py`

**Interfaces:**
- Consumes: connector usage keys `provider`, `estimated`, `local_deployment_kind`, `local_deployment_id`, `unit_price_input`, `unit_price_output` (C4, C4b); the existing `token_conversion_rates` lookup (unchanged).
- Produces:
  - Rate upsert defined here (`upsert_virtual_rate(db, provider, model, price_input, price_output)`, called for every local kind through `apply_pricing`): `input_rate = base * 1e6 / price_input_per_1m` with `base` = 0.001 (Decimal; AM2). Rates are DIVISORS (LLM tokens per credit, `token_manager.py:134`). Never deleted; historical usage keeps its snapshot.
  - Price-change path: `fleet_vllm.py` calls `apply_pricing` on deployment create and on every price PATCH (`price_mode` / `price_*`), which updates the same rate row. AUTO daily recompute is owned by C1 task C3 (reconcile loop, which calls this function) — referenced, not duplicated here.
  - `TokenManager.process_usage(..., estimated: bool = False, local_deployment_kind: int | None = None, local_deployment_id: int | None = None, unit_price_input: Decimal | None = None, unit_price_output: Decimal | None = None)`.

**Decisions:**
- Debit = tokens ÷ rate (rate is a divisor) through the existing lookup, unchanged. No USD-per-credit or class-map env vars, no new reserve/reconcile logic (AM2).
- `token_usage` is a per-`(api_key_id, date)` rollup: lookups for a local kind add `local_deployment_kind == k AND local_deployment_id == X AND unit_price_input == pin AND unit_price_output == pout`; commercial lookups add `local_deployment_kind IS NULL`. A price change opens a new row for the day; old rows are never rewritten. `estimated` is sticky-true.

- [ ] **Step 1: Write the failing tests**

```python
# test_virtual_price.py
test_upsert_rate_row_created                  # provider "vllm", model == served_model_name, price 2.00/1M with base 0.001 -> input_rate == Decimal("500.0") exactly (0.001*1e6/2.00); output 4.00 -> 250.0
test_upsert_rate_row_updated_on_price_patch   # PATCH input price to 4.0 -> same row id, input_rate == 250.0 (0.001*1e6/4.0), no second row; also asserts create calls the upsert
test_rate_row_kept_after_delete               # delete leaves the rate row; usage history keeps its snapshot
test_usage_row_key_distinguishes_kind_id_and_prices  # usage_row_key(1,7,"2.0","4.0") differs from price "3.0", from kind 2, and from id 8
# test_token_manager_vllm.py   (pydal sqlite with the real token_usage definition)
test_quota_debit_equals_tokens_times_rate     # 1_000_000 in + 500_000 out at stored rates 500.0/250.0 -> debit == 1_000_000/500 + 500_000/250 == Decimal(4000) credits (exact Decimal)
test_price_change_creates_new_row_old_row_unchanged   # prices 2.0 then 3.0, same key/day -> 2 rows, first unchanged   (Review Focus 5)
test_same_price_accumulates_one_row_non_vllm_never_merges
test_estimated_persisted_and_sticky           # (Review Focus 1)
test_token_usage_table_accepts_fleet_columns
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/quota/test_virtual_price.py tests/unit/test_token_manager_vllm.py -v`
Expected: 9 failed or errored, 0 passed.

- [ ] **Step 3: Implement**
  - `shared/quota/virtual_price.py` (create): `upsert_virtual_rate` (`input_rate = 0.001 × 1e6 / price_input_per_1m`, output likewise; upsert keyed by `provider` + `model`; never deletes) and `usage_row_key`.
  - `services/management/app/api/v1/fleet_vllm.py`: `apply_pricing` after the create insert and in the price PATCH path.
  - `shared/database/models.py:356-373`: `token_usage` fields and the minimal `vllm_deployments` fields the sqlite test proves missing.
  - `shared/utils/token_manager.py:171-184,202-260,262-330`: snapshot columns and per-price row split; `process_usage` gains the new keyword arguments.
  - `proxy/apps/proxy_server/main.py:2136,2564`: pass `local_deployment_kind`, `local_deployment_id`, `estimated`, and prices from `usage`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/quota tests/unit/test_token_manager_vllm.py tests/unit/test_conversion_rate_store.py tests/unit/test_default_conversion_rates.py tests/unit/proxy -q`
Expected: 0 failed; the 10 new tests pass.

- [ ] **Step 5: Commit**

```bash
git add shared/quota/virtual_price.py services/management/app/api/v1/fleet_vllm.py shared/database/models.py shared/utils/token_manager.py proxy/apps/proxy_server/main.py tests/unit/quota/test_virtual_price.py tests/unit/test_token_manager_vllm.py
git commit -m "feat(quota): vLLM virtual-price rate rows and per-price usage snapshots"
```

---

> Task C7 (prefer-local routing rule) is in `2026-11-05-vllm-ray-gpu-fleet-C2-proxy-chargeback-part1b.md` (execute right after C6).

> Tasks C8–C9 and the open items are in `2026-11-05-vllm-ray-gpu-fleet-C2-proxy-chargeback-part2.md`.
