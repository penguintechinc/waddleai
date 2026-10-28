# WaddleAI Rust Data-Plane — P2 Proxy Core (Light/Hot Path) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Rust proxy that serves `/v1/chat/completions`, `/v1/messages`, `/v1/models` through ONE ported pipeline (Auth → TokenBudget → SecurityIn → Cache → Routing → Quota → Dispatch → SecurityOut → Meter) for **local models only** (Ollama / llama.cpp), contract-gated against the Python proxy's local-model path, with zero billing/quota drift.

**Architecture:** New library crates join the P0 workspace (`rust/`): `pipeline` (Stage trait, `PipelineContext`, `ProxyPipeline`), `tokens`, `security`, `quota`, `respcache`, `routing`, `connectors` (local only), `metering`; the `waddleai-proxy` bin gains the Axum routers. Stages are ported 1:1 from `proxy/apps/proxy_server/pipeline/stages.py` (same names, same order, same short-circuit/flag semantics). Everything consumes P0 interfaces by name — not re-planned here.

**Tech Stack:** Axum, `async-trait`, `tiktoken-rs`, `regex`, `reqwest` (rustls), `fred`/`redis` (P0 client), `tokio`, `tokio-stream`/`futures`, `serde_json`, `tracing`+OTel (P0), `cargo-llvm-cov`.

**Spec:** `docs/superpowers/specs/2026-10-28-waddleai-rust-dataplane-migration-design.md` §5.1 (+§5.1.1 auth modes), §6, §7, §11. **P0 plan:** `2026-10-28-waddleai-rust-dataplane-P0-foundation.md`.

## P0 interfaces consumed (by name)

| P0 name | Used by |
|---|---|
| `Config` | all crates (`redis_url`, `database_url`, ollama/llama.cpp base URLs added as P2 additive fields) |
| `db::connect()` + generated entities (`organizations`, `api_keys`, `virtual_keys`, `model_registry`, `routing_rules`, `content_filter_rules`, `token_conversion_rates`, quota tables, usage/metering tables) | Routing, Security, Meter, Quota |
| `cache::client`, `cache::keys`, `cache::invalidation` | Quota, Cache, Routing (registry/rules cache), Security (rule cache) |
| `auth::authenticate_api_key(&str) -> UserContext`, `verify_jwt -> Claims`, `UserContext{org_id,user_id,tenant,scopes,teams,auth_mode}` | AuthStage |
| `flags::enabled(flag, distinct_id)`, `flags::has_feature(feature, scope)` | `ProxyPipeline` gating, QuotaStage, SecurityIn (`pii_ner_detection`), CacheStage |
| `telemetry::init_telemetry`, Prometheus registry (Python metric names), `/healthz /livez /readyz /metrics` | pipeline spans, Meter, routes |
| `tests/contract/` harness (normalizer/comparator) | Task 14 |

## Global Constraints

- **Axum** for REST; routes are **boundary translation only** (wire JSON ⇄ `PipelineContext`) around the single pipeline — no logic in handlers. No Tonic in P2.
- **tiktoken-rs parity:** counts must equal Python `shared/utils/token_manager.py` byte-for-byte on a shared corpus; **port estimate paths as-is** (do not "fix" estimate-vs-real). Anthropic/Gemini use their own count paths, not tiktoken.
- **Valkey Lua reuse verbatim:** `LUA_RESERVE`/`LUA_RECONCILE` (`shared/quota/lease.py:25,86`) are exported from Python to `.lua` files and embedded via `include_str!`; a drift test fails if bytes differ. Python stays the single source of the script text.
- **Byte-compat with live Python (shared Valkey):** cache keys, quota keys, `wa-{key_id}-{secret}`, invalidation signal — via P0 `keys`.
- **One authoritative quota writer per tenant** — routing-level invariant (spec §11); the Rust proxy never double-reserves; Rust and Python never both serve one tenant's live traffic.
- **Port bugs verbatim:** #347 quota overdraw and the org=0 gRPC follow-up ported as-is, each pinned by a documented regression test (`# regression: gh-347`), not fixed here.
- **Fail loudly on unsupported paths:** anything Python does that P2 doesn't (security v2, memory/knowledge stages, commercial provider, passthrough dispatch, tier-3 NER) returns an explicit typed error — never a silent fallback or no-op.
- **No `unsafe`**; no `unwrap`/`expect` outside tests; `tracing` only (no `println!`); secrets never logged/in spans; PII never in spans/labels/log bodies.
- **Everything behind PostHog flags (default OFF)**; cutover at ingress, not in-proxy.
- **Coverage:** `cargo-llvm-cov` ≥ 90% per crate; `cargo-deny` pins + no PRC deps (note `tiktoken-rs` vendored BPE data is fetched at build — pin + checksum it).
- Doc comment (2–3 lines) on every struct/trait/fn; no ASCII dividers.

## Review Focus

1. **Streaming SSE correctness** — OpenAI + Anthropic event sequences, `[DONE]`/`message_stop`, client disconnect mid-stream, partial-usage metering, split-chunk UTF-8, backpressure → Tasks 10, 12.
2. **Quota reserve/reconcile parity** — reserve before dispatch, reconcile on success, `settle_aborted` charges consumed tokens and refunds only zero-usage; release exactly once under cancellation (shielded settle); **#347 ported as-is** → Tasks 1, 7, 11.
3. **`auth_mode` detection + passthrough opt-in gate** — `wa-` → virtual; non-`wa-` accepted ONLY if enabled by key/org policy else 401; passthrough never dispatches in P2; credential never logged → Task 3.
4. **Regex tier parity** — `regex` crate (RE2-style, no backrefs/lookaround) vs Python `re`: every `THREAT_PATTERNS`/`BUILTIN_PATTERNS` entry must be translatable or explicitly reworked with an equivalence test; ReDoS-safe by construction → Tasks 4, 5.
5. **tiktoken parity** — encoder selection per model, special-token handling, message-overhead arithmetic, estimate paths → Task 2.
6. Stage-driver semantics: flag-skip is evaluated **before** blocked short-circuit; `meter` obeys the same short-circuit as Python (port verbatim, pinned by test) → Task 1.

---

## Interface Definitions (P3/P4 consume these — treat as stable)

```rust
// crates/pipeline/src/stage.rs
#[async_trait::async_trait]
pub trait Stage: Send + Sync {
    /// Stable stage name ("auth","token_budget","security_in","cache","routing","quota","dispatch","security_out","meter").
    fn name(&self) -> &'static str;
    /// Optional PostHog flag gating this stage (None = always run).
    fn flag(&self) -> Option<&'static str> { None }
    /// Executes the stage, mutating ctx; set ctx.blocked/status_code/block_reason to short-circuit. Err = stage fault (driver releases quota, propagates).
    async fn call(&self, ctx: &mut PipelineContext) -> Result<(), StageError>;
}

pub enum StageError { Unsupported(&'static str), Upstream(String), Internal(String) }

// crates/pipeline/src/driver.rs
pub struct ProxyPipeline { /* stages: Vec<Arc<dyn Stage>>, flags: Arc<dyn FlagResolver>, tracer */ }
impl ProxyPipeline {
    pub fn new(stages: Vec<Arc<dyn Stage>>, flags: Arc<dyn FlagResolver>) -> Self;
    pub async fn run(&self, ctx: &mut PipelineContext) -> Result<(), StageError>;
    pub async fn run_until(&self, ctx: &mut PipelineContext, stage: &str) -> Result<(), StageError>; // exclusive
    pub async fn run_after(&self, ctx: &mut PipelineContext, stage: &str) -> Result<(), StageError>; // exclusive
}
```

```rust
// crates/pipeline/src/context.rs — field-for-field port of Python PipelineContext + P3/P4 seams
pub struct PipelineContext {
    pub user: Option<UserContext>,            // P0
    pub auth_mode: AuthMode,                  // NEW §5.1.1: Virtual (default) | Passthrough
    pub passthrough_credential: Option<secrecy::SecretString>, // provider key to forward (P3); never logged
    pub body: serde_json::Value,
    pub model: Option<String>,
    pub messages: Vec<serde_json::Value>,
    pub prompt_text: String,
    pub response_text: String,
    pub usage: Option<Usage>,                 // {input_tokens, output_tokens} (+ provider extras map)
    pub blocked: bool,
    pub block_reason: Option<String>,
    pub status_code: u16,                     // default 200
    pub stream: bool,
    pub stage_log: Vec<String>,               // "ran:"/"skipped:"/"short-circuit:" + name
    pub provider: Option<String>,
    pub requested_model: Option<String>,
    pub finish_reason: Option<String>,
    pub reservation_id: Option<String>,       // TokenBudget -> Meter
    pub quota_reservation: Option<QuotaReservation>,
    pub quota_release: Option<QuotaGuard>,    // replaces Python closure; driver .take()s + settles once
    pub response_format: ResponseFormat,      // Openai | Anthropic (part of cache key)
    pub cache_hit: bool,
    pub cache_status: CacheStatus,            // Exact | Semantic | Miss
    pub tokens_saved: u64,
    pub cache_write_back: Option<WriteBack>,
    pub preferred_backend: Option<String>,
    pub stream_iter: Option<BoxStream<'static, Result<Bytes, std::io::Error>>>, // synthetic SSE replay
    pub session_id: Option<String>,
    pub usage_meta: serde_json::Map<String, serde_json::Value>,
    pub pii_detected: bool,
    pub fallback_chain: Vec<String>,
    pub routed_from: Option<serde_json::Value>,
    pub explicit_tool_type_hint: Option<String>,
    pub escalate_hint: Option<String>,
    pub security_degraded: bool,
    pub upstream_mapping_id: Option<String>,
    pub pseudonym_map: PseudonymMap,          // P3 seam: placeholder<->value, request-scoped, empty in P2
    pub extensions: Extensions,               // P4 seam: typed map for plugin-hook data
}
```

```rust
// crates/pipeline/src/seams.rs — P3 seams (stubbed/failing-loud in P2)
#[async_trait] pub trait NerClient: Send + Sync {
    /// Tier-3 PII-NER; returns detected spans only (proxy substitutes + holds the map).
    async fn analyze(&self, req: &NerRequest) -> Result<Vec<EntitySpan>, NerError>;
}
pub struct NerRequest<'a> { pub text: &'a str, pub entities: &'a [EntityKind], pub org_id: i64, pub content_hash: [u8;32] }
pub struct EntitySpan { pub kind: EntityKind, pub start: usize, pub end: usize, pub score: f32 }
pub enum NerError { Unavailable, Timeout, Rejected(String) }
/// P2 impl: `NoNer` -> Err(NerError::Unavailable) (drives the ported degraded/fail-open|closed policy).

#[async_trait] pub trait Dispatcher: Send + Sync {
    /// Non-streaming upstream call; fills ctx.response_text/usage/provider/finish_reason.
    async fn dispatch(&self, ctx: &mut PipelineContext) -> Result<(), DispatchError>;
    /// True streaming; yields normalized chunks, accumulates ctx.response_text/usage on completion.
    fn stream_dispatch<'a>(&'a self, ctx: &'a mut PipelineContext) -> BoxStream<'a, Result<StreamChunk, DispatchError>>;
}
#[async_trait] pub trait Connector: Send + Sync {          // one per provider; P2 = ollama, llamacpp
    fn provider(&self) -> &'static str;
    async fn chat(&self, req: &ConnectorRequest) -> Result<ConnectorResponse, ConnectorError>;
    fn chat_stream(&self, req: &ConnectorRequest) -> BoxStream<'static, Result<StreamChunk, ConnectorError>>;
}
pub struct ConnectorRequest { pub model: String, pub messages: Vec<Value>, pub params: Value,
                              pub credential: Option<Credential> /* None for local; P3 injects managed/passthrough */ }
pub trait Depseudonymizer: Send + Sync {                   // P3: rolling-buffer restore across SSE chunks
    fn restore_text(&self, map: &PseudonymMap, s: &str) -> String;
    fn restore_stream(&self, map: &PseudonymMap) -> Box<dyn ChunkTransform>;
}   // P2 impl: IdentityDepseudonymizer
pub struct DispatchStage { /* Arc<dyn Dispatcher>, ConnectorRegistry, Arc<dyn Depseudonymizer> */ }
// P3 adds commercial Connectors to ConnectorRegistry + replaces LocalDispatcher; the Stage impl does not change.
```

---

### Task 1: `pipeline` crate — Stage trait, `PipelineContext`, `ProxyPipeline` driver

**Files:** Create `rust/crates/pipeline/{src/stage.rs,context.rs,driver.rs,seams.rs,flags.rs}`; Test `tests/driver.rs`

**Interfaces:** Produces everything in "Interface Definitions" above; `trait FlagResolver { async fn enabled(&self, flag:&str, distinct_id:Option<&str>)->bool }` adapter over P0 `flags::enabled`.

- [ ] **Step 1: Write tests** `driver.rs`: `runs_stages_in_order_and_logs_ran`; `flag_off_skips_with_skipped_log` (and **skip is evaluated before blocked short-circuit** — blocked ctx + flag-off stage logs `skipped:`, not `short-circuit:`); `blocked_ctx_short_circuits_remaining_stages` (logs `short-circuit:{name}`, stage `call` not invoked); `stage_error_releases_quota_once_then_propagates`; `blocked_after_stage_releases_quota_once` (guard taken, second release no-op); `release_failure_logged_never_raised`; `release_survives_task_cancellation` (shielded: spawn settle, abort outer, assert settlement completed — mirrors `settle_shielded`); `run_until_exclusive_and_run_after_exclusive` (head/tail partition equals `run`); `run_after_on_blocked_ctx_releases_quota_first`; `meter_after_block_short_circuits_like_python` (characterization of ported behavior); `dispatch_span_sets_genai_attrs` (`gen_ai.system/request.model/response.model/usage.input_tokens/output_tokens/response.finish_reason` — names exact; no prompt content).
- [ ] **Step 2: Run `cargo test -p pipeline`** → FAIL.
- [ ] **Step 3: Implement** per `_run_stage` (stages.py:335) semantics; distinct_id = `user.user_id` string (Python `ctx.user.id`). `FlagResolver` default `false` on error.
- [ ] **Step 4: Run** → PASS; clippy/fmt clean.
- [ ] **Step 5: Commit** `feat(rust): pipeline Stage trait, context, driver`.

### Task 2: `tokens` crate — tiktoken parity + TokenBudgetStage

**Files:** Create `rust/crates/tokens/` (`count`, `estimate`, `convert`, `limiter`, `stage`); `scripts/gen-token-parity-corpus.py` (dumps `{model, text|messages, expected_count}` JSON from Python `TokenManager`); Test `tests/parity.rs`, `tests/budget_stage.rs`

**Interfaces:** `fn count_tokens(text:&str, provider:&str, model:&str)->u64`; `fn estimate_io_tokens(messages:&[Value])->(u64,u64)`; `fn calculate_waddleai_tokens(i,o,provider,model)->u64` (rates from `token_conversion_rates`); `TokenLimiter::{reserve,reconcile}`; `TokenBudgetStage: Stage` name `"token_budget"`. Locate Python `TokenLimiter` first (`grep -rn "class TokenLimiter" shared proxy`) and reuse its Lua verbatim the same way as Task 7.

- [ ] **Step 1: Generate corpus** (ASCII, unicode/emoji, long, empty, per-model families, message arrays with roles) committed under `tests/fixtures/`.
- [ ] **Step 2: Write tests** `parity.rs::count_matches_python_corpus` (every row equal; **prints rows examined, FAIL if 0**); `encoder_selection_matches_token_manager` (gpt-4*/gpt-3.5 → `cl100k`, newer → `o200k`, unknown → Python's fallback, as-is); `estimate_io_matches_python_incl_estimate_paths`; `anthropic_gemini_do_not_use_tiktoken`; `conversion_rate_lookup_matches_python`. `budget_stage.rs`: `skips_without_vkey`; `skips_without_limits`; `blocks_429_with_decision_reason`; `stashes_reservation_id_on_allow`.
- [ ] **Step 3: Run** → FAIL.
- [ ] **Step 4: Implement** (port behavior including estimates; vendored BPE pinned + checksummed).
- [ ] **Step 5: Run** → PASS.
- [ ] **Step 6: Commit** `feat(rust): tiktoken parity + token budget stage`.

### Task 3: AuthStage + `auth_mode` detection + passthrough opt-in gate

**Files:** Create `rust/crates/pipeline/src/stages/auth.rs`, `auth_mode.rs`; Test `tests/auth_stage.rs`

**Interfaces:** Consumes P0 `authenticate_api_key`/`UserContext`. `fn detect_auth_mode(headers:&HeaderMap)->CredentialKind` (`WaddleKey` | `ProviderKey(SecretString)` | `None`); `AuthStage: Stage` name `"auth"`; `trait PassthroughPolicy { async fn enabled(&self, key:&UserContext)->bool }` (reads per-key/org flag — **builder greps `api_keys`/`virtual_keys`/org policy for an existing column first; if absent, a Python-side Alembic column + regenerated entities (Rust never migrates) — record outcome in PR**).

Per §5.1.1/§10.1: passthrough request carries the `wa-` key (org + entitlement + rate scope) **plus** the provider key in a separate header (builder confirms the header name against the Python proxy; record it). `wa-` only → Virtual.

- [ ] **Step 1: Write tests**: `none_user_blocks_401_unauthenticated`; `user_without_tenant_blocks_403_missing_organization` (reasons verbatim from stages.py:424); `wa_key_alone_sets_virtual`; `wa_key_plus_provider_key_sets_passthrough_when_enabled`; `provider_key_when_not_enabled_rejected_401` (opt-in gate); `provider_key_without_wa_key_rejected`; `passthrough_credential_never_in_debug_or_logs` (format `{:?}` of ctx and captured tracing output contain no key material); `passthrough_p2_dispatch_fails_loudly` (DispatchStage returns `StageError::Unsupported("passthrough dispatch: P3")` — never silently routes to managed creds); `malformed_wa_key_rejects_no_panic` (P0 behavior surfaced as 401).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(rust): auth stage + auth_mode detection + passthrough gate`.

### Task 4: `security` crate — PromptSecurity `THREAT_PATTERNS` scanner

**Files:** Create `rust/crates/security/src/prompt.rs`; `scripts/export-patterns.py` (dumps `THREAT_PATTERNS` (`shared/security/prompt_security.py:195`) with flags/severity/category to JSON); Test `tests/prompt.rs`

**Interfaces:** `PromptScanner::scan_messages(&[Value]) -> Vec<Threat>`; `fn should_block(&[Threat])->bool` (same threshold logic as Python).

- [ ] **Step 1: Export patterns + a labeled corpus** (each pattern: ≥2 positive, ≥2 negative strings generated by running Python).
- [ ] **Step 2: Write tests**: `every_exported_pattern_compiles_in_regex_crate` (any non-translatable pattern — backreference/lookaround — listed explicitly with a hand-reworked equivalent + proof test; **count compiled == count exported**); `corpus_verdict_equals_python` (per-pattern hit/no-hit identical; rows examined printed, 0 = FAIL); `case_insensitive_and_unicode_flags_preserved`; `should_block_threshold_matches_python`; `pathological_input_linear_time` (1MB adversarial string < budget).
- [ ] **Step 3–4: Run FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): prompt-security regex scanner (THREAT_PATTERNS parity)`.

### Task 5: ContentFilter tiers 1 & 2 + NER seam

**Files:** Create `rust/crates/security/src/content_filter.rs`, `ner.rs`; Test `tests/content_filter.rs`

**Interfaces:** `ContentFilter::filter_input(text, user_id, org_id, ip)->FilterResult{allowed, filtered_text, violations}`; `filter_output(...)` same shape; T1 `BUILTIN_PATTERNS` (23 PII/PCI, per-org toggles, never license-gated); T2 org custom rules loaded from `content_filter_rules` with Valkey-cached, invalidation-refreshed in-memory cache (**seed at startup + lazy-load; never fail-closed-empty**); T3 via `NerClient` (seam) gated on flag + `has_feature("pii_ner_detection")`; `NoNer` default.

- [ ] **Step 1: Write tests**: `builtin_patterns_parity_with_python_corpus` (23 patterns: detect + redaction output byte-equal; rows printed); `per_org_toggle_disables_pattern`; `custom_rule_overrides_builtin`; `redaction_replacement_text_matches_python`; `rules_cache_seeded_on_startup_and_lazy_loads` (cold cache + first request still applies org rules — guards the empty-cache fail-close defect); `invalidation_signal_refreshes_rules` (P0 `invalidation`); `tier3_skipped_when_flag_or_license_off_tiers12_still_run`; `tier3_unavailable_applies_degraded_policy` (`NoNer` → `security_degraded=true`; fail-open vs fail-closed per org policy, warn rate-limited — ported `content-filter-guard` semantics); `tier3_never_called_with_raw_pii_cache_key` (content-hash only).
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): content filter tiers 1-2 + NER seam`.

> **Out of P2 (tracked, fail-loud):** tier-4 LLM auditor / `IntentClassifier` (shieldgemma HTTP) → P3, behind an `Auditor` trait; ported-now behavior = `StageError::Unsupported` only if the org has it enabled (ingress excludes such orgs until then).

### Task 6: SecurityInStage + SecurityOutStage (v1 path)

**Files:** Create `rust/crates/pipeline/src/stages/{security_in,security_out}.rs`; Test `tests/security_stages.rs`

**Interfaces:** `SecurityInStage{scanner, filter, flags}` name `"security_in"`; `SecurityOutStage{filter, flags}` name `"security_out"`.

- [ ] **Step 1: Write tests**: `empty_messages_passthrough`; `injection_blocks_400_prompt_injection_detected`; `pii_hard_block_400_pii_detected`; `redacted_violation_sets_pii_detected_not_blocked`; `messages_rewritten_with_filtered_text_preserving_other_fields`; `scan_order_prompt_before_content_filter` (blocked injection never calls filter); `security_v2_flag_on_fails_loudly` (`StageError::Unsupported("security_v2")` — Python `_call_v2` is not ported in P2; ingress must keep v2 orgs on Python); `output_filter_redacts_response_text`; `output_block_sets_status_and_reason_per_python` (read `SecurityOutStage.__call__` stages.py:1361 and mirror exactly); `non_string_content_blocks_handled_as_python` (list-of-parts content).
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): security in/out stages`.

### Task 7: `quota` crate — Lua reuse + QuotaEngine + QuotaStage

**Files:** Create `scripts/export-quota-lua.py` (writes `rust/crates/quota/lua/reserve.lua`, `reconcile.lua` from `LUA_RESERVE`/`LUA_RECONCILE`), `rust/crates/quota/src/{engine,lease,stage,guard}.rs`; Test `tests/lua_drift.rs`, `tests/engine.rs`, `tests/quota_stage.rs`

**Interfaces:** `QuotaLimiter::{reserve, reconcile, release, sweep_expired}` mirroring `shared/quota/lease.py:177` (SCRIPT LOAD / EVALSHA with `NOSCRIPT` reload); `QuotaEngine::reserve(user, model, messages)->Outcome{blocked, reason, reservation}`; `QuotaGuard::settle_aborted(self, &PipelineContext)` (port of `settle_aborted`, quota_stage.py:33); `QuotaStage: Stage` name `"quota"`, mode by `ctx.auth_mode`: Virtual = credit-debit; Passthrough = **rate-limit-only, no debit** (§5.1.1; mode dispatch implemented + tested, passthrough requests unreachable end-to-end until P3).

- [ ] **Step 1: Write tests** `lua_drift.rs::embedded_lua_equals_python_constants` (runs the exporter, byte-diff vs `include_str!`; **FAIL on difference**). `engine.rs` against a **real Valkey**: `reserve_admits_then_counters_incremented_exactly_as_python` (same keys via P0 `keys`); `reserve_denies_quota_exhausted_daily_subcap_override_exceeded` (reasons verbatim); `reconcile_adjusts_to_actual`; `release_refunds_full_reserve`; `settle_aborted_charges_when_upstream_tokens_consumed` / `refunds_only_zero_usage_cache_hit_blocked_before_dispatch`; `settle_aborted_estimate_failure_never_refunds_consumed` (comment-pinned behavior); `overdraw_regression_gh_347_ported_as_is` (documented current behavior asserted, **not fixed**); `ttl_sweeper_expires_orphan_reservations`; `rust_and_python_share_counters` (reserve via Python `QuotaLimiter`, reconcile via Rust, and reverse — proves byte-compat). `quota_stage.rs`: `skips_when_engine_none_or_org_none_or_cache_hit`; `flag_resolved_with_org_id_distinct_id_default_false`; `fail_open_on_engine_error_logged`; `deny_sets_429_with_block_message`; `admit_stores_reservation_and_guard`; `passthrough_mode_rate_limit_only_never_debits`.
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): quota engine (Lua verbatim) + quota stage`.

### Task 8: `respcache` — CacheStage (exact + semantic)

**Files:** Create `rust/crates/respcache/src/{exact,semantic,affinity,singleflight,replay,embed,stage}.rs`; Test `tests/cache_stage.rs`

**Interfaces:** `ResponseCache::lookup(&PipelineContext)->LookupResult{status, cached, write_back}`; `annotate_miss(&mut ctx)` (affinity → `preferred_backend`); `EmbeddingClient::embed(&str)->Vec<f32>` (Ollama `/api/embeddings` via reqwest; model per Python `shared/cache/config.py`); `replay_openai_sse`/`replay_anthropic_sse`; `CacheStage: Stage` name `"cache"` flag `waddleai.response_cache`.

- [ ] **Step 1: Write tests**: `exact_key_bytes_match_python` (`shared/cache/keys.py`; includes `response_format` in key); `openai_and_anthropic_requests_never_share_entry`; `exact_hit_populates_ctx_provider_cache` (`provider="cache"`, `cache_hit`, `tokens_saved`, usage); `semantic_hit_above_threshold_miss_below`; `embedding_unavailable_degrades_to_exact_only_visibly` (logged + metric; never silent); `miss_stashes_write_back_and_preferred_backend`; `singleflight_collapses_concurrent_identical_requests`; `streaming_hit_sets_stream_iter_replay_event_sequence_equals_python`; `cache_hit_skips_quota_dispatch` (driver-level); `write_back_only_after_security_out_pass`; `metrics_record_lookup_and_tokens_saved_names_match`; `cache_shared_with_python_entry_roundtrip`.
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): response cache stage (exact+semantic)`.

### Task 9: `routing` — RoutingStage (pure decision logic)

**Files:** Create `rust/crates/routing/src/{offers,policy,cascade,escalate,alias,stage}.rs`; Test `tests/routing.rs`

**Interfaces:** `RoutingStage{registry: Arc<RegistryCache>}` name `"routing"`; `RegistryCache` over `model_registry`/`routing_rules` (seed + lazy-load, Valkey-invalidated, per-org offers `_load_offers`, stages.py:907). Reads spec §7 semantics in `stages.py:803–955` and `shared/routing/*`; sets `ctx.model`, `requested_model`, `fallback_chain`, `routed_from`, `provider` hints.

- [ ] **Step 1: Write tests (table-driven, rstest)**: `alias_resolves_and_sets_routed_from`; `stage0_tool_type_cascade_uses_header_hint` (kebab-case vocabulary); `escalate_hint_and_auto_high_low_suffix`; `pii_detected_sensitivity_clamp_excludes_external_candidates`; `capability_veto_redirects`; `policy_sorted_fallback_chain_excludes_chosen`; `cold_cache_lazy_loads_never_empty_fail_closed`; `invalidation_refreshes`; `explicit_provider_prefix_ollama_llama2_routes`; `golden_decisions_equal_python` (decisions from running Python RoutingStage over a fixture matrix — rows printed; 0 = FAIL); `local_only_filter_p2` (commercial candidates removed from the *executable* set but still appear in decisions so routing parity holds; dispatch of a commercial pick → `Unsupported("provider: P3")`).
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): routing stage`.

### Task 10: `connectors` (local) + DispatchStage + streaming

**Files:** Create `rust/crates/connectors/src/{ollama,llamacpp,registry}.rs`, `rust/crates/pipeline/src/stages/dispatch.rs`, `local_dispatcher.rs`; Test `tests/ollama.rs`, `tests/llamacpp.rs`, `tests/dispatch_stage.rs` (wiremock for protocol; one gated live test via `make test-gpu` against Ollama host)

**Interfaces:** `OllamaConnector`/`LlamaCppConnector: Connector` (port `shared/utils/llm_connectors.py` Ollama/LlamaCpp raw HTTP: `/api/chat` NDJSON, llama.cpp OpenAI-compat SSE); `LocalDispatcher: Dispatcher`; `DispatchStage: Stage` name `"dispatch"`; `ConnectorRegistry::get(provider)`; failover over `ctx.fallback_chain` for **retryable-only** errors (port `_prepare_dispatch`/`_record_dispatch_error`, stages.py:1165,1261); pooled `reqwest::Client` (timeouts, keep-alive).

- [ ] **Step 1: Write tests**: `ollama_chat_maps_request_and_usage` (prompt_eval_count/eval_count → input/output tokens); `ollama_stream_ndjson_to_chunks_with_final_usage`; `llamacpp_sse_to_chunks`; `upstream_5xx_retryable_fails_over_to_next_chain_entry`; `upstream_4xx_not_retried`; `dispatch_sets_provider_model_finish_reason`; `dispatch_error_sets_status_and_records_without_leaking_upstream_body_secrets`; `commercial_provider_unsupported_loud`; `passthrough_mode_unsupported_loud`; `depseudonymize_hook_called_identity_in_p2`; `stream_dispatch_accumulates_response_text_and_usage_for_security_out_and_meter`; `stream_client_disconnect_cancels_upstream_and_settles_partial_usage`; `utf8_split_across_chunks_reassembled`; `dispatch_does_not_inject_credentials_for_local`.
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): local connectors + dispatch stage (seams for P3)`.

### Task 11: `metering` — MeterStage

**Files:** Create `rust/crates/metering/src/{event,buffer,stage}.rs`; Test `tests/meter.rs`

**Interfaces:** `MeteringBuffer::record(MeteringEvent)` (batched SeaORM write to the same usage table the Python buffer writes; flush on size/interval/shutdown); `MeterStage{buffer, limiter}` name `"meter"`; reconcile `ctx.reservation_id` via `TokenLimiter::reconcile`; pooled-quota reconcile of `ctx.quota_reservation` with actual tokens (port the `_finalize` path; locate in `main.py`/`proxy_server`).

- [ ] **Step 1: Write tests**: `skips_without_vkey`; `records_event_when_usage_provider_model_present_incl_cache_provider` (`cache_status`, `tokens_saved`); `no_event_without_usage`; `reconciles_token_budget_reservation_with_input_plus_output`; `quota_reconcile_called_once_then_guard_cleared`; `estimated_flag_false_for_provider_usage_true_when_estimated` (estimate path as-is); `buffer_flushes_on_size_interval_and_shutdown_no_loss`; `db_down_buffers_and_retries_never_fails_response` (+ metric); `metric_names_labels_match_python`; `row_schema_equals_python_written_row` (insert via both, compare column-for-column).
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): meter stage + metering buffer`.

### Task 12: Axum routes — `/v1/chat/completions`, `/v1/messages`, `/v1/models` (+ SSE)

**Files:** Create `rust/crates/waddleai-proxy/src/routes/{chat,messages,models,sse,wire}.rs`; Modify `main.rs` (build pipeline in the Python order, mount routers beside P0 health); Test `tests/routes.rs`

**Interfaces:** `fn build_pipeline(deps)->ProxyPipeline` (order: auth, token_budget, security_in, cache, routing, quota, dispatch, security_out, meter — `main.py:1113–1403`); handlers = `parse_wire -> PipelineContext -> run | run_until("dispatch")→stream→run_after("dispatch") -> render_wire`. Error bodies/status/`block_reason` mapping identical to Python (400 security, 401, 403, 429, 503). `usage.waddleai` additive object rendered from `usage_meta`/`routed_from`. `/v1/models` from the registry cache. `/v1/messages/count_tokens` and `/mem0/*`, `/api/*`, MCP, gRPC are **not** in P2 (P6) → ingress keeps them on Python.

- [ ] **Step 1: Write tests**: `chat_nonstream_roundtrip_wire_shape`; `messages_nonstream_anthropic_wire_shape` (content blocks, `stop_reason`, usage names); `stream_openai_event_sequence_ends_with_done`; `stream_anthropic_event_sequence_message_start_to_message_stop`; `blocked_request_wire_error_matches_python` (400/401/403/429 bodies); `quota_denied_429_message`; `cache_hit_stream_replays_sse`; `post_stream_security_out_and_meter_run_on_accumulated_text` (run_after semantics); `client_disconnect_mid_stream_releases_or_charges_correctly`; `invalid_json_400_no_panic`; `oversized_body_rejected`; `models_lists_registry_models`; `headers_session_memory_tool_type_escalate_threaded_into_ctx`; `request_id_and_trace_headers_propagated`; `unsupported_routes_not_mounted_404_not_silent_stub`.
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): axum chat/messages/models routes`.

### Task 13: Shadow-compare (flagged) + telemetry parity

**Files:** Create `rust/crates/waddleai-proxy/src/shadow.rs`; Modify telemetry registrations; Test `tests/shadow.rs`, `tests/telemetry_parity.rs`

**Interfaces:** `ShadowMirror::submit(req, python_resp)` — flag `waddleai.rust_proxy_shadow` (default OFF); mirrors a sampled copy to the Rust pipeline **with dispatch to a recorded/stub-local upstream only**, **never double-reserving quota or double-metering** (shadow runs Quota/Meter in dry-run: no Valkey/DB writes); diff via the P0 comparator; emits `waddleai_shadow_diff_total{surface,kind}`.

- [ ] **Step 1: Write tests**: `shadow_off_by_default_zero_side_effects`; `shadow_never_writes_quota_or_metering` (Valkey/DB write counters unchanged — **assert counters examined > 0**); `shadow_diff_detected_on_shape_change`; `shadow_timeout_never_affects_primary_response`; `gen_ai_span_attrs_and_prometheus_names_labels_match_python` (scrape both, compare name+label sets).
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): shadow-compare + telemetry parity`.

### Task 14: Contract gate for the local-model path + coverage + smoke

**Files:** Modify `tests/contract/` (add local-model chat/messages/models/stream cases targeting both implementations via `RUST_PROXY_URL`), `Makefile` (`test-contract-rust`, `test-rust-proxy`), `.github/workflows/rust-proxy-build.yml` (contract job vs Ollama stub); Test `tests/contract/test_local_path_parity.py`

- [ ] **Step 1: Write contract cases** (normalized compare via P0 harness): non-stream OpenAI/Anthropic; streaming event-sequence + content modulo IDs; blocked injection; PII redaction; quota-denied; cache exact hit; models list; each runs against Python and Rust; **assert case count > 0 and both sides responded**.
- [ ] **Step 2: Run** → FAIL on missing Rust server wiring; fix until PASS.
- [ ] **Step 3: Smoke + telemetry assertions** (log ≥1, metric ≥1, histogram ≥1) with OTLP test sink; counts printed.
- [ ] **Step 4: Verify** `cargo llvm-cov --fail-under-lines 90` per crate; `cargo deny check`; `cargo audit`; fail-once check of each gate (make it red once, confirm).
- [ ] **Step 5: Commit** `test(contract): local-model path parity vs Python + CI gate`.

---

## Self-Review

- **Spec coverage (§5.1 stage table, P2 row):** Auth+modes T3; TokenBudget T2; SecurityIn/Out regex T4–T6 (NER seam T5, v2/LLM-auditor fail-loud); Quota Lua-verbatim + single-writer T7; Cache T8; Routing T9; Dispatch (local only, seams) T10; Meter T11; Axum routes T12; shadow-compare (§11) T13; contract gate (§7) T14. Deferred by design: commercial/passthrough dispatch, depseudonymize, NER impl (P3); plugin hooks (P4); `/mem0`, MCP, gRPC, `count_tokens` (P6).
- **Gaps for the owner to confirm (not in the P2 brief, but present in Python's pipeline):** security **v2** path (`_call_v2`), **memory_stages/knowledge_stage** (§6A session memory/scratchpad/CodeRAG), and the tier-4 **LLM auditor/IntentClassifier**. This plan makes them fail loudly and keeps those orgs/routes on Python via ingress flags; if parity for them is wanted in P2, add tasks (est. 2–3).
- **Review Focus mapped:** SSE T10/T12; quota T1/T7/T11 (#347 pinned T7); auth_mode T3; regex parity T4/T5; tiktoken T2; driver semantics T1.
- **Type consistency:** `PipelineContext`/`Stage`/seams defined once above and used verbatim by T1–T13; `UserContext`/flags/keys are P0 names; `QuotaGuard` replaces Python's `quota_release` closure (driver `.take()`s it).
- **Placeholders:** none; "builder confirms" items (passthrough header name, existing enable-column, `TokenLimiter` location, `_finalize` site) are explicit investigation steps with a recorded outcome, per §9/§11.
- **Proportion:** signatures + test names/assertions only; 14 tasks.
