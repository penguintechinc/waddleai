# External Webhooks — W3 Rust Proxy Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the Rust proxy `ExternalDecision` hook at `pre_dispatch` / `post_dispatch` (dispatching `external_http` registry entries to `svc-webhooks`) and emit proxy events (`request.completed`, `request.blocked`, `security.violation`, `pii.detected`, `quota.exceeded`) via Valkey `XADD`.

**Architecture:** The P4 `PluginRegistry` already yields ordered, scope-matched chains per hook. W3 teaches it `kind`: `wasm` entries go to `svc-plugins` (P4 `GrpcPluginHost`), `external_http` entries go to `svc-webhooks` `Decide` via a new `ExternalDecisionHost`. A pre-dispatch Decide is *started* at the end of SecurityIn (so it overlaps Cache/Routing/Quota) and awaited at the hook barrier; composition (W2 `decision-core`) merges it with the built-in verdict. Async events are a fire-and-forget `XADD` that never blocks or fails a request.

**Tech Stack:** Rust (P2 `pipeline`, P4 `plugin-registry`/`plugin-host-client`, W2 `decision-core`, W0 `webhooks-proto`), Tonic, `fred` (P0), `tokio`.

**Spec:** `docs/superpowers/specs/2026-11-05-external-webhooks-design.md` (Decision protocol, Event catalog, Delivery). Depends on P2 + P4 Task 10 (registry), W1, W2.

## Global Constraints

- **Third party sees exactly what the LLM sees, both directions:** outbound prompt/tool args sent **after SecurityIn redaction + pseudonymization**; LLM responses (`post_dispatch`, completed events) sent **before `_depseudonymize`** (pseudonyms intact, never restored real values); a decision's `modified_content` is treated as pseudonymized text and passes through the normal depseudonymize + SecurityOut path. Raw unredacted content never sent, any tier.
- Content (`data.content`) only when the endpoint has `content_opt_in`; default = metadata + UUIDs. No PII in events (`pii.detected` carries entity **types/counts only**).
- Hook deadline = endpoint `timeout_ms` capped at **500 ms** (proxy). Timeout/error ⇒ `fail_mode`; every fail-open emits `decision_degraded_total` + `security.decision_degraded` event and sets `ctx.security_degraded` only for fail-closed-eligible hooks per P4 semantics.
- MODIFY honored only when `can_modify` and only at proxy hooks (`pre_dispatch`, `post_dispatch`); first DENY short-circuits; ordering = registry `order`.
- **Redaction and pseudonymization run in every `decision_composition` mode** (1 built-in / 2 external-only / 3 both deny-wins); mode 3 = concurrent evaluation, most restrictive wins; deny reason records `builtin|external|both`.
- **Streaming (v1):** when a decision endpoint is bound to `post_dispatch` for the route, the route runs **non-streaming** (buffered final output) and replays it to the client as synthetic SSE (P2 `stream_iter`); documented, surfaced in the UI (W5).
- Flags default OFF: `waddleai.webhooks-decisions`, `waddleai.webhooks-events`, kill-switch `waddleai.disable-external-decisions`. Zero registry match ⇒ zero RPCs, zero allocation (P4 `has_any` fast path).
- Events: `XADD waddleai.events MAXLEN ~ 100000`; `XADD` failure is logged + counted, never a request failure. Type prefix `io.waddleai.`.
- No shim between phases: the pipeline `Stage` trait (P2) is unchanged; stages only mutate `PipelineContext`.
- Coverage ≥ 90% per crate; telemetry parity names unchanged, new metrics `decision_latency` (histogram), `decision_degraded_total`, `proxy_events_emitted_total{type}`.

## Review Focus

1. **Real PERSON/EMAIL fixture never in any webhook body** in any mode (prompt out, response out, `modified_content` round-trip) → Tasks 4, 5, 8.
2. **External `modified_content` that smuggles a pseudonym placeholder or a real value** → passes depseudonymize + SecurityOut, never bypasses it → Task 5.
3. **Decision RPC slower than hook budget** → bounded at 500 ms; request never hangs → Task 3, 4.
4. **Mode 3 concurrency leaves a dangling Decide when built-in blocks** (leak/cancel) → Task 4.
5. **Streaming client when decision bound** gets a valid SSE stream, not a 500 → Task 6.

---

### Task 1: Registry `kind` support and chain split

**Files:** Modify `rust/crates/plugin-registry/src/{lib.rs,snapshot.rs,matcher.rs,loader.rs}`; Test `tests/matcher.rs` (extend)

**Interfaces:**
```rust
pub enum PluginKind { Wasm = 1, ExternalHttp = 2 }
// PluginRow gains: kind: PluginKind, webhook_endpoint_id: Option<i64>
pub enum ChainRef { Wasm(PluginRef), External(ExternalRef) }
pub struct ExternalRef { pub endpoint_public_id: Uuid, pub can_modify: bool, pub content_opt_in: bool, pub timeout_ms: u32, pub fail_mode: FailureMode }
impl PluginRegistry { pub fn resolve_split(&self, scope: &RequestScope, hook: Hook) -> (Arc<[PluginRef]>, Arc<[ExternalRef]>); pub fn has_any_external(&self, hook: Hook) -> bool; }
```
Existing `resolve`/`has_any` semantics for wasm entries unchanged (P4 contract).

- [ ] **Step 1: Write tests**: `kind2_rows_never_returned_by_wasm_resolve`; `resolve_split_orders_each_kind_by_position_then_id`; `other_org_external_not_returned`; `has_any_external_o1_false_when_none`; `external_row_with_null_digest_loads`.
- [ ] **Step 2: Run** `cargo test -p plugin-registry` → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS (existing P4 tests green).
- [ ] **Step 5: Commit** `feat(rust): registry supports external_http kind`.

### Task 2: `PipelineContext` extensions

**Files:** Modify `rust/crates/pipeline/src/context.rs`; Test `tests/context.rs`

**Interfaces:** `ctx.extensions` typed entries (no new public fields on the P2 struct): `ExternalDecisionState { pending: Option<JoinHandle<DecideOutcome>>, builtin: Option<Verdict>, composition: Composition, outcome: Option<Composed> }`, `ProxyEventDraft { types: SmallVec<EventType>, pii_counts: BTreeMap<EntityKind,u32> }`. `composition` resolved once per request from the org cache.

- [ ] **Step 1: Write tests**: `extensions_roundtrip_typed_state`; `dropping_ctx_aborts_pending_decide`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(pipeline): external decision state in extensions`.

### Task 3: `ExternalDecisionHost` gRPC client

**Files:** Create `rust/crates/plugin-host-client/src/external.rs`; Test `tests/external.rs`

**Interfaces:**
```rust
#[async_trait] pub trait ExternalDecider: Send + Sync {
    async fn decide(&self, req: ExternalDecideRequest) -> Result<DecideOutcome, InvokeError>;
}
pub struct ExternalDecideRequest { pub hook: Hook, pub org_id: Uuid, pub event_json: String, pub content: Option<String>, pub deadline: Duration /* ≤ 500ms */ }
pub struct GrpcExternalDecider { /* pooled Channel::balance_channel over headless svc-webhooks, breaker, metrics */ }
```
Same pooled-channel + breaker + outlier-ejection pattern as P4 `GrpcPluginHost` (reuse its `breaker.rs`; do not duplicate).

- [ ] **Step 1: Write tests**: `deadline_capped_at_500ms`; `breaker_opens_after_failures_returns_error_fast`; `per_call_grpc_timeout_equals_remaining_budget`; `no_new_connection_per_call`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(proxy): external decider gRPC client`.

### Task 4: `ExternalDecisionStage` — pre_dispatch with early start

**Files:** Create `rust/crates/plugin-host-client/src/external_stage.rs`; Modify `rust/crates/waddleai-proxy/src/pipeline_build.rs`; Test `tests/external_stage.rs`, `tests/wiring.rs`

**Interfaces:** Two stages sharing `ExternalDecisionState`:
- `ExternalDecisionStart` (name `"ext_decision_start"`, placed immediately after `security_in`): if flags allow, composition ≠ 1, and `registry.has_any_external(PreDispatch)` ⇒ builds the event from the **post-SecurityIn pseudonymized** prompt (content only if endpoint opted in) and `tokio::spawn`s `decide`.
- `ExternalDecisionPreDispatch` (name `"ext_decision_pre_dispatch"`, placed after `plugins_pre_dispatch`, immediately before `dispatch`): awaits the handle, applies `compose(...)`; DENY ⇒ `ctx.blocked=true`, `status_code=403`, `block_reason="external:<reason>"` (or `builtin`/`both`); MODIFY ⇒ replace `ctx.messages/prompt_text`, re-run P2 body invariants and **re-estimate tokens** (reservation reconciled), then the modified text is treated as pseudonymized.
- Both flag-gated `waddleai.webhooks-decisions`; kill-switch skips both and records degraded.

- [ ] **Step 1: Write tests**: `no_external_binding_zero_rpc_and_ctx_unchanged`; `decide_overlaps_cache_routing_quota` (mock host latency 80 ms, total added latency < 20 ms); `deny_short_circuits_with_external_reason`; `mode3_truth_table_end_to_end` (all four combos); `mode3_builtin_block_aborts_pending_decide`; `mode2_redaction_still_runs_and_builtin_verdict_ignored`; `modify_replaces_prompt_and_recounts_tokens`; `timeout_failopen_proceeds_with_degraded_signal`; `timeout_failclosed_denies`; `prompt_sent_is_pseudonymized_known_person_email_fixture_absent`; `content_omitted_when_not_opted_in`.
- [ ] **Step 2: Run** `cargo test -p plugin-host-client external_stage` → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS; P2 `make test-contract` green.
- [ ] **Step 5: Commit** `feat(proxy): ExternalDecision pre_dispatch hook`.

### Task 5: `ExternalDecisionPostDispatch` — before `_depseudonymize` and SecurityOut

**Files:** Create `rust/crates/plugin-host-client/src/external_post.rs`; Modify `pipeline_build.rs`; Test `tests/external_post.rs`

**Interfaces:** Stage `"ext_decision_post_dispatch"` placed after `plugins_post_dispatch`, before `security_out`; operates on `ctx.response_text` **while it still holds pseudonyms** (P3 moves depseudonymize to the egress transform that runs after SecurityOut/at final write — if P3 depseudonymizes earlier in `dispatch`, this task adds a `ctx.defer_depseudonymize` seam so restore runs after this stage; record the P3 interface actually found). Non-streaming only. DENY ⇒ blocked, 403. MODIFY ⇒ replaces `ctx.response_text`, then normal depseudonymize + SecurityOut applies.

- [ ] **Step 1: Write tests**: `response_sent_to_decider_still_has_pseudonyms_never_real_values`; `modify_content_goes_through_depseudonymize_and_security_out`; `modify_with_real_email_is_redacted_by_security_out`; `deny_blocks_response_403`; `timeout_failopen_returns_original_with_degraded`; `skipped_for_streaming_route_without_binding`; `mode3_both_must_allow`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(proxy): ExternalDecision post_dispatch hook`.

### Task 6: Streaming downgrade when a decision endpoint is bound

**Files:** Modify `rust/crates/waddleai-proxy/src/routes/{chat.rs,messages.rs}`, `rust/crates/connectors` dispatch selector; Test `tests/stream_downgrade.rs`

**Interfaces:** `fn route_must_buffer(registry: &PluginRegistry, scope: &RequestScope) -> bool` (true iff an external `post_dispatch` entry matches and flags are on). When true the request's `stream` is executed non-streaming internally and replayed as synthetic SSE (`ctx.stream_iter`, P2); response framing (event sequence, `[DONE]`) identical to a real stream.

- [ ] **Step 1: Write tests**: `bound_post_dispatch_forces_internal_nonstream_and_sse_replay`; `sse_replay_event_sequence_matches_contract_snapshot_shape`; `unbound_route_streams_normally`; `flag_off_streams_normally`; `denied_stream_request_returns_json_error_not_broken_sse`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(proxy): buffer route when post_dispatch decision bound`.

### Task 7: Proxy events via `XADD`

**Files:** Create `rust/crates/events-emit/{Cargo.toml,src/lib.rs}`; Modify `pipeline_build.rs` (emit in `MeterStage` tail and on block paths); Test `tests/emit.rs`

**Interfaces:**
```rust
pub trait EventSink: Send + Sync { fn emit(&self, ev: ProxyEvent); } // non-blocking, bounded channel, drops+counts when full
pub enum ProxyEventType { RequestCompleted, RequestBlocked, SecurityViolation, PiiDetected, QuotaExceeded }
pub struct ProxyEvent { pub event_id: Uuid, pub ty: ProxyEventType, pub org_id: Uuid, pub team_id: Option<Uuid>, pub subject: Option<Uuid>, pub actor: Option<Uuid>, pub data: serde_json::Value, pub content: Option<serde_json::Value> }
pub struct ValkeyStreamSink { /* XADD waddleai.events MAXLEN ~ 100000, background task */ }
```
`pii.detected` data = `{entity_types: {PERSON: n, EMAIL: m}}` only.

- [ ] **Step 1: Write tests**: `request_completed_emitted_once_per_request`; `blocked_request_emits_request_blocked_and_security_violation_when_applicable`; `quota_exceeded_event`; `pii_detected_contains_types_and_counts_only_no_values`; `xadd_failure_never_fails_request_and_increments_counter`; `valkey_down_channel_full_drops_and_counts`; `flag_off_emits_nothing`; `event_has_no_username_email`; `completed_event_response_content_is_pre_depseudonymize_and_only_when_opted_in`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(proxy): emit proxy events to waddleai.events stream`.

### Task 8: Contract, latency, tenant-isolation, flag-on E2E

**Files:** Create `tests/e2e/test_proxy_external_hooks_e2e.py`, `rust/crates/waddleai-proxy/tests/external_isolation.rs`; Modify `tests/contract/` (add flag-on snapshots)

- [ ] **Step 1: Write tests**: `contract_snapshots_unchanged_with_flags_off`; `deny_endpoint_blocks_chat_completion_with_403_shape`; `modify_endpoint_rewrites_prompt_upstream_sees_modified_pseudonymized_text`; `cross_org_endpoint_never_consulted`; `added_p99_with_no_binding_under_1ms` (benchmark gate); `known_person_email_fixture_absent_from_all_receiver_bodies_both_directions`; `telemetry_histogram_decision_latency_received_count_printed`.
- [ ] **Step 2: Run** → FAIL. **Step 3:** wire chart values (proxy → svc-webhooks mTLS + machine JWT). **Step 4:** PASS with printed counts on alpha (fresh cluster).
- [ ] **Step 5: Commit** `test(proxy): external hooks contract and e2e`.

## Self-Review

- **Spec coverage:** hook positions + `external_http` routing → T1, T3–T5; content visibility rules → T4, T5, T8; composition/mode 3 concurrency → T4; streaming rule → T6; proxy events → T7; kill-switch/degraded → T4, T5.
- **Builder choices (within spec):** early-start/await-at-barrier is how "run concurrently, latency = slower of the two" is realized; P3's actual depseudonymize seam must be confirmed in Task 5 and recorded.
- **Type consistency:** `Hook`, `Verdict`, `Composition`, `DecideOutcome` from W0/W2; `Stage` trait and `ctx.extensions` from P2; `PluginRegistry` from P4.
