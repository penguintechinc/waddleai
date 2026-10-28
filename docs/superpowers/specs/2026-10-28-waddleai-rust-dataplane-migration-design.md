# WaddleAI Data-Plane → Rust Migration — Design

**Date:** 2026-10-28
**Status:** Draft for review
**Supersedes (for these components only):** the language-strategy decision in `docs/superpowers/specs/2026-07-09-waddleai-platform-spec.md` ("AIProxy stays Python"). This document deliberately revisits that call for the proxy and the inference-fleet agent.
**Related prior art:** `~/code/waddles/docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` (Waddles' own pure-Rust data-plane + WASM bundle-host design — the primary reference implementation to build on).

---

## 1. Intent & Why

Migrate WaddleAI's **data plane** — the `proxy/apps/proxy_server` request pipeline — and the **k8s inference-fleet management agent** (the component that deploys/manages Ollama + llama.cpp on our own cluster) from Python to **Rust**.

**Drivers (owner-stated, 2026-10-28):**
1. **Performance** — a native, memory-managed data plane with no interpreter overhead on the hot path.
2. **Code security** — memory-safety and a dramatically smaller supply-chain/CVE surface than a large Python dependency tree in the line of traffic.

**Company policy** (owner-stated): *all real-time / data-plane LOGIC components must be Rust.* Envoy was evaluated as a base and **rejected** — it is C++ and therefore fails the memory-safety goal.

**Key reframing that makes this tractable:** the policy governs the **logic/forwarding plane**. **Model-inference backends are a separate tier** and are inherently not Rust (Ollama and llama.cpp are already C/C++ model servers we call over the network). The one heavy-ML piece of the proxy (Presidio/spaCy PII-NER) therefore belongs in that inference tier, not the Rust logic plane — so it stays Python without violating the policy.

---

## 2. Goals / Non-Goals / Anti-Goals

### Goals
- A **Rust proxy** (Axum + Tonic) that replaces `proxy/apps/proxy_server` at **full API parity**: `/v1/chat/completions`, `/v1/messages`, `/v1/models`, `/mem0/*`, `/api/v1/*`, the gRPC surface (`:50051`), and the MCP mount (`/mcp`, `/mcp/admin`).
- A **Rust `kube-rs` fleet controller** replacing today's imperative Ollama/llama.cpp/Cilium management with a proper desired-state reconcile loop.
- **`svc-plugins`** — a WASM executor image hosting complex/custom logic plugins (authored in Rust *or* Python→WASM), independently scalable.
- **`svc-classification`** — a dedicated **Python 3 / Quart + Presidio/spaCy** image for PII-NER (tier-3) and classification; native ML inference, independently scalable.
- **No behavioral regression** — proven by contract-snapshot parity against the existing Python services.
- Memory-safe logic plane; measurable latency improvement on the proxy's own overhead.

### Non-Goals
- **Not** rewriting the database schema or migrations in Rust. SQLAlchemy + Alembic remain the sole schema/migration authority (`services/management/alembic/`); the Rust services **read** the existing schema via SeaORM/sqlx.
- **Not** porting Presidio/spaCy to Rust. A Rust-native NER (GLiNER/ONNX) was considered and **rejected**: lower/unproven PII recall and ongoing model-management headache for a conditional, cacheable stage.
- **Not** replacing Ollama or llama.cpp. They stay; only the **controller** that manages them becomes Rust.
- **Not** changing the quota Lua scripts (`shared/quota/*.py` LUA) — they run inside Valkey and are reused verbatim.
- **Not** Envoy (C++ — fails the memory-safety driver).
- **Not** a big-bang cutover.

### Anti-Goals (failure even if it "works")
- Any regression in **PII egress protection** or the **pseudonymize↔depseudonymize** round-trip (customer names must never reach external providers).
- Any **billing/quota drift** versus the current reserve→reconcile behavior.
- Introducing **memory-unsafe code into the Rust logic plane** (unsafe stays confined to the already-C/C++ inference engines behind the network boundary).
- **Breaking the API contract** for existing clients (CLI, webui, SDKs, MCP consumers).
- A migration that can't be rolled back per-route / per-flag.

---

## 3. Constraints

- **All-Rust data-plane-logic policy**; ML-inference backends (Ollama, llama.cpp, `svc-classification`) are the documented exception.
- **House standards:** Axum + Tonic + SeaORM; `kube-rs` for the controller; `tracing` + OTel sink (no Rust penguin-logging crate yet — known gap); gRPC is the service-to-service standard; deps pinned + `cargo-deny`/`cargo-audit`; rootless + stateless containers; Cilium default-deny + Tetragon + Pod Security Admission `restricted`.
- **Schema authority stays Python** (SQLAlchemy/Alembic). Rust never emits migrations.
- **No Rust shared libs exist yet** for auth/DAL — the only published-ish Rust shared crate is `penguin-bundle-host` (MIT, wire codec + manifest validator). Rust OIDC/JWKS/RBAC and DAL must be built (and should be contributed back to `penguin-libs` per house rule before hand-rolling).
- **Hot-path latency budget:** proxy overhead is ~2–5 ms today vs 0.5–60 s upstream. Out-of-process calls (`svc-plugins`, `svc-classification`) must use **pooled gRPC over persistent channels with client-side LB across a headless Service** — NOT per-call ClusterIP (which pins gRPC to one pod and adds connect latency) and NOT Valkey streams (a broker round-trip is wrong for a synchronous hot-path filter). Valkey streams are reserved for async/fire-and-forget work only.
- **Everything behind PostHog flags**, default OFF; strangler migration with the Python proxy as fallback until parity is proven.

---

## 4. Architecture

```
                         ┌─────────────────────────────────────────────┐
  client ── REST/gRPC ──▶│            RUST PROXY  (Axum/Tonic)          │
  (CLI/webui/MCP/SDK)    │  stage pipeline (ported 1:1 from Python):   │
                         │  Auth(JWKS) → TokenBudget(tiktoken-rs) →    │
                         │  SecurityIn(regex tiers) → Quota(Valkey Lua)│
                         │  → Cache → Routing → Dispatch(reqwest REST) │
                         │  → SecurityOut(regex) → Meter               │
                         │  DAL: SeaORM/sqlx over existing PG schema   │
                         └───┬───────────────┬───────────────┬─────────┘
            pooled gRPC (headless svc LB)    │               │ pooled gRPC
            ┌────────────────────────────────┘               └──────────────┐
            ▼                                                                 ▼
  ┌───────────────────────┐                                     ┌─────────────────────────┐
  │   svc-plugins          │                                     │  svc-classification      │
  │   (WASM executor)      │                                     │  (Python3/Quart +        │
  │   Rust or Python→WASM  │                                     │   Presidio/spaCy)        │
  │   complex/custom logic │                                     │  PII-NER tier-3 +        │
  │   componentize-py +    │                                     │  classification; native  │
  │   wasmtime; reuse      │                                     │  ML; own HPA; conditional│
  │   penguin-bundle-host  │                                     │  + cached                │
  └───────────────────────┘                                     └─────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────────────────────────┐
  │  RUST FLEET CONTROLLER (kube-rs)  — reconcile loop                                     │
  │  desired-state CRDs → Ollama/llama.cpp DaemonSets, Services, Cilium CRDs, model pulls, │
  │  node/VRAM caps, idle teardown.  Replaces today's imperative, API-triggered Python.    │
  └─────────────────────────────────────────────────────────────────────────────────────┘

  Inference tier (unchanged, not Rust — model servers): Ollama / llama.cpp DaemonSets,
  embedding models.  svc-classification sits in THIS tier conceptually.
```

---

## 5. Component Design

### 5.1 Rust Proxy Core (Axum/Tonic)

The current pipeline is already a clean list of stage objects (`proxy/apps/proxy_server/pipeline/stages.py`: `Stage` ABC :229, `ProxyPipeline` :257, `PipelineContext` :151) — port **stage-for-stage**, each `async fn call(ctx) -> ctx`, flag-gateable.

| Stage (Python site) | Rust plan |
|---|---|
| AuthStage (:424) | `jsonwebtoken` + JWKS; honor claims `sub/iss/aud/iat/exp/scope/tenant/teams/roles`; tenant-scoping at query layer |
| TokenBudgetStage (:459) | `tiktoken-rs` (validate encoder parity vs `token_manager.py`) |
| SecurityInStage (:527) | **regex tiers native**: PromptSecurityScanner `THREAT_PATTERNS` (`prompt_security.py:195`) → `regex` crate; ContentFilter T1 `BUILTIN_PATTERNS` (:258) + T2 custom rules + LLM auditor (HTTP) → native; IntentClassifier (Ollama `shieldgemma` HTTP) → native. **Tier-3 NER → gRPC call to `svc-classification`** (see 5.3) |
| QuotaStage (`quota_stage.py`) | reserve→reconcile orchestration in Rust; **reuse the Valkey Lua verbatim** (`shared/quota/lease.py` `LUA_RESERVE`) via `redis-rs`/`fred` |
| CacheStage (:718) | exact + semantic cache; semantic embedding lookup via Ollama HTTP (embeddings stay an external call) |
| RoutingStage (:803) | pure decision logic → native Rust |
| DispatchStage (:956) | **`reqwest` REST clients** for OpenAI/Anthropic/Gemini/Bedrock (reimplement the 4 SDKs as REST — biggest volume, not hard); Ollama/llama.cpp already raw HTTP. SSE streaming + `_depseudonymize` (:1245) |
| SecurityOutStage (:1330) | regex native + tier-3 NER via `svc-classification` |
| MeterStage (:1497) | tiktoken estimate + DB write (SeaORM) + quota reconcile |

**DAL:** reimplement `penguin-dal` access as **SeaORM/sqlx** over the existing schema (`services/management/app/models_sqlalchemy.py`, ~60 tables). Per-service DB accounts + PII-tokenization rules apply. Rust never owns migrations.

**API surface:** Quart → Axum routers; gRPC (`grpc_server.py`) → Tonic; mem0 blueprint + MCP mount reproduced. Contract snapshots (§7) are the gate.

#### 5.1.1 End-user auth modes — virtual tokens + passthrough

Two credential models, resolved per request in **AuthStage** (`ctx.auth_mode`):

- **Virtual-token mode** (today's model, default): client presents a WaddleAI `wa-{key_id}-{secret}` key. The proxy authenticates the user/org, enforces WaddleAI **quota/credits**, and at dispatch **injects WaddleAI-managed provider credentials** (encrypted `provider_credentials`, BYOK). WaddleAI is the biller.
- **Passthrough mode** (new): client presents **their own upstream provider key**; the proxy **forwards that credential** to the provider and does **not** inject managed creds. The provider bills the user directly; WaddleAI is a transparent policy layer. **The end-user is NOT WaddleAI-authenticated in this mode** — the proxy forwards the credential and treats the upstream's acceptance (a non-401/403 **greenlight**) as the auth; an upstream rejection is surfaced to the client unchanged. The *only* difference from virtual mode is **where the user authenticates** (with WaddleAI vs. the provider directly). Org/tenant context for security policy, rate-limits, and metering attribution comes from the **passthrough-enabled key / deployment config**, not from validating the end-user. (Builder: confirm how org context is carried for passthrough — a WaddleAI org header alongside the provider key, or deployment-scoped single-org.)

**Adopted defaults** (owner approved "go forward with those thoughts", 2026-10-28 — veto on review):
- **Mode selection:** detected per request by credential type (`wa-…` → virtual; a raw provider key → passthrough), but **passthrough is only honored when the org/key policy has it explicitly enabled** — otherwise a non-`wa-` credential is rejected (ergonomic detection + opt-in safety).
- **Quota/billing:** passthrough charges **no WaddleAI credits** (the user pays the provider), but **security + metering always run**, and org-level **rate/limit policy may still be enforced** — QuotaStage runs in *rate-limit-only* mode (no credit debit) for passthrough.
- **Security:** PII/security (SecurityIn/Out) + the pseudonymize↔depseudonymize round-trip apply in **both** modes — the proxy is the egress boundary regardless of who pays.

**Pipeline impact:** AuthStage and DispatchStage branch on `ctx.auth_mode`; QuotaStage is mode-conditional (credit-debit vs rate-only vs skip); every other stage is mode-agnostic. (Builder: confirm whether a passthrough-enable flag already exists on `api_keys`/`virtual_keys`/org policy, or needs an Alembic column.)

### 5.2 `svc-plugins` (WASM executor)

Complex/custom logic plugins, authored in **Rust or Python→WASM** (`componentize-py` 0.25.1, pure-Python only — confirmed). WASI 0.2 components via **wasmtime** (pooling allocator, epoch deadlines, fuel, per-instance memory caps).

- **Reuse:** `penguin-bundle-host` (penguin-libs, **MIT**, v0.3.0) — length-prefixed frame codec + correlation-id mux + manifest validator. **Fork/adapt** the Waddles `bundle-executor` (AGPL — reference implementation, re-author host-call set + WIT world for WaddleAI's domain; **licensing caveat: do not vendor AGPL into non-AGPL code**).
- **Transport:** pooled gRPC over a **headless Service** (client-side LB), per §3.
- **Use for:** operator/tenant-extensible logic you don't want to redeploy the proxy to change — custom request/response transforms, per-org routing/cost policies, bespoke compliance/audit hooks, org-specific redaction beyond the built-ins, new connector/MCP adapters. **Not** for heavy ML inference (WASM can't run it performantly in any language), and **not** for core logic (auth/routing/dispatch/metering stay native in the proxy).

#### Plugin registry & dispatch (same pattern WaddleAI already uses for config)

The proxy already "reads config from Postgres with Valkey-cached hot paths (policy resolution, routing rules, registry) invalidated on Management writes — no Management→Proxy runtime RPC" (root `CLAUDE.md`). The plugin system reuses that exact pattern (and mirrors Waddles' DB-backed bundle registry):

- **`plugins` registry table** (Alembic-owned; written via the Management API, never by the proxy): `id`, `digest`, `version`, `enabled`, **scope selectors** (org/tenant/model/route), **pipeline hook + order** (which stage boundary it slots at — e.g. pre-dispatch, security-in, post-dispatch — and its ordering within that hook), `config` (JSONB), and an **artifact ref** (OCI/S3) for the WASM component.
- The **Rust proxy keeps an in-memory cache** of the registry, seeded at startup and refreshed on a Valkey invalidation signal / version bump — identical to how `routing_rules`/`model_registry` are cached today; **no per-request DB hit, no fail-closed-empty cache** (seed + lazy-load).
- **Per request:** the proxy matches the cached scope selectors → produces an ordered plugin list per hook → invokes each via **pooled gRPC to `svc-plugins`**, passing the digest; `svc-plugins` loads that WASM component from the artifact store (signature-verified, per the Waddles executor) and keeps warm, resource-capped instances.

#### In scope for v0.4.X (owner decision §10.3)

The full WASM plugin host **ships in the `release/v0.4.X` migration** (not deferred): the registry table, scope/hook dispatch, artifact store + signature verification, and the executor (MIT `penguin-bundle-host` for the wire codec + a re-authored host; the AGPL Waddles executor is referenced only, never vendored). The proxy's plugin trait/seam and the registry cache are built alongside the core proxy so the first plugins can land within the same release.

### 5.3 `svc-classification` (Python 3 / Quart + Presidio/spaCy) — NEW

Dedicated ML-inference image for the **tier-3 PII-NER** and classification work that is Python-ecosystem-captive.

- Keeps the mature **Presidio + spaCy `en_core_web_lg`** stack (native wheels) — the existing `shared/security/ner_filter.py` logic extracted almost as-is (it's already `ProcessPoolExecutor`-isolated + `ImportError`-optional — a natural seam).
- **Entity ownership:** `PERSON` (redact, the unique value), `EMAIL`/`PHONE` (reinforce regex), `LOCATION`/`ORG` (log). Feeds the **pseudonymize↔depseudonymize** round-trip so names never egress to external providers.
- **Exposed to the proxy over gRPC**, behind **one Rust trait** in the proxy, so the backend is swappable (a future Rust-native NER could replace it without touching the pipeline — explicitly not planned now).
- Own Deployment + HPA; reached via **pooled gRPC + headless-Service client-side LB** (same pattern as `svc-plugins`, per owner decision).
- **Conditional + cached:** only invoked when cheaper tiers don't decide (flag/license-gated) — not on every request's critical path.
- **Fail-mode policy must port:** the existing `content-filter-guard` degraded-signal + fail-open/fail-closed behavior (per-org policy, warn-rate-limit) is load-bearing and must be preserved across the gRPC boundary.

### 5.4 Rust Fleet Controller (`kube-rs`)

Today's fleet management is **imperative and API-triggered** (no reconcile loop): `services/management/app/services/{ollama_manager,llamacpp_manager,cilium_policy}.py` call the synchronous Python `kubernetes` client on demand; plus static Helm manifests. This is a **greenfield improvement**, not a 1:1 port.

- A proper **`kube-rs` reconcile loop** over desired-state CRDs: Ollama/llama.cpp DaemonSets + Services, Cilium CRDs (`CiliumNetworkPolicy`/`CiliumEnvoyConfig`), model pulls, node/VRAM caps (`shared/fleet/caps.py`), idle teardown (`idle_teardown.py` — whose scheduler is "not yet landed" today; the loop lands it).
- Re-express the **6-method `InferenceFleetBackend` contract** (`shared/fleet/base.py:104`) as Rust traits; keep the RBAC scopes (`FLEET_ADMIN`/`CILIUM_ADMIN`/`LLAMACPP_ADMIN`/`OLLAMA_ADMIN`).
- Cloud/external backends (vertex/bedrock/azure/exo — register-and-route HTTP clients) port as `reqwest` clients.

### 5.5 Reuse & Scaffolding

- `clients/waddleai-cli` provides directly-copyable scaffolding: `reqwest`/`tokio`/`serde`/`tracing`/`clap` patterns, `deny.toml`, `rust-toolchain.toml`, committed `Cargo.lock`, and the `rust-build.yml` CI shape (fmt/clippy/test/audit/deny/coverage). The server is a **new workspace crate** (no shared business logic with the CLI).
- Build on the Waddles 2026-09-14 Rust-data-plane spec for the executor + transport patterns.

---

## 6. Transport & Topology (decided)

- Proxy ↔ `svc-plugins` and proxy ↔ `svc-classification`: **gRPC (Tonic) over persistent pooled channels, client-side load-balanced across a headless Service.** Avoid per-call ClusterIP LB (gRPC pins to one pod + connect cost) and new-connection-per-call (the measured latency killer in Waddles).
- **Valkey streams** (`penguin-spine` pattern): reserved for **async** work (audit, async logging), never a synchronous hot-path filter call.
- Per-callee **concurrency caps + deadline backstop + trip-out of wedged replicas** (copy the Waddles executor's proven pattern).
- mTLS between proxy and the service tier; default-deny Cilium; each service holds only the capabilities it needs.

---

## 7. Migration Strategy (strangler, parity-gated)

**Target branch: a new `release/v0.4.X`**, cut after `release/v0.3.X` is wrapped as-is. All phases below (P0–P6), including the WASM plugin host (P4), land on `release/v0.4.X`.

1. **Contract snapshots first.** Capture request/response snapshots for every public surface (`/v1/chat/completions`, `/v1/messages`, `/v1/models`, `/mem0/*`, `/api/v1/*`, gRPC, MCP) into `tests/contract/` with a `make test-contract` gate. These are the **merge gate** for every phase — the Rust services must pass them unchanged.
2. **Flagged, per-route cutover.** The Rust proxy runs alongside the Python proxy; a PostHog flag (default OFF) routes a surface/tenant to Rust. Shadow-compare where feasible before flipping.
3. **Suggested phase order** (each independently shippable + contract-gated):
   - P0: Rust crate scaffold (workspace, CI, deny, SeaORM connection, JWKS auth) + contract-snapshot harness.
   - P1: `svc-classification` extracted from the current Python NER (so the seam exists before the proxy needs it).
   - P2: Rust proxy read/light path — Auth, TokenBudget, regex SecurityIn/Out, Routing, Cache, Quota (Lua reuse), Meter — dispatch to **local** (Ollama/llama.cpp) models first.
   - P3: commercial dispatch (`reqwest` REST connectors) + the pseudonymize↔restore round-trip calling `svc-classification`.
   - P4: `svc-plugins` WASM host (reuse `penguin-bundle-host`, fork executor).
   - P5: Rust `kube-rs` fleet controller (greenfield; can proceed in parallel after P0).
   - P6: MCP mount + mem0 surface parity; retire the Python proxy per-route as parity holds.
4. **Rollback** is a flag flip to the Python proxy at any phase.

---

## 8. Risks & Mitigations

- **Provider-SDK reimplementation volume** (OpenAI/Anthropic/Gemini/Bedrock as `reqwest` REST) — biggest effort; mitigate with a shared connector trait + per-provider contract tests. Lose SDK retry/typing ergonomics (re-add explicitly).
- **penguin-dal / penguin-aaa have no Rust equivalent** — must build SeaORM DAL + OIDC/JWKS/RBAC; well-bounded but substantial; contribute to penguin-libs.
- **`svc-classification` availability** becomes a data-path dependency — the existing fail-open/fail-closed policy must port exactly; cache aggressively; conditional invocation keeps it off the common path.
- **AGPL executor** — reuse only the MIT `penguin-bundle-host`; re-author the executor; legal review of any Waddles code lifted.
- **tiktoken parity** — validate `tiktoken-rs` encoders byte-for-byte against the Python counts before trusting metering/quota.
- **Quota correctness** — reuse the Lua verbatim; reconcile-vs-reserve regression tests (note pre-existing overdraw issue **#347**).

---

## 9. Left to the Builder

- Exact crate selection (`fred` vs `redis-rs`; `axum` version; `ort` vs `candle` — N/A now since NER is Python; `tonic` + `prost`; `kube-rs` + `k8s-openapi` versions) and the Cargo workspace layout.
- The gRPC `.proto` contracts for `svc-plugins` (host API) and `svc-classification` (classify/analyze).
- The WIT world + host-call capability set for `svc-plugins` (adapt from Waddles).
- The CRD definitions for the fleet controller's desired state.
- Precise phase sizing and the shadow-compare harness.

---

## 10. Resolved — Owner Decisions (approved 2026-10-28)

1. **Passthrough org-context & abuse model — APPROVED.** A passthrough request carries a WaddleAI `wa-…` key (identifies org + passthrough entitlement + rate-limit/abuse scope, revocable) **plus** the user's provider key (forwarded; the upstream greenlight is the user-auth). No *end-user* WaddleAI validation; the org is always known and abuse-controlled via the `wa-` key.
2. **Passthrough scope — APPROVED.** Build the auth-mode seam in the Rust proxy now; ship passthrough as a **fast-follow after parity**, within the v0.4.X line.
3. **WASM plugin host — IN SCOPE for v0.4.X (reversed from defer, per owner).** The full `svc-plugins` host — registry table, scope/hook dispatch, artifact store + signature verification, and the executor (MIT `penguin-bundle-host` wire codec + a re-authored host) — **ships in the `release/v0.4.X` migration**, not deferred. P4 is a real deliverable.
4. **Fleet controller scope — APPROVED.** The Management API becomes the CRD writer (FLEET/CILIUM/LLAMACPP/OLLAMA RBAC scopes authorize CRD writes); the Rust `kube-rs` controller reconciles; the Python `ollama_manager`/`llamacpp_manager`/`cilium_policy` retire once parity holds. Build on the current fleet code.

**Release line — APPROVED:** the migration targets a **new `release/v0.4.X`**, cut **after `release/v0.3.X` is wrapped as-is**. All phases (P0–P6), including the WASM host, land on `release/v0.4.X`.

---

## 11. Builder-Check Resolutions (settled calls — not owner-gated)

Calls a sensible builder can make; recorded here so the plan is unambiguous. Owner may still veto any on review.

- **Passthrough-enable flag** lives per-key on `api_keys`/`virtual_keys` (with an org-level default). Builder greps for an existing column first; if absent, it's a **Python-side Alembic** column — consistent with "Rust never owns migrations."
- **Pseudonymize round-trip:** the token↔name **mapping lives in the proxy's request context** — `svc-classification` returns spans only; the proxy substitutes placeholders, holds the map, and restores on egress. **Streaming** depseudonymization uses a rolling buffer at the proxy so placeholders split across SSE chunks are reassembled. The NER-result **cache is keyed by a content hash (never raw PII in Valkey)**, stores detected spans, TTL-bounded.
- **Correction to "conditional" NER:** tier-3 runs **whenever the org enables PERSON detection** (regex can't find names), i.e. effectively per-request when enabled — *not* "only when cheaper tiers fail." Budget the gRPC hop accordingly (target a few ms + cache); it's not a rare call.
- **Parity bar:** contract snapshots normalize volatile fields (timestamps/IDs/latencies) and compare **status + response shape + semantic content**; streaming is compared as **event-sequence + content modulo IDs**. **Bugs are ported verbatim** unless explicitly flagged (e.g. the org=0 gRPC follow-up and **#347** quota overdraw are ported as-is + documented regression tests, not fixed in the migration). P0 audits existing `tests/contract/` coverage and fills gaps.
- **Strangler invariant:** **exactly one proxy implementation serves a given tenant at a time** — the single authoritative quota writer, so credits can't drift. The Rust proxy must be **byte-compatible** with the existing Valkey cache-key formats, the `wa-{key_id}-{secret}` O(1) key format, and the Valkey cache-invalidation signal, so both proxies share state safely during cutover.
- **Rust shared libs:** build **in-repo first** behind a clean crate boundary (extract to `penguin-libs` later), for JWKS/RBAC/DAL. SeaORM entities via **`sea-orm-cli generate entity`** against a migrated DB **+ a CI drift check** — no hand-mapping ~60 tables.
- **Flag eval & cutover mechanism:** the Rust proxy evaluates PostHog flags + `penguin_licensing` entitlements over REST with an **offline last-known cache** (mirror Python, fail-safe OFF). Request **cutover between Rust and Python happens at the ingress/gateway layer**, keyed on the flag per tenant/surface — not an in-proxy shim.
- **Telemetry parity:** keep **identical `gen_ai.*` span attributes and Prometheus metric names/labels** (dashboards depend on them), via `tracing` + OTel.
- **tiktoken:** `tiktoken-rs`, **port current behavior including the estimate paths** (do not "fix" estimate-vs-real here); Anthropic/Gemini use their own count paths, not tiktoken.
- **Bedrock connector:** use the **official AWS Rust SDK** (`aws-sdk-bedrockruntime`) for SigV4 + event-stream — a justified exception to "reimplement the SDKs as REST" (SigV4 is error-prone; the AWS Rust SDK is memory-safe and maintained). The other three commercial providers stay raw `reqwest` REST.
- **Shadow-compare:** included in **P2 behind a flag** (mirror live traffic, diff) — contract snapshots alone can't catch streaming/concurrency differences.
- **Rust coverage gate:** `cargo-llvm-cov`, **90%**, mirroring the Python gate and the `clients/waddleai-cli` CI shape.
- **PRC-dependency / supply-chain rule** enforced via `cargo-deny` in CI (per house standard).
