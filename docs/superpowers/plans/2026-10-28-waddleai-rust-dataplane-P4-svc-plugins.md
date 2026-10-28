# WaddleAI Rust Data-Plane — P4 `svc-plugins` (WASM Plugin Host) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Rust tasks: Sonnet minimum (`penguin-rust-dev`); Python/Alembic tasks: Sonnet (`penguin-python-dev`); Helm: `k8s-manifest-builder`.

**Goal:** Ship the full WASM plugin host in `release/v0.4.X`: a Postgres-backed plugin registry (Alembic table + Management API), a Rust in-proxy registry cache with scope-match dispatch, and a sandboxed `svc-plugins` wasmtime executor reached by pooled gRPC — plus Rust and Python→WASM authoring paths — so operators/tenants can add request/response transforms, policy and audit hooks without redeploying the proxy.

**Architecture:** Management writes `plugins` rows (Python/Alembic owns schema) and bumps the existing Valkey invalidation signal. The Rust proxy's `PluginRegistry` (seeded at startup, refreshed on the signal, never fail-closed-empty) resolves, per request and per hook, an ordered plugin chain. Plugins attach at stage boundaries as ordinary `Stage`s (`PluginHookStage`) inserted by `build_pipeline` — P2's `ProxyPipeline` is **not modified**; per-request hook data rides in `PipelineContext.extensions`. One `InvokeChain` gRPC call per hook (not per plugin) goes over a pooled, client-side-LB'd channel to the `svc-plugins` headless Service. The executor loads components **by digest** from the artifact store (OCI/S3), verifies digest + Ed25519 signature **before** compiling, then runs each invocation in a fresh `Store` (fuel + memory + epoch capped) off a cached, pre-linked `InstancePre`.

**Tech Stack:** Rust (pinned stable), `wasmtime` + `wasmtime-wasi` (exact-pinned; Waddles proves `=49.0.4` with `component-model`, `async`, `cranelift`, `pooling-allocator`, WASI p2 — builder re-verifies against RustSec before locking), `wit-bindgen`, `tonic`/`prost`, `ed25519-dalek`, an OCI client + `aws-sdk-s3`, `penguin-bundle-host` (MIT, `=0.3.0`, git rev pinned), `componentize-py==0.25.1` (pure-Python), Alembic/SQLAlchemy + Quart (Python side).

**Spec:** `docs/superpowers/specs/2026-10-28-waddleai-rust-dataplane-migration-design.md` — §5.2 (registry & dispatch, "in scope for v0.4.X"), §6 (transport), §11 (settled calls). **Consumes:** P0 (`Config`, `db` entities, `cache` invalidation, `flags`, telemetry) and P2 (`Stage`, `ProxyPipeline`, `PipelineContext.extensions`, `build_pipeline`).

## Global Constraints

- **Pins:** wasmtime/wasmtime-wasi exact-pinned (`=x.y.z`) in one workspace `[workspace.dependencies]` entry; `componentize-py==0.25.1` hash-pinned in a plugin-build requirements lockfile; `penguin-bundle-host` by git rev + `=0.3.0`; every crate `=`-pinned; `cargo-deny` (advisories, licenses, **no PRC/sanctioned crates**, bans on duplicate wasmtime majors) + `cargo-audit` green. Re-pin deliberately — a wasmtime bump is a security event (sandbox escapes ship as CVEs).
- **Licensing:** reuse only `penguin-bundle-host` (MIT). `~/code/waddles/core/bundle_executor/` is **AGPL — read for patterns (engine/InstancePre, epoch ticker, `StoreLimits`, signature verify, drain), never copy files or large verbatim blocks**. Executor code is re-authored for WaddleAI's domain; the WIT world is WaddleAI's own (`waddleai:plugin`), not `waddle:bundle`. Record "read, not vendored" in the PR description; legal-review checklist item in Task 16.
- **Alembic owns the `plugins` table.** Rust never emits migrations; SeaORM entity is **generated** (`gen-entities.sh`) and covered by P0's drift gate. Registry rows are written only via the Management API, never by the proxy or executor.
- **Entity rules (`critical-rules.md`):** BIGINT surrogate PK + `public_id` UUID across boundaries; **no string references** (model/org scope are FK ids, not names); hook/route/failure-mode are **int enum codes**; booleans only for `enabled`.
- **Transport:** pooled persistent gRPC (Tonic) + client-side LB over a headless Service; **never** per-call ClusterIP, new-connection-per-call, or Valkey streams on the synchronous path (streams only for the async audit fan-out, if used). Proxy↔executor authenticated (mTLS/SPIFFE-ready + short-lived machine JWT interceptor); default-deny Cilium.
- **Fresh `Store` per invocation, never reused across requests or tenants** — no guest state leaks. "Warm" means cached `InstancePre` + precompiled `.cwasm` + pooling-allocator slot reuse, not warm guest memory.
- **Capability-gated host calls; deny by default.** A plugin gets only the imports its manifest declares, validated at registration and at load; undeclared imports fail component validation.
- **Shared multi-tenant, stateless:** one HPA-scaled executor fleet for all tenants; the `.cwasm` cache is an ephemeral `emptyDir` (rebuildable, not state); no per-tenant pods.
- **Everything behind PostHog flags, default OFF:** `waddleai.plugins` (master), per-hook sub-flags optional; unseen/OFF → zero plugin work, zero latency. Dead executor never fails a request except where a plugin is explicitly `fail_closed`.
- **Plugins see pseudonymized text** (pre-dispatch hooks run after SecurityIn/pseudonymize); envelopes carry org/user **UUIDs only**, never usernames/emails; no PII/secrets in spans, metric labels, or logs.
- **Coverage:** `cargo-llvm-cov` ≥ 90% per crate in CI; Python side 90% (`make test-unit`). `unsafe` forbidden in all crates except the single audited `Module::deserialize` (precompiled-cache) call site, behind an integrity check (Task 6).
- **Logging:** `tracing` + OTel (no Rust penguin-logging crate yet — known gap); OTLP from `OTEL_*`; telemetry failure never fails a request.

## Review Focus

1. **Untrusted plugin resource exhaustion** — infinite loop, allocation bomb, huge output, fork-bomb-by-host-call, pathological compile. Fuel + epoch deadline + `StoreLimits` memory/table/instance caps + output/envelope size caps + compile-time cap; pooling-allocator limits so one tenant can't starve slots → Tasks 4, 5, 8, 15.
2. **Signature-verify bypass** — verify must precede compile/cache-insert; digest must equal `sha256(bytes)`; cache entries keyed by verified digest only; precompiled-cache tampering; trust-anchor rotation; proxy-supplied claims never trusted → Tasks 6, 11.
3. **Registry cache stale/empty fail-closed** — never-seeded ≠ loaded-empty; missed invalidation; DB/Valkey down at start or mid-run; seed + lazy-load + periodic version poll; last-known served; degraded is *visible* → Tasks 10, 12.
4. **Plugin crash isolation** — guest trap/panic/OOM/timeout must not take down the executor, poison a pooled slot, or fail unrelated requests; wedged replicas tripped out; per-plugin `failure_mode` honored (policy fail-closed vs transform fail-open) → Tasks 5, 8, 9, 12.
5. **Hot-path latency of the plugin hop** — zero cost when no plugin matches; one gRPC hop per hook (chain RPC); cached `InstancePre`; no per-call connect; bounded hook budget; measured p50/p99 gate → Tasks 10, 12, 15.

---

## Interface Definitions (later tasks and P6 treat these as stable)

**WIT world `waddleai:plugin@1.0.0`** (`rust/wit/waddleai-plugin/plugin.wit`; single source of truth, referenced by path — never inlined):

```wit
package waddleai:plugin@1.0.0;

interface types {
  /// Projected, read-only view of the request — NOT the PipelineContext. Bodies are canonical JSON text.
  record hook-input {
    hook: hook-point,
    org-id: string,            // public UUID
    user-id: option<string>,   // public UUID, never a name/email
    model: option<string>,     // resolved model id as routed
    route: route-kind,
    stream: bool,
    request-json: option<string>,   // pre-dispatch/security-in/audit
    response-json: option<string>,  // post-dispatch/audit (non-stream, or accumulated for observe-only)
    trace-context: option<string>,  // W3C traceparent
  }
  enum hook-point { pre-dispatch, security-in, post-dispatch, audit }
  enum route-kind { chat-completions, messages, embeddings, other }
  variant outcome {
    allow,
    modify(string),            // replacement body JSON (request or response, per hook); host schema-validates + size-caps
    deny(denial),              // policy/transform veto -> proxy short-circuits
  }
  record denial { status: u16, code: string, message: string }
  record plugin-error { code: string, message: string, retryable: bool }
}
interface host-log    { log: func(level: log-level, msg: string); enum log-level { debug, info, warn, error } }
interface host-config { get-config: func() -> string; /* resolved plugin `config` JSONB as JSON text */ }
interface host-kv     { /* per-(plugin,org)-scoped; size/ttl/key-count capped */
  get: func(key: string) -> result<option<list<u8>>, kv-error>;
  set: func(key: string, value: list<u8>, ttl-seconds: u32) -> result<_, kv-error>;
  increment: func(key: string, delta: s64, ttl-seconds: u32) -> result<s64, kv-error>;
  variant kv-error { too-large(u64), quota-exceeded, backend(string) } }
interface host-http   { /* granted ONLY if manifest egress allowlist non-empty; host-side SSRF guard; secrets by ref */
  send: func(req: request) -> result<response, http-error>; /* shapes as Waddles http iface, re-authored */ }
interface hooks {
  use types.{hook-input, outcome, plugin-error};
  on-hook: func(input: hook-input) -> result<outcome, plugin-error>;   // single dispatch export; plugin switches on input.hook
}
world plugin {
  import host-log; import host-config; import host-kv; import host-http;
  export hooks;
}
```

Capability mapping: **request/response transform** = `modify`; **policy** = `deny`; **audit** = `audit` hook (outcome ignored except `plugin-error`, never blocks the response) + `host-log`/`host-http`/`host-kv` for emission. A plugin cannot read headers/credentials/`PipelineContext` internals, cannot change auth/quota/routing/dispatch state, and cannot see the passthrough provider key.

**Registry table `plugins`** (Alembic migration `0NN_plugins.py`):

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT PK | surrogate, internal only |
| `public_id` | UUID UNIQUE | API/log reference |
| `name` | TEXT | display alias, not a reference |
| `digest` | TEXT | `sha256:<hex>` of the component; immutable per row |
| `version` | TEXT | semver display |
| `enabled` | BOOLEAN | kill-switch |
| `hook` | SMALLINT | enum: 1 pre_dispatch, 2 security_in, 3 post_dispatch, 4 audit |
| `position` | INTEGER | order within (hook, scope); ties broken by `id` |
| `scope_org_id` | BIGINT NULL FK organizations | NULL = all orgs |
| `scope_tenant_id` | BIGINT NULL FK tenants | NULL = all tenants |
| `scope_model_id` | BIGINT NULL FK model_registry | NULL = all models |
| `scope_route` | SMALLINT NULL | enum: 1 chat_completions, 2 messages, 3 embeddings; NULL = all |
| `config` | JSONB | passed to plugin via `host-config` (size-capped, no secrets — secret refs only) |
| `artifact_ref` | TEXT | `oci://…@sha256:…` or `s3://bucket/key`; digest-pinned |
| `signature` | TEXT | detached Ed25519 over `signing_payload(digest, hook, version)`, base64 |
| `signer_key_id` | TEXT | trust-anchor id the executor resolves (display of key, executor holds keys) |
| `failure_mode` | SMALLINT | enum: 1 fail_open, 2 fail_closed (default: fail_closed for `security_in`, else fail_open) |
| `limits` | JSONB NULL | optional per-plugin lowering of fuel/memory/deadline (never raising above executor caps) |
| `created_at`/`updated_at` | TIMESTAMPTZ | |

Indexes: `(enabled, hook, position)`; partial on `scope_org_id`. Singleton `plugin_registry_version(id=1, version BIGINT)` bumped in the same transaction as every write — the cheap poll target and cache-version.

**Rust registry cache** (`rust/crates/plugin-registry`):

```rust
pub enum Hook { PreDispatch, SecurityIn, PostDispatch, Audit }
pub enum FailureMode { FailOpen, FailClosed }
pub struct PluginRef { pub public_id: Uuid, pub digest: Digest, pub artifact_ref: String, pub signature: String,
    pub signer_key_id: String, pub config_json: Arc<str>, pub failure_mode: FailureMode, pub limits: Option<LimitOverrides> }
pub struct RequestScope { pub org_id: i64, pub tenant_id: i64, pub model_id: Option<i64>, pub route: RouteKind }
pub enum RegistryHealth { Fresh { version: i64 }, Stale { version: i64, age: Duration }, Unseeded }

pub struct PluginRegistry { /* ArcSwap<Snapshot>, version, last_ok, loader: Arc<dyn RegistryLoader> */ }
impl PluginRegistry {
    pub async fn seed(loader: Arc<dyn RegistryLoader>, inval: InvalidationStream, cfg: RegistryCfg) -> Arc<Self>; // never errors: bounded-retry, then starts Unseeded + lazy-loads
    pub fn resolve(&self, scope: &RequestScope, hook: Hook) -> Arc<[PluginRef]>;   // sync, lock-free, empty slice fast path
    pub fn has_any(&self, hook: Hook) -> bool;                                      // O(1) pre-check, skips scope-match
    pub async fn refresh(&self) -> Result<RefreshOutcome, RegistryError>;           // full reload iff version changed; keeps last-known on error
    pub fn health(&self) -> RegistryHealth;
    pub fn spawn_background(self: &Arc<Self>) -> JoinHandle<()>;                    // invalidation listener + periodic version poll
}
#[async_trait] pub trait RegistryLoader: Send + Sync { async fn load_all(&self) -> Result<(i64, Vec<PluginRow>), RegistryError>; async fn current_version(&self) -> Result<i64, RegistryError>; }
```

**Plugin host client + stage** (`rust/crates/plugin-host-client`, `rust/crates/pipeline` gains none):

```rust
#[async_trait] pub trait PluginHost: Send + Sync {
    async fn invoke_chain(&self, req: ChainRequest) -> Result<ChainOutcome, InvokeError>;
}
pub struct ChainRequest { pub hook: Hook, pub chain: Arc<[PluginRef]>, pub input: HookInput, pub deadline: Duration }
pub enum ChainOutcome { Allow, Modified(serde_json::Value), Denied(Denial), Degraded { applied: Vec<AppliedPlugin>, failed: Vec<PluginFailure> } }
pub struct GrpcPluginHost { /* tonic balanced Channel over DNS(headless svc), breaker, metrics */ }
pub struct PluginHookStage { hook: Hook, registry: Arc<PluginRegistry>, host: Arc<dyn PluginHost>, budget: Duration }
impl Stage for PluginHookStage { fn name(&self)->&'static str; /* "plugins_pre_dispatch" … */ fn flag(&self)->Option<&'static str> { Some("waddleai.plugins") } async fn call(&self, ctx:&mut PipelineContext)->Result<(),StageError>; }
pub struct PluginExtensions { pub applied: Vec<AppliedPlugin>, pub degraded: bool }   // stored in ctx.extensions
```

Stage placement in `build_pipeline` (P2 order `auth, token_budget, security_in, cache, routing, quota, dispatch, security_out, meter`): `plugins_security_in` after `security_in`; `plugins_pre_dispatch` after `quota`, immediately before `dispatch`; `plugins_post_dispatch` after `dispatch`, before `security_out`; `plugins_audit` after `meter` (fire-and-forget, bounded queue). Streaming: `modify` outcomes from `post_dispatch` are **not applied to streams** (bytes already sent) — registry rejects `modify`-capable post-dispatch plugins' application on `ctx.stream`; they run observe-only (`deny` recorded as audit/degraded signal, not blocking). Logged `skipped:plugins_post_dispatch(stream-transform)`.

**gRPC contract** (`rust/proto/waddleai/plugins/v1/executor.proto`): `service PluginExecutor { rpc InvokeChain(InvokeChainRequest) returns (InvokeChainResponse); }` + standard `grpc.health.v1`. `InvokeChainRequest { hook; repeated PluginRef{digest, artifact_ref, signature, signer_key_id, config_json, limits}; bytes input_json; uint32 deadline_ms; string traceparent }`. `InvokeChainResponse { oneof outcome {Allow, Modified(bytes), Denied}; repeated PluginResult{public_id, status, fuel_used, duration_us, error_code} }`. Executor status codes: `RESOURCE_EXHAUSTED` (cap hit, retryable-elsewhere), `DEADLINE_EXCEEDED`, `UNAUTHENTICATED`, `FAILED_PRECONDITION` (signature/digest/validation failure — **never retried**).

---

### Task 1: Dependency spike — pins, `penguin-bundle-host` fit, workspace crates

**Files:**
- Modify: `rust/Cargo.toml` (workspace members + `[workspace.dependencies]` pins), `rust/deny.toml`
- Create: `rust/crates/svc-plugins/`, `rust/crates/plugin-registry/`, `rust/crates/plugin-host-client/`, `rust/crates/plugin-proto/`, `rust/crates/plugin-sdk/` (empty libs + smoke), `docs/superpowers/plans/notes/P4-bundle-host-fit.md` (decision record — a short note, not a report)
- Test: `rust/crates/svc-plugins/tests/deps_smoke.rs`

**Interfaces:** Produces the crate skeletons and the recorded decision of which `penguin-bundle-host` modules WaddleAI uses.

- [ ] **Step 1: Write test** `deps_smoke.rs::bundle_host_manifest_validator_links` (imports the manifest validator from `penguin-bundle-host`, validates a known-good minimal manifest → `Ok`); `deps_smoke.rs::wasmtime_pooling_engine_builds` (a `Config` with `wasm_component_model(true)`, pooling allocator, `epoch_interruption(true)`, `consume_fuel(true)` yields an `Engine`).
- [ ] **Step 2: Run** `cargo test -p svc-plugins` → FAIL.
- [ ] **Step 3: Add deps** — `penguin-bundle-host` (git rev `0b97183f…`, `=0.3.0`, as Waddles pins it; confirm the rev still resolves and `cargo-deny` licence = MIT), wasmtime/wasmtime-wasi exact-pinned. **Read** the crate's `wire/` and `manifest/` APIs and write the decision note: the proxy↔executor transport is gRPC, so the frame codec/correlation-id mux is adopted **only** where it fits (e.g. canonical envelope encoding, or not at all) — record which modules are used and why; the manifest validator is adopted as the plugin-manifest/import-allowlist validator. Flag any gap to the owner instead of silently dropping the spec's "reuse the wire codec" line.
- [ ] **Step 4: Run** → PASS; `cargo deny check`, `cargo audit`, `clippy -D warnings`, `fmt --check` clean.
- [ ] **Step 5: Commit** `chore(rust): P4 crate skeletons + wasmtime/bundle-host pins`.

### Task 2: WaddleAI WIT world + host-call contracts

**Files:** Create `rust/wit/waddleai-plugin/plugin.wit`, `rust/wit/waddleai-plugin/wit_test.sh`; Create `rust/crates/svc-plugins/src/bindings.rs` (bindgen); Test `rust/crates/svc-plugins/tests/wit.rs`

**Interfaces:** Produces the WIT world defined above and `wasmtime::component::bindgen!` host bindings (`Plugin`, `HookInput`, `Outcome`, `PluginError`).

- [ ] **Step 1: Write tests** `wit.rs`: `wit_parses_and_world_resolves` (`wasm-tools component wit` equivalent via `wit-parser` → world `plugin` has the 4 imports + `hooks` export); `world_has_no_wasi_imports_beyond_allowlist` (list guest-visible imports == `{host-log,host-config,host-kv,host-http}` + minimal WASI p2 clocks/random only); `hook_point_enum_codes_match_registry` (WIT `hook-point` ↔ Rust `Hook` ↔ DB int codes, single table-driven test so the three never drift).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Author** `plugin.wit` per Interface Definitions (re-authored — shape informed by `waddles/wit/waddle-bundle/stage.wit`, no copied text) and the bindgen module.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(plugins): waddleai:plugin WIT world`.

### Task 3: Alembic `plugins` table + singleton version row (Python)

**Files:** Create `services/management/alembic/versions/0NN_plugins.py` (next free revision number — verify against current head, 027 exists in the working tree); Modify `services/management/app/models_sqlalchemy.py`; Test `tests/unit/management/test_plugins_schema.py`

**Interfaces:** Produces the `plugins` + `plugin_registry_version` tables (columns above) and SQLAlchemy models; reversible upgrade/downgrade.

- [ ] **Step 1: Write tests** `test_plugins_schema.py`: `upgrade_creates_tables_and_indexes`; `downgrade_is_clean`; `hook_check_constraint_rejects_unknown_code` (hook=9 → IntegrityError); `failure_mode_check_constraint`; `public_id_unique`; `fk_scope_org_requires_existing_org`; `version_row_singleton_enforced` (second row rejected).
- [ ] **Step 2: Run** `make test-unit` (scoped) → FAIL.
- [ ] **Step 3: Implement** migration + models; `init_schema()` parity (idempotent create) per repo convention. Re-run the P0 entity generator and commit the new `plugins` entity (drift gate must pass).
- [ ] **Step 4: Run** → PASS; Rust `entity_drift` test green.
- [ ] **Step 5: Commit** `feat(mgmt): plugins registry table (alembic) + entity`.

### Task 4: `svc-plugins` engine — pooling allocator, epoch, fuel, memory caps

**Files:** Create `rust/crates/svc-plugins/src/{engine.rs,limits.rs,error.rs}`; Test `tests/engine.rs`, `tests/limits.rs`

**Interfaces:**
- `fn build_engine(&EngineCfg) -> Result<Engine, ExecError>` — pooling allocator (`total_component_instances`, `max_memory_size`, `max_tables`, per-instance caps from `EngineCfg`), epoch interruption, fuel, async support, `cranelift` only, **no** threads/shared-memory/relaxed-simd.
- `fn spawn_epoch_ticker(&Engine, tick: Duration) -> JoinHandle<()>`; `fn ticks_for_deadline(Duration) -> u64`.
- `struct InvokeLimits { fuel: u64, memory_bytes: usize, deadline: Duration, max_output_bytes: usize, max_table_elems: u32 }`; `fn effective_limits(exec_caps, Option<LimitOverrides>) -> InvokeLimits` (**clamps down only**; an override above the executor cap is capped, never raised).
- `fn new_store(&Engine, &InvokeLimits, HostState) -> Store<HostState>` (sets fuel, epoch deadline, `StoreLimitsBuilder`).

- [ ] **Step 1: Write tests** (using tiny hand-written WAT components compiled in-test): `infinite_loop_trapped_by_fuel` (fuel exhaustion trap, bounded wall time); `infinite_loop_with_huge_fuel_trapped_by_epoch_deadline` (epoch fires within `deadline + tick`); `memory_grow_beyond_cap_denied` (grow returns -1/trap, executor survives); `allocation_bomb_does_not_exceed_rss_budget`; `override_cannot_raise_above_cap`; `override_can_lower`; `deadline_zero_rejected`; `engine_rejects_threads_proposal_component`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** (patterns per Waddles `engine.rs`, re-authored). Ticker is a single shared task; document why epoch (cheap, async-safe) is the wall-clock backstop and fuel the CPU-determinism cap.
- [ ] **Step 4: Run** → PASS; clippy/fmt/deny.
- [ ] **Step 5: Commit** `feat(svc-plugins): wasmtime engine with fuel/epoch/memory caps`.

### Task 5: Component loader — `InstancePre` cache, per-invocation isolation, trap containment

**Files:** Create `rust/crates/svc-plugins/src/{component.rs,invoke.rs,pool.rs}`; Test `tests/invoke.rs`, `tests/isolation.rs`

**Interfaces:**
- `struct LoadedPlugin { digest: Digest, pre: InstancePre<HostState>, manifest: PluginManifest }`
- `struct PluginCache` — LRU by digest, byte-bounded; `async fn get_or_load(&self, ref: &PluginRefWire) -> Result<Arc<LoadedPlugin>, ExecError>` (single-flight per digest: N concurrent first-requests compile once).
- `async fn invoke_one(&self, plugin: &LoadedPlugin, input: &HookInput, limits: &InvokeLimits, ctx: HostCtx) -> Result<Outcome, PluginFault>`; `enum PluginFault { Trap(String), FuelExhausted, DeadlineExceeded, OutOfMemory, OutputTooLarge, GuestError(PluginError), InvalidOutcome(String) }`.

- [ ] **Step 1: Write tests** (against fixture components built in Task 13 — until then, WAT/precompiled fixtures checked into `tests/fixtures/`): `allow_plugin_returns_allow`; `modify_plugin_returns_replacement_json`; `deny_plugin_returns_denial`; `trap_is_contained_next_invoke_succeeds` (guest `unreachable` → `Trap`; immediate follow-up invocation on same `InstancePre` succeeds); `no_state_leaks_between_invocations` (guest writes a global/linear-memory marker; second invocation sees fresh memory); `no_state_leaks_between_tenants_same_digest`; `modify_with_invalid_json_is_invalid_outcome`; `modify_over_max_output_rejected`; `single_flight_compiles_once_under_concurrency` (32 concurrent first calls → compile counter == 1); `pooling_slot_returned_after_trap` (run `total_component_instances + 10` trapping invocations, none starve); `host_panic_in_import_does_not_kill_executor` (catch_unwind boundary at host-call layer).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** with a fresh `Store` per invoke, `Linker` built once per engine (shared, `LinkerCache` idea only), `InstancePre` cached per digest. Guest outcome validated host-side (JSON parse, size, schema-lite) before it leaves the executor.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(svc-plugins): isolated per-invoke execution + InstancePre cache`.

### Task 6: Artifact store, digest + Ed25519 verification, precompiled `.cwasm` cache

**Files:** Create `rust/crates/svc-plugins/src/{artifact.rs,signing.rs,trust.rs,cwasm_cache.rs}`; Test `tests/artifact.rs`, `tests/signing.rs`

**Interfaces:**
- `#[async_trait] trait ArtifactStore { async fn fetch(&self, artifact_ref: &str, max_bytes: u64) -> Result<Bytes, ArtifactError>; }` impls: `OciStore`, `S3Store`, `FsStore` (tests only, behind `cfg(test)`/feature).
- `fn signing_payload(digest: &Digest, hook: Hook, version: &str) -> Vec<u8>` (domain-separated: prefix `waddleai-plugin-v1\0`).
- `struct TrustAnchors { keys: HashMap<KeyId, VerifyingKey> }`; `TrustAnchors::from_files(dir)` (mounted Secret; **empty set = refuse to start**, never "verify nothing").
- `fn verify_artifact(bytes:&[u8], expected:&Digest, sig:&str, key_id:&str, hook, version, &TrustAnchors) -> Result<VerifiedBytes, VerifyError>` — `VerifiedBytes` is a newtype only constructible here; `Engine::precompile`/`Component::new` accept **only** `VerifiedBytes` (type-level "verify before compile").
- `CwasmCache::{get(digest, engine_fingerprint), put(..)}` — entries are `sha256`-checked + HMAC'd with a per-pod ephemeral key before `Module::deserialize` (the one `unsafe`); key includes wasmtime version + target + config hash.

- [ ] **Step 1: Write tests** `signing.rs`: `valid_signature_verifies`; `wrong_key_rejects`; `tampered_bytes_digest_mismatch_rejects` (flip one byte → digest check fails *before* signature); `signature_over_different_hook_rejects` (domain separation / hook-binding); `unknown_signer_key_id_rejects`; `empty_trust_anchors_refuses_startup`; `revoked_key_removed_from_dir_rejects_after_reload`; `signature_malformed_base64_rejects_no_panic`; `proxy_supplied_digest_cannot_bypass` (registry says digest A, bytes hash to B → reject even if sig valid for B). `artifact.rs`: `oversize_artifact_rejected_streaming` (aborts at `max_bytes`, not after download); `s3_and_oci_fetch_roundtrip` (MinIO/`registry:2` test containers or fakes); `fetch_timeout_bounded`; `cwasm_tampered_on_disk_rejected_and_recompiled`; `cwasm_key_mismatch_after_wasmtime_bump_recompiles`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** (`ed25519-dalek` strict verification; constant-time digest compare; SSRF-safe fetch limited to configured store endpoints; component validation incl. import-allowlist via the manifest validator happens **after** verify, **before** cache insert).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(svc-plugins): artifact store + verify-before-compile + cwasm cache`.

### Task 7: Host-call implementations — `host-log`, `host-config`, `host-kv`

**Files:** Create `rust/crates/svc-plugins/src/host/{mod.rs,log.rs,config.rs,kv.rs}`; Test `tests/host_calls.rs`

**Interfaces:** `struct HostState { limits, org_id, plugin_id, config_json, kv: Arc<dyn PluginKv>, … }`; `trait PluginKv { get/set/increment }` backed by Valkey (`cache` crate) with keys namespaced `plugin:{plugin_public_id}:{org_public_id}:{key}` — a plugin can never address another plugin's or org's keys; per-plugin caps (`max_key_len`, `max_value_bytes`, `max_keys` via a counter, `max_ttl`).

- [ ] **Step 1: Write tests**: `log_is_rate_limited_and_size_capped` (flood of 10k log lines → bounded emitted, a `dropped` counter); `log_never_emitted_at_levels_below_configured`; `config_returns_row_config_verbatim`; `kv_set_get_roundtrip_scoped`; `kv_cannot_cross_plugin_namespace` (key `../other` / delimiter injection → rejected, namespaced literally); `kv_cannot_cross_org_namespace`; `kv_value_too_large_errors`; `kv_key_quota_exceeded`; `kv_valkey_down_returns_backend_error_no_panic`; `kv_increment_atomic_under_concurrency`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** (patterns per Waddles `host/imports.rs`, re-authored; no secrets/PII in log fields — guest log lines tagged `plugin_id` only).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(svc-plugins): log/config/kv host calls`.

### Task 8: Guarded `host-http` egress (capability-gated, SSRF-safe, secret refs)

**Files:** Create `rust/crates/svc-plugins/src/host/http.rs`; Test `tests/host_http.rs`

**Interfaces:** `host-http` linked **only** when the plugin manifest's `egress` allowlist is non-empty; `struct EgressPolicy { allow: Vec<Fqdn>, max_resp_bytes, timeout, max_redirects: 0 }`; secrets resolved host-side by reference name from a mounted Secret — secret values never enter the guest and are scrubbed from errors/responses.

- [ ] **Step 1: Write tests**: `no_egress_manifest_does_not_link_http` (import present → component load fails `FAILED_PRECONDITION`); `host_not_in_allowlist_denied`; `resolves_to_private_ip_denied` (DNS rebinding: resolve then pin IP; 127.0.0.0/8, 10/8, 169.254.169.254, ::1 all denied); `redirect_not_followed`; `response_over_cap_truncated_flagged`; `request_timeout_enforced`; `secret_ref_injected_host_side_and_scrubbed_from_error`; `unresolvable_secret_ref_denies_not_sends_unauthenticated`; `http_call_counts_against_deadline` (slow upstream can't outlive epoch deadline).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** (`reqwest` + custom resolver; async host fn cooperates with epoch interruption).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(svc-plugins): capability-gated SSRF-safe egress`.

### Task 9: gRPC executor service — chain invoke, concurrency cap, deadline backstop, health trip-out

**Files:** Create `rust/proto/waddleai/plugins/v1/executor.proto`, `rust/crates/plugin-proto/` (prost/tonic build), `rust/crates/svc-plugins/src/{server.rs,chain.rs,auth.rs,main.rs}`; Test `tests/server.rs`, `tests/chain.rs`

**Interfaces:** Tonic `PluginExecutor::InvokeChain` + `grpc.health.v1`; `struct ExecutorCaps { max_concurrent: usize, queue_wait: Duration, hard_deadline: Duration }` — a `Semaphore` (acquire with `queue_wait`, else `RESOURCE_EXHAUSTED`) and a `tokio::time::timeout(hard_deadline)` **outside** the epoch deadline (backstop if a host call or compile wedges); `Wedge detector`: N consecutive backstop firings → health `NOT_SERVING` (k8s readiness drops the replica; client-side LB ejects it); auth interceptor validating short-lived machine JWT (`aud=svc-plugins`, scope `plugins:invoke`, tenant claim present) and/or mTLS SPIFFE ID.

- [ ] **Step 1: Write tests** `chain.rs`: `chain_runs_in_order_threading_modified_body` (A modifies, B sees A's output); `deny_short_circuits_remaining_chain`; `chain_total_deadline_shared_not_per_plugin_multiplied`; `fail_open_plugin_trap_skips_and_continues` (reported in `PluginResult`); `fail_closed_plugin_trap_returns_denied_error_outcome`; `one_bad_plugin_does_not_affect_next_request`. `server.rs`: `unauthenticated_rejected`; `wrong_audience_jwt_rejected`; `over_concurrency_returns_resource_exhausted_not_queue_forever`; `hard_deadline_backstop_fires_when_host_call_wedges`; `consecutive_wedges_flip_health_not_serving`; `signature_failure_maps_failed_precondition_never_retryable`; `oversize_request_rejected`; `traceparent_propagated_to_span`; `graceful_shutdown_drains_inflight`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Executor trusts **none** of the request's claims: it re-verifies digest/signature against its own trust anchors on load (Task 6) and clamps limits (Task 4).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(svc-plugins): gRPC chain executor with caps + trip-out`.

### Task 10: Rust `PluginRegistry` cache — seed, lazy-load, invalidation, scope-match

**Files:** Create `rust/crates/plugin-registry/src/{lib.rs,snapshot.rs,matcher.rs,loader.rs}`; Test `tests/registry.rs`, `tests/matcher.rs`

**Interfaces:** `PluginRegistry`, `RegistryLoader` (SeaORM-backed `DbRegistryLoader`), `PluginRef`, `RequestScope`, `RegistryHealth` as defined above. Snapshot = per-hook pre-sorted `Vec<PluginRow>` + a `has_any[hook]` bitset; `resolve` filters by scope (org/tenant/model/route, NULL = wildcard), sorted `(position, id)`, returns an `Arc<[PluginRef]>` memoized per `(hook, org, model, route)` in a small bounded cache invalidated on snapshot swap.

- [ ] **Step 1: Write tests** `matcher.rs`: `null_scope_matches_all`; `org_scope_matches_only_that_org`; `model_scope_requires_model_id_match`; `route_scope`; `disabled_plugin_excluded`; `ordering_by_position_then_id`; `resolve_other_org_excludes_scoped_plugin` (tenant-isolation: org A's plugin never returned for org B); `no_match_returns_empty_slice_fast`. `registry.rs`: `seed_loads_snapshot_and_reports_fresh`; `seed_db_down_starts_unseeded_does_not_error_or_block_startup`; `unseeded_resolve_triggers_lazy_load_and_succeeds_when_db_returns` (single-flight); `unseeded_and_db_still_down_returns_empty_and_health_unseeded` (**distinguishable** from loaded-empty: `health()==Unseeded`, metric `waddleai_plugin_registry_unseeded=1`); `loaded_empty_is_fresh_not_unseeded`; `invalidation_signal_triggers_reload_when_version_bumped`; `invalidation_with_same_version_is_noop`; `missed_invalidation_recovered_by_periodic_version_poll`; `refresh_error_keeps_last_known_snapshot_and_marks_stale` (policy plugins keep enforcing); `snapshot_swap_is_atomic_under_concurrent_resolve` (loom/stress: no torn reads); `corrupt_row_skipped_not_whole_registry_dropped` (bad digest string → that row logged+skipped, others load); `resolve_is_lock_free_bench_guard` (no `.await`/mutex on the path — compile-time via sync fn + clippy `await_holding_lock`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** with `arc_swap`; subscribes via P0 `cache::subscribe_invalidations`, **reusing the same signal/key Management already bumps for routing_rules/model_registry** (add a `plugins` invalidation kind byte-compatible with the Python publisher added in Task 11).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(rust): plugin registry cache (seed/lazy-load/invalidate)`.

### Task 11: Management API for plugin registration (Python) — register, verify, publish invalidation

**Files:** Create `services/management/app/api/v1/plugins.py`, `shared/plugins/registry.py` (validation + invalidation publish); Modify `services/management/app/api/v1/__init__.py`, OpenAPI annotations (`make generate-openapi`); Test `tests/unit/management/test_plugins_api.py`

**Interfaces:** `POST/GET/PATCH/DELETE /api/v1/plugins[/{public_id}]`; RBAC via OIDC scopes `plugins:read|write|admin` (tenant middleware first; org-admin may register only org-scoped plugins for their own org; global/tenant-wide scope requires `plugins:admin`); response via `@validate_response` DTOs (no raw model serialization); `register_plugin(...)`: fetches artifact, recomputes `sha256`, **verifies signature against the platform public key set** (defence in depth — executor re-verifies), runs the manifest/import-allowlist validator (shared Python wrapper over `penguin-bundle-host`'s validator rules or a CLI call to the Rust `plugin-tool validate`), writes row + bumps `plugin_registry_version` in one transaction, then publishes the invalidation.

- [ ] **Step 1: Write tests**: `register_valid_signed_plugin_201_and_invalidation_published`; `register_bad_signature_422_no_row`; `register_digest_mismatch_422`; `register_unknown_import_422` (component imports outside allowlist); `register_oversized_artifact_413`; `org_admin_cannot_register_global_scope_403`; `org_admin_cannot_scope_to_other_org_403`; `tenant_claim_missing_401`; `patch_enabled_false_bumps_version_and_invalidates`; `digest_immutable_on_patch_400` (new digest = new row/version); `response_field_set_exact` (regression: no leaked internals); `config_secret_looking_value_rejected` (denylist heuristic: must be a secret ref); `list_filters_by_hook_scope_enabled_and_paginates` (filter coverage per `testing.md`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**; flag `waddleai.plugins_admin`; audit-log every write.
- [ ] **Step 4: Run** → PASS; `make openapi-lint`.
- [ ] **Step 5: Commit** `feat(mgmt): plugin registry API with signature + manifest validation`.

### Task 12: `GrpcPluginHost` client + `PluginHookStage` + pipeline wiring

**Files:** Create `rust/crates/plugin-host-client/src/{client.rs,stage.rs,breaker.rs,apply.rs}`; Modify `rust/crates/waddleai-proxy/src/pipeline_build.rs` (the P2 `build_pipeline`); Test `tests/client.rs`, `tests/stage.rs`, `tests/wiring.rs`

**Interfaces:** `GrpcPluginHost`, `PluginHookStage`, `PluginExtensions` as defined above. Client: one long-lived `tonic` `Channel::balance_channel` fed by a DNS watcher on the headless Service (re-resolve ~5s), HTTP/2 keepalive, per-endpoint circuit breaker + outlier ejection, per-call `grpc-timeout` = remaining hook budget. Stage: `has_any(hook)` pre-check → `resolve` → empty ⇒ return immediately (no allocation, no await) → else build projected `HookInput` from `PipelineContext` (UUIDs only), `invoke_chain`, then `apply` outcome.

Outcome application (`apply.rs`): `Modified` → replace `ctx.body`/`ctx.messages`/`ctx.prompt_text` (pre-dispatch) or `ctx.response_text` (post-dispatch, non-stream only) after re-running P2's body invariants; `Denied` → `ctx.blocked=true`, `status_code`, `block_reason="plugin:<code>"` (short-circuits via the driver); `Degraded` → `ctx.security_degraded=true` only if a fail-closed-eligible hook, plus `PluginExtensions.degraded`.

- [ ] **Step 1: Write tests** `stage.rs`: `no_matching_plugin_makes_zero_grpc_calls_and_zero_alloc` (mock host call count == 0; `ctx` byte-identical); `flag_off_stage_skipped_and_logged`; `modify_replaces_prompt_and_recounts_tokens_hook` (TokenBudget reservation reconciled — a modified prompt can't dodge quota: tokens re-estimated); `deny_short_circuits_with_status_and_reason`; `chain_is_one_rpc_per_hook_not_per_plugin` (3 matching plugins → 1 `invoke_chain`); `executor_unreachable_fail_open_chain_continues_marks_degraded`; `executor_unreachable_fail_closed_chain_denies_503_plugin_unavailable`; `mixed_failure_modes_fail_closed_wins`; `hook_budget_exceeded_treated_as_executor_failure_per_mode`; `stream_post_dispatch_modify_not_applied_observe_only`; `audit_hook_never_blocks_or_delays_response` (queue full → drop + metric, not backpressure); `envelope_contains_uuids_only_no_username_email` (property test over a populated ctx); `plugin_cannot_modify_auth_quota_routing_fields` (apply layer only touches allowed fields). `client.rs`: `balances_across_headless_endpoints_roundrobin`; `new_endpoint_picked_up_on_dns_change`; `wedged_endpoint_ejected_after_failures_then_recovers`; `channel_reused_no_connect_per_call` (connection counter == endpoints, over 1k calls); `failed_precondition_not_retried`; `resource_exhausted_retried_once_on_another_endpoint`. `wiring.rs`: `build_pipeline_inserts_plugin_stages_at_boundaries_in_order`; `plugins_flag_off_pipeline_behavior_identical_to_p2` (contract snapshots from P2 pass unchanged with flag OFF).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Streaming path: pre-dispatch hooks complete before `run_until("dispatch")` returns, so stream setup is unaffected; post-dispatch/audit run inside `run_after("dispatch")`.
- [ ] **Step 4: Run** → PASS; P2 `make test-contract` still green.
- [ ] **Step 5: Commit** `feat(proxy): plugin hook stages + pooled gRPC host client`.

### Task 13: Plugin authoring — Rust SDK + Python→WASM path + signing tool

**Files:**
- Create: `rust/crates/plugin-sdk/` (re-exports bindgen guest bindings + `#[plugin]` helper), `plugins/examples/rust-redact/`, `plugins/examples/python-policy/{app.py,requirements.lock}`, `plugins/build_python_plugin.sh`, `rust/crates/plugin-tool/` (bin: `sign`, `verify`, `validate`, `publish`), `Makefile` targets `build-plugins`, `sign-plugin`
- Test: `rust/crates/plugin-tool/tests/tool.rs`, `plugins/examples/tests/test_example_plugins.py`, `rust/crates/svc-plugins/tests/examples_e2e.rs`

**Interfaces:** Rust plugins: `cargo build --target wasm32-wasip2` producing a component. Python plugins: `componentize-py==0.25.1 -d rust/wit/waddleai-plugin -w plugin componentize app -o plugin.wasm` (**pure-Python only** — no native wheels; pattern per `waddles/bundles/build_python_bundles.py`, re-authored). `plugin-tool sign --key-file <path> --hook <h> --version <v> plugin.wasm` reads the private key from a **file/stdin only** (never argv), prints `{digest, signature, key_id}`; signing runs in CI/KMS, the private key is never in the repo.

- [ ] **Step 1: Write tests** `tool.rs`: `sign_then_verify_roundtrip`; `key_via_argv_rejected` (no `--key <literal>` flag exists); `validate_rejects_component_with_undeclared_import`; `validate_rejects_core_module_not_component`; `publish_pushes_by_digest_and_prints_artifact_ref`. `examples_e2e.rs` (real executor + real components, flag-on path): `rust_example_redacts_via_modify`; `python_example_denies_matching_prompt`; `python_example_pure_python_only_build_rejects_native_wheel` (build script fails loudly if a `.so`/native dep slipped in); `both_examples_pass_import_allowlist`; `example_built_twice_has_same_digest` (reproducible-build check; if componentize-py is non-deterministic, record and gate on content-addressing instead).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** SDKs/tool/examples; document authoring in `docs/PLUGINS.md` (authoring guide, hook semantics, limits, failure modes, signing flow) — docs ship with the change.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(plugins): rust SDK, componentize-py path, signing tool, examples`.

### Task 14: Telemetry — metrics, spans, logs (executor + proxy hop)

**Files:** Create `rust/crates/svc-plugins/src/metrics.rs`, `rust/crates/plugin-host-client/src/metrics.rs`; Test `tests/telemetry.rs` (both crates)

**Interfaces:** Counters/histograms (names/labels frozen here; **no org/user/plugin-name labels — `plugin_digest_prefix`-free; use bounded `hook` + `outcome` + `fault_kind` only**): `waddleai_plugin_invocations_total{hook,outcome}`, `waddleai_plugin_invoke_duration_seconds{hook}` (histogram), `waddleai_plugin_fuel_consumed` (histogram), `waddleai_plugin_faults_total{fault_kind}` (`trap|fuel|deadline|oom|output|invalid|verify`), `waddleai_plugin_chain_hop_duration_seconds{hook}` (proxy-side, includes network), `waddleai_plugin_registry_health{state}` gauge, `waddleai_plugin_registry_refresh_total{result}`, `waddleai_plugin_cache_total{result=hit|miss|cwasm_hit}`, `waddleai_plugin_executor_inflight`, `…shed_total`. Spans: `plugin.chain` (proxy) → `plugin.invoke` (executor, linked via `traceparent`).

- [ ] **Step 1: Write tests**: `invoke_emits_counter_and_histogram_with_expected_names`; `fault_kinds_counted_per_kind`; `no_forbidden_labels_present` (scan registry output: no `org`, `user`, `plugin`, `digest` label keys); `span_chain_has_executor_child_via_traceparent`; `dead_otlp_exporter_does_not_fail_invoke`; `histograms_present_for_smoke_telemetry_gate` (≥1 histogram observed per `testing.md`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** (`tracing` + OTel; OTLP from `OTEL_*`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(plugins): executor + hop telemetry`.

### Task 15: Adversarial + latency acceptance (flag-on, real components)

**Files:** Create `rust/crates/svc-plugins/tests/adversarial.rs`, `rust/crates/plugin-host-client/benches/hop.rs` (criterion), `tests/integration/test_plugins_acceptance.py` (real proxy + executor + Postgres/Valkey, flag ON), `plugins/fixtures/{loop,membomb,trap,bigout,slowhttp}/`

**Interfaces:** consumes everything above; no new production interfaces.

- [ ] **Step 1: Write tests** `adversarial.rs` (each built from a hostile fixture component, signed with a test key, **real engine, no mocks**): `infinite_loop_killed_within_deadline_plus_tick_and_executor_serves_next`; `memory_bomb_capped_executor_rss_bounded`; `trap_isolated_per_request`; `output_bomb_rejected`; `compile_bomb_bounded_by_compile_timeout_and_size_cap` (huge function count component); `unsigned_artifact_never_compiled` (assert **zero** compile invocations via instrumentation counter); `signed_by_unknown_key_never_compiled`; `swapped_artifact_bytes_under_valid_digest_row_rejected`; `1000_concurrent_invocations_stay_within_max_concurrent_and_shed_excess`; `hostile_tenant_cannot_starve_others` (per-org fair share / per-org inflight sub-cap, if implemented — else documented known limit + test of the global shed); `slowhttp_cannot_exceed_deadline`. `benches/hop.rs` + CI gate: `no_match_overhead_under_5us_p99`; `one_allow_plugin_hop_p50_under_3ms_p99_under_10ms` on loopback with warm `InstancePre` (budgets recorded, builder tunes from measurement and records the number — the gate must be able to fail). `test_plugins_acceptance.py`: `end_to_end_python_plugin_modifies_request_reaches_upstream_modified`; `deny_plugin_returns_403_style_block_with_plugin_code`; `registry_write_to_effect_under_N_seconds_via_invalidation` (Management API write → next request sees plugin, without proxy restart); `disable_plugin_takes_effect_after_invalidation`; `executor_killed_midrun_fail_open_requests_still_succeed_fail_closed_requests_503`; `valkey_down_registry_serves_last_known`; `flag_off_zero_plugin_calls` (executor receives 0 RPCs); `contract_snapshots_unchanged_flag_off`.
- [ ] **Step 2: Run** → FAIL (fixtures missing).
- [ ] **Step 3: Build** fixtures (Rust `wasm32-wasip2` + WAT) and wire `make test-plugins` + the bench gate into CI.
- [ ] **Step 4: Run** → PASS; counts printed (cases examined > 0, per `critical-rules.md` Verification Integrity — a zero-denominator run is FAIL).
- [ ] **Step 5: Commit** `test(plugins): adversarial + latency + e2e acceptance`.

### Task 16: Packaging — Dockerfile, Helm, CI, supply chain, docs

**Files:** Create `rust/crates/svc-plugins/Dockerfile`, `k8s/helm/waddleai/templates/svc-plugins/{deployment.yaml,service-headless.yaml,hpa.yaml,pdb.yaml,ciliumnetworkpolicy.yaml,tetragon.yaml,serviceaccount.yaml}`, values entries in `alpha.yml`/`beta.yml`/`gamma.yml`/`production.yml`, `.github/workflows/rust-svc-plugins-build.yml`; Modify `docs/PLUGINS.md`, `docs/RELEASE_NOTES.md`, docs-site deployment page; Test `tests/helm/test_svc_plugins_render.py`

**Interfaces:** Image `ghcr.io/penguintechinc/waddleai/svc-plugins` (multi-stage, pinned digests, rootless `USER`, `runAsNonRoot`, read-only rootfs, `emptyDir` for cwasm cache only); headless Service (`clusterIP: None`, port-named `grpc`) so clients resolve pod IPs; HPA on CPU + `executor_inflight`; PDB `minAvailable: 1`; default-deny CNP allowing ingress only from proxy pods on the gRPC port, egress only to Postgres? (**no** — executor has no DB access), Valkey (kv), artifact store, DNS, and per-plugin `host-http` egress via an explicit allowlist policy; Tetragon `TracingPolicy` allowlisting the single executor binary; PSA `restricted`; trust-anchor public keys as a mounted Secret; chart values wired for `waddleai.plugins` disabled by default. CI builds beta+ images only (alpha/pre-alpha local), per repo policy.

- [ ] **Step 1: Write tests** `test_svc_plugins_render.py`: `renders_headless_service_clusterip_none`; `deployment_nonroot_readonly_rootfs_no_privilege_escalation`; `no_cluster_ip_service_for_grpc_port` (guards the pin-to-one-pod anti-pattern); `cnp_default_deny_with_proxy_only_ingress`; `executor_has_no_postgres_egress`; `trust_anchor_secret_mounted_and_required`; `hpa_and_pdb_present`; `plugins_flag_default_off_in_all_values`; `helm_lint_and_template_clean` (`authoring-helm-charts` validation ladder incl. rollout/drain math).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** chart + Dockerfile + CI (fmt, clippy, test, llvm-cov ≥90%, audit, deny, `plugin-tool` build, `make test-plugins`, bench gate); update docs (`docs/PLUGINS.md`, RELEASE_NOTES, docs-site) and OpenAPI. PR description records "AGPL Waddles executor read-only; nothing vendored" + a manual diff-skim checklist against `waddles/core/bundle_executor/` for accidental verbatim blocks.
- [ ] **Step 4: Run** `helm lint`, `helm template`, `make test-security`, pre-commit (smoke incl. telemetry counts) → PASS.
- [ ] **Step 5: Commit** `feat(deploy): svc-plugins image, chart, CI, docs`.

---

## Self-Review

- **Spec coverage (§5.2, §6, §10.3, §11):** WASM executor with pinned wasmtime + pooling + epoch/fuel/memory + warm `InstancePre` + cwasm cache (T4–T6); MIT `penguin-bundle-host` reuse + AGPL reference-only (T1, Global Constraints, T16 legal checklist); WaddleAI WIT world + host-call set (T2, T7, T8); OCI/S3 artifact store + Ed25519 verify (T6, T11); concurrency cap + deadline backstop + wedge trip-out (T9); Rust and Python→WASM authoring (T13); Alembic `plugins` table (T3) + Management API (T11); Rust registry cache seed/lazy-load/invalidate, never fail-closed-empty (T10); scope-match → ordered chain → pooled gRPC with digest (T10, T12); proxy plugin seam via `extensions` + stage boundaries (T12); all behind flags default OFF (T12, T16).
- **Review Focus mapped:** (1) exhaustion → T4/T5/T8/T15; (2) signature bypass → T6/T11/T15; (3) registry stale/empty → T10/T15; (4) crash isolation → T5/T9/T12/T15; (5) hot-path latency → T10/T12/T15 (bench gate that can fail).
- **Decisions the spec left open, made here (owner may veto):** (a) **one `InvokeChain` RPC per hook**, not per plugin, to cap hops; (b) **no guest-instance reuse** across invocations/tenants — "warm" = `InstancePre` + `.cwasm`; (c) **per-plugin `failure_mode`** (fail-closed default only for `security_in`; fail-open elsewhere), unavailable executor never blanket-fails requests; (d) **post-dispatch transforms are non-stream only** (streams are observe-only), matching P2's SecurityOut limit; (e) **audit hook is fire-and-forget** with a bounded drop-on-full queue — it never blocks the response; (f) modified prompts are re-token-estimated so a plugin can't dodge quota.
- **Spec deviation to confirm:** the spec says reuse `penguin-bundle-host`'s *wire codec*. Proxy↔executor is gRPC (§6) so the frame codec/mux has no natural role; the **manifest validator** is reused for certain. T1 records exactly which modules are used rather than forcing the codec in or silently dropping it.
- **Known risks/gaps flagged:** `penguin-libs` was not locally checkable while authoring this plan — T1 verifies the pinned rev/APIs; per-org fair-share inside the executor is a stretch goal tested as "global shed + documented limit" if not built; wasmtime exact version is a re-verify-at-build item against RustSec; componentize-py reproducible-digest is unverified (T13 records outcome); Python-side validator reuse (shared wrapper vs shelling to `plugin-tool validate`) is a builder choice, shelling is the default.
- **Proportion:** signatures, test names + assertions, and contracts only; no bodies. Waddles patterns referenced by file, not transcribed.
- **Type consistency:** `Hook`/`FailureMode`/`PluginRef` are defined once in Interface Definitions and used verbatim in T10–T12; DB enum codes ↔ WIT `hook-point` ↔ Rust `Hook` pinned by a single test (T2); invalidation kind added in T11 is consumed in T10; `UserContext` (P0) and `PipelineContext.extensions` (P2) are consumed unchanged.
