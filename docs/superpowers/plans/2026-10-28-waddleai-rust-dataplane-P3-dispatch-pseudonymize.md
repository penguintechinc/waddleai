# WaddleAI Rust Data-Plane — P3 Dispatch, Pseudonymize & Tier-4 Security Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the P2 Rust proxy from local-only to **all providers** (OpenAI/xAI, Anthropic, Gemini, Bedrock), with a real pooled-gRPC `NerClient`, the **pseudonymize↔depseudonymize round-trip** (incl. SSE rolling-buffer restore), the **passthrough dispatch mechanism**, and the tier-4 guard-model paths (IntentClassifier + LLM auditor) — contract-gated against the Python proxy's commercial path, no billing/metering drift.

**Architecture:** Existing P2 crates grow: `connectors` gains `openai`, `anthropic`, `gemini`, `bedrock`, `sse`, `credentials`, and the full `ConnectorRegistry` + `RegistryDispatcher` (replaces P2 `LocalDispatcher`); `security` gains `ner_grpc`, `pseudonym`, `restore`, `intent`, `auditor`. The P2 `Stage` impls (`DispatchStage`, `SecurityInStage`) do **not** change shape — P3 swaps seam implementations (`Dispatcher`, `NerClient`, `Depseudonymizer`) and fills the `ConnectorRegistry`.

**Tech Stack:** `reqwest` (rustls) for OpenAI/Anthropic/Gemini/Ollama-style HTTP, **`aws-sdk-bedrockruntime` + `aws-config`** (the one SDK exception), `tonic`/`prost` (NER client; protos from P1), `secrecy`, `zeroize`, `eventsource-stream`-style parser (own `sse` module — see Task 2), `wiremock`, `rstest`, `cargo-llvm-cov`.

**Spec:** `docs/superpowers/specs/2026-10-28-waddleai-rust-dataplane-migration-design.md` §5.1 (DispatchStage row), §5.1.1 (auth modes / passthrough), §5.3 (classification), §6 (pooled gRPC), §11 (pseudonymize round-trip, cache, "NER runs whenever PERSON detection is enabled"). **Siblings:** P0 `…-P0-foundation.md`, P1 `…-P1-svc-classification.md`, P2 `…-P2-proxy-core-light.md`.

## Interfaces consumed (by exact name)

| From | Name | Use in P3 |
|---|---|---|
| P2 `pipeline` | `Stage`, `StageError{Unsupported,Upstream,Internal}`, `PipelineContext` (`pseudonym_map`, `auth_mode`, `passthrough_credential: Option<SecretString>`, `messages`, `response_text`, `usage`, `provider`, `finish_reason`, `fallback_chain`, `stream_iter`, `security_degraded`, `pii_detected`, `usage_meta`) | everything |
| P2 seams | `Connector`, `ConnectorRequest{model,messages,params,credential}`, `ConnectorResponse`, `ConnectorError`, `StreamChunk`, `Dispatcher`, `DispatchError`, `ConnectorRegistry::get`, `NerClient`, `NerRequest`, `EntitySpan{kind,start,end,score}`, `EntityKind`, `NerError`, `Depseudonymizer`, `ChunkTransform`, `PseudonymMap`, `Credential`, `LocalDispatcher`/`NoNer`/`IdentityDepseudonymizer` (replaced) | implement/replace |
| P2 `security` | `ContentFilter` tier-3 degraded policy, `Auditor` trait seam (Task 5 note), `SecurityInStage` | tier-3/4 wiring |
| P2 `auth` | `AuthMode::{Virtual,Passthrough}`, `detect_auth_mode` | dispatch branch |
| P1 proto | `waddleai.classification.v1` `ClassificationService.Analyze`/`GetEntityConfig`; `AnalyzeRequest{api_version,text,language,phase,org_public_id,flag_distinct_id,disabled_entity_labels,fail_mode}`; `AnalyzeResponse{spans,ner_status,backend,degraded,degraded_reason,block_recommended,cache_hit,analyzed_chars,truncated,model_fingerprint}`; `EntitySpan{entity_type,entity_label,start_cp,end_cp,start_byte,end_byte,raw_score,confidence,action}`; enums `NerStatus`, `DegradedReason`, `FailMode`, `EntityType` | `GrpcNerClient` |
| P0 | `Config`, `flags::enabled`, `telemetry`, `db` entities (`provider_credentials`, `ai_providers`), `cache` | credentials, flags |

## Global Constraints

- **`reqwest` + `rustls` only** for HTTP; **AWS Rust SDK is the single SDK exception** (Bedrock SigV4 + event-stream). No other vendor SDKs; no `native-tls`/OpenSSL.
- **Pooled gRPC** to `svc-classification`: one `tonic` channel set over the headless Service (DNS re-resolve), honors server `max_connection_age` (reconnect on GOAWAY), per-call deadline, short-lived signed JWT (`classification:analyze`) — SPIFFE-ready (mTLS-SVID accepted as alternative).
- **Port verbatim:** request/response shaping, retry (`_with_retries` 3 attempts, 100 ms base), error taxonomy (`ProviderTimeout/RateLimit/Server/Client` → retryable-only failover), and **metering/estimate behavior as-is** (Anthropic/Gemini `_resolve_token_counts` estimate paths, `_adapt_token_limit`, whatever streaming-usage behavior `shared/utils/llm_connectors.py` has at the branch base). **Do not "fix" estimates or stream-usage gaps** — characterization tests pin them; discrepancies go to a GitHub issue.
- **No PII in logs/spans/metrics/errors**: no prompt text, no entity text, no `PseudonymMap` contents, no credential material; `PseudonymMap` and credential types have redacting `Debug` and `zeroize` on drop. Upstream error bodies are never logged.
- **Fail loudly:** unsupported provider/mode → typed `DispatchError::Unsupported(..)`; an unreachable NER backend is a *signaled* degraded state handled by policy, never an empty "all clear".
- `unsafe` forbidden; no `unwrap`/`expect` outside tests; `tracing` only; doc comment (2–3 lines) on every struct/trait/fn; no ASCII dividers.
- **Everything behind PostHog flags (default OFF)**: commercial providers keyed by existing routing flags; pseudonymize path by `waddleai.pii_ner` + `pii_ner_detection` entitlement (via P1 gate — proxy only passes `flag_distinct_id`); passthrough dispatch behind `waddleai.passthrough_auth` (rollout is **P6**; P3 builds the mechanism, default OFF).
- **Coverage:** `cargo-llvm-cov` ≥ 90% per crate (new + touched); `cargo-deny`/`cargo-audit` clean, AWS crates pinned exact, no PRC deps; amd64 first (owner directive 2026-10-13).

## Review Focus (the five failure modes)

1. **Placeholder split across SSE chunks** — `[PSEUDO_PERSON_3]` arriving as `[PSE` + `UDO_PERS` + `ON_3]`, split inside a multi-byte codepoint, split across the upstream *event* boundary, placeholders adjacent/overlapping a legit `[`, stream ending mid-placeholder, client disconnect with buffer non-empty → Tasks 8, 9.
2. **Per-provider stream-format differences** — OpenAI `data:`/`[DONE]` (+ usage-only final chunk), Anthropic typed events (`message_start`/`content_block_delta`/`message_delta`/`message_stop`, `ping`, `error`), Gemini SSE `streamGenerateContent?alt=sse` cumulative-vs-delta + `usageMetadata`, Bedrock event-stream binary frames wrapping Anthropic-format JSON; CRLF vs LF, multi-line `data:`, comment lines, partial UTF-8 → Tasks 2–6.
3. **`svc-classification` unreachable → open/closed policy** — must match P1/`content_filter` semantics (`degraded` + `block_recommended`); critically: for a **commercial** destination a dead NER under *pseudonymize* means names would egress — policy decides, never silent pass → Tasks 10, 11.
4. **Passthrough credential forwarding/leakage** — forwarded only to the provider its key type matches, never to a failover target, never logged/traced/in URLs/echoed, upstream 401/403 surfaced unchanged, no managed-credential fallback on empty passthrough credential → Task 7 (+ 3–6 header handling).
5. **Bedrock SigV4** — region/creds resolution, clock skew, event-stream framing, converse (non-stream) vs `invoke_model_with_response_stream` (stream) parity with Python, throttling → retry taxonomy, SDK default retry vs ported retry (no double-retry) → Task 6.

---

## Interface Definitions (P6 touches `PseudonymMap`/`Depseudonymizer`/`ConnectorRegistry`; treat as stable)

```rust
// crates/connectors/src/registry.rs — REPLACES P2's local-only registry/LocalDispatcher
pub struct ConnectorRegistry { /* HashMap<&'static str, Arc<dyn Connector>> */ }
impl ConnectorRegistry {
    pub fn new() -> Self;
    /// Registers a connector under `connector.provider()`; duplicate provider = Err (never silent overwrite).
    pub fn register(&mut self, c: Arc<dyn Connector>) -> Result<(), RegistryError>;
    /// Looks up a provider; unknown = DispatchError::Unsupported("provider: <name>") — never a silent default.
    pub fn get(&self, provider: &str) -> Result<Arc<dyn Connector>, DispatchError>;
    pub fn providers(&self) -> Vec<&'static str>;
    /// Builds local (ollama, llamacpp) + commercial (openai, xai, anthropic, gemini, bedrock) from Config; commercial ones only if enabled in `ConnectorsConfig`.
    pub fn from_config(cfg: &ConnectorsConfig, http: reqwest::Client) -> Result<Self, RegistryError>;
}
pub struct RegistryDispatcher { /* Arc<ConnectorRegistry>, Arc<dyn CredentialStore>, Arc<dyn Depseudonymizer>, retry policy */ }
impl Dispatcher for RegistryDispatcher { /* dispatch + stream_dispatch per P2 trait */ }

#[async_trait] pub trait CredentialStore: Send + Sync {
    /// Managed-credential lookup (`provider_credentials`, selector per org/provider); never called in Passthrough mode.
    async fn resolve(&self, org_id: i64, provider: &str) -> Result<Credential, CredentialError>;
}
pub enum Credential {                       // P2 declared the type; P3 fixes the variants
    ApiKey(secrecy::SecretString),          // managed OpenAI/xAI/Anthropic/Gemini key
    Aws { region: String, provider_chain: bool }, // Bedrock — SDK default credential chain
    Passthrough(secrecy::SecretString),     // user's own provider key, forwarded as-is
}
pub enum DispatchError {                    // P2 declared; P3 adds variants (additive)
    Unsupported(String), Retryable(ConnectorError), NonRetryable(ConnectorError),
    UpstreamRejected { status: u16, body: bytes::Bytes },   // passthrough greenlight: surfaced unchanged
    CredentialMismatch, NoCredential, Internal(String),
}
```

```rust
// crates/security/src/pseudonym.rs
/// Request-scoped placeholder<->value map. Holds raw PII: redacting Debug, zeroize on drop, never serialized.
pub struct PseudonymMap { /* forward: HashMap<Zeroizing<String>, String>, reverse: HashMap<String, Zeroizing<String>>, counters: HashMap<EntityKind,u32> */ }
impl PseudonymMap {
    pub fn new() -> Self;
    /// Stable placeholder `[PSEUDO_{KIND}_{n}]` for `value`; same value => same placeholder within the request.
    pub fn placeholder_for(&mut self, kind: EntityKind, value: &str) -> String;
    pub fn lookup(&self, placeholder: &str) -> Option<&str>;
    pub fn is_empty(&self) -> bool;
    pub fn len(&self) -> usize;
    /// Longest placeholder currently in the map, in bytes — sizes the SSE hold-back window.
    pub fn max_placeholder_len(&self) -> usize;
}
pub struct Pseudonymizer { /* Arc<dyn NerClient>, FailPolicy, flags */ }
impl Pseudonymizer {
    /// Replaces NER-detected spans in `ctx.messages` with placeholders (fills `ctx.pseudonym_map`); returns outcome incl. degraded/blocked.
    pub async fn apply(&self, ctx: &mut PipelineContext, dest: DestinationKind) -> Result<PseudonymizeOutcome, StageError>;
}

// crates/security/src/restore.rs
pub struct PlaceholderRestorer;           // impl Depseudonymizer — replaces IdentityDepseudonymizer
impl Depseudonymizer for PlaceholderRestorer {
    fn restore_text(&self, map: &PseudonymMap, s: &str) -> String;                       // non-streaming
    fn restore_stream(&self, map: &PseudonymMap) -> Box<dyn ChunkTransform>;             // rolling buffer
}
pub trait ChunkTransform: Send {                                                         // P3 fixes the methods
    /// Feeds the next text delta; returns the safe-to-emit prefix (may be empty). Buffers only a possible placeholder prefix.
    fn push(&mut self, delta: &str) -> String;
    /// End of stream (or disconnect): flushes remaining buffer, restoring any now-complete placeholder, emitting the rest verbatim.
    fn finish(&mut self) -> String;
}

// crates/security/src/ner_grpc.rs
pub struct GrpcNerClient { /* tonic Channel pool, JwtProvider, deadline, config */ }
impl NerClient for GrpcNerClient {
    async fn analyze(&self, req: &NerRequest) -> Result<Vec<EntitySpan>, NerError>;      // spans use start_byte/end_byte
}
impl GrpcNerClient { pub async fn analyze_detailed(&self, req: &NerRequest) -> Result<NerAnalysis, NerError>; }
pub struct NerAnalysis { pub spans: Vec<EntitySpan>, pub truncated: bool, pub analyzed_chars: u32, pub cache_hit: bool, pub backend: String }
// Additive P2 amendments (Task 1 patches P2 call sites): NerRequest gains {phase, flag_distinct_id, disabled_labels, fail_mode};
// NerError gains `Degraded { reason: DegradedReason, block_recommended: bool }` (mark enum #[non_exhaustive]); NerClient gains default
// `analyze_detailed` that wraps `analyze`.
```

---

### Task 1: P2 seam amendments, `CredentialStore`, shared HTTP client, redaction types

**Files:** Modify `rust/crates/pipeline/src/seams.rs` (additive fields/variants per above), `connectors/src/{lib,error}.rs`; Create `connectors/src/{credentials,http}.rs`; Test `connectors/tests/{credentials,http}.rs`

**Interfaces:** `Credential` variants, `CredentialStore`, `DbCredentialStore` (reads `provider_credentials` via P0 entities, selector parity with `RoundRobinSelector`/`WeightedSelector`), `fn build_http_client(cfg:&HttpCfg)->reqwest::Client` (rustls, pooled, connect/read timeouts, `redirect(none)`, HTTP/2), `fn scrub_err(e: reqwest::Error)->ConnectorError` (calls `without_url()`).

- [ ] **Step 1: Write tests:** `credential_debug_never_prints_secret` (format `{:?}` of every variant + `PipelineContext`); `reqwest_error_scrubbed_of_url_and_query` (Gemini-style `?key=` URL never survives into `ConnectorError` text); `round_robin_selector_matches_python_sequence`; `weighted_selector_distribution_within_tolerance` (rows examined printed); `db_store_returns_no_credential_not_default` (no row → `CredentialError::NotFound`, never empty key); `redirects_disabled` (a 302 is an error — never follow with auth headers); `p2_struct_literal_sites_compile_with_new_fields` (P2 `NerRequest` construction migrated to `NerRequest::new`).
- [ ] **Step 2: Run `cargo test -p connectors -p pipeline`** → FAIL.
- [ ] **Step 3: Implement.** Builder greps P2 for every `NerRequest {` / `match NerError` site and patches them; record count in PR.
- [ ] **Step 4: Run** → PASS; clippy/fmt clean.
- [ ] **Step 5: Commit** `feat(rust): credential store, http client, P2 seam amendments`.

### Task 2: Streaming parsers — SSE + event-stream normalizer

**Files:** Create `rust/crates/connectors/src/sse.rs`; Test `tests/sse.rs`

**Interfaces:** `struct SseParser` — `fn push(&mut self, bytes:&[u8]) -> Vec<SseEvent>`, `fn finish(&mut self)->Vec<SseEvent>`; `SseEvent{event:Option<String>, data:String, id:Option<String>}`. Handles CRLF/LF/CR, multi-line `data:`, comment lines (`:`), BOM, **UTF-8 sequences split across `push` calls** (buffer bytes, never lossy-decode a partial char), `[DONE]` left to the provider layer. `fn ndjson_lines(...)` for Ollama-style (P2 reuse, moved here if P2 hand-rolled).

- [ ] **Step 1: Write tests:** `lf_and_crlf_and_cr_events`; `multi_line_data_joined_with_newline`; `comment_and_keepalive_ignored`; `event_split_across_every_byte_boundary` (property: feeding a corpus 1 byte at a time == whole); `multibyte_char_split_across_pushes_reassembled` (emoji + CJK, every split point); `trailing_event_without_blank_line_flushed_on_finish`; `oversized_event_rejected_not_unbounded` (max event bytes guard → error); `invalid_utf8_is_error_not_replacement_char`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement. Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(rust): SSE parser with split-safe UTF-8`.

### Task 3: OpenAI + xAI connector (reqwest)

**Files:** Create `connectors/src/openai.rs` (xAI = `OpenAiConnector::xai()` config variant, port of `XAIConnector`); Test `tests/openai.rs` (wiremock)

**Interfaces:** `OpenAiConnector{http, base_url, kind}: Connector` (`provider()` = `"openai"`/`"xai"`); ports `_adapt_token_limit` (`max_tokens`↔`max_completion_tokens` by model family), `chat_completion` (`/v1/chat/completions`, `Authorization: Bearer` set `sensitive`), `stream_chat_completion` (SSE, `stream_options.include_usage` **exactly as Python does today**), retry via ported `with_retries`; error mapping HTTP→`ProviderTimeout/RateLimit/Server/Client`.

- [ ] **Step 1: Write tests:** `request_body_matches_python_golden` (fixtures captured from running Python connector against a recording stub; rows printed, 0 = FAIL); `adapt_token_limit_table` (rstest per model family, as-is); `usage_mapped_prompt_completion_tokens`; `stream_deltas_then_usage_chunk_then_done`; `stream_usage_behavior_equals_python_characterization` (pins current Python streaming-usage output incl. any zero/estimate gap — **not fixed**); `done_sentinel_ends_stream_no_extra_chunk`; `stream_error_event_midstream_surfaces_connector_error`; `429_retry_after_honored_then_rate_limit_error`; `5xx_retried_3x_then_server_error`; `4xx_not_retried`; `authorization_header_marked_sensitive_and_absent_from_error_text`; `no_credential_sent_when_credential_none_is_an_error_for_commercial` (loud); `xai_base_url_and_quirks`.
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): openai/xai connectors`.

### Task 4: Anthropic connector

**Files:** Create `connectors/src/anthropic.rs`; Test `tests/anthropic.rs`

**Interfaces:** `AnthropicConnector: Connector`; `/v1/messages`, `x-api-key` (sensitive) + `anthropic-version` (as pinned in Python SDK use); system-message extraction (`_estimate_input_tokens(system_message, user_messages)`); `resolve_token_counts(...)` **ported with the estimate fallback + `usage_estimated` metric hook**; streaming over typed events.

- [ ] **Step 1: Write tests:** `system_message_lifted_to_system_field`; `request_body_matches_python_golden`; `usage_from_message_start_and_message_delta_summed_as_python`; `estimate_used_when_provider_usage_missing_and_metric_incremented` (as-is); `stream_event_sequence_message_start_to_message_stop` (`content_block_start/delta/stop`, `ping` ignored); `stream_error_event_maps_to_connector_error`; `stop_reason_mapped_to_finish_reason_as_python`; `thinking_or_non_text_blocks_handled_as_python` (characterization — unknown block types ignored, not crash); `retry_taxonomy_matches_openai_tests`; `x_api_key_never_logged`.
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): anthropic connector`.

### Task 5: Gemini connector

**Files:** Create `connectors/src/gemini.rs`; Test `tests/gemini.rs`

**Interfaces:** `GeminiConnector: Connector`; `models/{model}:generateContent` and `:streamGenerateContent?alt=sse`; **credential in `x-goog-api-key` header, never `?key=`** (keeps it out of URLs/logs — wire-equivalent upstream); role mapping (`assistant`→`model`), `systemInstruction`, `usageMetadata` → `_resolve_token_counts` (ported with `_int_or_zero` and estimate fallback), `_fallback_model_list`.

- [ ] **Step 1: Write tests:** `role_and_system_mapping_matches_python_golden`; `usage_metadata_mapped_and_int_or_zero_semantics`; `estimate_fallback_as_python`; `stream_chunks_are_deltas_or_cumulative_as_python_handles` (characterization — if Gemini emits cumulative text the Python path's handling is ported verbatim, asserted with a fixture); `final_chunk_carries_usage`; `safety_block_finish_reason_maps_as_python`; `key_in_header_not_url` (wiremock asserts query has no `key`); `empty_candidates_error_not_panic`; `retry_taxonomy`.
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): gemini connector`.

### Task 6: Bedrock connector (AWS Rust SDK)

**Files:** Create `connectors/src/bedrock.rs`; Modify `rust/deny.toml` (allow AWS crates, exact pins), `Cargo.toml`; Test `tests/bedrock.rs` (SDK `StaticReplayClient`/`aws-smithy-http-client` test utils — no network)

**Interfaces:** `BedrockConnector{client: aws_sdk_bedrockruntime::Client}: Connector`; **non-stream = `converse`** (message/system/inference-config shaping as Python `chat_completion`, usage from `ConverseOutput.usage`); **stream = `invoke_model_with_response_stream`** with Anthropic-on-Bedrock body (as Python `stream_chat_completion`), decoding `chunk.bytes` JSON events → `StreamChunk`; region default `us-east-1` **as Python hard-codes today** (config-overridable additive, default unchanged); credentials via SDK default provider chain (`Credential::Aws{provider_chain:true}`); SDK retry set to `RetryConfig::disabled()` and the ported `with_retries` used instead — **one retry layer only**; `SdkError`→taxonomy mapping parity with the botocore `ClientError` mapping (`ThrottlingException`→RateLimit, `ModelTimeoutException`/`ServiceUnavailable`→Server/Timeout, `ValidationException`/`AccessDenied`→Client).

- [ ] **Step 1: Write tests:** `converse_request_shape_matches_python_golden`; `usage_input_output_tokens_mapped`; `stream_event_stream_frames_decoded_to_chunks` (replayed binary event-stream, frames split across HTTP body chunks); `stream_final_usage_from_message_delta_as_python`; `throttling_maps_rate_limit_and_retried_by_ported_layer_only` (counts exactly 3 attempts total, not 3×SDK-retries); `access_denied_not_retried`; `sigv4_authorization_header_present_and_scoped_to_bedrock_runtime_region` (inspect captured request: `AWS4-HMAC-SHA256`, credential scope `…/us-east-1/bedrock/aws4_request`, `x-amz-date` present, no secret in headers); `region_override_changes_endpoint_and_scope`; `no_credentials_resolved_is_loud_error_not_unsigned_request`; `credentials_never_in_error_or_debug`; `passthrough_credential_rejected_for_bedrock` (→ `Unsupported("passthrough: bedrock")`, see Task 7). One **gated live test** (`make test-live-bedrock`, skipped-counts-as-FAIL only in the dedicated target) behind env creds.
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): bedrock connector via aws-sdk-bedrockruntime`.

### Task 7: `ConnectorRegistry` (full) + `RegistryDispatcher` + passthrough dispatch

**Files:** Modify `connectors/src/registry.rs`; Create `connectors/src/dispatcher.rs`, `passthrough.rs`; Delete `pipeline/src/stages/local_dispatcher.rs` (P2) after parity; Test `tests/registry.rs`, `tests/dispatcher.rs`, `tests/passthrough.rs`

**Interfaces:** `RegistryDispatcher: Dispatcher` — port of `DispatchStage._prepare_dispatch`/`_record_dispatch_error` (stages.py:1165/1261): select provider from `ctx.model`/`ctx.provider`, failover over `ctx.fallback_chain` for **retryable-only** errors, fills `ctx.provider/model/response_text/usage/finish_reason`. Credential resolution: `Virtual` → `CredentialStore::resolve(org, provider)`; **`Passthrough` → `Credential::Passthrough(ctx.passthrough_credential.clone().ok_or(NoCredential)?)`** — the managed store is **never consulted** in this mode. `fn key_kind_matches(provider:&str, key:&SecretStr)->bool` (prefix/shape check: Anthropic `sk-ant-`, OpenAI/xAI `sk-`/`xai-`, Gemini `AIza`) — mismatch → `CredentialMismatch` (400) **before** any network call. Passthrough disables cross-provider failover (a user key is never sent to a different provider). Upstream 4xx/401/403 in passthrough → `DispatchError::UpstreamRejected{status, body}` → `ctx.status_code = status`, body relayed unchanged (headers: allowlist only — `content-type`, `retry-after`, `x-request-id`); the upstream non-401/403 response is the "greenlight". Passthrough + Bedrock → `Unsupported`. `DispatchStage` removes P2's loud-fail for `commercial_provider_unsupported_loud` / `passthrough_mode_unsupported_loud` **only when the corresponding flag is ON**; flag OFF keeps the loud typed error.

- [ ] **Step 1: Write tests:**
  Registry: `register_duplicate_provider_errors`; `get_unknown_provider_unsupported_loud`; `from_config_registers_only_enabled_commercial`; `local_connectors_still_registered_p2_parity`.
  Dispatcher: `virtual_mode_uses_managed_credential_for_org`; `retryable_error_fails_over_to_next_chain_entry`; `non_retryable_not_failed_over`; `fallback_chain_exhausted_sets_503_dispatch_error_as_python`; `dispatch_sets_provider_model_finish_reason_usage`; `cache_free_dispatch_error_never_leaks_upstream_body_secrets`; `stream_dispatch_accumulates_text_and_usage_for_security_out_and_meter`; `stream_client_disconnect_cancels_upstream_and_settles_partial_usage`; `golden_dispatch_matrix_equals_python` (provider × stream/non-stream fixture matrix from running Python; rows printed, 0 = FAIL).
  Passthrough: `passthrough_forwards_ctx_credential_to_upstream_not_managed` (wiremock captures `Authorization`/`x-api-key`; asserts equals the user key and `CredentialStore` mock call-count == 0); `passthrough_without_credential_errors_no_managed_fallback`; `passthrough_key_type_mismatch_rejected_before_network` (OpenAI-shaped key on an Anthropic route → 400, wiremock hit-count 0); `passthrough_never_fails_over_to_other_provider`; `upstream_401_surfaced_unchanged_status_and_body`; `upstream_200_is_greenlight_and_flows_through_pipeline`; `passthrough_key_absent_from_logs_spans_metrics_and_debug` (canary key; scan captured tracing output, span attrs, metric label values); `response_headers_allowlisted_only` (upstream `set-cookie`/`x-api-key` echo dropped); `bedrock_passthrough_unsupported_loud`; `passthrough_flag_off_keeps_loud_unsupported`; `passthrough_quota_mode_unchanged_rate_only` (P2 QuotaStage still no-debit — regression pin only).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement. Step 4: Run** → PASS (P2's `dispatch_stage` suite must stay green with `RegistryDispatcher` substituted for `LocalDispatcher`).
- [ ] **Step 5: Commit** `feat(rust): full connector registry, dispatcher, passthrough dispatch`.

> Builder-confirm (record in PR): (a) whether Python rotates/selects among multiple managed credentials per request or per cache — port as-is; (b) the passthrough header carrying the provider key (P2 Task 3 recorded it) — Task 7 consumes `ctx.passthrough_credential` only, never re-parses headers.

### Task 8: `PseudonymMap` + `Pseudonymizer` (input substitution)

**Files:** Create `rust/crates/security/src/{pseudonym,pseudonymize}.rs`; Test `tests/pseudonym.rs`

**Interfaces:** per "Interface Definitions". `Pseudonymizer::apply` flow: gate (flag/entitlement owned by svc-classification → `GATE_DISABLED` ⇒ no-op, **not degraded**) → for each message string content (incl. list-of-parts text parts as P2 `non_string_content_blocks_handled_as_python`) call `NerClient::analyze_detailed` → validate spans → drop overlaps (keep longest/leftmost, deterministic) → substitute **right-to-left by byte offset** → `PseudonymMap::placeholder_for`. `DestinationKind::{Local,Commercial}` — port `_COMMERCIAL_PROVIDERS` classification; local destinations skip pseudonymization per ported `applies_to` default (builder confirms against `stages.py:_apply_upstream_filter` + `PolicyResolver`; the P2 v2 path stays `Unsupported` — P3 implements only the v1-reachable "PERSON detection enabled" trigger from spec §11 and records the trigger source in the PR). Placeholder format `[PSEUDO_{KIND}_{n}]` — **same shape as `upstream_filters.py:135`** so Python/Rust outputs compare.

- [ ] **Step 1: Write tests:** `same_value_same_placeholder_across_messages`; `different_values_distinct_placeholders_per_kind_counter`; `substitution_by_byte_offsets_correct_for_multibyte_text` (`"é 😀 Alice Smith"`); `span_not_on_char_boundary_rejected_not_sliced` (→ `NerError::Rejected`, policy applied, never panic); `overlapping_spans_resolved_deterministically`; `span_past_end_of_text_rejected`; `placeholder_text_already_in_input_not_confused` (user text literally containing `[PSEUDO_PERSON_1]` — escaped/rejected per documented rule; assert round-trip does not restore attacker-chosen placeholder to someone else's value: map only contains placeholders the proxy minted, and pre-existing literal placeholders are detected at input and rewritten with a collision-free counter); `map_debug_redacted_and_zeroized_on_drop`; `no_pii_in_logs_or_spans` (canary name absent); `gate_disabled_is_noop_not_degraded`; `non_string_content_parts_processed`; `empty_messages_noop`; `pseudonymize_only_for_commercial_destination`; `map_only_in_ctx_never_in_valkey_or_db` (no `cache`/`db` handle in the type's dependencies — compile-level + test double asserting zero calls).
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): pseudonym map + request-side pseudonymizer`.

### Task 9: `PlaceholderRestorer` — buffered/rolling restore (the SSE core)

**Files:** Create `security/src/restore.rs`; Modify `pipeline/src/stages/dispatch.rs` (apply transform to text deltas in `stream_dispatch`, `restore_text` in non-stream), Test `tests/restore.rs`, `tests/dispatch_restore.rs`

**Interfaces:** `PlaceholderRestorer`, `ChunkTransform::{push,finish}`. Algorithm: scan `buffer + delta`; any complete `[PSEUDO_…_n]` present in the map is replaced; hold back only the **longest suffix that is a proper prefix of some placeholder form** (starts at the last `[` and is ≤ `max_placeholder_len`, and is a prefix of `[PSEUDO_`‑grammar or of a map entry) — everything before it is emitted. A held suffix that can no longer match (grammar violation or exceeds window) is released verbatim. `finish()` flushes: completes/restores if possible, else emits verbatim. Operates on **decoded text deltas** (`StreamChunk.delta`), after SSE/event-stream parsing — so provider framing never splits a placeholder; hold-back adds ≤ one placeholder length of latency. Unmapped `[PSEUDO_*]` tokens (model hallucinated) pass through unchanged (never invent a value). Tool-call/JSON argument deltas are **not** restored (ported verbatim — Python restores `response_text` only); noted in Self-Review. `ctx.response_text` accumulates the **restored** text (what the client got) so SecurityOut/Meter see the same text, as Python.

- [ ] **Step 1: Write tests (property + table):** `restore_text_replaces_all_occurrences_and_unknown_left_alone`; `placeholder_split_at_every_char_boundary` (proptest: for each placeholder and each split into 2–5 pieces, concat of `push…+finish` == `restore_text(whole)`); `split_one_char_at_a_time`; `placeholder_split_across_two_sse_events_end_to_end` (wiremock stream OpenAI + Anthropic fixtures where the placeholder spans events; client-visible text contains the name, never `[PSEUDO`); `bracket_that_is_not_a_placeholder_released_promptly` (`"[1]"`, `"[note"` followed by non-matching char → emitted at the first disambiguating char, not held to end); `held_suffix_bounded_by_max_placeholder_len` (assert buffer ≤ bound under adversarial `[[[[[[…` stream — no unbounded growth/OOM); `stream_ends_mid_placeholder_flushes_verbatim`; `client_disconnect_calls_finish_and_buffer_zeroized`; `multibyte_codepoints_adjacent_to_placeholder`; `adjacent_placeholders_back_to_back`; `restored_value_containing_bracket_not_rescanned` (restored name `"[Bob]"` isn't re-processed); `empty_map_is_passthrough_zero_hold_back` (no latency/alloc when no pseudonymization happened); `restored_text_flows_to_security_out_and_meter` (driver-level); `tool_call_deltas_not_restored_characterization`; `emitted_chunks_preserve_order_and_no_text_loss_or_duplication` (fuzz: output concat == restore(whole input), 10k random splits; **cases examined printed**, 0 = FAIL).
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): rolling-buffer placeholder restore (replaces IdentityDepseudonymizer)`.

### Task 10: `GrpcNerClient` — pooled gRPC to `svc-classification`

**Files:** Create `security/src/{ner_grpc,ner_pool,jwt}.rs`, `build.rs` (tonic-build from `proto/waddleai/classification/v1/classification.proto` — consumed, not copied; CI drift check vs the committed P1 `.desc`); Test `tests/ner_grpc.rs` (in-process tonic test server), `tests/ner_golden.rs`

**Interfaces:** `GrpcNerClient::connect(cfg)`: `tonic` `Channel::balance_channel` over DNS-resolved endpoints of the **headless Service** with periodic re-resolve (`NER_DNS_REFRESH_S`, default ≈ `max_connection_age`/2) so scale-out is picked up; reconnect on server GOAWAY (P1 `max_connection_age` ≈ 300 s); per-call deadline (`NER_DEADLINE_MS`, bounded by request deadline); `JwtProvider` mints/caches a short-lived machine JWT (`classification:analyze`, ≤1 h, refresh before expiry; SPIFFE mTLS alternative behind config); request mapping `NerRequest→AnalyzeRequest{api_version:"v1", language:"en", org_public_id, flag_distinct_id (str(org_id) / "server"), disabled_entity_labels, fail_mode, phase}`; response mapping: `GATE_DISABLED`→`Ok(vec![])`; `OK`→spans via **`start_byte/end_byte`** (validated `end≥start`, within `text.len()`); `degraded=true`→`Err(NerError::Degraded{reason, block_recommended})`; transport errors→`Unavailable`/`Timeout`; `INVALID_ARGUMENT`→`Rejected`; `UNAUTHENTICATED`→refresh token once then `Unavailable`; `UNIMPLEMENTED api_version`→`Rejected` + loud error log (contract skew). **Never** retries non-idempotent-looking errors blindly: retry only `UNAVAILABLE` once on a different endpoint. Circuit breaker (open after N consecutive failures, half-open probe) so a dead service costs microseconds, not deadlines.

- [ ] **Step 1: Write tests:** `maps_request_fields_including_flag_distinct_id_and_fail_mode`; `spans_use_byte_offsets_not_codepoints` (multibyte fixture: `text.as_bytes()[start..end]` is the entity); `gate_disabled_returns_empty_ok_not_degraded`; `degraded_response_maps_to_Degraded_error_with_block_recommended`; `backend_unavailable_status_maps_degraded`; `truncated_flag_surfaced_via_analyze_detailed`; `span_out_of_bounds_rejected`; `dns_refresh_picks_up_new_endpoint_and_drops_removed` (fake resolver 1→3 pods; calls spread across ≥2 endpoints); `reconnects_after_server_max_connection_age_goaway`; `deadline_exceeded_maps_timeout`; `unavailable_retried_once_on_other_endpoint_then_error`; `circuit_opens_after_n_failures_and_fails_fast` (call latency ≪ deadline) and `half_open_probe_recovers`; `jwt_refreshed_before_expiry_and_scope_claim_present`; `unauthenticated_triggers_single_refresh`; `api_version_unimplemented_loud`; `request_text_never_logged_or_in_span_attrs` (canary); `metrics_emitted` (`waddleai_ner_client_requests_total{outcome}`, duration histogram, circuit state gauge — bounded labels); `golden_p1_contract_roundtrip` (spin the real P1 service image in the integration job, or its recorded responses; **assert ≥1 PERSON span and examined count > 0**).
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): pooled gRPC NerClient for svc-classification`.

### Task 11: NER integration policy — fail-open/closed, truncation, wiring into SecurityIn / DispatchStage

**Files:** Modify `security/src/{content_filter,ner}.rs` (P2 tier-3 seam: swap `NoNer`→`GrpcNerClient`), `pipeline/src/stages/dispatch.rs` (call `Pseudonymizer::apply` before the connector call; install `PlaceholderRestorer`), `waddleai-proxy/src/main.rs` (wiring); Create `security/src/fail_policy.rs`; Test `tests/ner_policy.rs`, `tests/dispatch_pseudonymize.rs`

**Interfaces:** `FailPolicy::resolve(org_policy, env) -> FailMode` (org setting → `SECURITY_AUDITOR_FAIL_MODE` → `OPEN`); applies P1 `block_recommended` and **mirrors `content_filter` semantics** (kill-switch `waddleai.disable-auditor-fail-mode-policy`, counter `waddleai_content_filter_fail_total{phase,mode}`, WARN rate-limited per `(org,phase)` 60 s). **Pseudonymize-path rule:** a degraded NER on a *commercial* destination with `FailMode::Closed` → block (`400`/`503` as ported content-filter-guard reason, `ctx.security_degraded=true`); `Open` → proceed **and** mark `ctx.security_degraded=true` + counter (names may egress — visible, never silent). **Truncation:** P1 analyzes only the first 2000 chars; for the pseudonymize path `analyze_detailed` `truncated=true` would leave later text un-pseudonymized, so the proxy **windows** content into ≤2000-codepoint segments (whitespace-boundary, bounded parallelism, entity-straddling-boundary overlap of 64 chars de-duplicated) — **deliberate delta, kill-switch `waddleai.disable-ner-windowing`, owner sign-off required (Self-Review)**. NER runs per request whenever PERSON detection is enabled (no "only if cheaper tiers fail" logic).

- [ ] **Step 1: Write tests:** `open_mode_degraded_proceeds_marks_security_degraded_and_counts`; `closed_mode_degraded_commercial_blocks`; `closed_mode_degraded_local_destination_does_not_block` (local dest skips pseudonymization); `killswitch_on_legacy_silent_open`; `warn_rate_limited_per_org_phase`; `invalid_fail_mode_defaults_open_warns`; `ner_called_on_every_request_when_enabled` (no skip heuristics; call-count == requests); `windowing_covers_text_past_2000_chars_all_entities_pseudonymized` (name at char 2500 is replaced); `window_boundary_straddling_entity_found_once`; `windowing_killswitch_restores_single_call_behavior` ; `windowing_bounded_concurrency_and_total_deadline`; `dispatch_stage_pseudonymizes_before_connector_call_and_upstream_never_sees_name` (wiremock asserts request body has placeholder, no canary name); `response_restored_before_client_nonstream_and_stream`; `ner_down_never_yields_empty_all_clear` (every error path produces degraded policy outcome); `tier3_in_security_in_uses_same_client_instance`; `security_v2_still_unsupported_loud` (unchanged from P2).
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): NER policy, windowing, pseudonymize wired into dispatch`.

### Task 12: Tier-4 `IntentClassifier` (Ollama guard model over HTTP)

**Files:** Create `security/src/intent.rs`; Test `tests/intent.rs` (wiremock Ollama)

**Interfaces:** `IntentClassifier{http, ollama_base_url, default_model}`; `async fn classify(&self, messages, resolved: &ResolvedPolicy) -> IntentResult{action:IntentAction, categories: BTreeMap<String,Verdict>, escalated, degraded}`; port of `shared/security/intent_classifier.py` — `_build_messages`, `_parse_categories`, `_token_to_verdict` (`BLOCK|FLAG|ALLOW`), `_fail_mode_verdict`, `_aggregate_action`, two-pass escalation (last user message, then full context), `POST {base}/api/chat` with `auditor_timeout_ms` (default 5000), default model `shieldgemma:2b`; `Verdict`/`IntentAction` as **int-backed enums** internally (string only at the wire/log boundary, as Python); `SECURITY_CATEGORIES` as-is.

- [ ] **Step 1: Write tests:** `payload_equals_python_golden_for_shieldgemma_and_granite`; `parse_categories_table` (rstest incl. malformed/unparseable → `unparseable` → degraded); `token_to_verdict_case_table`; `fail_mode_open_vs_closed_verdict_on_transport_error`; `timeout_applies_fail_mode_and_marks_degraded`; `two_pass_escalates_on_flag_and_uses_full_context`; `aggregate_action_block_over_flag_over_allow`; `non_200_is_degraded_not_allow`; `no_prompt_text_in_logs_or_spans` (canary); `golden_verdicts_equal_python` (corpus from running Python with a stub guard; rows printed, 0 = FAIL); `live_shieldgemma_flags_known_bad_prompt` (gated `make test-gpu`; Ollama host per project memory; skipped in default run, **counted** in the gpu target).
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): tier-4 intent classifier`.

### Task 13: Tier-4 LLM auditor (`_invoke_llm_auditor`) behind the P2 `Auditor` seam

**Files:** Create `security/src/auditor.rs`; Modify `security/src/content_filter.rs` (wire `Auditor`; remove P2 `Unsupported`-if-enabled), `pipeline/src/stages/security_in.rs`; Test `tests/auditor.rs`

**Interfaces:** `trait Auditor { async fn audit(&self, text:&str, phase:Phase, violations:&[FilterViolation], org_id:Option<i64>) -> AuditorResult }`; `OllamaAuditor: Auditor` — port of `_invoke_llm_auditor` (content_filter.py:997): model-family message builders (`_build_shieldgemma_messages`, `_build_granite_guardian_messages`, standard chat with NER-enriched summary capped 5 pattern / 8 NER violations), `_load_system_prompt(org_id)`, `POST {base}/api/chat`, **10 s total timeout**, response parsing, `AuditorResult{should_block, reason, degraded, error_class}` (`error_class` = enum, never `str(e)`), `waddleai_security_auditor_duration_seconds{outcome=allow|block|degraded}` histogram (same buckets), invoked only in the ported "uncertain zone" (0.3–0.6) via `_should_invoke_auditor` logic; message-construction failure propagates (fail-closed at the call site) — **not** swallowed as degraded, as Python's comment mandates.

- [ ] **Step 1: Write tests:** `shieldgemma_message_shape_matches_python_golden`; `granite_guardian_message_shape`; `standard_model_prompt_includes_capped_violation_summaries`; `uncertain_zone_gate_only_invokes_between_0_3_and_0_6` (rstest boundaries as-is); `timeout_returns_degraded_with_error_class_enum`; `connection_refused_degraded`; `non_200_degraded`; `message_build_error_propagates_not_degraded`; `degraded_applies_fail_mode_policy_via_task_11` (open → allow+counter; closed → block); `histogram_labels_bounded_to_three_outcomes`; `matched_text_sent_to_auditor_but_never_logged` (canary absent from logs/spans); `tier4_disabled_by_flag_skipped_cleanly`; `golden_auditor_decisions_equal_python` (stub guard; rows printed).
- [ ] **Step 2–4: FAIL → implement → PASS.**
- [ ] **Step 5: Commit** `feat(rust): tier-4 LLM auditor`.

### Task 14: Contract gate (commercial + pseudonymize), live tests, coverage, smoke

**Files:** Modify `tests/contract/` (add: openai/anthropic/gemini/bedrock non-stream+stream via provider stubs, pseudonymize round-trip, passthrough dispatch, NER-down open/closed), `Makefile` (`test-contract-rust-commercial`, `test-live-commercial`, `test-live-bedrock`), `.github/workflows/rust-proxy-build.yml`; Create `tests/contract/stubs/` (recorded-response provider stubs: OpenAI SSE, Anthropic events, Gemini SSE, Bedrock event-stream); Test `tests/contract/test_commercial_parity.py`, `tests/integration/test_pseudonymize_e2e.py`

- [ ] **Step 1: Write contract cases** (P0 comparator; each runs against Python **and** Rust with identical stub upstreams): non-stream + stream per provider (event sequence + content modulo IDs; usage/metering rows equal incl. estimate paths); pseudonymize: upstream stub **records** the request → assert no canary name, assert client response restored (nonstream + stream, with stub chunking placeholder across events); NER outage with `fail_mode` open/closed; passthrough: stub upstream asserts it received the user key, managed creds untouched, 401 surfaced unchanged; **assert case count > 0 and both sides responded**.
- [ ] **Step 2: Run** → FAIL on wiring; fix until PASS. Rust-only characterization diffs vs Python are bugs unless pinned as ported-verbatim.
- [ ] **Step 3: Live/gated** (`make test-live-commercial`: real OpenAI/Anthropic/Gemini keys from env, one cheap call each, streaming; `test-live-bedrock`; live P1 + real Presidio person-name round-trip in alpha amd64) — in these targets a skipped test is a FAIL; counts printed.
- [ ] **Step 4: Smoke + telemetry** (log ≥1, metric ≥1, histogram ≥1; `gen_ai.*` attrs on dispatch span incl. commercial providers; NER-client and auditor metrics present) with OTLP sink; counts printed.
- [ ] **Step 5: Verify** `cargo llvm-cov --fail-under-lines 90` per touched crate; `cargo deny check`; `cargo audit`; AWS crate pin/checksum review; **fail-once check** of the placeholder-split test (disable hold-back → test must go red) and the passthrough-leak canary test (log the key → red).
- [ ] **Step 6: Commit** `test(contract): commercial dispatch + pseudonymize round-trip parity`.

---

## Self-Review

- **Spec coverage:** §5.1 DispatchStage reqwest REST + SSE + `_depseudonymize` → T3–T7, T9; Bedrock via the AWS SDK exception → T6; §5.1.1 passthrough dispatch mechanism (forward credential, no managed injection, upstream greenlight/401 surfaced unchanged) → T7 (rollout = P6, flag default OFF); §5.3/§6/§11 pooled gRPC + headless re-resolve + max_connection_age → T10; proxy-held map, spans-only server, rolling-buffer SSE restore → T8–T9; degraded/open/closed → T10–T11; tier-4 IntentClassifier + auditor → T12–T13; replaces P2 `LocalDispatcher`/`NoNer`/`IdentityDepseudonymizer` → T7/T10/T9.
- **Review Focus mapped:** split placeholder T9 (+T2 parser); stream-format differences T2–T6; NER unreachable policy T10–T11; passthrough leakage T7 (+T1 sensitive headers/URL scrub); Bedrock SigV4/retry T6.
- **Deliberate deltas needing owner sign-off:** (1) NER **windowing** past P1's 2000-char truncation for the pseudonymize path (Python silently skips text past 2000 — a real egress gap on commercial destinations; kill-switch provided). (2) Gemini key moved from URL query to `x-goog-api-key` header (wire-equivalent upstream, removes URL-logging leak). (3) Collision-safe handling of user text that already contains `[PSEUDO_*]`. (4) Bedrock SDK retry disabled in favor of the ported retry layer. Everything else is port-verbatim, including estimate/metering gaps (file issues, do not fix here).
- **Known limitations carried (not fixed):** tool-call/JSON-argument deltas are not restored (Python restores `response_text` only); security v2 (`_call_v2`, `PolicyResolver.applies_to`) remains `Unsupported` loud from P2 — P3 implements only the v1-reachable PERSON-detection trigger; Bedrock passthrough unsupported (SigV4 needs a credential set, not one key); non-`en` text rejected by P1 → `Rejected`, treated per policy as not-degraded.
- **Builder-confirm items (record outcome in PR):** pseudonymize trigger source vs `_apply_upstream_filter`/`applies_to`; managed-credential selection semantics; `NerRequest::new` call-site count patched in P2 code; current Python streaming-usage behavior at the branch base (pinned by characterization tests); P1 image/golden availability for the integration job (gated on P1 merge).
- **Placeholders:** none; test names carry assertions, signatures defined once above and used verbatim.
- **Type consistency:** `Credential`/`DispatchError`/`NerError` additive to P2 definitions; `ChunkTransform::{push,finish}` fixed here (P2 left the method set open); `PseudonymMap` shape stable for P6.
- **Proportion:** 14 tasks; signatures + test names/assertions only, no bodies.
