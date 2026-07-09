# AIProxy: AILB Migration & Data-Plane Completion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task ends in a real `git commit`.

**Branch:** `feature/aiproxy-migration` (off `release/v0.2.X`). **Depends on:** `chore/consolidate-quart-k8s` (Flask→Quart already done — both `proxy/` and `services/management/` run Quart + hypercorn).

**Spec:** `docs/superpowers/specs/2026-07-09-waddleai-platform-spec.md` §5 (with §3.2 pipeline, §3.5 scale guardrails, §2.3 registry, §13.1 migrations, §14.2/§14.5). Authoritative.

**Migration source:** marchproxy `origin/release/v0.2.x` @ `9dca05a67dde01bfcff2f721db59a02397287c6b` (Python 3.12/FastAPI; Python→Python re-merge). Read-only: `git -C /home/penguin/code/marchproxy show origin/release/v0.2.x:proxy-ailb/<path>`. Every ported file records `migrated-from: marchproxy@9dca05a` in its commit trailer.

---

**Goal:** Absorb the MarchProxy AILB module into the WaddleAI AIProxy (`proxy/` container) and complete the data plane: a stage-class `ProxyPipeline` shared by `/v1/chat/completions` and `/v1/messages` (parity bug fix); Valkey-atomic token/budget gating (replacing the AILB's in-memory counters); the superset router with circuit breaker; the `THREAT_PATTERNS` prompt-injection scanner wired into the pipeline; Big-5 dispatch (Gemini/xAI/Bedrock) with SSE streaming on every connector; batched metering as the sole writer to `token_usage`; in-repo protos; deletion of all MarchProxy coupling; and migrations 006–007.

**Architecture:** The pipeline is an ordered list of independently testable stage objects, each `async def __call__(self, ctx: PipelineContext) -> PipelineContext`, feature-flag aware, ordered **cheapest-gates-first** (auth → token/budget gate → security-in → dispatch → security-out → meter) so guard-model inference and provider dispatch never run for a request a cheaper Valkey/regex gate would refuse. Both API endpoints reduce to boundary format-translation around the one pipeline. All counters/affinity live in Valkey (stateless pods); durable state in Postgres via penguin-dal; NER/CPU work stays off the event loop (`ProcessPoolExecutor`); metering batches per-second. Every new behavior sits behind PostHog flag `waddleai.native_rate_limit` (default OFF), evaluated via `shared/licensing/features.py::features.enabled(...)` with fail-safe OFF.

**Tech Stack:** Python 3.13, Quart + hypercorn, penguin-dal (runtime) / SQLAlchemy + Alembic (schema), penguin-aaa (auth), Valkey 8 (Lua atomic counters), aiohttp / provider SDKs (openai, anthropic, google-genai, boto3), orjson, pytest + pytest-asyncio, fakeredis (unit), grpcio + grpcio-tools.

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `shared/utils/token_limiter.py` | Valkey Lua atomic TPM + monthly token/$ budget counters; reserve-at-submit / reconcile-at-completion |
| Create | `tests/unit/test_token_limiter.py` | Token-gate unit tests (fakeredis) |
| Modify | `shared/utils/token_manager.py` | Merge AILB normalized-token/cost model + `DEFAULT_CONVERSION_RATES` seed; keep DB-backed metering |
| Create | `tests/unit/test_token_manager_costmodel.py` | Ported AILB `test_token_manager.py` (adapted) |
| Modify | `shared/utils/request_router.py` | Merge AILB `intelligent.py` — superset of 6 strategies + `ProviderStats` circuit breaker (3-fail trip, 5-min cooldown) |
| Create | `tests/unit/test_request_router_merge.py` | Ported AILB router tests (adapted) |
| Modify | `shared/security/prompt_security.py` | Merge AILB `THREAT_PATTERNS` corpus + `scan_messages`/`should_block`/`add_custom_pattern` |
| Create | `tests/unit/test_prompt_security_merge.py` | Ported AILB `test_prompt_security.py` (adapted) |
| Modify | `shared/utils/llm_connectors.py` | Add `GeminiConnector`, `XAIConnector`, `BedrockConnector`; add `stream_chat_completion` to base + all connectors; retries + circuit breaker |
| Modify | `tests/unit/test_llm_connectors.py` | Connector + streaming tests |
| Create | `shared/utils/metering.py` | Batched per-second `token_usage` writer (sole writer) |
| Create | `tests/unit/test_metering.py` | Batched-writer unit tests |
| Create | `proxy/apps/proxy_server/pipeline/__init__.py` | `PipelineContext`, `Stage` base, `ProxyPipeline`, stage-log |
| Create | `proxy/apps/proxy_server/pipeline/stages.py` | `AuthStage`, `TokenBudgetStage`, `SecurityInStage`, `DispatchStage`, `SecurityOutStage`, `MeterStage` |
| Create | `tests/unit/proxy/test_pipeline.py` | Stage + ordering + flag-aware + stage-log tests |
| Modify | `proxy/apps/proxy_server/main.py` | Both endpoints call the shared `ProxyPipeline`; `/v1/messages` parity + fidelity; add `/v1/messages/count_tokens` |
| Create | `tests/unit/proxy/test_endpoint_parity.py` | Stage-log parity assertion across both endpoints |
| Create | `proto/waddleai/v1/proxy.proto` | In-repo protos (replaces vendored marchproxy stubs) |
| Modify | `scripts/generate_proto.sh` | Regenerate from `proto/waddleai/` — no `~/code/marchproxy` dependency |
| Delete | vendored stubs + AILB coupling | See Task 14 deletion inventory |
| Create | `services/management/alembic/versions/006_drop_ailb_add_native_limits.py` | Migration 006 |
| Create | `services/management/alembic/versions/007_model_registry.py` | Migration 007 |
| Modify | `services/management/app/models_sqlalchemy.py` | `VirtualKey` budget cols + `TokenUsage.source`; new `ModelRegistry`; `ProviderCredential.plan_budget` |
| Create | `tests/unit/management/test_migration_006.py`, `test_migration_007.py` | Round-trip + downgrade on seeded snapshot |

---

### Task 1: Valkey atomic token/budget gate (`token_limiter.py`)

Rewrites the AILB's in-memory, thread-locked per-minute counters (`app/tokens/token_manager.py` `_minute_usage` dict + `threading.Lock` — unsafe across replicas) into Valkey Lua atomic counters. Stateless-pod requirement (§3.5). RPM is NOT here (Cilium edge, separate branch); this gate does **TPM + monthly token budget + monthly $ budget**. Flag `waddleai.native_rate_limit`.

**Files:** Create `shared/utils/token_limiter.py`, `tests/unit/test_token_limiter.py`. Modify `services/management/app/models_sqlalchemy.py` (`VirtualKey`: add `budget_monthly_tokens`, `budget_monthly_usd`; note `tpm_limit`/`rpm_limit` already declared).

- [ ] **Step 1: Write failing tests** — `tests/unit/test_token_limiter.py` using `fakeredis.aioredis`. Cover: (a) `reserve(vkey_id, estimated_tokens)` decrements a TPM window atomically and returns `allowed=True` until the window limit, then `allowed=False` with a reason; (b) monthly token budget rejects when cumulative > `budget_monthly_tokens`; (c) monthly $ budget rejects when cumulative cost > `budget_monthly_usd`; (d) `None` limit = unlimited (always allowed); (e) `reconcile(vkey_id, reservation_id, actual_tokens, actual_usd)` corrects the reserved estimate at completion (streaming reserve-at-submit / reconcile-at-completion); (f) two concurrent `reserve` calls at the boundary never both succeed past the limit (asyncio.gather); (g) flag OFF → gate is a no-op that always allows.

- [ ] **Step 2: Run tests, verify they fail** — `python3 -m pytest tests/unit/test_token_limiter.py -v --no-cov` → `ImportError: cannot import name 'TokenLimiter'`.

- [ ] **Step 3: Implement `TokenLimiter`** — atomic Lua scripts loaded once via `EVALSHA`. Keys `waddleai:tpm:{vkey_id}:{yyyymmddHHMM}` (60s TTL), `waddleai:budget:tok:{vkey_id}:{yyyymm}`, `waddleai:budget:usd:{vkey_id}:{yyyymm}` (35d TTL). Reservation token in `waddleai:resv:{uuid}` (short TTL) so `reconcile` can adjust. Signatures:
  ```python
  @dataclass(slots=True)
  class GateDecision:
      allowed: bool
      reason: Optional[str]        # tpm_exceeded | monthly_tokens_exceeded | monthly_usd_exceeded
      reservation_id: Optional[str]

  class TokenLimiter:
      def __init__(self, valkey, features) -> None: ...
      async def reserve(self, vkey_id: int, estimated_tokens: int,
                        estimated_usd: float, limits: "KeyLimits") -> GateDecision: ...
      async def reconcile(self, reservation_id: str, actual_tokens: int, actual_usd: float) -> None: ...

  def create_token_limiter(valkey, features) -> TokenLimiter: ...
  ```
  Monotonic Lua: `INCRBY` then compare to limit; on over-limit, `DECRBY` back and return rejected. Gate short-circuits to allow when `features.enabled("native_rate_limit", distinct_id=str(org_id))` is False.

- [ ] **Step 4: Add model columns** — in `VirtualKey`: `budget_monthly_tokens = Column(Integer, nullable=True)`, `budget_monthly_usd = Column(Integer, nullable=True)  # micro-USD, nullable=unlimited`. Comment: `tpm_limit`/`rpm_limit` already present; Alembic 006 (Task 15) formalizes all four.

- [ ] **Step 5: Run tests, verify pass** — `python3 -m pytest tests/unit/test_token_limiter.py -v --no-cov` → all green.

- [ ] **Step 6: Commit**
  ```bash
  git add shared/utils/token_limiter.py tests/unit/test_token_limiter.py services/management/app/models_sqlalchemy.py
  git commit -m "feat(proxy): Valkey atomic token/budget gate replacing AILB in-memory counters" \
             -m "migrated-from: marchproxy@9dca05a" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 2: Merge AILB cost model into `token_manager.py`

WaddleAI `token_manager.py` is already DB-backed (penguin-dal, `token_usage`/`usage_cache`). The AILB `TokenManager` (`app/tokens/token_manager.py`) contributes the normalized-token/cost model and `DEFAULT_CONVERSION_RATES` (seed for `token_conversion_rates`, which already exists as a table). Keep WaddleAI's DB metering; import the AILB's per-model normalization + cost math. Rename AILB `marchproxy_tokens` → `waddleai_tokens` on merge.

**Files:** Modify `shared/utils/token_manager.py`. Create `tests/unit/test_token_manager_costmodel.py` (adapt AILB `proxy-ailb/tests/test_token_manager.py` — 13 classes / 61 tests; keep `TestTokenConversion`, `TestCostCalculation`, `TestConversionRates`, `TestEdgeCases`; drop `TestQuotaEnforcement`/`TestThreadSafety` — quota now lives in Task 1's Valkey gate).

- [ ] **Step 1: Port + adapt the AILB test module** — copy the retained test classes, rewrite imports to `shared.utils.token_manager`, replace `marchproxy_tokens`→`waddleai_tokens` and `create_token_manager(redis_client=...)`→`create_token_manager(db)`.

- [ ] **Step 2: Run tests, verify they fail** — `python3 -m pytest tests/unit/test_token_manager_costmodel.py -v --no-cov` → failures on missing conversion-rate/normalization behavior.

- [ ] **Step 3: Implement** — port `calculate_waddleai_tokens` (per-model `input_rate`/`output_rate`, unknown-model default `(input + output*2)//10`) and `calculate_cost` (WaddleAI-token 1:1 with normalized; USD = `waddleai_tokens * base_cost_per_waddleai_token`). Add `DEFAULT_CONVERSION_RATES` constant (from AILB) used by `_load_conversion_rates` when the DB table is empty.

- [ ] **Step 4: Run tests, verify pass**; run `python3 -m pytest tests/ -k token --no-cov --tb=short 2>&1 | tail -5` (no regressions).

- [ ] **Step 5: Commit**
  ```bash
  git add shared/utils/token_manager.py tests/unit/test_token_manager_costmodel.py
  git commit -m "feat(proxy): merge AILB normalized-token/cost model into token_manager" \
             -m "migrated-from: marchproxy@9dca05a" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 3: Merge AILB router into `request_router.py`

Reconcile AILB `app/router/intelligent.py` with WaddleAI `shared/utils/request_router.py` (common ancestor). Keep the superset: all **six** `RoutingStrategy` values with a real `COST_OPTIMIZED` branch (AILB left it unwired), plus `ProviderStats` EMA latency + circuit breaker (skip provider at `consecutive_failures >= 3` or recent-failure-within-5-min). WaddleAI already has `ProviderStats` and inline breaker logic; ensure parity and add missing branches.

**Files:** Modify `shared/utils/request_router.py`. Create `tests/unit/test_request_router_merge.py` (adapt AILB router tests + WaddleAI existing).

- [ ] **Step 1: Diff both routers** — `git -C /home/penguin/code/marchproxy show origin/release/v0.2.x:proxy-ailb/app/router/intelligent.py` vs `shared/utils/request_router.py`; record any AILB strategy branch or breaker nuance not present in WaddleAI.

- [ ] **Step 2: Write failing tests** — cover: each of 6 strategies selects deterministically on a stubbed connector set; `COST_OPTIMIZED` picks the lowest `cost_per_token` provider; breaker skips a provider after 3 consecutive failures; breaker re-admits after a success resets `consecutive_failures`; failover fallback chain raises only when all providers fail.

- [ ] **Step 3: Run tests, verify they fail** (missing COST_OPTIMIZED branch / breaker parity).

- [ ] **Step 4: Implement** — add the `_cost_optimized_selection` branch to `_select_provider` if absent; confirm EMA (0.9/0.1) and 5-min cooldown match AILB. No behavior change to the aioredis routing-instruction path (that becomes the §7 engine on a later branch).

- [ ] **Step 5: Run tests, verify pass**; `python3 -m pytest tests/ -k router --no-cov --tb=short 2>&1 | tail -5`.

- [ ] **Step 6: Commit**
  ```bash
  git add shared/utils/request_router.py tests/unit/test_request_router_merge.py
  git commit -m "feat(proxy): merge AILB intelligent router (6 strategies + ProviderStats breaker)" \
             -m "migrated-from: marchproxy@9dca05a" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 4: Merge + prepare `prompt_security.py` (wired in Task 11)

WaddleAI `shared/security/prompt_security.py` already has `THREAT_PATTERNS`, `ThreatType`, `Action`, `SECURITY_POLICIES` (strict/balanced/permissive), `PromptSecurityScanner(db, policy_name)`. The AILB scanner adds `scan_messages`, `should_block`, `add_custom_pattern`, and a wider `THREAT_PATTERNS` corpus (injection/jailbreak/data-extraction/prompt-leak/credential-harvesting). Merge the superset of patterns + methods. MarchProxy never wired this into a request path — wiring happens in Task 11 (pipeline stage 3).

**Files:** Modify `shared/security/prompt_security.py`. Create `tests/unit/test_prompt_security_merge.py` (adapt AILB `test_prompt_security.py` — 16 classes / 62 tests).

- [ ] **Step 1: Port + adapt AILB test module** — rewrite imports to `shared.security.prompt_security`; adapt `PromptSecurityScanner(policy_name=..., redis_client=...)` → WaddleAI `PromptSecurityScanner(db, policy_name=...)` signature; keep pattern-detection, policy-tier, sanitize, custom-pattern, message-scanning, confidence, and severity classes.

- [ ] **Step 2: Run tests, verify they fail** — missing `scan_messages`/`should_block`/`add_custom_pattern` and any AILB-only patterns.

- [ ] **Step 3: Implement** — union the two `THREAT_PATTERNS` dicts (dedupe regexes); add `scan_messages(messages, ...)`, `should_block(threats) -> bool`, `add_custom_pattern(threat_type, pattern) -> bool`. Preserve existing WaddleAI DB logging.

- [ ] **Step 4: Run tests, verify pass**; `python3 -m pytest tests/ -k security --no-cov --tb=short 2>&1 | tail -5`.

- [ ] **Step 5: Commit**
  ```bash
  git add shared/security/prompt_security.py tests/unit/test_prompt_security_merge.py
  git commit -m "feat(security): merge AILB THREAT_PATTERNS corpus + scan_messages/should_block" \
             -m "migrated-from: marchproxy@9dca05a" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 5: `GeminiConnector` (Big-5)

**Files:** Modify `shared/utils/llm_connectors.py` (add class after `AnthropicConnector`; register `"gemini"` in `_load_connectors`). Modify `tests/unit/test_llm_connectors.py` (`TestGeminiConnector`).

- [ ] **Step 1: Write failing tests** — mirror the existing `TestLlamaCppConnector` shape: `chat_completion` success returns `(content, usage)` with `usage["provider"] == "gemini"`; API error raises; `count_tokens` via SDK `count_tokens` with tiktoken fallback; `list_models`; `health_check` healthy/unhealthy.

- [ ] **Step 2: Run tests, verify they fail** — `ImportError: cannot import name 'GeminiConnector'`.

- [ ] **Step 3: Implement `GeminiConnector(LLMConnector)`** — `google-genai` SDK (`google.genai`, Apache-2.0; EU/NORAM origin OK). Async client, OpenAI→Gemini message translation, `usage` normalized to `{prompt_tokens, completion_tokens, total_tokens, provider:"gemini", model}`.

- [ ] **Step 4: Register** — in `_load_connectors`, `elif link.provider == "gemini": connector = GeminiConnector(link.name, config)`; update module docstring provider list.

- [ ] **Step 5: Run tests, verify pass**; full suite tail.

- [ ] **Step 6: Commit**
  ```bash
  git add shared/utils/llm_connectors.py tests/unit/test_llm_connectors.py
  git commit -m "feat(proxy): add GeminiConnector (Big-5 dispatch)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 6: `XAIConnector` (Big-5)

**Files:** Modify `shared/utils/llm_connectors.py`, `tests/unit/test_llm_connectors.py` (`TestXAIConnector`).

- [ ] **Step 1: Write failing tests** — same shape as Task 5; `usage["provider"] == "xai"`.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement `XAIConnector`** — xAI exposes an OpenAI-compatible API; subclass/reuse `OpenAIConnector` machinery with `base_url` = `https://api.x.ai/v1` (configurable), overriding `provider` label to `"xai"`.
- [ ] **Step 4: Register** `"xai"` in `_load_connectors`.
- [ ] **Step 5: Run tests, verify pass**; full suite tail.
- [ ] **Step 6: Commit**
  ```bash
  git add shared/utils/llm_connectors.py tests/unit/test_llm_connectors.py
  git commit -m "feat(proxy): add XAIConnector (OpenAI-compatible Big-5 dispatch)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 7: `BedrockConnector` (Big-5)

**Files:** Modify `shared/utils/llm_connectors.py`, `tests/unit/test_llm_connectors.py` (`TestBedrockConnector`).

- [ ] **Step 1: Write failing tests** — mock `boto3` bedrock-runtime client; `chat_completion` maps InvokeModel response to `(content, usage)`, `usage["provider"] == "bedrock"`; error path raises; `count_tokens` fallback to tiktoken; `health_check`.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement `BedrockConnector`** — `boto3` (Apache-2.0), `bedrock-runtime` `invoke_model` / `converse`; blocking boto calls wrapped in `asyncio.to_thread` (never on the event loop, §3.5). Credentials via the provider-credential pattern (`account_meta`).
- [ ] **Step 4: Register** `"bedrock"` in `_load_connectors`.
- [ ] **Step 5: Run tests, verify pass**; full suite tail.
- [ ] **Step 6: Commit**
  ```bash
  git add shared/utils/llm_connectors.py tests/unit/test_llm_connectors.py
  git commit -m "feat(proxy): add BedrockConnector (Big-5 dispatch, boto3 off event loop)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 8: SSE streaming passthrough on all connectors + retries/breaker

The migrated AILB had **no** streaming; no WaddleAI connector streams today. Add an async-generator `stream_chat_completion` to the base `LLMConnector` and every connector (OpenAI, Anthropic, Ollama, LlamaCpp, Gemini, xAI, Bedrock), plus jittered-backoff retries and circuit-breaker integration from the merged router.

**Files:** Modify `shared/utils/llm_connectors.py`, `tests/unit/test_llm_connectors.py`.

- [ ] **Step 1: Write failing tests** — for each connector, `stream_chat_completion(messages, model, **kwargs)` yields incremental text chunks and a final usage dict; mock the SDK/aiohttp streaming response. Add a retry test (transient error retried with backoff; 4xx not retried) and a breaker test (repeated failures open the breaker).

- [ ] **Step 2: Run tests, verify they fail** — `AttributeError: ... has no attribute 'stream_chat_completion'`.

- [ ] **Step 3: Implement** — abstract on base:
  ```python
  async def stream_chat_completion(
      self, messages: List[Dict[str, str]], model: str, **kwargs
  ) -> AsyncIterator[StreamChunk]: ...
  ```
  `StreamChunk = @dataclass(slots=True)` with `delta: str`, `usage: Optional[dict]`, `done: bool`. Each connector wires provider-native streaming (OpenAI/xAI `stream=True`, Anthropic `messages.stream`, Gemini `generate_content_stream`, Ollama/LlamaCpp `"stream": True` NDJSON/SSE, Bedrock `invoke_model_with_response_stream` via `asyncio.to_thread` bridged to an async queue). Add `_with_retries` helper (jittered exponential backoff, no retry on 4xx) and record failures into the router `ProviderStats` breaker.

- [ ] **Step 4: Run tests, verify pass**; full suite tail.

- [ ] **Step 5: Commit**
  ```bash
  git add shared/utils/llm_connectors.py tests/unit/test_llm_connectors.py
  git commit -m "feat(proxy): SSE streaming passthrough + retries/breaker on all connectors" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 9: Batched metering writer (`metering.py`)

AIProxy becomes the **sole writer** to `token_usage`, batched per-second at scale (§3.5, §5.3). The AILB webhook ingest is deleted in Task 14.

**Files:** Create `shared/utils/metering.py`, `tests/unit/test_metering.py`. Modify `services/management/app/models_sqlalchemy.py` (`TokenUsage`: add `source = Column(String(50), default="aiproxy")`).

- [ ] **Step 1: Write failing tests** — `MeteringBuffer.record(event)` accumulates in-memory; `flush()` writes one aggregated row per (vkey, model, provider, minute) not per request; a 1-second timer coalesces N events into ≤1 write (assert DAL insert/update call count); records carry `source="aiproxy"`.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement** — `MeteringBuffer(db, interval=1.0)` with an `asyncio` background flush task; aggregates token/cost/request counts; upserts into `token_usage`/`usage_cache`. Add `source` to the model. `create_metering_buffer(db)`.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add shared/utils/metering.py tests/unit/test_metering.py services/management/app/models_sqlalchemy.py
  git commit -m "feat(proxy): batched per-second metering as sole token_usage writer" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 10: `ProxyPipeline` stage classes + stage-log

Extract the pipeline into ordered, flag-aware, independently testable stage classes (§3.2, §5.4). Cheapest-gates-first.

**Files:** Create `proxy/apps/proxy_server/pipeline/__init__.py`, `proxy/apps/proxy_server/pipeline/stages.py`, `tests/unit/proxy/test_pipeline.py`.

- [ ] **Step 1: Write failing tests** — `ProxyPipeline([...stages]).run(ctx)` calls stages in order, threading `ctx`; `ctx.stage_log` lists each stage that executed; a stage that sets `ctx.blocked = True` (e.g. `TokenBudgetStage` over limit, `SecurityInStage` BLOCK) short-circuits remaining expensive stages; each stage is skipped (logged as `skipped`) when its flag is OFF; ordering is auth → token/budget → security-in → dispatch → security-out → meter.

- [ ] **Step 2: Run tests, verify they fail** — `ModuleNotFoundError: ...pipeline`.

- [ ] **Step 3: Implement** — 
  ```python
  @dataclass(slots=True)
  class PipelineContext:
      user: object
      body: dict
      model: Optional[str] = None
      messages: list = field(default_factory=list)
      prompt_text: str = ""
      response_text: str = ""
      usage: Optional[object] = None
      blocked: bool = False
      block_reason: Optional[str] = None
      status_code: int = 200
      stream: bool = False
      stage_log: list = field(default_factory=list)

  class Stage:
      name: str
      flag: Optional[str] = None
      async def __call__(self, ctx: PipelineContext) -> PipelineContext: ...

  class ProxyPipeline:
      def __init__(self, stages: list["Stage"], features): ...
      async def run(self, ctx: PipelineContext) -> PipelineContext: ...   # short-circuit on ctx.blocked; append stage_log ("ran"|"skipped"|"short-circuit")
  ```
  Stages in `stages.py` wrap the components built in Tasks 1–9: `AuthStage` (existing `get_current_user`), `TokenBudgetStage` (`TokenLimiter.reserve`), `SecurityInStage` (`PromptSecurityScanner.scan_messages` + `ContentFilter.filter_input`), `DispatchStage` (`request_router` + connectors, streaming-aware), `SecurityOutStage` (`ContentFilter.filter_output`), `MeterStage` (`MeteringBuffer.record` + `TokenLimiter.reconcile`). Order encodes cheapest-first.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add proxy/apps/proxy_server/pipeline/ tests/unit/proxy/test_pipeline.py
  git commit -m "feat(proxy): ordered ProxyPipeline stage classes with stage-log" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 11: Wire both endpoints to the shared pipeline (parity + fidelity)

`/v1/chat/completions` and `/v1/messages` both reduce to boundary format-translation around the one `ProxyPipeline` — fixing the parity bug where `/v1/messages` skips content filtering and memory. Includes the Claude Code fidelity audit and `/v1/messages/count_tokens`.

**Files:** Modify `proxy/apps/proxy_server/main.py`. Create `tests/unit/proxy/test_endpoint_parity.py`.

- [ ] **Step 1: Write failing tests** — (a) both endpoints, given equivalent requests, produce the **same `ctx.stage_log`** (parity assertion); (b) `/v1/messages` now runs `SecurityInStage`/`SecurityOutStage` (previously absent — regression guard); (c) streaming request returns an SSE stream from `DispatchStage`; (d) tool_use / `system` array / thinking blocks / `cache_control` passthrough preserved in translation; (e) `POST /v1/messages/count_tokens` returns `{ "input_tokens": N }`.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement** — build the pipeline once at startup (`proxy_server.pipeline`); rewrite `chat_completions()` and `claude_messages()` to: parse → build `PipelineContext` → `await pipeline.run(ctx)` → translate `ctx` back to OpenAI vs Anthropic response shape (SSE when `ctx.stream`). Preserve Anthropic `content` array, `system`, `thinking`, `tool_use`, and client-supplied `cache_control` (untouched passthrough). Add the `count_tokens` route using the dispatch connector's `count_tokens`.

- [ ] **Step 4: Run tests, verify pass**; run the golden contract snapshots — `make test-contract 2>&1 | tail -20` (must stay green; `usage.waddleai` additions are additive-only per §14.2).

- [ ] **Step 5: Commit**
  ```bash
  git add proxy/apps/proxy_server/main.py tests/unit/proxy/test_endpoint_parity.py
  git commit -m "feat(proxy): both endpoints share ProxyPipeline; /v1/messages parity + count_tokens" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 12: In-repo protos + rewrite `generate_proto.sh`

Keep the gRPC server skeleton (house standard, port 50051) but define protos in-repo under `proto/waddleai/`; delete the `~/code/marchproxy` dependency. `waddleai_pb2`/`media_pb2` definitions are recovered fresh (no source `.proto` in marchproxy); NLB-specific protos die.

**Files:** Create `proto/waddleai/v1/proxy.proto`. Modify `scripts/generate_proto.sh`. (Vendored-stub deletion is Task 14.)

- [ ] **Step 1: Write failing test/check** — `tests/unit/proxy/test_proto_generation.py`: asserts `proto/waddleai/v1/proxy.proto` exists, declares `package waddleai.v1;`, and every request message has an `api_version` field (house gRPC rule, `backend.md`). Run → fails (file absent).

- [ ] **Step 2: Author `proto/waddleai/v1/proxy.proto`** — `syntax="proto3"; package waddleai.v1; option go_package=...;` minimal service used by the AIProxy skeleton (health + any recovered `waddleai`/`media` messages), each request carrying `string api_version = 1;`.

- [ ] **Step 3: Rewrite `scripts/generate_proto.sh`** — generate from `proto/waddleai/**/*.proto` into `proxy/apps/proxy_server/grpc_proto/waddleai/`; remove all `MARCHPROXY_PROTO_DIR` / `~/code/marchproxy` references; rewrite package imports to relative.

- [ ] **Step 4: Regenerate + verify** — `bash scripts/generate_proto.sh && python3 -m pytest tests/unit/proxy/test_proto_generation.py -v --no-cov`.

- [ ] **Step 5: Commit**
  ```bash
  git add proto/ scripts/generate_proto.sh proxy/apps/proxy_server/grpc_proto/waddleai/ tests/unit/proxy/test_proto_generation.py
  git commit -m "chore(proxy): in-repo protos under proto/waddleai; drop marchproxy proto dependency" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 13: WaddleAI-side deletion inventory (§5.6)

Delete all MarchProxy sync plumbing, AILB routes, webhook ingest, vendored proto stubs, env, and their tests. Do this **before** migration 006 so nothing writes to the AILB tables when they are folded+dropped.

**Files (delete):** `services/management/app/services/marchproxy_config.py`, `services/management/app/services/provider_sync.py`, `services/management/app/grpc/client.py`, `services/management/app/grpc/proto/marchproxy/`, `services/management/app/api/v1/ailb.py`, `services/management/app/api/v1/ailb_memory.py`, `proxy/apps/proxy_server/grpc_proto/marchproxy/`, tests `tests/unit/management/test_marchproxy_config.py`, `test_ailb_routes.py`, `test_ailb_memory.py`. **Modify:** `services/management/app/api/v1/webhooks.py` (remove AILB ingest routes `handle_usage_webhook`/`handle_health_webhook`/`handle_batch_webhook` + helpers), `services/management/app/api/v1/__init__.py` (drop `ailb`, `ailb_memory` blueprints), `services/management/app/config.py` + `k8s/helm/waddleai/templates/management-deployment.yaml` + `infrastructure/kubernetes/base/{configmap,management-deployment}.yaml` (remove `MARCHPROXY_AILB_*`), `proxy/apps/proxy_server/main.py` (remove `grpc_server` MarchProxy wiring comment/imports if stale), and the AILB portions of `tests/unit/management/test_webhook_routes.py`/`test_webhook_routes_extra.py`/`test_app_init.py`. Re-home memory/RAG/embedding config endpoints to `/api/v1/memory-config` (create `services/management/app/api/v1/memory_config.py`, ported from `ailb_memory.py` bodies).

- [ ] **Step 1: Write/adjust failing tests first** — add `tests/unit/management/test_no_marchproxy.py` asserting the deleted modules are un-importable and `grep`-clean; add `tests/unit/management/test_memory_config.py` for the re-homed endpoints. Run → fail.

- [ ] **Step 2: Delete files + strip env + re-home endpoints** as listed; update `api/v1/__init__.py` blueprint imports.

- [ ] **Step 3: Verify** — `grep -rn "marchproxy\|MARCHPROXY_AILB\|ailb" services/ proxy/ k8s/ infrastructure/ --include=*.py --include=*.yaml | grep -vi "migration\|migrated-from"` returns nothing; `python3 -m pytest tests/unit/management/test_no_marchproxy.py tests/unit/management/test_memory_config.py -v --no-cov`; full suite tail.

- [ ] **Step 4: Commit**
  ```bash
  git add -A
  git commit -m "chore: delete MarchProxy AILB coupling; re-home memory config to /api/v1/memory-config" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 14: Migration 006 — drop AILB, add native limits, fold usage

Down-revision `005_add_content_filter_tables`. Drops `marchproxy_ailb_sync`; folds `ailb_usage_events` + `ailb_usage_records` → `token_usage` with `source='ailb_import'` (billing/dashboard continuity, Q#1) then drops them; drops `virtual_keys.ailb_key_id`/`ailb_sync_status`; adds `virtual_keys.rpm_limit`/`tpm_limit` (if not already created by baseline) + `budget_monthly_tokens`/`budget_monthly_usd`; adds `token_usage.source`; seeds `token_conversion_rates` from `DEFAULT_CONVERSION_RATES`. Round-trip + downgrade tested (house rule).

**Files:** Create `services/management/alembic/versions/006_drop_ailb_add_native_limits.py`, `tests/unit/management/test_migration_006.py`. Also drop `MarchProxyAILBSync`/`AILBUsageEvent`/`AILBUsageRecord` classes + `virtual_keys.ailb_*` from `models_sqlalchemy.py`.

- [ ] **Step 1: Write failing round-trip test** — on a SQLite/seeded snapshot with sample `ailb_usage_events`/`ailb_usage_records` rows: `upgrade` → those rows appear in `token_usage` with `source='ailb_import'`, the three AILB tables are gone, `virtual_keys` has the four limit cols + no `ailb_*`, `token_usage.source` exists, and `token_conversion_rates` is seeded; `downgrade` → schema returns to the 005 shape (folded rows may remain as `ailb_import` — assert documented behavior). Run → fails (no 006).

- [ ] **Step 2: Implement migration 006** — guarded `op.add_column`/`op.drop_column` (check-if-exists for `rpm_limit`/`tpm_limit`); an INSERT-SELECT fold from the two AILB tables into `token_usage`; `op.bulk_insert` seed for conversion rates. Provide a complete `downgrade()`.

- [ ] **Step 3: Update ORM models** — remove the three AILB model classes and `virtual_keys.ailb_*` columns.

- [ ] **Step 4: Run tests, verify pass** — `python3 -m pytest tests/unit/management/test_migration_006.py -v --no-cov`; `alembic -c services/management/alembic.ini heads` shows a single head `006_...`.

- [ ] **Step 5: Commit**
  ```bash
  git add services/management/alembic/versions/006_drop_ailb_add_native_limits.py tests/unit/management/test_migration_006.py services/management/app/models_sqlalchemy.py
  git commit -m "feat(db): migration 006 — drop AILB tables, fold usage to token_usage, add native limits" \
             -m "migrated-from: marchproxy@9dca05a" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 15: Migration 007 — model registry + dual-default seed + plan_budget

Down-revision `006`. Adds `model_registry(name, role, license, origin, min_vram, ollama_tag, resolved_digest, is_utility)` seeded with the §2.3 dual-default set (utility models flagged `is_utility=True`, excluded from Free caps per Q#7); adds `provider_credentials.plan_budget jsonb`. (Rate limiting itself is the Cilium-edge branch; this task only lands the registry + the budget config column the token gate reads.) Round-trip + downgrade tested.

**Files:** Create `services/management/alembic/versions/007_model_registry.py`, `tests/unit/management/test_migration_007.py`. Modify `models_sqlalchemy.py` (`ModelRegistry` class; `ProviderCredential.plan_budget = Column(JSON)`).

- [ ] **Step 1: Write failing round-trip test** — `upgrade` creates `model_registry` seeded with the dual-default rows (`gemma3:1b` + `granite3.3:2b`, `shieldgemma:2b` + `granite-guardian3:2b`, `nomic-embed-text`, etc.) with correct `license`/`origin`/`is_utility`, and adds `provider_credentials.plan_budget`; assert **no Chinese-origin** entries in the seed; `downgrade` drops both. Run → fails.

- [ ] **Step 2: Implement migration 007 + `ModelRegistry` ORM model + `plan_budget` column.**

- [ ] **Step 3: Run tests, verify pass** — `python3 -m pytest tests/unit/management/test_migration_007.py -v --no-cov`; `alembic ... heads` shows single head `007_...`.

- [ ] **Step 4: Commit**
  ```bash
  git add services/management/alembic/versions/007_model_registry.py tests/unit/management/test_migration_007.py services/management/app/models_sqlalchemy.py
  git commit -m "feat(db): migration 007 — model_registry dual-default seed + provider_credentials.plan_budget" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 16: §5.8 acceptance verification + scale smoke

Turn each §5.8 acceptance item into an explicit verify step. Add a scale smoke test and the zero-marchproxy guard; confirm ported AILB tests are green.

**Files:** Create `tests/integration/test_aiproxy_acceptance.py`, `tests/smoke/test_scale_streams.py`.

- [ ] **Step 1: Contract snapshots green** — `make test-contract 2>&1 | tail -20`.
- [ ] **Step 2: Streamed tool-use via `/v1/messages`** — integration test: Claude Code-shaped streamed tool_use turn completes end-to-end (stub upstream connector).
- [ ] **Step 3: OpenAI SDK streaming via `/v1/chat/completions`** — integration test asserts SSE chunks + final usage.
- [ ] **Step 4: Identical pipeline stages** — assert equal `stage_log` for both endpoints (reuses Task 11 parity test in the acceptance suite).
- [ ] **Step 5: Token/budget gate under parallel load** — fire N parallel requests at the limit boundary; assert the Valkey gate admits exactly up to the limit and rejects the rest (edge/Cilium RPM is a separate branch — not tested here).
- [ ] **Step 6: Zero marchproxy references** — `grep -rn "marchproxy" --include=*.py --include=*.yaml . | grep -vi "migration\|migrated-from"` returns nothing.
- [ ] **Step 7: Ported AILB tests green** — `python3 -m pytest tests/unit/test_token_manager_costmodel.py tests/unit/test_request_router_merge.py tests/unit/test_prompt_security_merge.py -v --no-cov`.
- [ ] **Step 8: Scale smoke** — `tests/smoke/test_scale_streams.py`: 1K concurrent streamed requests through one pod (stubbed upstream) without event-loop stalls; assert p99 proxy overhead < 50ms.
- [ ] **Step 9: Coverage gate** — `python3 -m pytest tests/ --cov --cov-fail-under=90 2>&1 | tail -15` (≥90% on changed modules, §14.2).
- [ ] **Step 10: Commit**
  ```bash
  git add tests/integration/test_aiproxy_acceptance.py tests/smoke/test_scale_streams.py
  git commit -m "test(proxy): §5.8 acceptance suite + scale smoke + zero-marchproxy guard" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

## Self-Review Against Spec §5

| Spec §5 requirement | Task |
|---|---|
| §5.4/§3.2 ProxyPipeline stage classes, cheapest-first, stage-log | 10 |
| §5.4 both endpoints share pipeline; `/v1/messages` parity | 11 |
| §5.4 Claude Code fidelity + `/v1/messages/count_tokens` | 11 |
| §5.2 token_manager port → Valkey atomic gate | 1 |
| §5.2 normalized-token/cost merge + conversion-rate seed | 2 |
| §5.2 router merge (6 strategies + ProviderStats breaker) | 3 |
| §5.2 prompt_security THREAT_PATTERNS merge | 4 |
| §5.2 wired into pipeline stage 3 | 10, 11 |
| §5.3 Big-5 Gemini/xAI/Bedrock connectors | 5, 6, 7 |
| §5.3 SSE streaming on all connectors + retries/breaker | 8 |
| §5.3 metering sole writer, batched per-second | 9 |
| §5.5 in-repo protos + rewrite generate_proto.sh | 12 |
| §5.6 deletion inventory (sync, routes, webhook, env, tests) | 13 |
| §5.7 migration 006 (drop/fold/add/seed) round-trip + downgrade | 14 |
| §13.1 migration 007 (model_registry + plan_budget) | 15 |
| §5.8 acceptance items (each an explicit verify) | 16 |
| §3.5 stateless pods / NER off loop / batched metering | 1, 7, 9 |
| §14.5 flag `waddleai.native_rate_limit`, fail-safe OFF | 1, 10 |
