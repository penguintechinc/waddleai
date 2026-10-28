# WaddleAI Rust Data-Plane — P6 Parity, Cutover & Python Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the migration: port every stage and surface P2–P5 left behind (memory/knowledge/security-v2 stages, `/mem0/*`, `/mcp`, `/mcp/admin`, gRPC), ship passthrough as the post-parity fast-follow, build the ingress-level per-tenant cutover with a quota single-writer fence, flip tenants Python→Rust (shadow → flip → observe), and retire `proxy/apps/proxy_server` surface by surface until it is deleted.

**Architecture:** New crates join the `rust/` workspace: `memory` (scratchpad/dedup/summarizer/token-len stores), `knowledge` (retriever), `secv2` (policy resolver/engine/bypass/intent), `mem0-api`, `mcp-server`, `grpc-surface`; the `waddleai-proxy` bin gains routers + Tonic server; a small `impl-selector` Rust service (ingress ext-authz) decides Rust-vs-Python per request. Stages implement P2's `Stage` trait and are slotted into P2's `ProxyPipeline` at the Python order. Nothing here invents behavior: every item is a verbatim port gated by a contract snapshot.

**Tech Stack:** Axum, Tonic + `prost`, `rmcp` (official Rust MCP SDK — reuse before hand-rolling streamable-HTTP), SeaORM, `fred`/`redis` (P0), `regex`, `reqwest`, `async-trait`, `tracing`+OTel, `cargo-llvm-cov`, existing ingress/Gateway API + an ext-authz hook.

**Spec:** `docs/superpowers/specs/2026-10-28-waddleai-rust-dataplane-migration-design.md` §5.1/§5.1.1, §7, §10, §11. **Depends on:** P0 (`Config`, `db`, `cache::keys`, `auth`, `flags`, contract harness), P1 (`svc-classification` proto + `shared/security/ner_filter.py`), P2 (`Stage`, `PipelineContext`, `ProxyPipeline`, `StageError::Unsupported`, `ShadowMirror`, `test-contract-rust`), P3 (commercial dispatch, pseudonymize round-trip, passthrough dispatch, tier-4 auditor), P4 (`svc-plugins`), P5 (fleet controller; `shared/fleet/placement.py` retirement). **P3 and P4 plan files were not present when this was written** — their interfaces are referenced by spec §5/§10, not by plan; see Self-Review.

## Global Constraints

- **Contract-snapshot gate per surface:** no surface is cut over or retired until `make test-contract-rust` passes for it against both implementations with **examined-case counts printed** (zero examined = FAIL), covering filters/pagination/invalid input (testing.md) and streaming event-sequence.
- **One implementation per tenant at a time (routing-level, spec §11):** both fleets stay shared, stateless, multi-tenant; no per-tenant stacks. A tenant's **quota-bearing surfaces** (`/v1/chat/completions`, `/v1/messages`, gRPC `ReportUsage`/`EvaluateRoute` that reserve) flip **together**, atomically, guarded by a Valkey owner fence (Task 12). Non-quota surfaces (`/v1/models`, `/mem0/*`, `/mcp*`, memory gRPC RPCs) may flip independently.
- **Byte-compat shared state:** Valkey cache/quota/scratchpad/dedup/summary key formats, `wa-{key_id}-{secret}`, invalidation signal, mem0/knowledge tables via generated SeaORM entities (never hand-mapped; Rust never owns migrations). During cutover both fleets read/write the same keys.
- **No-regression anti-goal:** PII egress protection, pseudonymize↔restore, billing/quota parity, API contract, per-route rollback. **Bugs ported verbatim** (#347 overdraw, org=0 gRPC) with `# regression: gh-…` pins; not fixed in-migration.
- **Fail loud:** any Python behavior still unported returns `StageError::Unsupported` and the ingress selector **excludes** such tenants from Rust (config-driven capability check, Task 12) — never a silent no-op. Rollback never silently drops a Rust-only feature (Task 15).
- **Flags default OFF** (`waddleai.rust_proxy.{surface}` per tenant, kill-switch convention `…disable-…` N/A: these are migration flags, not core mechanisms). **Passthrough ships OFF**, opt-in per key.
- **Memory/knowledge safety (spec §9.7):** scoped composite keys, provenance headers on unverified memory, trust-tier ordering, quarantined entries never retrieved, PII scanner/filter applied to substituted/stored content exactly as Python.
- **Rust:** no `unsafe`, no `unwrap`/`expect` outside tests, `tracing` only, secrets/PII never in spans/labels/logs, every fn/struct 2–3 line doc, no ASCII dividers, deps pinned, `cargo-deny`.
- **Coverage:** `cargo-llvm-cov` ≥ 90% per new crate; CI asserts minimum collected-test count. amd64 first (owner directive 2026-10-13).

## Review Focus (the 5 failure modes)

1. **Split-brain quota during cutover** — two writers on one tenant's counter, in-flight reservations straddling a flip, ingress and fence disagreeing, partial flip of the quota surface group → Tasks 12, 13, 15.
2. **mem0 / MCP contract drift** — mem0 response shapes, scope/auth semantics, MCP tool schemas + streamable-HTTP session behavior, `/mcp/admin` scope gating → Tasks 8, 9, 11.
3. **Memory/knowledge PII + poisoning (§9.7)** — scratchpad marker substitution leaking cross-session content, summarizer output unscanned, unverified memory injected without provenance, quarantined/low-trust content retrieved, cross-tenant/session key collisions → Tasks 3–6.
4. **Passthrough enablement safety** — non-`wa-` credential accepted without opt-in, credential logged/spanned/cached, passthrough debiting credits or skipping security, upstream 401/403 mangled, org context spoofable → Task 14.
5. **Rollback dropping a Rust-only feature** — flipping a tenant back to Python while passthrough keys (or any Rust-only capability) are active silently fails or degrades to a different auth/billing model → Task 15.

---

## Interface Definitions (new in P6)

```rust
// crates/impl-selector: per-request implementation choice (ingress ext-authz)
pub enum Impl { Python, Rust }
#[async_trait] pub trait ImplSelector { async fn select(&self, surface: Surface, credential_hint: &KeyId) -> Result<Impl, SelectError>; }
pub enum Surface { Models, Mem0, Mcp, GrpcMemory, QuotaGroup /* chat, messages, gRPC reserving RPCs */ }

// crates/cutover (shared by impl-selector + both proxies' QuotaStage fence)
pub struct OwnerFence; // Valkey key `waddleai:impl_owner:{tenant_id}` = {impl, epoch}
impl OwnerFence { pub async fn assert_owner(&self, tenant: TenantId, me: Impl) -> Result<Epoch, FenceError>; // FenceError::NotOwner -> 503 Retry-After
                  pub async fn begin_drain(&self, tenant: TenantId) -> Epoch; pub async fn commit_flip(&self, tenant: TenantId, to: Impl, epoch: Epoch) -> Result<(), FenceError>; }

// crates/capabilities: what each impl can serve (drives exclusion + rollback guard)
pub struct TenantRequirements { pub needs_passthrough: bool, pub needs_security_v2: bool, pub needs_plugins: bool, pub needs_commercial: bool, /* ... */ }
pub fn rust_can_serve(req: &TenantRequirements) -> Result<(), Vec<&'static str>>;   // reasons
pub fn python_can_serve(req: &TenantRequirements) -> Result<(), Vec<&'static str>>; // false for Rust-only features

// stages (all impl P2's Stage; names/flags identical to Python main.py:1296-1330)
// "scratchpad","summarize","knowledge","dedup" slotted between "security_in" and "cache"
```

---

### Task 1: Pipeline slot extension + stage-order contract

**Files:** Modify `rust/crates/pipeline/src/{builder.rs,driver.rs}`, `rust/crates/waddleai-proxy/src/pipeline_build.rs`; Test `rust/crates/pipeline/tests/stage_order.rs`

**Interfaces:** Consumes P2 `Stage`/`ProxyPipeline`. Produces `PipelineBuilder::standard(cfg) -> Vec<Box<dyn Stage>>` returning the **full Python order**: `auth, token_budget, security_in, scratchpad, summarize, knowledge, dedup, cache, routing, quota, dispatch, security_out, meter` (verify against `main.py:1285-1330` + `stages.py` before coding; record any difference).

- [ ] **Step 1: Write tests** `stage_order.rs`: `standard_order_matches_python_snapshot` (golden list exported from Python's built pipeline by a script under `tests/contract/`); `memory_stages_flag_skip_logged_not_run` (`skipped:{name}` in `stage_log`, flag evaluated before blocked short-circuit — P2 T1 semantics); `unported_stage_enabled_returns_unsupported` (a tenant needing `secv2` before Task 7 gets `Unsupported`, never no-op).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** the builder; replace P2's hard-wired list. **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(rust): full-order pipeline builder`.

### Task 2: `memory` crate — byte-compatible stores

**Files:** Create `rust/crates/memory/` (`scratchpad.rs`, `dedup_store.rs`, `token_len_cache.rs`, `provenance.rs`, `config.rs`); Test `tests/stores.rs` (real Valkey + migrated Postgres)

**Interfaces:** `ScratchpadStore::{get,put}(org,user,session,key)`, `DedupStore::{seen,record}`, `TokenLenCache::get_or_count`, `ProxyMemoryConfigResolver::resolve(&UserContext)->ProxyMemoryConfig`, `ProvenanceTag`/`recall` — each mirroring `shared/memory/{scratchpad,dedup_store,token_len_cache,provenance}.py` key formats via P0 `cache::keys`.

- [ ] **Step 1: Write tests:** `scratchpad_key_bytes_equal_python` (golden keys exported from Python); `python_written_scratchpad_readable_by_rust_and_vice_versa` (real Valkey, both directions, **assert keys examined > 0**); `keys_scoped_by_org_user_session_never_collide` (two orgs/users/sessions, same key name → isolated); `dedup_store_ttl_matches_python`; `config_resolver_defaults_match_python_per_key_knobs`; `provenance_tag_roundtrip_and_unverified_header_text_verbatim`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (SeaORM entities for any DB-backed piece; no hand-mapping). **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(rust): memory stores byte-compat with python`.

### Task 3: ScratchpadStage

**Files:** Create `rust/crates/memory/src/stages/scratchpad.rs`; Test `tests/stage_scratchpad.rs`

**Interfaces:** `ScratchpadStage: Stage` name `"scratchpad"`, flag `waddleai.proxy_memory`; marker regex `waddleai://scratchpad/([A-Za-z0-9_.\-]+)` (re-check it is `regex`-crate expressible; it is, no lookaround); async substitution (Rust has no async `re.sub` — resolve all matches first, then replace, preserving order); scans substituted content via P2 `PromptSecurity` + `ContentFilter` exactly as `memory_stages.py:93-189`.

- [ ] **Step 1: Write tests:** `no_session_or_org_is_noop_ctx_unchanged`; `per_key_knob_off_is_noop`; `marker_replaced_with_stored_content_in_all_message_roles_python_does`; `missing_marker_left_as_is_per_python`; `substituted_content_injection_blocks_400` (poisoned scratchpad value → blocked, not injected); `substituted_pii_filtered_like_python`; `marker_for_other_session_not_resolved` (cross-session isolation); `unicode_and_adjacent_markers`; `store_error_is_stage_error_not_silent_skip`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS (`# regression:` pins for any Python quirk ported verbatim).
- [ ] **Step 5: Commit** `feat(rust): scratchpad stage`.

### Task 4: SummarizationStage

**Files:** Create `rust/crates/memory/src/stages/summarize.rs`, `summarizer.rs`; Test `tests/stage_summarize.rs`

**Interfaces:** `ConversationSummarizer::summarize(messages, cfg)->Summary` (calls the configured **local** model via P2 connectors; rolling-summary storage at session scope per §9.7); `SummarizationStage: Stage` name `"summarize"`; trigger thresholds from `ProxyMemoryConfig`, token counts via P2 `tokens` (parity).

- [ ] **Step 1: Write tests:** `below_threshold_noop`; `above_threshold_replaces_old_turns_with_summary_keeps_recent_n`; `summary_stored_session_scoped_not_org` (§9.7 default-narrow); `summary_scanned_before_injection` (summarizer output containing injection/PII → blocked/filtered like Python); `summarizer_model_unavailable_is_visible_degraded_not_silent_truncate` (match Python's chosen behavior, pinned; if Python silently continues, port verbatim + metric + WARN); `token_counts_equal_python_on_shared_corpus`; `no_pii_in_logs_or_spans` (capture subscriber).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(rust): summarization stage`.

### Task 5: DedupStage

**Files:** Create `rust/crates/memory/src/stages/dedup.rs`; Test `tests/stage_dedup.rs`

**Interfaces:** `DedupStage: Stage` name `"dedup"`; content-hash keys via `DedupStore` (HMAC/hash scheme identical to Python — never raw text in Valkey); `TokenLenCache` fallback counter (`memory_stages.py:350`).

- [ ] **Step 1: Write tests:** `repeated_block_replaced_with_reference_second_time`; `first_occurrence_untouched_and_recorded`; `dedup_scoped_per_session_user_org` (identical text in two orgs never deduped across); `short_blocks_below_min_tokens_skipped`; `store_keys_contain_no_raw_text` (scan Valkey keys+values: **count keys examined > 0**, none contain the text); `savings_metric_emitted`; `cache_miss_falls_back_to_counter_same_count`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(rust): dedup stage`.

### Task 6: `knowledge` crate — KnowledgeInjectStage + retriever

**Files:** Create `rust/crates/knowledge/` (`retriever.rs` pgvector via SeaORM/sqlx raw for `<=>`, `stage.rs`, `budget.rs`); Test `tests/knowledge.rs` (real pgvector Postgres)

**Interfaces:** `KnowledgeRetriever::retrieve(query, scopes, sources, k)->Vec<Block>`; `KnowledgeInjectStage: Stage` name `"knowledge"`, flag `KNOWLEDGE_INJECT_FLAG`; per-source flags (`code/docs/uploaded/memory`, `_SOURCE_FLAGS` in `knowledge_stage.py:32-47`) evaluated **per source via P0 `flags::enabled`, failure → OFF**; `_truncate_to_budget` port; MCP-capable + per-key exclusion semantics (`:49-82`).

- [ ] **Step 1: Write tests:** `only_flag_enabled_sources_queried`; `flag_check_error_treated_off`; `per_key_source_exclusion_honored`; `retrieval_filtered_to_caller_org_repo_branch_session` (§9.7 composite key; other tenant's/branch's rows never returned — seed 2 tenants, **assert rows examined > 0**); `quarantined_memories_never_retrieved`; `unverified_blocks_carry_provenance_header_verbatim`; `ranking_relevance_times_trust_order_matches_python_fixture`; `low_trust_expired_ttl_excluded`; `truncate_to_budget_drops_lowest_rank_first_exact_token_count`; `retrieved_text_scanned_for_injection_before_injection` (indirect prompt injection via stored doc → blocked/neutralized per Python; if Python does not scan, port verbatim **and file the gap as an issue** — security_gaps memory); `embedding_via_ollama_http_unavailable_degrades_visibly`; `no_retrieved_text_in_spans_or_labels`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(rust): knowledge retriever + inject stage`.

### Task 7: `secv2` — security_v2 path in SecurityIn/Out

**Files:** Create `rust/crates/secv2/` (`policy_resolver.rs`, `policy_engine.rs`, `bypass.rs`, `intent.rs`); Modify P2/P3 `SecurityInStage`/`SecurityOutStage` to branch on `waddleai.security_v2`; Test `tests/secv2.rs`

**Interfaces:** `PolicyResolver::resolve(user, direction)->ResolvedPolicy`; `SecurityPolicyEngine::evaluate(content, "input"|"output", &ResolvedPolicy, &UserContext)->Verdict`; `BypassResolver::resolve(user)->Option<Bypass{active,mode}>`; `IntentClassifier::classify(...)` reusing P3's tier-4 `Auditor` seam (shieldgemma HTTP). Source: `stages.py:577-690` (`_call_v2`) and the output counterpart (`:1453`), `shared/security/{policy_resolver,policy_engine,intent_classifier,bypass}.py`.

- [ ] **Step 1: Write tests (table-driven vs Python golden verdicts exported from the Python engine):** `v2_flag_off_is_byte_identical_to_v1`; `bypass_skip_mode_skips_engine_and_audits`; `bypass_inactive_ignored`; `engine_block_verdict_status_and_reason_verbatim`; `engine_redact_rewrites_messages`; `intent_classifier_disabled_in_policy_not_called`; `intent_block_action`; `classifier_unreachable_follows_fail_mode_policy_same_as_python` (open/closed per policy, visible counter); `policy_resolution_cached_and_invalidated_via_p0_signal`; `bypass_use_audited_to_security_logs` (identity, timestamp — §9.7 style); `every_python_rule_regex_translates_or_is_reworked_with_equivalence_test` (P2 review-focus 4 carried over).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS; then flip P1/P2 `Unsupported` gate for v2 off.
- [ ] **Step 5: Commit** `feat(rust): security v2 policy path`.

### Task 8: mem0-compatible routes (`/mem0/*`)

**Files:** Create `rust/crates/mem0-api/` (`routes.rs`, `dto.rs`); Modify `waddleai-proxy/src/router.rs`; Test `tests/mem0_routes.rs`, `tests/contract/test_mem0_contract.py` additions

**Interfaces:** `POST /mem0/memories`, `POST /mem0/memories/search`, `GET /mem0/memories`, `DELETE /mem0/memories/{id}`, `DELETE /mem0/memories` (mirrors `mem0_api.py:132,235,323,401,482`); explicit response DTOs per route (security.md output-validation — no raw model serialization); auth via P0 `authenticate_api_key`; tenant-scoped at ORM layer.

- [ ] **Step 1: Write tests** (each also run as contract case vs Python): `create_returns_exact_python_shape`; `search_filters_user_agent_run_limit_threshold`, `search_empty_result_shape`, `search_invalid_filter_400_message_verbatim`; `list_pagination_and_filter_combos`; `delete_one_cross_tenant_404_not_403_leak` (match Python); `delete_all_scoped_to_caller`; `unauthenticated_401_shape`; `memory_write_trust_tier_unverified_by_default` (§9.7); `response_field_set_exact` (regression test failing on any extra field); `request_validation_rejects_oversized_and_wrong_types`; `memory_writes_pii_tokenized_ids_only`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS; extend contract snapshots (capture from Python first).
- [ ] **Step 5: Commit** `feat(rust): mem0-compatible API + contract`.

### Task 9: MCP mount (`/mcp`, `/mcp/admin`)

**Files:** Create `rust/crates/mcp-server/` (`mount.rs`, `user_tools.rs`, `admin_tools.rs`, `auth.rs`); Test `tests/mcp.rs`, `tests/contract/test_mcp_contract.py`

**Interfaces:** Streamable-HTTP MCP via `rmcp` (spike first: confirm it supports the Python mount's session/response semantics, else fall back to a minimal in-crate implementation behind the same trait — decision recorded); `McpMount::authenticate_from_request(&Parts)->UserContext` + `build_tool_context` (mirrors `mcp_mount.py:87-137`); `WaddleAITools` (user) and `AdminTools` (admin) tool sets with **identical tool names/JSON schemas**.

- [ ] **Step 1: Write tests:** `tools_list_schema_equals_python_snapshot` (both `/mcp` and `/mcp/admin`); `unauthenticated_401_json_shape` (`_send_json`); `admin_endpoint_requires_admin_scope_user_token_403`; `user_endpoint_cannot_call_admin_tool`; `tenant_claim_required_and_scoped`; `tool_call_roundtrip_per_user_tool` (table-driven over every tool, real DB); `memory_promote_correct_dispute_tools_follow_9_7_trust_rules`; `streamable_http_sse_and_json_modes`; `session_header_handling_matches_python`; `tool_errors_are_mcp_errors_not_500`; `auth_failure_logged_without_token`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS; verify against a real MCP client (the Claude Code/OpenCode config in the CLI harness sync) in an e2e.
- [ ] **Step 5: Commit** `feat(rust): MCP mount + contract`.

### Task 10: Tonic gRPC surface (`WaddleAIService`)

**Files:** Create `rust/crates/grpc-surface/` (`build.rs` over `proto/waddleai/v1/proxy.proto`, `interceptor.rs`, `api_version.rs`, `service.rs`); Test `tests/grpc.rs`, `tests/contract/test_grpc_contract.py`

**Interfaces:** `WaddleAIService` with `EvaluateRoute`, `EvaluateSecurity`, `StoreTurn`, `GetContext`, `SearchMemories`, `ReportUsage` (proto:130-135); `GrpcAuthInterceptor` (port 50051, JWT + SPIFFE-ready mTLS identity, `grpc_server.py:46`); `ApiVersionRouter` (`:159`, unknown/missing version → `UNIMPLEMENTED`); `CallerIdentity` from credential metadata only, **never request body** (`:227`). The **proto file is reused unchanged** (single source of truth); a descriptor-golden test fails on drift.

- [ ] **Step 1: Write tests:** `proto_descriptor_equals_committed_golden`; `no_credential_returns_unauthenticated_where_python_does`; `bad_credential_aborts_unauthenticated`; `identity_from_metadata_not_body` (body claims org A, token org B → org B); `unknown_api_version_unimplemented_message_verbatim`; `evaluate_route_uses_p2_routing_same_decision_as_python`; `evaluate_security_uses_p2_p7_engine`; `store_turn_get_context_search_memories_share_memory_crate`; `report_usage_reconciles_quota_via_fence` (assert owner fence checked, #347 semantics ported); `org_zero_followup_ported_verbatim` (`# regression: gh-…`, documented); `deadline_and_cancellation_release_reservation_once`; `interceptor_order_auth_before_version_before_handler`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (also contract cases driving both servers with the same proto client). **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(rust): tonic gRPC surface + contract`.

### Task 11: Full-surface contract gate + gap audit

**Files:** Modify `tests/contract/*` (add `RUST_PROXY_URL` parametrization everywhere not yet done), `Makefile` (`test-contract-rust` runs **all** surfaces), CI workflow; Test `tests/contract/test_surface_inventory.py`

- [ ] **Step 1: Write tests:** `inventory_every_python_route_has_a_snapshot` (enumerate Quart `url_map` + gRPC methods + MCP tools; **FAIL on any route without a snapshot**; print routes examined); `inventory_every_rust_route_exists_in_python` (no accidental new surface); `contract_run_prints_cases_examined_per_surface`; `streaming_cases_event_sequence_modulo_ids`; `commercial_and_passthrough_cases_use_recorded_upstream`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Capture missing snapshots from live Python**, fill gaps (the P0 audit list). **Step 4: Run** → PASS against both fleets in kind/alpha.
- [ ] **Step 5: Commit** `test(contract): all-surface parity gate`.

### Task 12: Ingress-level cutover — selector, owner fence, capability exclusion

**Files:** Create `rust/crates/{impl-selector,cutover,capabilities}/`, Helm `templates/ingress-cutover.yaml` (ext-authz wiring + header routing), Python-side minimal fence check `proxy/apps/proxy_server/quota_hooks.py` (flag-gated, additive — the **only** Python logic change in P6); Test `tests/selector.rs`, `tests/fence.rs`, `tests/cutover_e2e.rs` (kind)

**Interfaces:** see Interface Definitions. Flow: ingress → ext-authz `impl-selector` (O(1) `key_id → org/tenant` lookup via P0 `cache::keys`, **no secret verification, no PII**; evaluates `waddleai.rust_proxy.{surface}` per tenant via P0 flags with last-known cache, fail-safe → **Python**) → sets `x-waddleai-impl` → ingress routes to the Rust or Python Service. Quota-bearing surfaces consult `OwnerFence` at `QuotaStage` in **both** proxies; a request reaching the non-owner gets `503 Retry-After` (never reserves).

- [ ] **Step 1: Write tests:** `selector_flag_off_routes_python`; `selector_flag_unreachable_uses_last_known_then_python`; `selector_unknown_key_routes_python_never_errors` (Python produces the canonical 401); `selector_adds_no_pii_and_no_secret_verification`; `selector_p99_budget_under_2ms_cached` (bench threshold); `quota_surface_group_flips_atomically` (chat+messages+ReportUsage resolve the same Impl for a tenant); `non_quota_surface_flips_independently`; `capability_exclusion_routes_python_when_tenant_needs_unported_feature` (reasons listed); `fence_not_owner_returns_503_no_reserve` (**assert Valkey quota counters unchanged**); `fence_epoch_prevents_stale_commit`; `drain_waits_for_inflight_reservations_then_flips` (in-flight reservation started on Python before drain settles on Python, never on Rust — uses `quota_sweeper`/lease TTL as the backstop); `two_writers_never_both_succeed_property_test` (concurrent reserve storm during a flip, tenant counter == sum of admitted reservations); `kind_e2e_flip_and_flip_back_preserves_counters`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (check first whether the existing ingress/Gateway supports ext-authz header routing; use it — do not build a router). **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(cutover): ingress impl-selector + owner fence + capability exclusion`.

### Task 13: Shadow-compare extension + soak criteria

**Files:** Modify `rust/crates/waddleai-proxy/src/shadow.rs` (P2 T13), add `rust/crates/waddleai-proxy/src/shadow_report.rs`, `scripts/cutover-gate.sh` + `make cutover-gate`; Test `tests/shadow_p6.rs`

**Interfaces:** Extends `ShadowMirror` to the new stages/surfaces; **read-only surfaces shadowed live** (`/v1/models`, mem0 `GET`/`search`, MCP `tools/list`); **mutating surfaces (mem0 write/delete, StoreTurn, ReportUsage, chat quota)** are shadowed only against a **scratch tenant/transaction-rolled-back DB + stub upstream**, never against live state. `cutover-gate.sh` reads shadow diff rate + contract status and emits GO/NO-GO with counts.

- [ ] **Step 1: Write tests:** `shadow_mutating_route_never_touches_live_tenant_state` (counters/rows unchanged, **examined > 0**); `shadow_diff_rate_reported_per_surface_with_sample_count`; `gate_no_go_when_samples_below_minimum` (zero/low denominator = FAIL); `gate_no_go_on_any_unexplained_diff`; `shadow_timeout_never_affects_live_response`; `memory_stage_shadow_uses_copy_of_session_not_live_keys`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(cutover): p6 shadow extension + GO/NO-GO gate`.

### Task 14: Passthrough fast-follow (ship end-to-end)

**Files:** Modify `services/management/app/api/v1/` (enable/disable endpoint + scope), Alembic migration **only if** P2 T3's recorded finding says no per-key column exists (Python-side, `api_keys`/`virtual_keys` + org default), `webui` toggle + docs (`docs/passthrough.md`, API docs, release notes), `rust/crates/connectors` (consume P3 passthrough dispatch), Helm values; Test `rust/crates/waddleai-proxy/tests/passthrough_e2e.rs`, `tests/contract/test_passthrough_contract.py`, `tests/e2e/tests/passthrough.spec.js`

**Interfaces:** Consumes P2 `detect_auth_mode`/`PassthroughPolicy` + P3 passthrough dispatch. Adds the **enablement surface**: `PUT /api/v1/keys/{public_id}/passthrough {enabled}` (scope `keys:admin`), org default, audit row to `security_logs` (admin identity, timestamp, key `public_id` UUID — never the key); flag `waddleai.passthrough` (default OFF, license-gated tier check per `integrating-license-server`). Request shape: `wa-` key + provider key in the header name recorded by P2 T3. Quota runs rate-limit-only; Security In/Out + pseudonymize round-trip + metering always run.

- [ ] **Step 1: Write tests:** `non_wa_credential_rejected_401_when_not_enabled`; `enabled_key_forwards_provider_key_not_managed_creds`; `provider_key_never_in_logs_spans_metrics_cache_keys_or_error_bodies` (capture all sinks; **assert sinks examined > 0**); `passthrough_charges_zero_credits_but_rate_limit_enforced` (counters: credits unchanged, rate bucket decremented); `security_in_out_and_pseudonymize_roundtrip_apply_in_passthrough` (PII never reaches recorded upstream); `metering_row_attributed_to_wa_key_org`; `upstream_401_403_surfaced_unchanged_status_and_body`; `org_context_from_wa_key_only_header_org_spoof_ignored`; `revoked_wa_key_stops_passthrough_immediately_via_invalidation`; `enable_requires_admin_scope_and_is_audited`; `enable_blocked_without_license_tier`; `flag_off_disables_even_enabled_keys`; `virtual_mode_behavior_unchanged_regression`; playwright `toggle_passthrough_in_webui_and_roundtrip` (artifacts `/tmp/playwright-waddleai`, cleaned in `if: always()`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** enablement, docs (tier, abuse model per spec §10.1, rollback caveat from Task 15), `seed-mock-data` entries. **Step 4: Run** → PASS; live recorded-upstream test for each commercial provider.
- [ ] **Step 5: Commit** `feat(passthrough): ship opt-in passthrough end-to-end`.

### Task 15: Rollback safety for Rust-only features

**Files:** Modify `rust/crates/{capabilities,impl-selector}`, `services/management/app/api/v1/` (flip-back preflight endpoint), `scripts/cutover-flip.sh` + `make cutover-flip TENANT=… SURFACE=… TO=python|rust`; Test `tests/rollback.rs`, `tests/e2e_rollback.sh`

**Interfaces:** `python_can_serve(&TenantRequirements)` is `Err` when the tenant has passthrough-enabled keys (or any other Rust-only capability: commercial dispatch if Python path differs, plugins `svc-plugins` scopes, etc. — enumerate from P3/P4 deliverables). `cutover-flip.sh` runs the preflight and **refuses** flip-back with the exact blocking reasons unless the operator first disables the feature (which is itself audited); emergency override requires a recorded `--accept-feature-loss` with reason, audited.

- [ ] **Step 1: Write tests:** `flip_back_blocked_when_passthrough_keys_active_lists_keys_by_uuid`; `flip_back_allowed_after_passthrough_disabled`; `emergency_override_audited_and_python_returns_explicit_4xx_for_passthrough_not_silent_virtual_fallback` (Python must reject a non-`wa-` credential, never bill the user's org); `rust_only_capability_registry_complete` (every Rust-only feature in P3/P4/P6 appears; adding one without registering FAILS — scan of `#[rust_only]` markers); `selector_never_routes_rust_only_tenant_to_python_automatically_on_flag_error` (flag outage keeps last-known Rust for such tenants, not Python); `rollback_preserves_counters_via_fence`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(cutover): rollback preflight for rust-only features`.

### Task 16: Staged cutover execution (runbook as scripts + gates)

**Files:** Create `scripts/cutover-gate.sh`, `scripts/cutover-flip.sh` (Task 13/15), `docs/runbooks/rust-cutover.md` (human doc — sync per house rule), Helm values per tier; Test `tests/e2e_cutover_sequence.sh`

Per-surface sequence (each step needs `make cutover-gate` GO with sample counts, then **observe** ≥ the soak window: alpha → beta (dal2 is offline as of 2026-08-21 — fail-closed until back, per devops.md) → gamma (upgrade-in-place) before any prod flip):

| Step | Surface / group | Tenant order | Notes |
|---|---|---|---|
| 0 | Prep: selector + fence deployed, flags OFF, Python unchanged | — | fence asserts no-op while all tenants Python-owned |
| 1 | `/v1/models` | internal `system` → `default` → all | read-only, pure |
| 2 | `/mem0/*` | internal → small → all | stateful but byte-compat shared tables |
| 3 | `/mcp`, `/mcp/admin` | internal first (OpenCode harness sync users) → all | |
| 4 | gRPC memory RPCs (`StoreTurn`,`GetContext`,`SearchMemories`,`EvaluateSecurity`) | internal → all | |
| 5 | **Quota group** (`/v1/chat/completions`, `/v1/messages`, gRPC `EvaluateRoute`/`ReportUsage`) local-model tenants | internal → tiny tenants → default → largest last | atomic per tenant, drain + fence |
| 6 | Quota group, commercial-provider tenants (P3) | same | after step-5 soak |
| 7 | Passthrough enablement offered (Task 14), flag default stays OFF; enable per customer on request | — | after step-6 soak |

- [ ] **Step 1: Write tests:** `sequence_script_refuses_out_of_order_step` (step 5 before 1–4 soak recorded → refuse); `flip_requires_gate_go_with_nonzero_samples`; `flip_records_who_when_epoch_in_audit_log`; `observe_window_alerts_on_error_rate_latency_quota_drift_vs_python_baseline` (alert rule tests: RED metrics, **quota drift = Rust-vs-Python reconcile delta**); `flip_and_flip_back_leaves_tenant_counter_equal_to_admitted_sum`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** scripts + Helm + dashboards/alerts (parity metric names). **Step 4: Run** → PASS; execute steps 0–6 in alpha, record evidence.
- [ ] **Step 5: Commit** `feat(cutover): sequenced cutover scripts, alerts, runbook`.

### Task 17: Per-route retirement + final removal of `proxy/apps/proxy_server`

**Files:** Delete (separate PR per row, **only after the surface is 100% Rust for one clean gamma soak + one release of legacy-bypass**); Modify `Makefile`, `.coveragerc`, Helm (remove Python proxy Deployment last), docs, `CLAUDE.md`-adjacent `docs/APP_STANDARDS.md`; Test `tests/test_python_proxy_retired.py`

| Order | Retire | Prerequisite (moves/keeps) |
|---|---|---|
| 1 | `/v1/models` handler | contract + shadow green |
| 2 | `mem0_api.py` | — |
| 3 | `mcp_mount.py` (+ `mcp_server` Python pieces only the proxy used) | — |
| 4 | `grpc_server.py` (+ `grpc_proto` stubs for the Python server) | proto stays in `proto/` — Rust reuses |
| 5 | `pipeline/memory_stages.py`, `knowledge_stage.py`, v2 branch in `stages.py` | `shared/memory/*`, `shared/security/*` kept only if still imported by management/svc-classification — grep-gate decides |
| 6 | `/v1/chat/completions`, `/v1/messages`, `stages.py`, `streaming.py`, `quota_hooks.py` fence shim, `auth_cache.py`, `feature_flag_cache.py`, `quota_sweeper.py` (sweeper ported to Rust first) | — |
| 7 | `proxy/apps/proxy_server/` directory, Python proxy Docker/Helm, fence + selector Python paths (selector then routes everything to Rust or is removed) | — |

**Must relocate before deletion (found while planning):** (a) `shared/security/ner_filter.py` is imported by P1's `svc-classification` ("retired at P6, not copied") → **move into `services/classification/app/`** with its tests; (b) `LUA_RESERVE`/`LUA_RECONCILE` in `shared/quota/lease.py` are P2's single source of truth, exported at build → **make checked-in `.lua` files the source**, update P2's drift test to compare against them, keep management's use of `shared/quota` working; (c) `shared/fleet/placement.py` per P5's table; (d) anything `services/management`, `services/classification` or tests import from `proxy.apps.proxy_server` or the shared modules above.

- [ ] **Step 1: Write tests:** `no_imports_of_retired_modules_remain` (AST grep over repo; **files scanned > 0**); `no_python_route_remains_for_retired_surface`; `lua_source_files_are_authoritative_and_drift_test_green`; `svc_classification_imports_ner_filter_from_own_package`; `helm_template_has_no_python_proxy_deployment_after_row_7`; `management_unit_suite_green_without_proxy_package`; `contract_suite_green_rust_only` (Python implementation no longer a comparison target — **freeze last Python snapshots as the permanent golden set**).
- [ ] **Step 2: Run** → FAIL. **Step 3: Relocate (a)–(d) first**, then delete per row (one PR each; delete branch + worktree after merge). **Step 4: Run** full gates (`make lint`, `make test-unit`, `make test-contract`, cargo gates, smoke + telemetry counts) → PASS.
- [ ] **Step 5: Commit** `refactor(proxy): retire python proxy surface-by-surface` (per PR); final `chore(proxy): remove proxy/apps/proxy_server`.

### Task 18: Final gates, docs, telemetry, release readiness

**Files:** Modify `docs/` (architecture, API, passthrough, runbook, upgrade notes, screenshots if webui changed — full `docs/screenshots/` set recaptured), `.coveragerc`/CI (drop proxy_server from Python coverage), `rust-proxy-build.yml`; Test `tests/smoke_p6.sh`

- [ ] **Step 1: Write tests:** `smoke_all_surfaces_rust_only` (health, models, chat stream, mem0, MCP, gRPC; telemetry sink asserts logs ≥1, metrics ≥1, histograms ≥1, spans ≥1, counts printed); `llvm_cov_gate_each_new_crate_90`; `fresh_install_and_rollback_helm_with_rust_only_proxy` (memory: fresh install not CI-tested — run manually too); `no_hand_rolled_logging` (service scan, files > 0).
- [ ] **Step 2–4:** Run → fix → PASS; arm64 deferred per owner directive (note in release notes). **Step 5: Commit** `docs+test(rust): p6 final gates and docs`.

---

## Self-Review

- **Spec coverage:** §5.1 deferred stages T1–T7; §2 surfaces `/mem0`, `/mcp*`, gRPC T8–T10, contract gate T11; §7 strangler/ingress cutover/shadow T12–T13, T16; §5.1.1/§10.2 passthrough fast-follow T14; rollback anti-goal T15; Python retirement T17; failure modes 1–5 map to Tasks 12/13/15, 8/9/11, 3–6, 14, 15.
- **Placeholders:** none; "spike/verify" items (`rmcp` fit, ingress ext-authz support, existing passthrough column, stage order vs `main.py`) are explicit steps with a recorded outcome.
- **Underspecified cross-phase dependencies (need owner/phase-author resolution):**
  1. **Ingress cannot see the tenant** — spec says cutover is "ingress-level keyed per tenant," but the tenant is only known after key lookup; P6 adds an `impl-selector` ext-authz service (Task 12). Not in any prior plan; confirm the ingress controller supports ext-authz header routing.
  2. **Quota single-writer is only a routing convention** — nothing in P0/P2 enforces it; Task 12 adds a Valkey owner fence checked by **both** proxies, which requires a small Python-side change (contradicts "Python unchanged"); needs sign-off.
  3. **P3/P4 plan files were absent** — P3's passthrough dispatch signature, pseudonymize hooks, tier-4 `Auditor` trait, and P4's plugin-scope requirements are assumed from spec/P2; Rust-only capability registry (Task 15) must be completed against the real P3/P4 interfaces.
  4. **P2 pipeline order omits the memory/knowledge slots** (and the Python order has them between `security_in` and `cache`); P2's builder is hard-wired — Task 1 reworks it.
  5. **Lua/NER single-source ownership** — P1 and P2 both treat Python files as source of truth "until P6"; relocation is P6 work (Task 17) but neither plan lists it.
  6. **Shadow-compare of stateful surfaces** (mem0 writes, StoreTurn, memory stages) — P2's shadow is read/local-only; P6 constrains to scratch tenants (Task 13); acceptable coverage for mutating paths is an open call.
  7. **Whether Python's knowledge/memory paths scan retrieved/summarized content for injection** (spec §3.6/§9.7 risk) — verify; if not, port verbatim and file an issue, don't silently add.
  8. **Beta/gamma availability** — dal2 offline (testing.md, 2026-08-21) blocks the beta soak step; the sequence fails closed until back.
- **Type consistency:** `Impl`, `Surface`, `OwnerFence`, `TenantRequirements`, `rust_can_serve/python_can_serve` are used identically across T12/T15/T16; stage names match Python (`scratchpad, summarize, knowledge, dedup`).
