# WaddleAI Platform Specification — v0.2.x → v0.6.x

**Status**: DRAFT COMPLETE — all 14 sections written; all 11 open questions resolved; ready for full review
**Date**: 2026-07-09
**Supersedes**: MarchProxy AILB integration (MarchProxy is deprecated)
**Implementation handoff**: per-feature-branch TDD plans in `docs/superpowers/plans/` (all drafted, ~157 tasks total, TDD, each task ends in a commit):
- `2026-07-09-phase0-license-server-waddleai-product.md` — §14.6 (license-server repo), 6 tasks
- `2026-07-09-phase1-consolidation.md` — §4 `chore/consolidate-quart-k8s`, 20 tasks
- `2026-07-09-phase1-aiproxy-migration.md` — §5 `feature/aiproxy-migration`, 16 tasks
- `2026-07-09-phase1-cilium-reconciler.md` — §12.1 `feature/cilium-policy-reconciler`, 9 tasks
- `2026-07-09-wave2-response-cache.md` — §6 `feature/response-cache`, 12 tasks
- `2026-07-09-wave2-proxy-memory-layers.md` — §6A `feature/proxy-memory-layers`, 14 tasks
- `2026-07-09-wave2-smart-routing.md` — §7 `feature/smart-routing`, 15 tasks
- `2026-07-09-wave3-security-v2.md` — §8 `feature/security-v2`, 13 tasks
- `2026-07-09-wave3-knowledge-layer.md` — §9 `feature/knowledge-layer`, 12 tasks
- `2026-07-09-wave4-inference-fleet.md` — §10 `feature/inference-fleet-v2`, 16 tasks
- `2026-07-09-wave4-mcp-integrations.md` — §11 `feature/mcp-v2-integrations`, 16 tasks
- `2026-07-09-wave4-tetragon-admission.md` — §12.2/3 `chore/tetragon-admission-policies`, 8 tasks

---

## Locked Decisions

| Topic | Decision |
|---|---|
| Product pillars | Token efficiency + local knowledge + security layer |
| Default apparatus | OpenCode (MIT); PenguinCode untouched (separate, lower-priority track) |
| Big-5 providers | OpenAI, Anthropic, Google Gemini, xAI, AWS Bedrock (Azure via OpenAI base-URL override) |
| Code licensing | Strictly OSI (MIT/Apache/BSD/GPL2+); no Chinese-origin models or libraries; EU/NORAM fine |
| Model weights | Gemma ToU acceptable as default; IBM Granite (Apache-2.0) always offered as alternative default |
| Consolidation | Early (Phase 1): Flask→Quart, retire legacy FastAPI plane, one `k8s/` tree, Helm deploys proxy, Valkey everywhere |
| Fleet | Pluggable `InferenceFleetBackend`; Ollama DaemonSets primary; llama.cpp + EXO (external only) pluggable |
| Deployment targets | Native: K8s/local. Professional: hybrid — same interface manages GCP Vertex AI + AWS Bedrock endpoints |
| Integration surfaces | OpenAI-compatible API, Anthropic-compatible API, MCP server, VS Code extension (last priority) |
| Security fail-mode | Degrade default; fail-closed available per policy scope |
| Memory delivery | Hybrid by client type: budgeted auto-inject for plain clients; MCP-pull for MCP-capable clients |
| License tiers | Free ≤5 nodes/≤3 models/K8s-local; Pro + Google SSO + hybrid targets, **dual-metered: per identity/seat (proxy + user features) and per managed node (deployment management to K8s/GCP/AWS)**; Enterprise + external KMS + multi-tenancy |
| Cilium-first | Assume Cilium Kubernetes; use native features (rate-limit CRDs, CNP, Tetragon, Gateway API) over in-app enforcement |
| AILB migration | **Migrate/merge the AILB module code from marchproxy `origin/release/v0.2.x`** (latest non-dependabot branch) into this repo — marchproxy will be deleted. Prefer Valkey over heavier stores wherever it suffices. **The component is renamed AIProxy**; "AILB" remains only as the historical name for MarchProxy artifacts |
| Pipeline ordering | Cheap gates before expensive stages — token/budget check precedes security filtering; guard-model inference is never spent on requests that fail cheaper checks |
| Language strategy | AIProxy stays **Python 3.13/Quart** (I/O-bound workload; AI ecosystem captive; AILB merge is Python→Python). **Rust** for the client-side `waddleai-mcp` stdio shim (static binary on dev machines); metrics-gated escape hatch for a Rust app-core later. **No component duplicates what Cilium already provides** — eBPF/XDP acceleration comes from Cilium features (XDP kube-proxy replacement, sockops/sockmap bypass), never hand-rolled |
| Presentation layers | **WebUI is first-class/primary**; a **CLI** serves quick-and-easy/headless use. Both are thin presentation layers over the same `/api/v1` — no logic lives in either. Proposed: CLI is the same Rust static binary as the MCP shim (`waddleai` with `waddleai mcp` subcommand) |

---

## 1. Vision & Product Definition

### 1.1 What WaddleAI is

WaddleAI is a self-hostable AI apparatus that sits between coding tools/agents and every model they use — local or commercial. It is three layers in one deployment:

1. **Token-efficiency layer** — every request passes through prefix/semantic caching, upstream prompt-cache orchestration, and smart routing that sends work to the cheapest model capable of it (local-first, escalating to commercial only when needed). The goal: teams spend commercial tokens only where commercial quality matters, and never pay twice for the same context.
2. **Local-knowledge layer** — organizational knowledge (codebases via CodeRAG, conversation memory, on-demand cached language documentation) lives inside the deployment and is served to any connected tool, either pulled precisely via MCP or injected within a hard token budget. Knowledge stays local; only the minimal necessary context ever reaches a commercial API.
3. **Security layer** — a policy-driven, admin-toggleable filter pipeline (regex/PII → custom rules → NER → guard-model auditor) plus a request-intent classifier that blocks security- and legally-concerning requests at the proxy, before dispatch. An open-source analogue to Claude's Auto Mode classifier: it does not need to match frontier quality — users who need best-in-class safety use Claude through WaddleAI and get both.

WaddleAI is **not** a model host by itself and **not** a coding agent. It provisions and fronts inference (hardened Ollama fleets, llama.cpp, EXO, and — on Professional — Vertex AI/Bedrock endpoints), and it makes existing agents better and cheaper.

### 1.2 Who deploys it and why

| Persona | Deployment | Primary win |
|---|---|---|
| OSS developer / homelab | Single-node K8s (Free tier) | OpenCode + local models with real routing, memory, and caching — a coherent apparatus instead of raw Ollama |
| Engineering team lead | Team cluster (Free/Pro) | One `wa-` key per dev; Claude Code/Cursor/OpenCode all point at WaddleAI; commercial spend drops via cache + local-first routing; shared CodeRAG over team repos |
| Platform/security engineering | Org cluster (Pro/Enterprise) | Central policy: which models exist, who reaches them, what gets blocked/redacted, full usage metering; Cilium-enforced isolation; SSO; KMS + multi-tenancy at Enterprise |

### 1.3 The apparatus story

**OpenCode is the default documented apparatus** — the reference open-source front end, configured via a generated `opencode.json` (custom provider → WaddleAI `/v1`, models from `/v1/models`, MCP entry). The same deployment simultaneously serves:

- **Claude Code** via `ANTHROPIC_BASE_URL` → WaddleAI `/v1/messages` (full fidelity: streaming, tool_use, thinking blocks, prompt-cache passthrough),
- **Cursor / Gemini Antigravity / Continue / anything OpenAI-compatible** via base-URL override,
- **Any MCP client** via the WaddleAI MCP server (code search, docs, memory, routing controls).

Migration promise: pointing an existing tool at WaddleAI is a one-line config change, and removing it is the same line back — no lock-in, immediate value (metering + security on day one, savings as cache/routing warm up).

### 1.4 Open + commercial, together by design

WaddleAI treats commercial ecosystems as first-class escalation targets, not competitors. The canonical flow: a request classified as trivial runs on a local gemma3/granite/mistral model at near-zero cost; a request classified as hard routes to Claude or Codex-class models with the org's key — with WaddleAI's cache having already stripped every redundant token, its memory layer having supplied local context, and its response always reporting which model actually served it (`waddleai.routed_from`; never silent substitution).

### 1.5 Non-goals (v0.2–v0.6)

- Training or fine-tuning models.
- Being a general-purpose API gateway for non-AI traffic (that was MarchProxy; it is deprecated, not migrating here).
- A hosted SaaS control plane — WaddleAI is self-hosted; PenguinTech services are limited to licensing/flags (`license.penguintech.io`).
- Replacing PenguinCode (kept as-is; convergence evaluated in v0.6.x).

---

## 2. Licensing & Model Policy

### 2.1 Code dependencies — strict OSI

All code dependencies MUST carry OSI-approved, commercially unrestricted licenses (MIT, Apache-2.0, BSD, GPL-2.0+, MPL-2.0). Forbidden: AGPL (third-party), SSPL, BUSL, Elastic, RSAL, Commons Clause, CC-BY-NC.

Standing violations to fix in Phase 1:
- `infrastructure/kubernetes/base/redis-deployment.yaml` uses `redis:7-alpine` (RSAL/SSPL) → **Valkey** (`valkey/valkey:8-bookworm`, digest-pinned) everywhere; the Helm chart already complies.
- EXO (GPLv3) is compatible only as an **external orchestrated service** — API calls across a network boundary, no EXO code vendored or linked in-repo.

New-dependency review gate: license check is part of PR review; `pip-licenses` audit added to `make test-security`.

### 2.2 Supply-chain origin policy

**No models or libraries of Chinese origin** (e.g., Qwen, DeepSeek, GLM/ChatGLM, Yi, Kimi, MiniMax — and library equivalents), consistent with the existing PRC supply-chain rule. EU/NORAM-origin components are fine (Mistral, HuggingFace, IBM, Google, Meta, Microsoft, Nomic). Enforced by the model registry (§2.3): registry entries carry `origin` and `license` fields; the deny-list is checked at model registration and at fleet `place_model` time.

### 2.3 Model weights — dual-default pattern

Model weights may use non-OSI-but-commercial licenses (Gemma ToU, Llama Community) **only for runtime-pulled models, never vendored into images**, and every Gemma/Llama default MUST have an Apache-2.0 alternative selectable in config ("dual-default pattern"):

| Role | Default | License | Apache-2.0 alternative | Also selectable |
|---|---|---|---|---|
| Routing classifier | `gemma3:1b` | Gemma ToU | `granite3.3:2b` (IBM) | `phi4-mini` (MIT), `smollm2:1.7b` (Apache-2.0) |
| Security auditor / intent classifier | `shieldgemma:2b` | Gemma ToU | `granite-guardian3:2b` (IBM) | `granite-guardian3:8b` |
| Embeddings | `nomic-embed-text` | Apache-2.0 | — (already Apache) | `mxbai-embed-large` (Apache-2.0) |
| Local generation tiers | org-configured | — | Mistral family (Apache-2.0) | Gemma 3, Llama 3.x (flagged licenses) |

Every model reference in code/config/Helm resolves through a **model registry** (DB-backed, seeded by migration) recording: name, role, license, origin, min VRAM, Ollama tag. No hardcoded model strings outside the registry seed. Weights are pinned by Ollama tag + recorded digest at pull time (logged for reproducibility; Ollama tags are mutable, so the registry stores the resolved digest).

### 2.4 License tiers (enforced via license.penguintech.io)

| Capability | Free | Professional | Enterprise |
|---|---|---|---|
| Physical inference nodes | ≤ 5 | Unlimited (**metered per managed node**) | Unlimited |
| Registered models | ≤ 3 | Unlimited | Unlimited |
| Deployment targets | K8s/local only | + GCP Vertex AI, AWS Bedrock (hybrid) | + same |
| SSO | — | Google SSO/OAuth2 | + SAML 2.0 |
| Data encryption | At-rest via DB/storage layer | same | + external KMS (AWS KMS, GCP Cloud KMS) envelope encryption for provider credentials, memory, cache |
| Multi-tenancy | Single tenant | Single tenant | Multi-tenant (tenant claim isolation per house auth standard) |
| Big-5 + Ollama/llama.cpp routing, exact cache, security layer, CodeRAG, docs cache, MCP | ✅ | ✅ | ✅ |
| Support | Community | Standard | Premium (tiers priced separately) |

**Professional metering model (dual):**
- **Per identity/seat** — covers the proxy and user-facing features: authenticated identities (SSO users + named `wa-` key owners) counted monthly; seat count reported to the license server by Management.
- **Per managed node** — covers deployment management: each node WaddleAI actively manages as an inference target (K8s fleet nodes, GCP Vertex AI endpoints, AWS Bedrock provisioned endpoints — cloud endpoints count as nodes) counted monthly.
- A customer using only the proxy (no fleet management) pays seats only; a customer using WaddleAI purely as fleet automation pays nodes only.

**Tier name mapping**: the license server models tiers as `community < professional < enterprise`. WaddleAI's **Free = `community`**, **Professional = `professional`**, **Enterprise = `enterprise`**. Tier is per-product on the license (`licenses.products = {"waddleai": "professional"}`).

Enforcement points (full integration contract in §14.6):
- **Node/model caps (Free/`community`)**: checked in fleet backend `provision`/`place_model` and at model-registry registration; over-cap → clear error naming the tier limit. Counting semantics (Q#7 confirmed): *physical node* = distinct K8s node (by node UID) running ≥1 fleet pod, external nodes counted by registered endpoint; *model* = distinct model-registry entry with an active placement — **utility models (guard, routing classifier, embeddings) excluded** from the 3-model cap so the free stack's own machinery doesn't consume it.
- **Pro/Enterprise features**: Google SSO, `vertex_ai`/`bedrock` fleet-backend creation, per-model/tool security scoping, KMS key-ring config, tenant creation — each gated by `LicenseClient.check_feature("<feature>")` (`penguin-licensing` SDK) **plus** its PostHog flag.
- **Metering**: seat and managed-node counts reported to the license server via the periodic **checkin** payload (`usage: {users, nodes}`), which the server upserts into `entitlement_usage` and overage-checks (§14.6).
- **Graceful degradation** (house standard + SDK behavior): license server unreachable → SDK's cached entitlement (5-min TTL) honored, then fail-safe to `community`/flag-OFF; never crash. Domain bypass by host suffix (`*.penguincloud.io`, `*.penguintech.cloud`) only — never env var.
- Feature flags are orthogonal: every feature also behind a self-hosted PostHog flag `waddleai.{feature}` (default OFF at launch), evaluated with the standard `posthog-python` SDK against `POSTHOG_HOST` (§14.5).

### 2.5 Third-party content in the docs cache

Cached language documentation (§9) is redistributed to users: MDN and cppreference are CC-BY-SA (serve with attribution + license notice in MCP resource metadata and injected-context provenance headers); official Python/Rust/Go/Node/Ruby docs are permissively licensed (PSF, MIT/Apache, BSD, MIT, Ruby). The fetcher respects robots.txt and per-source rate limits; a per-source license table ships in §9.

---

## 3. Target Architecture Overview

### 3.1 Components (end state, v0.5.x)

```
                        ┌──────────────────────────── Cilium Kubernetes ───────────────────────────────┐
                        │                                                                              │
 Claude Code ──────┐    │  ┌─ Gateway API (Cilium) ─┐     ┌───────── WaddleAI AIProxy (Quart) ───────┐ │
 OpenCode ─────────┤    │  │ HTTPRoute + TLS        │     │ /v1/chat/completions  /v1/messages       │ │
 Cursor/Antigravity├──►─┼──┤ CiliumEnvoyConfig      ├──►──┤ /v1/models  /mem0/*  /mcp (HTTP)         │ │
 OpenAI SDKs ──────┤    │  │ rate-limit (RPM @edge) │     │ ProxyPipeline:                            │ │
 MCP clients ──────┘    │  └────────────────────────┘     │  auth → token/budget → security → cache  │ │
                        │                                 │  → routing → memory → dispatch → meter   │ │
                        │                                 └──────┬──────────────────────┬────────────┘ │
                        │                                        │ local dispatch       │ commercial   │
                        │  ┌── Inference Fleet ────────────────┐ │                      │ dispatch     │
                        │  │ Ollama DaemonSet (hardened img)   │◄┘                      ▼              │
                        │  │ llama.cpp / EXO(ext) / VertexAI / │            OpenAI · Anthropic ·       │
                        │  │ Bedrock backends (Pro)            │            Gemini · xAI · Bedrock     │
                        │  │ ClusterIP-only + CNP (proxy-only) │                                       │
                        │  └───────────────────────────────────┘                                       │
                        │                                                                              │
                        │  ┌─ Management (Quart) ─────────────┐   ┌─ Data stores ────────────────────┐ │
                        │  │ /api/v1: users/orgs/keys/quotas/ │   │ Postgres 16 + pgvector           │ │
                        │  │ providers/fleet/routing/security │   │  (usage, registry, memory,       │ │
                        │  │ Cilium policy reconciler (CRDs)  │   │   coderag, docs cache, policies) │ │
                        │  │ CodeRAG indexer · docs fetcher   │   │ Valkey (exact cache, counters,   │ │
                        │  │ license/flag client              │   │   classifier cache, policy cache)│ │
                        │  └──────────────────────────────────┘   └──────────────────────────────────┘ │
                        │   Tetragon TracingPolicies · CiliumNetworkPolicy · admission policies (opt)  │
                        └──────────────────────────────────────────────────────────────────────────────┘
                                     WebUI (React, separate container) · waddleai-mcp stdio shim (client-side)
```

Four first-party containers: **AIProxy** (data plane, the `proxy/` container), **management** (control plane), **webui**, **docs-site** — plus fleet images WaddleAI provisions (hardened Ollama, llama.cpp). Both Python services: Quart + hypercorn, Python 3.13, penguin-dal runtime / SQLAlchemy+Alembic schema, penguin-aaa auth.

### 3.2 The request pipeline (data plane)

Both `/v1/chat/completions` and `/v1/messages` execute the **same `ProxyPipeline`** (parity is a Phase 1 bug fix — today `/v1/messages` skips filtering and memory):

| Stage | What | Backed by | Phase |
|---|---|---|---|
| 0. Edge | TLS, RPM rate-limit rejection | Cilium Gateway + CiliumEnvoyConfig (Management-reconciled) | 1 |
| 1. Auth | OIDC JWT / `wa-`/`sk-` keys, org+tenant resolution | penguin-aaa, existing | — |
| 2. Token/budget gate | TPM counters, monthly token/$ budgets (what Cilium can't see) | Valkey + `token_usage` aggregates | 1 |
| 3. Security in | Tier 1–3 filters + intent classifier + tier-4 auditor per resolved policy | `shared/security/*`, guard model via Ollama | 1 (parity) / 3 (v2) |
| 4. Cache | Exact (Valkey) → semantic (pgvector) lookup; upstream prompt-cache orchestration on miss | `shared/cache/response_cache.py` | 2 |
| 5. Routing | Explicit → heuristics → classifier cascade; escalation policy; session affinity | `shared/routing/engine.py` | 2 |
| 6. Memory | Client-type detection → MCP-pull (no-op here) or budgeted context injection | memory/CodeRAG/docs stores | 3 |
| 7. Dispatch | Provider connector (big-5, Ollama, llama.cpp, fleet endpoints), SSE passthrough, retries, circuit breaker | `shared/utils/llm_connectors.py` | 1 |
| 8. Security out | Output filtering per policy (closes the output-guardrail gap) | same as stage 3 | 3 |
| 9. Metering | Sole writer to `token_usage`; cache/injection/savings accounting; Prometheus | existing `usage_tracker`, extended | 1 |

Design rules: each stage is a class with `async def __call__(ctx) -> ctx`, feature-flag aware, and independently testable; endpoints differ only in format translation at the boundary; **stages are ordered cheapest-first** — edge rejection, then Valkey counter checks, then regex/NER filters, with guard-model inference and provider dispatch last, so expensive work is never spent on a request a cheaper gate would refuse. Response `usage` gains an additive-only `waddleai` object (`cache`, `cached_tokens`, `tokens_saved`, `injected_tokens`, `routed_from`).

### 3.3 Control plane

Management (Quart) owns configuration and reconciliation, never sits in the request path:
- CRUD for orgs/users/keys/quotas/providers/model registry/routing policies/security policies/fleet backends/cache config.
- **Cilium policy reconciler**: renders CiliumEnvoyConfig rate-limit + CiliumNetworkPolicy CRDs from DB settings; RBAC-scoped ServiceAccount; no-ops gracefully when Cilium CRDs absent.
- **Indexing workers**: CodeRAG repo indexer, docs-cache fetcher (async jobs).
- License/flag client (`license.penguintech.io`), seat accounting (Pro), KMS envelope encryption (Enterprise).

Proxy reads config from Postgres with Valkey-cached hot paths (policy resolution, routing rules, registry) invalidated on Management writes — no Management→Proxy runtime RPC needed for config. Service-to-service calls that do exist use gRPC per house standard (protos in-repo under `proto/waddleai/`, replacing vendored MarchProxy stubs).

### 3.4 Data stores

- **Postgres 16 + pgvector** (single instance, per-service DB accounts): all durable state — 33 existing tables plus new (`model_registry`, `routing_policies`, `routing_rules_v2`, `model_aliases`, `security_policies`, `cache_configs`, `response_cache_entries`, `code_repos`, `code_chunks`, `docs_cache_pages`, `fleet_backends`); ledger in §13. Read-replica pool already supported for memory reads.
- **Valkey 8** (per-service ACL users): exact response cache, TPM/budget counters, classifier + policy resolution caches, session-affinity map. Nothing durable — Valkey loss degrades performance, never correctness.
- **No ChromaDB/Supabase/Qdrant in the default path** — pgvector is the standard backend; others remain optional adapters in `rag_integration.py`.

### 3.5 Language, runtime & scale strategy

**Design scale target: 1,000 developers × 10 concurrent agent sessions = 10,000 active sessions**, ≈ 500–1,500 req/s sustained, 5,000–10,000 concurrent in-flight SSE streams (agents spend most wall-clock waiting on model output, so in-flight fraction is high).

- **AIProxy stays Python 3.13 / Quart.** The workload is I/O-dominated (proxy overhead ~2–5ms vs 0.5–60s upstream latency); the expensive stages are Python-ecosystem captive (Presidio/spaCy, tiktoken, embeddings, provider SDKs); the CPU-hot internals (tiktoken, tokenizers, pydantic-core, orjson) are already Rust cores; and the AILB merge is Python→Python.
- **Scale guardrails (load-bearing requirements, not suggestions):**
  1. **Nothing CPU-heavy on the event loop** — tier-3 NER runs in a `ProcessPoolExecutor`/dedicated worker pool; guard-model calls are async network I/O; JSON via orjson; uvloop enabled.
  2. **Stateless proxy pods** — all counters/caches/affinity in Valkey, durable state in Postgres (this is why the AILB's in-memory rate counters are rewritten during migration). Scaling = HPA replicas; multiple hypercorn workers per pod.
  3. **Batched metering writes** to Postgres (per-second aggregation, not per-request rows at 1K req/s).
  4. **Security-stage capacity is policy-scoped by design** — cheapest-gates-first plus sampling/scoping keeps guard-model GPU and NER CPU (the true dominant costs at scale, in any language) bounded.
- Deployment envelope at target scale: ~6–12 AIProxy pods (4 hypercorn workers each), Valkey at ~5–10K ops/s (trivial), guard/NER capacity sized by security policy scope.
- **Rust**: the client-distributed `waddleai-mcp` stdio shim is written in Rust (static binary on dev machines, no Python runtime — §11). A **Rust app-core** (dispatch/streaming fan-out/cache lookup only — never Cilium-owned functions) is a v0.6.x evaluation item with explicit triggers: sustained >1,500 req/s per deployment, proxy CPU (not upstream) >30% of p95 latency, or on-prem footprint requirements. eBPF/XDP acceleration is consumed via Cilium (XDP kube-proxy replacement/LB, sockops/sockmap same-node bypass for AIProxy→fleet traffic, bandwidth manager) — never hand-rolled in-app.

### 3.6 Security posture (cross-cutting)

- Known AI-specific risks tracked from prior audit and addressed in-spec: **output guardrails** (stage 8), **indirect prompt injection via memory** (retrieved context is provenance-tagged and scanned by stage-3 filters before injection, §9), **semantic-cache poisoning** (semantic cache is org-scoped, opt-in, and entries are keyed to post-filter content, §6).
- Org boundary is the hard isolation wall for cache/memory/CodeRAG; tenant claim boundary above it at Enterprise.
- Fleet pods are unauthenticated by nature (Ollama) → reachable only from proxy pods via CNP; never exposed via Service type other than ClusterIP.
- Everything rootless, digest-pinned, Debian bookworm per house container standards; Tetragon policies (optional) block exec/unexpected egress in proxy and fleet pods.

---

## 4. Phase 1 — Consolidation & Platform Hygiene

**Goal**: one management plane, one K8s tree, one deployable chart, license-clean infrastructure — before any new feature lands on it.

### 4.1 Golden contract snapshots (do this first)

Before touching any code, capture request/response contract snapshots for every public surface: `/v1/chat/completions`, `/v1/messages`, `/v1/models`, `/mem0/*`, and all `/api/v1/*` management routes (status codes, response shapes, auth behavior, error formats). These snapshots are the merge gate for the entire phase: they must pass unchanged against the migrated services. Store under `tests/contract/` with a `make test-contract` target.

### 4.2 Flask → Quart management migration

`services/management/` migrates from Flask to Quart (house standard; API-compatible — `quart.Blueprint`, `g`, `jsonify` are drop-ins):
- Blueprint-by-blueprint conversion of `app/api/v1/*` to `async def` handlers; blocking DB calls wrapped per house async rules until penguin-dal async paths are wired.
- Server: hypercorn (replaces any gunicorn/werkzeug invocation); Dockerfile CMD updated.
- Auth: penguin-aaa OIDC middleware (same stack the proxy already uses) replaces any Flask-specific auth glue.
- Alembic migrations, SQLAlchemy models, and penguin-dal runtime usage unchanged.

### 4.3 Retire the legacy FastAPI plane

`management/apps/management_server/` (FastAPI + HTML admin templates) is deleted. Anything it does that `services/management/` + WebUI don't: audited first — known gaps to port before deletion: routing-config and memory-config admin surfaces (move to WebUI against `/api/v1` endpoints), MCP server start/stop (superseded by MCP v2 in §11; per Q#5, the old WebSockets MCP server under `management/apps/mcp_server/` has **no external consumers** — the VS Code extension is pure REST — so it is deleted here with the legacy plane, no compat window). The React WebUI becomes the only admin UI.

### 4.4 One Kubernetes tree

- `k8s/` is canonical: Helm chart (beta/prod) + Kustomize overlays (alpha). `infrastructure/kubernetes/` is deleted after an object-by-object parity check (its configmap/ingress content merged into `k8s/` where still relevant).
- **Helm chart gains the proxy** (today it deploys management/webui/postgres/redis/ollama but not the proxy): `proxy-deployment.yaml`, `proxy-service.yaml`, HTTPRoute rules for `/v1/*` and `/mem0/*`, values block with resource tiers per house standard.
- **Valkey everywhere**: `redis-*.yaml` templates renamed/replaced with `valkey-*` (`valkey/valkey:8-bookworm`, digest-pinned); the `redis:7-alpine` reference in the old tree dies with the tree; env var naming moves to `CACHE_*` per house standard with `REDIS_URL` kept as a deprecated alias for one release.
- Digest-pinning audit across all Dockerfiles/manifests; Python images to `python:3.13-slim-bookworm`.

### 4.5 Dependency hygiene

`requirements.in` → `uv pip compile --generate-hashes` for every service; drop unused deps discovered in the licensing survey; add `pip-licenses` gate to `make test-security`; single root `constraints` strategy so proxy/management share pinned versions of shared libs.

**Acceptance**: contract snapshots green; `make smoke-test` against a clean alpha deploy (Kustomize) and `helm template` golden files; zero references to `flask`, `fastapi` (outside PenguinCode), `redis:` images, or `infrastructure/kubernetes/` remain.

## 5. Phase 1 — AIProxy: AILB Migration & Data-Plane Completion

**Naming**: the WaddleAI data-plane service (the `proxy/` container) is the **AIProxy**. "AILB" below refers only to the historical MarchProxy module being absorbed.

### 5.1 Source of truth

Migrate from marchproxy **`origin/release/v0.2.x`** (latest non-dependabot branch, 2026-05-26). Survey findings: the AILB is **Python 3.12/FastAPI** (not Go) — 25 files, ~2,700 LOC app + ~2,750 LOC tests, all-OSI deps, no PRC-origin packages, no Postgres dependency, Redis optional (duck-typed client), local ChromaDB for memory/RAG. Its app code is *identical* to `main` (the release branch adds only build fixes and the Valkey chart swap), and it was originally ported **from** WaddleAI — so this is a Python→Python re-merge into the Quart AIProxy, not a rewrite or a vendoring exercise. Files are copied with provenance recorded in commit messages (`migrated-from: marchproxy@<sha>`).

### 5.2 Component disposition

| marchproxy `proxy-ailb/` component | Disposition | Notes |
|---|---|---|
| `app/tokens/token_manager.py` (+ 979-LOC test) | **Port** | Highest-value file: normalized-token/cost model + quota logic merges with `usage_tracker`; `DEFAULT_CONVERSION_RATES` becomes seed data for `token_conversion_rates`. **Rewrite required**: its in-memory fixed-window per-minute counters (thread-locked dict — unsafe across replicas) → Valkey atomic counters (Lua) for TPM/budget; RPM moves to the Cilium edge (§5.3) |
| `app/router/intelligent.py` | **Merge** | Sibling of WaddleAI's `shared/utils/request_router.py` (common ancestor). Diff both; keep the superset of the six strategies + `ProviderStats` EMA. The crude breaker (3-failure trip, 5-min cooldown) is upgraded per §5.3.4 — half-open probe, retryable-errors-only, Valkey-backed. Output feeds the §7 unified engine |
| `app/security/prompt_security.py` (481 LOC + 820-LOC test) | **Merge & wire** | `THREAT_PATTERNS` corpus (injection/jailbreak/data-extraction/prompt-leak/credential-harvesting) + strict/balanced/permissive policy tiers merge into `shared/security/prompt_security.py` — and get **wired into ProxyPipeline stage 3** (MarchProxy never wired it into its request path) |
| `tests/` (~2,750 LOC) | **Port** | Strongest asset; adapt to WaddleAI pytest layout alongside their modules |
| `app/auth/rbac.py` (742 LOC + 957-LOC test) | **Partial** (Q#11 resolved) | penguin-aaa OIDC scopes wholly authoritative; `Permission` enum salvaged as scope vocabulary (resource:action catalog), Role bundles inform scope-bundle definitions; HMAC key code/tests dropped except where they cover `wa-` key hashing |
| `app/memory/conversation.py`, `app/rag/retrieval.py` | **Drop code, salvage config** | ChromaDB local-file stores lose to WaddleAI's pgvector backend. Salvage: system-prompt injection templates, 0.7 relevance cutoff, top-3 defaults — as seeded config |
| `app/providers/{openai,anthropic,ollama}.py` | **Drop** | Thin SDK wrappers with **no streaming**; WaddleAI's connectors are the base, extended in §5.3 |
| `main.py` (FastAPI), `app/grpc/server.py` (NLB ModuleService), Dockerfile, docker-compose | **Drop** | Framework glue; NLB-specific gRPC; `/metrics` and `/healthz` were stubs anyway |

Valkey preference applied: migrated code's optional-Redis persistence lands on the existing Valkey deployment; ChromaDB is eliminated from the default path entirely (pgvector for vectors, Valkey for counters/hot state).

### 5.3 Data-plane completion (net-new work)

1. **RPM at the Cilium edge**: Management's policy reconciler (`services/management/app/services/cilium_policy.py`) renders CiliumEnvoyConfig `local_ratelimit` + CiliumNetworkPolicy from DB settings; RBAC-scoped ServiceAccount; no-op when CRDs absent. **Per-org limits at the edge, per-key enforcement in the token gate** (avoids CRD churn on key rotation) — Q#10 resolved.
2. **Token/budget gate in AIProxy** (`shared/utils/token_limiter.py`): Valkey Lua counters for TPM + monthly token/$ budgets; streaming reserve-at-submit, reconcile-at-completion; budget aggregates cached 60s.
3. **Big-5 dispatch completion** in `shared/utils/llm_connectors.py`: add **Gemini, xAI, Bedrock** connectors (existing: OpenAI, Anthropic, Ollama, llama.cpp); **SSE streaming passthrough for every connector** (the migrated AILB had none); per-provider **configurable timeouts** (today: none at all on the SDK connectors, hardcoded 300s on Ollama/llama.cpp).
4. **Failure taxonomy, retries & circuit breaker** (design reference: [`llm_api_resilience`](https://github.com/Inozem/llm_api_resilience), MIT — patterns only; the library itself is sync-only/no-streaming and unusable as a dependency):
   - **Typed provider errors** replace the current bare `Exception` collapse: `ProviderTimeoutError | ProviderRateLimitError | ProviderServerError | ProviderClientError`, mapped from real status codes in each connector. Only the first three are **retryable**; client-side errors (auth, 4xx, schema) surface immediately — never retried, never failed over, and never counted by the breaker (today a client-caused provider 400 counts toward ejecting a healthy backend).
   - **Retries**: per-provider `max_attempts` + jittered exponential backoff, applied to retryable errors only; injectable clock/sleeper so retry and breaker tests are deterministic instead of sleep-based.
   - **Circuit breaker** upgraded from the AILB's trip-and-cooldown (3 failures / 5 min, cold re-entry) to closed → open → **half-open with a single reserved probe** (no thundering herd on recovery), keyed per (provider, model), state in **Valkey** so 3+ replicas agree; the in-process `ProviderStats` copy remains as a local fast-path hint.
   - **Attempt-history observability**: on exhaustion, the surfaced/logged error carries the ordered attempt summary — `route [provider/model] → ErrorType` — never API keys, request bodies, or raw provider messages. Failure metrics actually emitted: `waddleai_llm_requests_total{status="error"}` on every failed dispatch, and the request counter records the real HTTP status (today hardcoded to 200 in the handler's `finally`).
5. **Metering**: AIProxy is the sole writer to `token_usage`, batched per-second at scale (§3.5); AILB webhook ingest deleted.
6. **TLS** at the Cilium Gateway (HTTPRoute exists); optional hypercorn certfile for bare-metal; cert-manager documented, not built.

### 5.4 Pipeline parity (bug fix)

Both `/v1/chat/completions` and `/v1/messages` execute the shared `ProxyPipeline` (§3.2) — today `/v1/messages` skips content filtering and memory injection. Includes a Claude Code fidelity audit: streaming, tool_use, system arrays, thinking blocks, prompt-cache passthrough, and `/v1/messages/count_tokens` (add if missing).

### 5.5 gRPC & protos

Keep the AIProxy gRPC server skeleton (house standard, port 50051) but: define protos **in-repo** under `proto/waddleai/`, delete vendored `grpc_proto/marchproxy/` stubs, rewrite `scripts/generate_proto.sh` with no `~/code/marchproxy` dependency. Note: `waddleai_pb2`/`media_pb2` have **no source `.proto` in marchproxy** — their definitions are recovered from the vendored stubs or redefined fresh; `module/nlb/types` protos are MarchProxy-NLB-specific and die.

### 5.6 WaddleAI-side deletion inventory

| Artifact | Action |
|---|---|
| `services/management/app/services/marchproxy_config.py`, `provider_sync.py` | Delete (providers consumed directly from DB by AIProxy) |
| `services/management/app/grpc/client.py` + `app/grpc/proto/marchproxy/` | Delete (was stubbed anyway) |
| `services/management/app/api/v1/ailb.py`, `ailb_memory.py` | Delete; memory/RAG/embedding config endpoints re-home to `/api/v1/memory-config` |
| `api/v1/webhooks.py` AILB ingest portion | Delete |
| `MARCHPROXY_AILB_*` env (config.py, configmaps, Helm hardcoded block) | Delete |
| Tables `marchproxy_ailb_sync`; `virtual_keys.ailb_*` columns | Drop (migration 007) |
| Tables `ailb_usage_events`, `ailb_usage_records` | **Fold into `token_usage`** with `source='ailb_import'`, then drop — Q#1 resolved (preserves billing/dashboard continuity) |
| Tests `test_marchproxy_config.py`, `test_ailb_routes.py`, AILB parts of webhook/usage tests | Delete/rewrite against native equivalents |

### 5.7 Schema (migration 007)

Drop/fold per §5.6; add `virtual_keys.rpm_limit`, `tpm_limit`, `budget_monthly_tokens`, `budget_monthly_usd` (nullable = unlimited); seed `token_conversion_rates` from the migrated `DEFAULT_CONVERSION_RATES`. Round-trip + downgrade tested on a seeded snapshot.

### 5.8 Acceptance

Contract snapshots green; Claude Code completes a streamed tool-use turn via `/v1/messages`; OpenAI SDK streaming via `/v1/chat/completions`; both endpoints traverse identical pipeline stages (stage-log assertion); rate limits enforced at edge (Cilium) and gate (Valkey) under parallel load at the limit boundary; provider-error taxonomy tests (timeout/429/5xx retried with backoff then failed over, 4xx surfaced immediately with breaker untouched); breaker half-open single-probe verified under concurrent callers (exactly one probe passes during recovery); failed dispatches visible in metrics with real status codes; zero `marchproxy` references outside historical migration notes; ported AILB tests green in the WaddleAI suite; scale smoke: 1K concurrent streamed requests through one pod without event-loop stalls (p99 proxy overhead < 50ms).

## 6. Phase 2 — Prefix & Semantic Cache

Pipeline stage 4 (`shared/cache/response_cache.py`), sitting after security-in, before routing. Three layers, cheapest lookup first. Flag: `waddleai.response_cache`.

### 6.1 Exact cache (Valkey)

- **Key**: SHA-256 of (org_id, resolved route/model class, normalized messages array, tools schema, temperature, top_p, max_tokens). Value: full response JSON + usage. TTL 24h default, per-org/key configurable; LRU bounded by `max_entry_kb` and per-org memory quota.
- **Eligibility**: deterministic requests only (`temperature == 0`), no tool-call results in messages. **Default ON** for eligible requests (byte-identical inputs → serving the identical response is safe); per-org/per-key disable.
- **Streaming hits** replay as synthetic SSE chunks so streaming clients are indistinguishable from a miss.
- **Isolation**: entries are org-scoped, written only from post-security-filter content (poisoning defense, §3.6); org boundary is absolute.

### 6.2 Semantic cache (pgvector, restricted — Q#3 resolved)

- Table `response_cache_entries(id, org_id, scope_key, model_class, prompt_embedding vector(768), context_hash, response jsonb, hit_count, created_at, expires_at)`, HNSW index. Embeds the last user message + rolling hash of prior context.
- **Default OFF.** When enabled: eligible only for single-turn/last-turn-only requests with no tools, no memory injection, `temperature == 0`, and router-classified informational/Q&A. Threshold 0.95 cosine, per-org tunable. Class list expands only with hit-quality telemetry.

### 6.3 Upstream prompt-cache orchestration (the biggest token win)

- **Anthropic** (Q#2 resolved): auto-inject `cache_control: {type: "ephemeral"}` breakpoints on stable prefixes >1024 tokens observed ≥2× (prefix hashes tracked per virtual key in Valkey). **Default ON**, per-org toggle; billing-profile change documented (writes 1.25x once, reads 0.1x). Client-supplied `cache_control` always passes through untouched.
- **OpenAI**: caching is automatic upstream — surface `cached_tokens` from provider usage into `usage.waddleai`.
- **Gemini**: explicit CachedContent created for repeated large prefixes above threshold; lifecycle (TTL, deletion) managed by the cache module.
- **Ollama/llama.cpp**: KV-cache reuse via **session-affinity routing** — same conversation/prefix hash → same fleet pod (affinity map in Valkey; honored by §7 dispatch).

### 6.4 Accounting

`usage.waddleai` gains `{cache: exact|semantic|upstream|miss, cached_tokens, tokens_saved}` (additive-only); `token_usage` gains `cache_status`, `tokens_saved`; dashboard panel showing hit rates and $ saved per org/key. New table `cache_configs(scope_type, scope_ref, exact_enabled, semantic_enabled, semantic_threshold, ttl_seconds, max_entry_kb, anthropic_cache_control)`.

### 6.5 Acceptance

Determinism-eligibility matrix tests; streaming replay byte-equivalence; TTL expiry; **org-isolation test treated as a security test** (org A can never see org B's entries); semantic should-hit/should-miss labeled corpus with threshold regression; `cache_control` injection verified against recorded Anthropic responses (cached_tokens actually reported); flag-off tests prove zero behavior change.

## 6A. Proxy Memory & Context-Efficiency Layers

Beyond the response cache (§6), the AIProxy retains several lazy-loaded, query-on-demand memories that shrink what actually reaches a model — the token-efficiency pillar made concrete. All are org-scoped, provenance-tagged, and pass §9.6 injection-safety. Flag: `waddleai.proxy_memory`.

### 6A.1 Session scratchpad (working set)

A per-session key/value store the agent writes intermediate artifacts into and recalls by key — so long agent loops stop re-sending large context every turn. Backed by Valkey (hot, session-TTL) spilling to Postgres for durability. Exposed as MCP tools (`scratchpad_put(key, value)`, `scratchpad_get(key)`, `scratchpad_list`) for MCP-capable clients; for plain clients, an opt-in convention (`X-WaddleAI-Session`) lets the proxy substitute a stored blob for a reference marker. This is the biggest structural saver: an agent stores a 20k-token analysis once and later references it by key instead of resending it. Isolation is per (org, session, user).

### 6A.2 Rolling conversation summarization

When a conversation's token count crosses a threshold (per-key configurable), older turns are distilled into a running summary while full turns stay in the store. The proxy injects `summary + recent-N turns` instead of the full history, keeping long sessions inside the context window cheaply. Summarization uses the §7.1 `summarize` tool-type model assignment (a cheap local model by default). Guardrails: summarization is opt-in per key, the compaction ratio and recent-turn count are tunable, and the original turns remain retrievable (via scratchpad/memory) so nothing is lost — only what's *injected* is compacted. Every summarized request notes `usage.waddleai.summarized: true` and the tokens elided.

### 6A.3 Embedding & retrieval-result cache

- **Embedding cache**: embeddings keyed by (model, content-hash) in Valkey→Postgres — identical chunks/queries (common across CodeRAG re-indexing, repeated docs lookups, duplicate user queries) are never re-embedded. Cuts embedding compute directly.
- **Retrieval-result cache**: RAG/CodeRAG/docs search results keyed by (query-hash, corpus-version, top-k) with a short TTL — repeated identical queries within a session skip the vector search entirely. Invalidated when the underlying corpus version bumps (CodeRAG re-index, docs re-fetch).

### 6A.4 Tool-schema / system-prompt dedup store

Agents resend identical large tool schemas and system prompts every turn. The proxy stores a canonical copy keyed by content-hash per (org, session) and:
- feeds the §6.3 upstream prompt-cache logic (these stable blocks are exactly the >1024-token prefixes that get `cache_control` breakpoints / Ollama KV affinity);
- **elides intra-request duplication** — the same file or block pasted multiple times in one request is de-duplicated to a single copy plus references, before dispatch and before token counting;
- backs a **tokenizer-length cache** (token counts of stable prefixes keyed by hash) so huge unchanged contexts aren't re-tokenized every turn.
Savings surface in `usage.waddleai.tokens_saved` alongside cache savings.

### 6A.5 Storage, config & accounting

New tables: `session_scratchpad(session_id, org_id, user_id, key, value, expires_at)`, `conversation_summaries(conversation_id, org_id, summary, covers_through_turn, updated_at)`, `embedding_cache(model, content_hash, embedding vector(768), created_at)`. Retrieval-result and tokenizer caches are Valkey-only (derivable). Per-key config block `proxy_memory: {scratchpad, summarization: {enabled, threshold_tokens, keep_recent, ratio}, embedding_cache, schema_dedup}`. All savings roll into the single additive `usage.waddleai` object.

### 6A.6 Acceptance

Scratchpad put/get/list round-trip + per-(org,session,user) isolation (security test); summarization threshold trigger + original-turn retrievability + injected-token reduction measured; embedding-cache hit avoids re-embed (call-count assertion) + corpus-version invalidation; schema-dedup elides a doubly-pasted block and reduces counted tokens; tokenizer-length cache correctness vs fresh count; injection-safety on all recalled content; flag-off = no memory layers active, behavior unchanged.

## 7. Phase 2 — Unified Smart Routing

One DB-driven engine (`shared/routing/engine.py`), in-process in the AIProxy (pipeline stage 5), replacing the three disjoint systems (hardcoded `model_configs` dict, Valkey NL `routing:instructions`, standalone `routing_matrix`). Flag: `waddleai.smart_routing`.

### 7.1 Decision framework — "best model for the tool job"

Two co-equal decision surfaces, composed with org policy:

1. **Tool-type model assignments (the admin's steering wheel).** A `model_assignments` table (evolves the existing `routing_matrix`) maps each tool type to a default model and optional escalation model — e.g., `research → gemma3:4b`, `command-run → claude-haiku ⤴ claude-sonnet`, `code-gen → local code model ⤴ claude-sonnet`. **WaddleAI's internal functions are pre-declared rows in the same table** (`security-audit → shieldgemma:2b`, `routing-classifier → gemma3:1b`, `embeddings → nomic-embed-text`, `docs-fetch/summarize`) — one WebUI screen ("Model Assignments") configures the whole deployment's model-per-job matrix; the §2.3 dual-default pattern is its seed data. Scopes: global defaults overridable per org.
2. **Capability matching (co-equal — it can veto).** Every request derives a requirements vector (min context from token count, `needs_tools`, `needs_vision`, structured-output, complexity when classified); every registry model carries offers (`capability_score 1–5`, `supports_tools/vision`, `context_window`, cost, `location: local|commercial`, live fleet state). If the assigned model **fails a hard requirement** (images to a text-only model, context overflow), capability matching **vetoes and re-routes** to the best qualified candidate rather than failing the request — the veto is logged in the decision trace and surfaced to the admin (assignment misconfiguration warning, also checked at save time). Capability matching alone also decides tool types with no assignment row and arbitrates escalation.

Org **policy filters and sorts** on top of both: allow-lists and tier caps filter; mode (`local_only | local_first | commercial_only | cost | latency`) sorts the qualified candidates — the sorted list *is* the fallback chain, so failover never lands on a model that couldn't serve the request.

**Tool-type determination** (cheapest first): explicit — `X-WaddleAI-Tool-Type` header, the invoked MCP tool implies it, or a `model: "waddleai/<tool-type>"` alias; else stage-1 heuristics (tool names present, endpoint, modality); else the stage-2 classifier, whose *primary* output is now `tool_type` (plus `{complexity: 1-5, domain, needs_reasoning}`), cached in Valkey by prefix hash. Classifier models per §2.3 dual-default: `gemma3:1b` default, `granite3.3:2b` Apache alternative, `phi4-mini`/`smollm2:1.7b` selectable.

### 7.2 Decision cascade (cheapest first)

- **Stage 0 — explicit model.** Client named a concrete model → resolve through `model_aliases`, honor exactly, subject to org allow-lists and the capability veto. **Admin-controlled aliasing**: alias rules (`gpt-4o` → local `mistral-large`; `claude-*` → policy X) are the migration lever that localizes existing tools without touching client config. Redirects always visible in `waddleai.routed_from`.
- **Stage 1 — heuristics** (<1ms, no LLM): `routing_rules_v2(priority, match jsonb, action jsonb)` — determines tool type / route from cheap signals; target ~70% of `auto` requests.
- **Stage 2 — classifier** (only when heuristics punt): structured JSON → assignment lookup → capability check.

### 7.3 Org routing policy, escalation & pressure signals

`routing_policies` per org: `mode`, `escalation_threshold`, `escalation_target`, `classifier_prompt` (absorbs the old NL routing-instructions UX), `de_escalation (never | idle_reset | task_detect)`, `sensitivity_routing`, `budget_pressure_enabled`, `provider_failover (off | same_class | any_qualified)`.

**Escalation triggers under `local_first`** (any one suffices): (1) classifier complexity ≥ org threshold; (2) local route unhealthy/overloaded (breaker open, no fleet endpoint has the model, queue depth exceeded); (3) failure/retry signals (malformed tool calls, client re-sent same prompt, N consecutive error-ish turns); (4) explicit hint — `X-WaddleAI-Escalate: true` or `auto:high` suffix (`auto:low` = manual reset). Escalation goes to the assignment row's `escalation_model` when set, else the org's `escalation_target`.

**Availability failover (cross-provider substitution)** — the "Anthropic is down → serve from Gemini" path, distinct from complexity escalation:
- **Trigger: retryable errors only** (§5.3.4 taxonomy — timeout/429/5xx/breaker-open, after the provider's own retry budget is exhausted). Client-caused errors never substitute — a bad request must not be replayed to a second provider and billed twice.
- For `auto`/alias-routed requests the mechanism is already the §7.1 design: the policy-sorted qualified-candidate list **is** the chain — on retryable failure the dispatcher advances to the next candidate, which may be a different provider.
- For **Stage-0 explicit-model requests** (client named `claude-sonnet-4`), substitution is a data-governance event, not a routing nicety — redirecting a request destined for one provider to another changes where org data flows. Gated by `provider_failover`: **`off` (default)** — fail fast with the taxonomy error; `same_class` — substitute only along the ordered `fallback_models` list on the model's `model_assignments` row (admin-curated equivalents, e.g. `claude-sonnet-4 → [gemini-2.5-pro, gpt-4o]`); `any_qualified` — the full capability-qualified candidate list. Per-org, default off.
- **Fidelity boundary**: requests carrying provider-specific features that don't translate (thinking blocks, `cache_control`, strict tool-schema variants) fail fast rather than degrade silently — capability matching keeps incompatible targets out of the chain.
- **Streaming boundary**: substitution is legal only before the first byte reaches the client; once streaming has begun the request is committed and the error surfaces.
- Every substitution is reported in `waddleai.routed_from` and the §7.4 decision trace with cause `provider_unavailable`. Sensitivity routing still binds: a `local_only`-clamped request never substitutes to a commercial provider regardless of `provider_failover`.

**Session behavior**: sticky after escalation (Valkey flag + TTL); **`de_escalation: idle_reset` default** — reset to local-first on idle gap (≥10 min, org-tunable) or new-conversation signal; `never` = pure sticky; `task_detect` ships in a later release built on real traffic data.

**Sensitivity-aware routing** (resolved): `sensitivity_routing: local_only | redact_then_any | ignore` — stage-3 runs before routing, so PII-flagged requests can be clamped to the local partition (or redacted before any commercial dispatch); overridable per tool-type assignment row. Security × local-knowledge synergy: sensitive content never leaves the deployment.

**Budget-pressure routing** (resolved): graduated, **admin toggle, ON by default** — at ~80% consumed the escalation threshold rises (commercial harder to reach); at ~95% routing clamps local-only; at 100% the existing stage-2 hard block applies. Thresholds org-tunable; every pressure-induced shift is visible in the decision trace.

**Budgets are typed and multi-scope.** Three budget types, evaluated together — pressure follows whichever binds first (minimum headroom wins):
- **Token** — WaddleAI-token or raw-token monthly caps (org/key scope, the existing model);
- **Dollar** — $ caps computed from the conversion-rate table (org/key scope);
- **Plan/usage** — for subscription-billed provider accounts (e.g., a Claude Team/Max plan): the budget attaches to the **provider credential**, not the org, and is window-based rather than cumulative — a `plan_budget` config on `provider_credentials` records window length, reset schedule, and estimated capacity, with remaining headroom continuously corrected from provider rate-limit/usage response headers. Pressure near window exhaustion shifts traffic to other credentials in the pool or to local, and the window reset lifts it automatically.

This also upgrades provider-credential pools (§5.2 merge): pool selection becomes budget-aware — a depleted Team-plan credential rotates out until its window resets, while pay-as-you-go credentials in the same pool keep serving.

### 7.4 Decision trace (first-class output)

Every routing decision logs: requirements vector, tool-type source (explicit/heuristic/classifier), rules fired, classifier output, assignment row applied, capability vetoes, qualified candidates with sort scores, pressure signals active, and the final choice. Per-request view in the WebUI; aggregate views for tuning assignments. The trace corpus is also the training data for future heuristics and `task_detect`. An opaque router is a router admins turn off.

### 7.5 Provider selection & dispatch

Within the chosen route, the merged AILB router logic (§5.2) picks the concrete endpoint: six strategies (`round_robin | cost_optimized | latency_optimized | load_balanced | failover | random`), `ProviderStats` EMA latency + the §5.3.4 circuit breaker (half-open probe, Valkey-backed), the automatic fallback chain (same-provider endpoints first, then cross-provider availability failover per §7.3 — retryable errors only, gated by `provider_failover` for explicit-model requests), and **session affinity** for local fleet targets (KV-cache reuse, §6.3).

### 7.6 Path migration & transparency

`routing_matrix` evolves into `model_assignments` (data-migrated; API kept compatible); hardcoded `model_configs` dict → `model_configs` table (Alembic data migration); Valkey `routing:instructions` → `routing_policies.classifier_prompt`. Responses always report the actually-served model in `model` plus `waddleai.routed_from` — **never silent substitution**; alias redirects and capability vetoes included.

### 7.7 Acceptance

Assignment CRUD + save-time capability validation warnings; capability-veto tests (image request against text-only assignment → re-routed + trace records veto); heuristic rule-table property tests; classifier recorded-output fixture set (stub Ollama in unit tests; real `gemma3:1b` nightly/GPU CI); escalation state machine covering all four triggers + idle_reset boundaries + per-row escalation_model precedence; sensitivity clamp test (PII-flagged request never dispatches commercial under `local_only`); budget-pressure threshold tests incl. toggle-off; chaos test (provider unhealthy mid-conversation → failover, no client-visible error); availability-failover tests (explicit-model request with provider down: `off` fails fast with the taxonomy error, `same_class` serves from `fallback_models` with `routed_from` cause `provider_unavailable`; a 4xx never substitutes; no substitution after first streamed byte; `local_only` sensitivity clamp beats `provider_failover`); alias redirect visible in `routed_from`; `usage` additive-only vs contract snapshots; flag-off = legacy routing byte-identical.

## 8. Phase 3 — Security Layer v2

Extends the existing 4-tier content filter (`shared/security/content_filter.py`) into a fully policy-driven, admin-toggleable system occupying pipeline stages 3 (input) and 8 (output). Flag: `waddleai.security_v2`. (The `/v1/messages` parity fix and the wiring of the migrated `THREAT_PATTERNS` prompt-injection scanner already landed in Phase 1, §5.)

### 8.1 Scoped policies

New `security_policies` table with resolution chain **global → org → model → tool/function** (most-specific wins; full chain ships in the first cut per Q#6 — tool scope keys on `tools[].function.name` and namespaced MCP tool names like `elder.*`):

`security_policies(id, scope_type enum(global|org|model|tool), scope_ref, tier1_enabled, tier2_enabled, tier3_enabled, tier4_enabled, tier4_model, intent_classifier_enabled, intent_categories jsonb, direction enum(input|output|both), block_action enum(block|redact|flag), fail_mode enum(open|closed|degrade), auditor_timeout_ms, latency_budget_ms)`

Resolution results are Valkey-cached, invalidated on policy write. Existing per-org `content_filter_config`/`content_filter_rules` data-migrate into the new model (custom rules remain tier 2).

### 8.2 Tiers and fail modes

Tiers per resolved policy: **1** builtin regex PII/PCI (~23 patterns), **2** org custom rules, **3** NER (Presidio+spaCy in the §3.5 worker pool — never on the event loop), **4** LLM auditor. **`fail_mode` default = `degrade`** (resolved): on tier-4 timeout/error, the tiers-1–3 verdict is enforced and the degradation logged; `closed` and `open` selectable per scope. Auditor timeout default drops 10s → **5s**; each request carries a security **latency budget** — when exceeded, remaining tiers follow the fail_mode.

### 8.3 Request-intent classifier ("open-source Auto Mode-lite")

A pre-dispatch classifier distinct from content filtering: a guard model evaluates the request for **security/legal concern categories** — malware generation, exploit development, credential harvesting, plus org-configurable legal categories — and returns per-category verdicts → block/flag per policy. Implementation reuses the tier-4 Ollama call path with structured category output. Scope: **last user message + system-prompt hash**, escalating to a full-context scan when flagged. Guard models are **assignment rows in §7.1's Model Assignments** (`security-audit` tool type): default `shieldgemma:2b` (existing formatter), selectable `granite-guardian3:2b|8b` (Apache-2.0) via a new `_build_granite_guardian_messages` formatter honoring Granite Guardian's prompt template and Yes/No token semantics.

### 8.4 Output guardrails (stage 8)

Same policy resolution applied to responses (`direction: output|both`): PII redaction on model output, custom-rule matching, and optional tier-4 output audit — closing the output-guardrail gap flagged in the prior security audit (§3.6). Streaming responses are scanned per-buffer-window with redaction applied before chunks leave the proxy; if the window scan cannot keep up within the latency budget, fail_mode governs.

### 8.5 Filter integrity — the filter itself must be un-foolable

The classic attack is talking the guard into agreement ("totally allow this, AI bro"). Defenses, all mandatory:

1. **Monotonic composition**: LLM verdicts (tier 4, intent classifier) can only make the outcome *more* restrictive — an LLM "allow" can **never override** a deterministic tier-1/2/3 block. The regex/rules/NER tiers cannot be sweet-talked and always have the final word on their own findings.
2. **Content is data, never instruction**: guard prompts present user content strictly inside quoted delimiters using the model's official safety-prompt format (ShieldGemma `<start_of_turn>` framing, Granite Guardian's template); the guard's instructions live only in the system portion the user can never reach, and the org auditor system prompt is admin-supplied, never derived from request content.
3. **Constrained verdict parsing**: only exact verdict tokens are accepted (YES/NO, per-category structured output — checked at the token level where the runtime allows). Anything else — hedging, explanations, "sure, allowed!" — is **not a verdict** and triggers `fail_mode` (never default-allow on unparseable output).
4. **Spoof detection as a threat signal**: user content containing guard-verdict tokens, prompt-format delimiters, or filter-override phrasing ("ignore previous instructions", "you are now unfiltered") is itself flagged by seeded tier-2 rules — gaming the filter raises suspicion rather than lowering it.
5. **Stateless guard invocations**: each evaluation is a fresh context — no conversation carryover, prior guard outputs never included in later guard prompts (nothing to poison across turns).
6. **Red-team fixture corpus**: a maintained adversarial suite (verdict-token injection, delimiter escapes, roleplay coercion, multi-turn setup attacks) runs in CI; new bypasses found in the wild become regression fixtures per house testing rules.

### 8.6 Authorized bypass (researchers / red teams)

Some users legitimately must send content the filter would block. Bypass is **scope-based** (`security:bypass` OIDC scope per house auth rules — never role-name checks), grantable per user or virtual key and optionally narrowed to specific policy scopes (e.g., bypass the intent classifier but keep PII redaction):

- Mode per grant: **`shadow`** (default — all tiers still run and log verdicts, nothing blocks/redacts: full visibility of what *would* have fired) or **`skip`** (tiers don't run — for latency-sensitive red-team tooling).
- Every bypassed request is **audit-logged** with the grant identity and flagged in `usage.waddleai`; the WebUI lists all active grants; grants support optional expiry (time-boxed engagements).
- Bypass does **not** disable §8.7 upstream redaction unless the grant explicitly includes it — threat-blocking and data-protection are separate concerns, so a red-teamer can probe attacks while HIPAA data is still stripped from upstream calls.

### 8.7 Upstream query filters (pre-provider redaction)

Admin-toggleable **data-boundary filters** that strip or transform sensitive data from requests *before they leave for an upstream provider* — e.g., a hospital pre-filtering PHI before anything reaches Anthropic:

- **Category toggles** per policy scope: PII, PCI/credit-card, PHI/HIPAA, credentials/secrets, custom regex sets — shipped as one-click **compliance preset bundles** (`hipaa`, `pci-dss`, `pii-basic`).
- **Destination-aware**: applies at the **dispatch boundary** with `applies_to: commercial | all` — the default protects only upstream/commercial destinations, so local fleet models (which never leave the deployment) can still receive raw content. Combined with §7 `sensitivity_routing`, admins choose per scope between *route-local* and *redact-then-send*.
- **Two modes**: `redact` (irreversible masking — `[REDACTED:SSN]`) or `pseudonymize` (reversible placeholders via the Presidio anonymizer; the placeholder↔value map lives in Valkey for the request lifetime only, and the response is de-pseudonymized before returning to the client — the provider never sees real values, the user never sees placeholders).
- Detection reuses tiers 1–3 (regex + custom + NER) — no new scanning cost; the transform is just a different action at the provider boundary. Redaction counts surface in `usage.waddleai` and the audit log.

### 8.8 Capacity scoping (scale requirement)

At §3.5 scale, tier-3/tier-4 cost dominates. Policies therefore support **sampling** (`sample_rate` per scope — audit N% of matching traffic); cheapest-gates-first ordering means guard inference only runs on requests that already passed cheaper gates. Guard-model throughput is fleet capacity the admin sizes via the `security-audit` assignment row.

### 8.9 Admin surface

`/api/v1/security-policies` CRUD + a **resolution-preview** endpoint ("what applies to org X + model Y + tool Z"); WebUI toggle matrix (rows = scopes, columns = tiers/intent/fail-mode/upstream-filters) with compliance-preset one-click enable and a bypass-grant management view. Audit log: `content_filter_audit_log` extended with `policy_id`, `intent_categories`, `degraded`, `bypass_grant_id`, `redaction_counts`.

### 8.10 Acceptance

Adversarial fixture suite (PII, injection strings, intent categories) with expected verdict per tier; **guard-integrity suite** — manipulation corpus never yields allow, monotonic-verdict property tests (LLM tier can't downgrade a tier-1 finding), malformed guard output → fail_mode not allow; **bypass tests** — shadow logs-but-passes, skip audited, scope-narrowed grants, expiry honored; **upstream-filter tests** — HIPAA preset strips PHI before a mocked Anthropic dispatch while the same request reaches a local model unredacted, pseudonymize round-trip (provider sees placeholders, client response restored), map absent from Valkey after request end; fail-mode matrix under auditor timeout; policy-resolution precedence + cache invalidation; Granite Guardian formatter recorded-output tests; streaming output-redaction; latency-budget assertion; sampling determinism; flag-off = v1 behavior unchanged.


## 9. Phase 3 — Auto Memory Layer

Four knowledge subsystems on one storage substrate (Postgres + pgvector; embeddings via the §7.1 `embeddings` assignment row, default `nomic-embed-text`, 768-dim). Flags: `waddleai.coderag`, `waddleai.docs_cache`, `waddleai.knowledge_ingest`.

### 9.1 CodeRAG (repo indexing)

- **Chunking**: tree-sitter (MIT core + official grammars) at function/class/module boundaries, each chunk prefixed with a context header (`path > class > signature`); line-window fallback for unparseable files.
- **Tables**: `code_repos(id, org_id, name, source_url, credentials_ref, index_status, last_commit)`, `code_chunks(repo_id, path, symbol, kind, start_line, end_line, content, embedding vector(768), content_hash)`.
- **Pipeline**: async Management worker — clone/pull (server-side; git credentials via the provider-credential pattern; data-residency implications documented) → diff stored `content_hash`es → re-chunk changed files only → embed → upsert. Triggers: push webhook (GitHub/Gitea), cron, manual.
- **Search**: hybrid — pgvector cosine + Postgres FTS (`tsvector` over content + symbols) fused by reciprocal-rank; symbol-exact match short-circuits.

### 9.2 On-demand language-docs research cache

- Table `docs_cache_pages(id, ecosystem, package, version, url, content_md, embedding, license, fetched_at, ttl)`.
- Fetch on first request (MCP `fetch_docs` or API) → HTML→Markdown via `markdownify` (MIT — not html2text/GPL) → chunk → embed → cache. TTL 30d versioned docs / 7d "latest". Fetcher respects robots.txt with per-source rate limits.
- Sources & licenses (served with attribution where required): docs.python.org (PSF), PyPI project docs, doc.rust-lang.org + docs.rs (MIT/Apache), pkg.go.dev (BSD), nodejs.org/api (MIT), MDN (CC-BY-SA), ruby-doc.org (Ruby), cppreference (CC-BY-SA).

### 9.3 Manual knowledge ingestion

`/api/v1/knowledge` (upload + CRUD) with WebUI surface (primary) and `waddleai knowledge upload` CLI mirror: **PDF and Markdown** manuals into the org knowledge base — PDF→text via `pypdf` (BSD-3; `docling` (IBM, MIT) optional for complex layouts; **PyMuPDF banned — AGPL**), Markdown direct → chunked → embedded → org-scoped `rag_documents`; source filename + uploader recorded as provenance; served through the same search/injection paths as fetched docs.

### 9.4 Conversation memory

The existing mem0-compatible layer (`/mem0/*`, pgvector backend, read-replica pool) is retained as-is; its config surface re-homes to `/api/v1/memory-config` (§5.6). Salvaged AILB defaults (0.7 relevance cutoff, top-3 injection) become seeded config.

### 9.5 Delivery — hybrid by client type (resolved)

- **MCP-capable clients** (an MCP session exists for the key, or the key is marked `mcp_capable`): no injection — the agent pulls precisely via §11 tools (`search_code`, `search_docs`, `memory_search`). Token-efficient default for OpenCode/Claude Code/Cursor users.
- **Plain OpenAI-compatible clients**: auto-injection ON — retrieved context ranked across sources, truncated to a hard **token budget (default 2000, per-key configurable)**, injected as a single system-adjacent message with a provenance header; accounted in `usage.waddleai.injected_tokens`.
- Per-key override in both directions (`memory_injection: {enabled, sources: [memory|coderag|docs], token_budget}`).

### 9.6 Injection-safety (cross-ref §3.6)

All retrieved content (memory, CodeRAG, docs, uploaded knowledge, external MCP results) is **provenance-tagged and passed through stage-3 tier-1/2 filters before entering any prompt** — retrieved text is data, never trusted instruction; provenance headers mark it as quoted material. Memory writes are similarly filtered at store time to resist planting.

### 9.7 Memory scoping, trust & isolation model

A single org-wide memory blob is wrong for real teams — it leaks task context between users, lets one person's mistake poison everyone, and can't tell "the repo's build command" (durable fact) from "I'm currently debugging the login bug" (one person's transient context). Memory is therefore **scoped, trust-tiered, attributable, and correctable**. This model governs both §6A proxy memory and §9 knowledge stores; every stored item carries `(scope, provenance, trust, version)`.

**Scope hierarchy** (every read is a composite key; narrower scopes override, broader scopes are shared read-only):

| Scope | Contains | Default visibility | Trust |
|---|---|---|---|
| `org` | Admin-curated knowledge, uploaded manuals (§9.3) | All org members, read-only | Verified (highest) |
| `project` / `workspace` | Cross-repo goals, conventions, shared decisions | Project members | Curated/confirmed |
| `repo` (+ `branch`/worktree ref) | CodeRAG code index, repo facts | Repo members | Derived-from-source |
| `user` | Personal preferences, cross-session facts | Owner only | User-owned |
| `session` / `task` | Working set, scratchpad (§6A.1), conversation memory, rolling summary (§6A.2) | Owner + that session only | Transient |

**Default is narrow** — auto-captured conversation memory and scratchpad live at **session scope**, not org. Promotion to broader scope (repo/project/org) is **explicit** (an MCP `memory_promote` / API action, or admin curation), never automatic. This directly answers the concurrency case: two developers in the same repo on different features **share the repo code index but each has an isolated session/task memory** — their working context never cross-contaminates, while the durable repo facts they both need are common.

**Concurrent repo/branch work**: CodeRAG chunks key on `(repo, branch/commit)` so parallel branches/worktrees resolve to their own indexed state; retrieval is filtered to the caller's active `(repo, branch, session)` context (from the request's workspace hint header or the linked session), so someone on `feature/A` doesn't get `feature/B`'s in-flight code as context.

**Trust tiers & retrieval weighting**: `verified` (admin) > `confirmed` (user-approved) > `derived` (from source code/docs) > `unverified` (auto-captured). Retrieval ranks by relevance × trust; **unverified memory is always injected with an explicit provenance header** ("unverified note captured from user X's session on <date>") so the model treats it as a claim, not fact. Low-trust items decay (TTL) unless confirmed.

**Correction & conflict** (the "user A entered wrong info" case):
- Every memory is **versioned and attributable** (author, source, timestamp, scope) — nothing is anonymous or unattributable.
- **Contradiction detection**: on write, semantically-conflicting existing memories are detected; rather than silently overwriting, the conflict is recorded and resolved by trust → confirmation → recency. A higher-trust correction supersedes and **quarantines** the wrong entry (held out of retrieval, not hard-deleted, for auditability).
- **Edit/delete/dispute**: users correct their own memories; repo/project members flag disputed shared memories (disputed → quarantined pending review); admins curate any scope. WebUI + MCP (`memory_correct`, `memory_dispute`) + API surfaces.
- A wrong auto-captured fact thus has three independent kill-switches: it decays on its own (low trust + TTL), it's superseded the moment anyone records the correct fact, and it can be explicitly disputed/deleted.

**Injection protection** (extends §9.6, applies to every scope and to §6A recalls):
1. **Writes are filtered**: content is scanned by security tiers 1–3 *before* storage — an injection payload ("ignore your instructions and…") is caught at store time and never persisted as clean memory; suspicious writes are quarantined and flagged.
2. **Reads are data, never instruction**: retrieved memory is inserted only as quoted reference material with a provenance header, never as a system/developer message; it structurally cannot carry role authority.
3. **Reads are re-filtered**: retrieved content passes tiers 1–3 again before entering a prompt (defense against pre-existing or scope-promoted poison).
4. **Provenance travels with content**: the injected block names its scope, author, trust tier, and date — the model sees "this is an unverified note from a teammate," not an instruction from the system.
5. **Cross-scope reads respect trust**: a session can read org/repo memory (higher trust) but a low-trust session memory can never be auto-elevated into another user's context without explicit promotion.

New/changed schema for the model: memory tables gain `scope_type`, `scope_ref`, `author_user_id`, `trust_tier`, `version`, `superseded_by`, `status (active|quarantined|disputed)`, `expires_at`; applies to `conversation_memory` / `memory_embeddings`, `session_scratchpad`, `conversation_summaries`, `rag_documents`, `code_chunks` (branch ref). Folded into migrations 009b/012. Note: `memory_embeddings.scope_type` (`user`/`org`) + `author_user_id` already shipped in migration 006 (v0.2.x memory access-control feature) — this section's `repo`/`project`/`session` scopes, trust tiers, and versioning **extend** that shipped column set; do not re-add.

### 9.8 Acceptance

Index this repo as fixture → symbol-retrieval precision on a labeled query set; incremental re-index correctness (one changed file → only its chunks re-embedded); docs fetch against a local HTTP fixture server (never live sites in CI) with TTL + attribution assertions; PDF/MD ingestion round-trip (upload → searchable → provenance intact); client-type detection matrix (MCP key vs plain key → injection on/off); token-budget truncation boundary tests; **scoping/trust/isolation suite**: session-memory isolation between two users in the same repo (security test), branch-scoped CodeRAG retrieval (feature/A never returns feature/B in-flight code), explicit-promotion-only (auto-captured memory never auto-appears at org scope), contradiction→quarantine→supersede flow, dispute/correct kill-switches, unverified-memory provenance header present, write-time injection caught before persistence, read-time re-filter on scope-promoted poison; org-isolation on all stores treated as security tests.



## 10. Phase 4 — Hardened Inference Fleet & Deployment Targets

Flag: `waddleai.fleet_v2`.

### 10.1 Fleet backend interface

`shared/fleet/base.py` — abstract `InferenceFleetBackend`: `provision(spec)`, `deprovision`, `health()`, `list_nodes()`, `place_model(model, constraints)`, `endpoints_for(model)`. Existing `OllamaDeploymentManager` + `LlamaCppManager` refactor to implement it (restructure, not rewrite — they already have docker/k8s/daemonset/external modes). Registry table: `fleet_backends(id, org_id, type enum(ollama|llamacpp|exo|vertex_ai|bedrock), mode, management_scope enum(register_and_route|full_lifecycle), config jsonb, credentials_ref, status)`.

- **Ollama** — primary; DaemonSet default (Deployment-with-nodeSelector pool mode available for mixed-GPU clusters).
- **llama.cpp** — existing DaemonSet/remote modes; exact token counts via `/tokenize`.
- **EXO** — `type: exo, mode: external` API-only plugin; GPLv3 boundary — no EXO code in-repo, network calls only.
- **Vertex AI / Bedrock** (Professional-gated at backend creation): `VertexAIFleetBackend` via `google-cloud-aiplatform`, `BedrockFleetBackend` via `boto3` (both Apache-2.0). Per Q#9 the admin chooses `management_scope` per backend: `register_and_route` (route/health/meter existing endpoints) or `full_lifecycle` (deploy/scale/undeploy — with **mandatory idle-teardown cost controls**: configurable idle window → automatic endpoint teardown, redeploy-on-demand, every action audit-logged). Cloud credentials per-org via the provider-credential pattern; cloud endpoints count as managed nodes for Pro metering (§2.4).

### 10.2 Hardened Ollama image

`ghcr.io/penguintechinc/waddleai/ollama` — digest-pinned upstream base, non-root UID, `readOnlyRootFilesystem: true` (writable mounts only for the model store PVC and tmp emptyDir), `allowPrivilegeEscalation: false`, all capabilities dropped, seccomp `RuntimeDefault`; model pulls in an initContainer running the same binary. Two tags: **`hardened`** (minimal, no shell) and **`debug`** (adds shell for `kubectl exec` troubleshooting). Trivy scan gate in CI; the Helm `ollama-daemonset.yaml` and GPU plugin subcharts (nvidia gpu-operator, amd-rocm, intel) update to consume it.

### 10.3 Access control

Ollama/llama.cpp have no native auth → **the AIProxy is the only authenticated path to inference**:
- In-cluster: fleet Services are ClusterIP-only; CiliumNetworkPolicy admits ingress exclusively from AIProxy pods (§12).
- External/bare-metal nodes (Q#4): **mTLS via cert-manager-issued certs in beta/prod; shared-token sidecar in alpha/dev**.

### 10.4 Placement, load balancing & caps

Fleet backends report per-node loaded models + VRAM headroom; the §7 routing engine consults `endpoints_for(model)` and balances with **session affinity** (KV-cache reuse, §6.3). Placement policy: pin hot models per node class, lazy-pull cold models; `place_model` validates the §2.2 origin deny-list and **enforces Free-tier caps** (≤5 nodes / ≤3 models per Q#7 semantics — utility models excluded) via the license client, failing with the tier limit named.

### 10.5 Acceptance

Interface conformance suite run against all five backends (Ollama + llama.cpp real in kind; EXO/Vertex/Bedrock mocked); CNP verification in kind (unauthorized pod cannot reach Ollama service); hardened image: Trivy gate + runs-as-nonroot/readonly-rootfs Helm assertions + model pull works via initContainer under `hardened` tag; mTLS handshake test against an external-node simulator; idle-teardown lifecycle test (idle window → teardown → redeploy-on-demand, all audit-logged); Free-tier cap tests at both enforcement points; Pro gating test for `vertex_ai`/`bedrock` creation.



## 11. Phase 4 — Integrations

Flag: `waddleai.mcp_v2`. Integration *docs* publish incrementally from v0.2 (Claude Code works the moment `/v1/messages` parity lands); the components below land in v0.5.

### 11.1 MCP server v2 (WaddleAI as MCP server)

Official `mcp` Python SDK (MIT). Two transports:
- **Streamable HTTP** mounted at `/mcp` in the AIProxy (auth via `wa-` bearer keys; shares the pipeline's auth/session);
- **stdio** via `waddleai-mcp` — a **Rust static binary** for dev machines (no Python runtime), talking to the proxy over HTTP.

Tools exposed: `search_code`, `get_symbol`, `search_docs`, `fetch_docs`, `memory_add`, `memory_search`, `list_models`, `get_routing_policy`, `usage_summary`. Resources: cached docs pages, repo chunks. (Q#5: the legacy WebSockets MCP has no consumers and is deleted in Phase 1 — no compat window.)

### 11.2 `waddleai` CLI

A Rust static binary — the same binary as the MCP shim, with `waddleai mcp` as the stdio-transport subcommand — a pure thin client of `/api/v1`: `login` (browser/device OAuth, token in OS keychain per client standards), `link <mcp-endpoint>` (per-user external-MCP linking, §11.4), `keys`, `usage`, `models`, `knowledge upload <pdf|md>`, `fleet status`. **WebUI remains the first-class, primary surface** — every capability ships in the WebUI first; the CLI mirrors it for headless/terminal users; neither holds business logic.

### 11.3 Client apparatus setup

- **OpenCode (default apparatus)**: `docs/integrations/opencode.md` + `examples/opencode/opencode.json` (custom provider → WaddleAI `/v1`, models from `/v1/models`, MCP entry); Management endpoint `/api/v1/integrations/opencode-config` renders per-virtual-key config.
- **Claude Code**: `ANTHROPIC_BASE_URL` + `wa-` token docs; depends on the Phase-1 `/v1/messages` fidelity work (streaming, tool_use, system arrays, thinking blocks, prompt-cache passthrough, `count_tokens`).
- **Cursor / Antigravity / generic**: OpenAI base-URL docs with per-tool quirk notes.
- **VS Code extension refresh**: update to new endpoints + surface cache/routing metadata; explicitly last and cuttable.

### 11.4 MCP gateway (WaddleAI as MCP client)

WaddleAI also *consumes* external MCP servers (e.g., the Elder MCP server): admins register endpoints via `/api/v1/integrations/mcp-endpoints` + WebUI (URL, transport, auth config — via the provider-credential pattern), org-scoped. Discovered tools are **namespaced** (`elder.*`) and re-served through WaddleAI's own MCP surface, so a client configures one MCP connection and sees the whole aggregated toolset.

- **Outbound auth**: static headers (`Authorization: Bearer <token>`, custom API-key headers) **and** OAuth2 — client-credentials for M2M plus the MCP-spec authorization-code flow with dynamic client registration; tokens cached/refreshed by Management, never exposed to end clients.
- **Identity mode per endpoint**: *shared account* (one org-wide credential) or *per-user identity* — the user links by logging in via WebUI/CLI and completing the OAuth2 authorization-code flow against the external server; WaddleAI stores the per-user+endpoint token (encrypted at rest, external-KMS envelope at Enterprise) and refreshes it, so calls carry the real caller's identity and the external system applies its own permissions. Unlinked users get a link-your-account URL in the tool result; unattributed keys fall back to the shared account if configured, else the tool is withheld.
- **Policy chokepoint**: external tool calls traverse the §8 per-tool security policies (block/flag/audit) — WaddleAI governs third-party MCP tools, not just models.

### 11.5 Acceptance

MCP v2 exercised via the official `mcp` client SDK over both transports; OpenCode + Claude Code config templates actually connect and complete a turn (beta checklist, scripted where possible); external-MCP gateway integration test against a fixture MCP server covering header + OAuth2 (client-credentials and auth-code) and both identity modes; namespaced tool collision handling; per-tool security policy applied to an external tool call.

## 12. Kubernetes & Cilium Integration

Cilium is the assumed substrate (§3). Protections ship **ON by default** (Q#8), each behind a per-class Helm values switch; every CRD-emitting template is guarded by a capability check so non-Cilium clusters skip gracefully.

### 12.1 Managed-by-WaddleAI policy (Management reconciler)

`services/management/app/services/cilium_policy.py` renders and upserts CRDs from DB settings via an RBAC-scoped ServiceAccount:
- **Rate limiting** — CiliumEnvoyConfig with Envoy `local_ratelimit` on the Gateway/HTTPRoute path, per-org descriptors (per-key stays in the AIProxy token gate — Q#10, no CRD churn on key rotation).
- **Network isolation** — CiliumNetworkPolicy: default-deny per namespace; explicit flows (client→Gateway, Gateway→AIProxy, AIProxy→fleet/Postgres/Valkey, Management→Postgres/Valkey/kube-apiserver); **fleet pods admit ingress only from AIProxy** (§10.3).

### 12.2 Shipped-with-chart protections (values-gated, on by default)

- **Tetragon TracingPolicies**: block/observe exec into AIProxy and fleet pods; egress allow-list from the AIProxy to known provider endpoints; flag unexpected file/network activity.
- **Admission policies** (ValidatingAdmissionPolicy, Kyverno optional): enforce rootless / non-root UID / readOnlyRootFilesystem / digest-pinned images / dropped capabilities on WaddleAI workloads.

### 12.3 CRD/agent bootstrap

On Cilium deployments the installer **detects missing CRDs/agents** (e.g., Tetragon not yet installed) and **offers to install them** (opt-in Helm dependency / documented step); when declined or absent, the dependent protections no-op with a clear status rather than failing the release. `helm template` renders cleanly with every protection class independently toggled.

### 12.4 Acceptance

`helm template` golden files per toggle combination; kind-cluster deploy verifies proxy + management + fleet pods, CNP default-deny effective (unauthorized cross-namespace call blocked), rate-limit CEC applied; CRD-absent path renders and deploys without the Cilium CRDs present; RBAC least-privilege check on the reconciler ServiceAccount; admission-policy negative test (a root/hostPath pod is rejected).



## 13. Data Model & Migration Ledger

Baseline: Alembic migrations `001`–`006` exist (baseline, provider_credentials, routing_matrix credential_label, drop provider api_key, content_filter tables, **memory scope** — `006_add_memory_scope` shipped 2026-07-15 with the v0.2.x memory access-control feature: `memory_embeddings.scope_type`/`author_user_id` + backfill). New work starts at **007**. SQLAlchemy models in `services/management/app/models_sqlalchemy.py`; penguin-dal for runtime, Alembic sole schema authority (house rule). Every migration ships with a downgrade and a round-trip test on a seeded snapshot.

### 13.1 Migration sequence

| # | Phase | Migration | Changes |
|---|---|---|---|
| 007 | §5,§12 | `drop_ailb_add_native_limits` | Drop `marchproxy_ailb_sync`; fold `ailb_usage_events`+`ailb_usage_records` → `token_usage` (`source='ailb_import'`) then drop; drop `virtual_keys.ailb_*`; guard-add `virtual_keys.budget_monthly_tokens/budget_monthly_usd` (rpm_limit/tpm_limit already exist — check-if-missing); add `token_usage.source`; add `organizations.rpm_limit` (per-org Cilium edge RPM, §12.1); seed `token_conversion_rates` from migrated `DEFAULT_CONVERSION_RATES` |
| 008 | §2,§5 | `model_registry` | `model_registry` (name, role, license, origin, min_vram, ollama_tag, resolved_digest, is_utility); seed dual-default set; add `provider_credentials.plan_budget jsonb` |
| 009a | §6 | `response_cache` | `cache_configs`, `response_cache_entries` (pgvector + HNSW); add `token_usage.cache_status/tokens_saved` |
| 009b | §6A | `proxy_memory` | `session_scratchpad`, `conversation_summaries`, `embedding_cache` (pgvector) — separate chained revision (down_revision = head at merge), not a shared file, to avoid two branches editing one migration |
| 010 | §7 | `routing_engine` | `model_configs` (seeded from hardcoded dict), `model_aliases`, `routing_rules_v2`, `routing_policies`, `routing_decision_traces` (§7.4 first-class trace corpus); evolve `routing_matrix` → `model_assignments` (add `escalation_model`, `tool_type`, `fallback_models` — ordered cross-provider equivalents for §7.3 availability failover) |
| 011 | §8 | `security_v2` | `security_policies`; extend `content_filter_audit_log` (`policy_id`, `intent_categories`, `degraded`, `bypass_grant_id`, `redaction_counts`); `security_bypass_grants`; migrate `content_filter_config` → scoped policies |
| 012 | §9 | `knowledge` | `code_repos`, `code_chunks`, `docs_cache_pages`, `docs_sources` (per-source license table, §2.5) (all pgvector); extend `rag_documents`/`memory_embeddings` with the remaining §9.7 scope/trust/version/status/provenance columns (`memory_embeddings.scope_type`/`author_user_id` already shipped in 006 — extend, don't re-add) |
| 013 | §10 | `fleet` | `fleet_backends`; extend `ollama_deployments`/`llamacpp_deployments` for the backend interface + `management_scope` |
| 014 | §11 | `integrations` | `mcp_endpoints`, `mcp_user_links` (per-user external-MCP tokens, encrypted) |

All migrations land within the `release/v0.2.X` line (one per feature branch, §14.1). **Numbers here express intended dependency order, not fixed Alembic revisions**: parallel feature branches (notably 009a/009b, and 010+ which branch off 008/009) get their final `down_revision` set to the actual head **at merge time** — each plan rebases onto the then-current head. This is the normal Alembic-DAG-resolved-at-merge model; do not treat the integers as pre-committed. Applied sequentially at release.

### 13.2 New tables by domain (summary)

- **Registry/limits**: `model_registry`, `model_configs`, `model_aliases`; `virtual_keys` native limit cols; `provider_credentials.plan_budget`.
- **Cache**: `cache_configs`, `response_cache_entries`.
- **Proxy memory (§6A)**: `session_scratchpad`, `conversation_summaries`, `embedding_cache` (retrieval-result + tokenizer caches are Valkey-only).
- **Routing**: `routing_rules_v2`, `routing_policies`, `model_assignments` (from `routing_matrix`).
- **Security**: `security_policies`, `security_bypass_grants`; extended audit log.
- **Knowledge**: `code_repos`, `code_chunks`, `docs_cache_pages`; extended `rag_documents`.
- **Fleet**: `fleet_backends`; extended deployment tables.
- **Integrations**: `mcp_endpoints`, `mcp_user_links`.

### 13.3 Dropped

`marchproxy_ailb_sync`, `ailb_usage_events`, `ailb_usage_records` (migrated first); `virtual_keys.ailb_*` columns; hardcoded `model_configs` dict and Valkey `routing:instructions` key (superseded, not DB drops). Encryption: `mcp_user_links` tokens and `provider_credentials` secrets use at-rest encryption, external-KMS envelope at Enterprise (§2.4).

## 14. Testing, Rollout & Release Mapping

### 14.1 Release mapping — everything ships in v0.2.x

**All sections §4–§12 land in the single `release/v0.2.X` release line.** The phase labels earlier in the document (Phase 1–4) are *sequencing/dependency ordering within v0.2.x*, not separate releases. Each feature is built on its own short-lived **feature/fix/chore branch off `release/v0.2.X`**, merged back into the release branch without a PR (house rule); only `release/v0.2.X → main` needs a PR. Release branches are permanent.

**Branch plan (each merges into `release/v0.2.X`):**

| Branch | Delivers | Depends on |
|---|---|---|
| `chore/license-server-waddleai-product` *(license-server repo)* | §14.6 define `waddleai` product + features + entitlement rows + `waddleai-flags` PostHog project | — (prerequisite) |
| `chore/consolidate-quart-k8s` | §4 Flask→Quart, retire FastAPI plane, one k8s tree, proxy in Helm, Valkey, contract snapshots | — (first) |
| `feature/aiproxy-migration` | §5 AILB code merge, token gate, big-5 dispatch, `/v1/messages` parity, `ProxyPipeline`, migrations 007–008 | consolidate |
| `feature/cilium-policy-reconciler` | §12 CiliumEnvoyConfig rate-limit + CNP reconciler, RBAC, CRD bootstrap | aiproxy-migration |
| `feature/response-cache` | §6 exact/semantic cache + upstream passthrough, migration 009a | pipeline |
| `feature/proxy-memory-layers` | §6A the four proxy memory layers (scratchpad, summarization, embedding/retrieval cache, schema-dedup), migration 009b | pipeline |
| `feature/smart-routing` | §7 routing engine, assignments, escalation, budgets, migration 010 | pipeline, registry |
| `feature/security-v2` | §8 scoped policies, intent classifier, guard integrity, bypass, upstream filters, migration 011 | pipeline, routing |
| `feature/knowledge-layer` | §9 CodeRAG, docs cache, PDF/MD ingest, hybrid delivery, migration 012 | pipeline, embedding cache |
| `feature/inference-fleet-v2` | §10 fleet interface, hardened Ollama, cloud targets, migration 013 | routing |
| `feature/mcp-v2-integrations` | §11 MCP server/gateway, CLI, apparatus docs, migration 014 | knowledge, routing |
| `chore/tetragon-admission-policies` | §12 optional Tetragon + admission policy values | cilium-reconciler |

Deferred to a later release (explicitly out of v0.2.x): `task_detect` de-escalation (§7.3), PenguinCode convergence, VS Code extension refresh (§11.3, cuttable). Every feature ships behind its PostHog flag defaulted OFF (§14.5), flipped on after beta validation, flag removed once stable. Licensed sub-features additionally gate on the license client (§14.6).

### 14.2 Standing gates (every release)

- **90%+ coverage** (lines/branches/functions/statements) on changed modules; builds fail below.
- **Golden contract snapshots** (§4.1) for `/v1/*`, `/mem0/*`, `/api/v1/*` are the merge gate — public-surface changes must be deliberate snapshot updates; `usage.waddleai` additions are additive-only.
- **Flag-off proof**: each feature has a test showing zero behavior change when its flag is off.
- **Security scans** (`make test-security`: bandit, gosec, pip-audit, trivy, gitleaks) + the new `pip-licenses` gate (no non-OSI code deps; model-origin deny-list checked at registry seed).
- **Cross-arch** amd64+arm64; rootless + digest-pinned + Debian bookworm container checks.
- **Regression rule**: every beta/prod bug and every GitHub-issue bug gets a regression test referencing the issue before close (per `e2e-regression-review`).

### 14.3 Acceptance by feature branch (all within v0.2.x)

- **consolidate / aiproxy-migration (§4,§5)**: contract snapshots green pre/post Flask→Quart; migration 007 round-trip + downgrade on a seeded AILB snapshot; `/v1/messages` parity (stage-log assertion both endpoints); Claude Code streamed tool-use turn + OpenAI SDK streaming; rate limits enforced at edge (Cilium) and gate (Valkey) at the boundary; scale smoke (1K concurrent streams/pod, p99 proxy overhead <50ms, no event-loop stall); zero `marchproxy`/`flask`/`fastapi`(non-PenguinCode)/`redis:` references remain.
- **response-cache / proxy-memory (§6,§6A)**: cache determinism-eligibility matrix, streaming replay byte-equivalence, **org-isolation as a security test**; semantic should-hit/should-miss corpus + threshold regression; `cache_control` injection verified against recorded Anthropic responses; scratchpad isolation, summarization threshold + retrievability, embedding-cache hit avoids re-embed, schema-dedup token reduction (§6A.6).
- **smart-routing (§7)**: capability-veto + all four escalation triggers + idle_reset boundaries + sensitivity clamp + budget-pressure (typed token/dollar/plan budgets, toggle-off) + chaos failover.
- **security-v2 (§8)**: adversarial + guard-integrity + bypass + upstream-filter suites (§8.10).
- **knowledge-layer (§9)**: CodeRAG symbol-retrieval precision + incremental re-index + injection-safety; docs fetch against local fixture server + attribution; PDF/MD ingestion round-trip; client-type injection matrix + token-budget truncation; org-isolation on all knowledge stores.
- **inference-fleet / mcp-integrations (§10,§11)**: fleet interface conformance across five backends (Ollama/llama.cpp real in kind, others mocked); CNP unauthorized-pod-blocked; hardened image Trivy + nonroot/readonly + initContainer pull; mTLS handshake against external-node simulator; idle-teardown lifecycle; Free-tier cap + Pro gating; MCP v2 over both transports; external-MCP gateway header+OAuth2 × both identity modes; per-tool security policy on an external tool call.
- **cilium / tetragon-admission (§12)**: `helm template` golden per toggle combo + kind deploy + CRD-absent graceful path + RBAC least-privilege + admission negative test.

### 14.4 Test infrastructure

pytest + pytest-asyncio; Ollama stubbed in unit tests with a real-model nightly/GPU CI tier for classifier/guard fixtures; kind cluster for K8s/Cilium/fleet integration; Playwright for WebUI smoke (`outputDir=/tmp/playwright-waddleai`, cleaned up always); beta validation through the internal LB (Cloudflare-bypass) per house testing rules.

### 14.5 Feature toggles via Penguin Licensing

**Every feature is turned on/off through Penguin Licensing** — the license server (`license.penguintech.io`) is the single control surface and runs self-hosted PostHog (Community Edition) under the hood for flag storage and evaluation. Admins flip a feature in Penguin Licensing; WaddleAI reads that state. Flags live in the `waddleai-flags` PostHog project the license server manages; the product evaluates them with the standard `posthog-python` SDK pointed at the same host:

```python
# Unified wrapper — one concept, "is this feature on for this caller?"
# Combines the PostHog flag (general on/off, managed in Penguin Licensing)
# with the license entitlement check (tier gating) so callers ask once.
if features.enabled("smart_routing", distinct_id=org_id):   # waddleai.smart_routing
    ...
```

- **`features.enabled(key, distinct_id)`** (thin `shared/licensing/features.py` helper) = PostHog `feature_enabled("waddleai.{key}", distinct_id)` **AND**, for licensed features, `LicenseClient.check_feature(key)` (§14.6). Fail-safe **OFF** on any error (graceful degradation). Centralizes the client, the `waddleai.` prefix, and the default-OFF behavior so no caller hand-rolls it.
- **Env**: `POSTHOG_KEY`, `POSTHOG_HOST=https://license.penguintech.io` (same host as the license server). Key convention `waddleai.{feature}` (server builds `flag_key = f"{product}.{name}"`).
- Flag keys (one per feature branch, §14.1): `waddleai.native_rate_limit`, `waddleai.response_cache`, `waddleai.proxy_memory`, `waddleai.smart_routing`, `waddleai.security_v2`, `waddleai.coderag`, `waddleai.docs_cache`, `waddleai.knowledge_ingest`, `waddleai.fleet_v2`, `waddleai.mcp_v2` — plus the finer admin toggles inside features (per-tier security scopes, cache modes) that also resolve through Penguin Licensing.
- All default **OFF** at launch; flipped on in Penguin Licensing after beta validation; the flag is removed once the feature is stable (flags are not permanent config — tier entitlement in §14.6 remains the durable gate).

### 14.6 License entitlement & metering (penguin-licensing)

**SDK**: `penguin-licensing` (PyPI) — `LicenseClient(license_key=…, product="waddleai", base_url=LICENSE_SERVER_URL)`; `product` **must** be passed (SDK default is `elder`). Env: `LICENSE_KEY`, `LICENSE_SERVER_URL` (default `https://license.penguintech.io`), `PRODUCT_NAME=waddleai`, `RELEASE_MODE` (gating active only when `true` — dev builds ungated), `APP_VERSION`; PostHog: `POSTHOG_KEY`, `POSTHOG_HOST`.

**Calls used**:
- `validate()` → tier + features + limits (5-min cached); startup + periodic.
- `check_feature("<feature>")` → bool for entitlement gates (fail-closed).
- `keepalive({"users": N, "nodes": M})` → the metering checkin; server upserts each key into `entitlement_usage` and returns overage warnings. WaddleAI reports **`users`** (Pro seats: SSO identities + named `wa-` key owners) and **`nodes`** (managed inference nodes incl. cloud endpoints) on a scheduled job (supercronic, per container standards).

**Two-layer gate** (house rule): a licensed feature checks its PostHog flag (general enablement) **and** `check_feature()` (entitlement). Free/`community` never sees Pro/Enterprise features even with a flag on.

**⚠️ New work required — no `waddleai` product exists in the license server yet** (survey confirmed: only test fixtures reference it). A prerequisite task (branch `chore/license-server-waddleai-product`, in the license-server repo) must define:
- a `products` row `name="waddleai"`;
- `product_features` rows with `flag_key=waddleai.{feature}`, `tier_requirements` mapping (e.g. `sso`, `hybrid_targets`, `security_scoping`, `semantic_cache` → professional; `kms_encryption`, `multi_tenancy` → enterprise; core proxy/routing/exact-cache/basic-security → community);
- `entitlement_usage` rows pre-seeded with `max_allowed` for `users`/`nodes` per tier (community: nodes ≤5, models ≤3; professional/enterprise: unlimited = `-1` / metered);
- a PostHog `waddleai-flags` project with the flag keys above.

**Auth flow note**: entitlement endpoints (`/api/v2/validate`, `/api/v2/checkin`) are JWT-gated — the SDK handles the `POST /api/auth/register` (license key → RS256 JWT) exchange and refresh; `/api/v2/features` accepts the raw key directly. Spec-level: rely on the SDK, don't hand-roll the HTTP.

**Acceptance**: gating-off in dev (`RELEASE_MODE!=true`) exposes all features; `community` tier blocks Pro/Enterprise feature creation with a tier-named error; flag-off suppresses a feature independent of entitlement; server-unreachable falls back to cached-then-`community` without crashing; checkin reports seats/nodes and surfaces overage warnings; domain-bypass honored for `*.penguincloud.io`/`*.penguintech.cloud`.

---

## Status

**Sections 1–14 complete** (incl. §6A proxy memory layers and §9.7 memory scoping/trust/isolation model). All 11 open questions resolved. Everything ships in **v0.2.x** across the per-feature branches in §14.1. Licensing/flagging aligned to the real `penguin-licensing` + self-hosted PostHog contract (§14.5/§14.6), with the license-server `waddleai` product definition flagged as a prerequisite. Ready for full-spec review, after which each feature branch gets a task-by-task TDD implementation plan in `docs/superpowers/plans/` (following the existing llamacpp plan format) for Opus to implement on `release/v0.2.X`.

---

## Open Questions Ledger — ALL RESOLVED (2026-07-09)

| # | Question | Decision |
|---|---|---|
| 1 | AILB historical usage rows | **Fold into `token_usage`** with `source='ailb_import'`, then drop tables — billing/dashboard continuity |
| 2 | Anthropic `cache_control` auto-inject | **Default ON** (prefixes >1024 tokens seen ≥2×), per-org toggle, billing-profile change documented |
| 3 | Semantic-cache first-cut scope | **Restricted classes @ 0.95**: default OFF; when enabled — single-turn/last-turn-only, no tools, no memory injection, temperature 0, router-classified informational/Q&A; threshold per-org tunable |
| 4 | AIProxy→external-fleet auth | **mTLS (cert-manager) for beta/prod; shared-token sidecar for alpha/dev** |
| 5 | WebSockets MCP compat window | **None needed** — code audit: VS Code extension is pure REST; only consumers are the legacy FastAPI plane (deleted v0.2) and an example script. Old MCP interface deleted with the legacy plane |
| 6 | Security scoping depth in first cut | **Full chain from the start**: global→org→model→tool/function — the §11 MCP gateway needs per-tool policy anyway |
| 7 | Free-tier counting semantics | **Confirmed §2.4 proposal**: node = distinct K8s node (UID) with ≥1 fleet pod (external nodes by registered endpoint); model = registry entry with active placement; **utility models (guard/routing/embedding) excluded** from the 3-model cap |
| 8 | Cilium protections default state | **All ON by default** (CNP, rate-limit CEC, Tetragon, admission policies) with per-class values switches for easy opt-out in less mature environments; on Cilium deployments the installer **offers to install missing CRDs/agents** (e.g., Tetragon); skip-if-absent otherwise |
| 9 | Vertex AI/Bedrock management scope | **Both modes shipped; per-backend admin choice**: `register-and-route` (route/health/meter existing endpoints) or `full-lifecycle` (provision/scale/deprovision + mandatory idle-teardown cost controls) — the risk decision belongs to the admin |
| 10 | Cilium rate-limit granularity | **Per-org at the Envoy edge, per-key in the AIProxy token gate** (no CRD churn on key rotation) |
| 11 | AILB `rbac.py` disposition | **penguin-aaa scopes wholly authoritative**; salvage the Permission enum as scope vocabulary (resource:action catalog) + Role bundles as scope-bundle definitions; HMAC key code/tests dropped except where they cover `wa-` key hashing |
