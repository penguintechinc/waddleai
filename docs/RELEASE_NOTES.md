# WaddleAI Release Notes

## Unreleased

### Operational readiness remediation — 2026-10-04

Sixteen fixes from an operational-readiness audit (gh-261–gh-276), merged to
`release/v0.2.X`. Every new mechanism is safe-by-default (opt-out kill-switch,
unseen/OFF = mechanism on) or opt-in (default OFF) — no action required to
adopt the defaults; see each bullet for exceptions.

#### Scale & resilience

- **Proxy API-key auth moved off the event loop** (gh-268): the DB lookup,
  bcrypt verify, and `last_used` write now run on a dedicated executor, fronted
  by a Valkey-backed cache keyed on `key_id` (`PROXY_AUTH_CACHE_TTL_SECONDS`,
  default 60s). New revoke endpoint `DELETE /api/v1/proxy-keys/{key_id}`
  invalidates the cache entry immediately. Kill-switch:
  `waddleai.disable-auth-cache`.
- **True SSE streaming** for `stream: true` on `/v1/chat/completions` and
  `/v1/messages` (gh-270) — previously buffered and sent as one blob. A
  load-shed `429` now always carries `Retry-After`. Kill-switch:
  `waddleai.disable-sse-streaming` (reverts to the old buffered behavior).
- **Bounded Valkey pool, request body cap, and per-worker concurrency limit**
  on the proxy (gh-267): `PROXY_VALKEY_MAX_CONNECTIONS`, `PROXY_MAX_BODY_BYTES`
  (10 MiB default), `PROXY_MAX_CONCURRENT_PER_WORKER` (100 default, falls back
  to the legacy `MAX_CONCURRENT_REQUESTS` if already set).
- **Hypercorn worker count is now runtime-configurable** (`HYPERCORN_WORKERS`,
  `k8s/helm/waddleai` `management.workers`/`proxy.workers`) instead of
  hardcoded per-Dockerfile (gh-264).
- **penguincode `Index`/`IndexCode` are now asynchronous** (gh-269): both
  return `job_id` + `state=QUEUED` immediately; poll `IndexStatus(job_id)` or
  list via `ListIndexJobs`. **Action required on upgrade:** apply penguincode
  migration `0008_index_jobs.sql` (Helm migration Job, same pattern as
  `0007_chat_sessions.sql` below) before deploying this change. Kill-switch:
  `penguincode.disable-index-queue` (reverts to the old synchronous, inline
  behavior).
- **penguincode chat sessions persisted to Postgres** (gh-262), surviving pod
  restarts — previously in-process only. **Action required on upgrade:**
  apply migration `0007_chat_sessions.sql`. Kill-switch:
  `penguincode.disable-shared-sessions`.
- **penguincode shares one bounded DB connection pool** plus graph
  depth/vector/node clamps and a server-side `statement_timeout` (gh-263),
  instead of opening a fresh connection per call. Kill-switch:
  `penguincode.disable-db-pool`.
- **penguincode gRPC server hardening** (gh-265): bounded thread pool,
  in-flight RPC concurrency limit, and message-size limits, so the server
  sheds load with `RESOURCE_EXHAUSTED` instead of accepting without limit.
  Kill-switches: `waddleai.disable-grpc-concurrency-limits`,
  `waddleai.disable-grpc-message-limits`.
- **Opt-in Ollama-embedding bulkhead** (gh-276): point embedding traffic
  (doc/code indexing, GraphRAG, mem0) at a second dedicated Ollama deployment
  via `OLLAMA_EMBEDDING_URL` (proxy) / `PENGUINCODE_EMBEDDING_OLLAMA_URL`
  (penguincode), separating it from the chat-serving instance. Unset (default)
  — unchanged single-Ollama behavior. Pair with the new `ollamaEmbeddings.*`
  Helm values.
- **Response-cache stampede protection** (gh-271).

#### Observability

- **penguincode gained a Prometheus `/metrics` route** (gh-272) alongside its
  existing OTLP push, matching the management/proxy convention. Kill-switch:
  `penguincode.disable-prometheus-metrics`.
- **W3C trace-context propagation across gRPC service boundaries** (gh-274).
- **New Grafana dashboards, PrometheusRule alerts, and SLO definitions**
  (gh-261) for management, proxy, and the penguincode server — all behind
  `monitoring.enabled` (default OFF; ON in beta/gamma/production). See
  `docs/operations/MONITORING.md` and `docs/operations/SLOS.md`. Two alert
  pairs reference metrics that don't exist for every service they're written
  against and will never fire until a follow-up adds that instrumentation —
  documented as known dangling references in both docs, not silently shipped
  as if resolved.
- **Helm probes/PDB/grace-period hardening** (gh-264): distinct
  liveness/readiness probes, a `startupProbe` on every Deployment, per-service
  `terminationGracePeriodSeconds`/`preStop` sleep, and a `PodDisruptionBudget`
  once a service's effective replica count exceeds 1 — plus a new HPA for the
  penguincode server.
- **New CLI client observability** (gh-273/gh-275): connectivity-status
  indicator, clearer error messages when the gRPC server or auth service is
  unreachable.

#### Graceful degradation

- **Feature-flag and license-check evaluation now degrades to last-known
  value on an outage** instead of a hardcoded default or hard denial (gh-266):
  `FEATURE_FLAG_TTL_SECONDS`, `LICENSE_MAX_STALE_SECONDS` (7 days). Env
  override `WADDLEAI_FLAG_<NAME>` bypasses the cache and PostHog entirely.
  Kill-switches: `WADDLEAI_FLAG_DISABLE_FLAG_DEGRADATION_CACHE`,
  `WADDLEAI_FLAG_DISABLE_LICENSE_STALE_CACHE`.
- **Management request-body cap and DB/Valkey pool bounds** (gh-266/gh-267):
  `MANAGEMENT_MAX_BODY_BYTES` (2 MiB default), `MANAGEMENT_VALKEY_MAX_CONNECTIONS`,
  `DB_MAX_RETRIES`/`DB_RETRY_DELAY`/`DB_RETRY_MAX_DELAY` (exponential backoff +
  full jitter on DB init).

#### Clients

- **penguincode CLI resilience** (gh-275): automatic retry with backoff on
  gRPC calls, a local offline read cache (`/docs search` degrades to a stale
  result instead of failing outright), and a silent, non-blocking startup
  update check. Kill-switches: `penguincode.disable-client-retry`,
  `penguincode.disable-offline-cache`, `penguincode.disable-update-check`.

See `docs/deployment/CONFIGURATION.md` for the full env var/flag index, and
`docs/api/openai-compatible.md` for the updated streaming contract.

### Models

- **Gemma 4 minimum raised from `e2b` to `e4b`.** Testing on 2026-09-07 found `gemma4:e2b` too weak for stage-2 routing classification -- it does not determine tool type and complexity reliably enough to route on. `e4b` is now the supported minimum for routing and the other quick/light internal roles (summarization, docs-fetch); **`gemma4:e4b` is the shipped default for every role** — it is the supported minimum and runs on modest hardware. **`gemma4:12b` is the documented recommendation, not a default**, for more complex operations (coding especially); opt in with `WADDLEAI_LOCAL_CHAT_MODEL`.
- **Migration 019** retags existing `model_registry` and `model_assignments` rows from `gemma4:e2b` to `gemma4:e4b` and raises that registry row's `min_vram` from 2 GB to 4 GB. Only rows still on the withdrawn tag are touched -- an operator who already moved an assignment to `12b`/`26b`/`31b` keeps their choice. Migrations 008 and 010 are left as the historical seeds they are.
- The Routing LLM Model selector in the WebUI no longer offers `e2b`. A deployment still pinned to it keeps seeing its stored value as a disabled legacy option, so the selector never silently misreports which model is live.
- Valid Gemma 4 tags remain `e2b`/`e4b`/`12b`/`26b`/`31b`. The `e` prefix marks the MatFormer effective-size variants only, so the 12B tag is `12b`, never `e12b`, and `gemma4:2b` does not exist.

### Security

- **Dropped the chromadb memory/RAG backend** (`ChromaDBMemoryStore` in `shared/utils/memory_integration.py`, `ChromaDBRAGStore` in `shared/utils/rag_integration.py`, and the `chromadb` dependency itself). PYSEC-2026-311 is a pre-authentication code injection vulnerability in chromadb's server component with no fixed release in any version >=1.0.0; it had been carried as an accepted `pip-audit` exception. pgvector (the default) and qdrant already cover the same ground, so the backend was removed instead of the exception being carried forward.
- `create_memory_manager(backend="chromadb")` and `create_rag_manager(backend="chromadb")` now fail fast with a `ValueError` naming `pgvector`/`mem0`/`qdrant` as replacements, instead of silently falling back to a different backend. `create_memory_manager(backend="mem0")` without `mem0ai` installed now raises `ImportError` rather than silently falling back to the removed ChromaDB store.
- **Migration:** if you were running with `backend="chromadb"`, switch to `backend="pgvector"` (default) or `backend="mem0"`. There is no automated migration tool -- re-index/re-populate memory and RAG documents from source data after switching backends.
- **penguincode `ChatService` tenancy gap closed.** Every `ChatService` RPC (`CreateSession`/`Chat`/`GetHistory`/`CloseSession`) used to run under penguincode's legacy HS256 client-server auth, with real multi-tenant isolation faked via a synthesized `_legacy` pseudo-tenant keyed on the HS256 token's `sub` -- unlike `KnowledgeService`/`LessonsService`, which already require a WaddleAI-issued RS256 JWT and derive a real tenant-bounded `ScopeContext`. `ChatService` now shares that same RS256/`ScopeContext` gate by default; a session remains visible to its own tenant and owning user only (`db/migrations/0007_chat_sessions.sql`'s existing scope columns, enforced in `sessions/store.py`). The penguincode CLI's chat calls now attach a WaddleAI bearer token (`WaddleAITokenProvider`), like `KnowledgeService`/`LessonsService` calls already do. **Kill-switch:** `penguincode.disable-chat-rs256-gate` (opt-out, unseen/OFF = the RS256 gate is active) reverts `ChatService` to the pre-fix legacy HS256 path as an emergency rollback for an operator mid-migration off the standalone client; enabling it logs a WARN once.

## v0.2.0 — 2026-08-10

> Merged to `main` as a squash of the `release/v0.2.X` branch. Not tagged and not deployed.

### Consolidation — one control plane, one deployment tree

- Retired the legacy FastAPI management plane. The control plane is now a single **Quart** service at `services/management/`, served by hypercorn (`asgi:app`, port 8001). The old `management/` tree is gone.
- Data plane is **Quart** at `proxy/` (`apps.proxy_server.main:app`, port 8080), exposing both OpenAI-compatible (`/v1/chat/completions`) and Anthropic-compatible (`/v1/messages`) endpoints.
- Single `k8s/` tree; the Helm chart at `k8s/helm/waddleai` deploys the proxy alongside the rest of the platform.
- **Valkey** replaces Redis throughout.
- Authentication moved to `penguin-aaa` (OIDC/JWT); Flask-Security-Too is gone.
- Runtime database access goes through `penguin-dal`; SQLAlchemy + Alembic remain the schema and migration authority.

### AIProxy data plane — MarchProxy AILB absorbed

- MarchProxy's AI Load Balancer is retired. WaddleAI owns its own data plane again.
- Ordered **`ProxyPipeline`** (`proxy/apps/proxy_server/pipeline/stages.py`) runs auth → rate limit → security → memory → dispatch → metering, with `/v1/messages` and `/v1/chat/completions` sharing identical stages.
- Provider connectors for OpenAI, xAI, Anthropic, Google Gemini, Ollama, llama.cpp and AWS Bedrock, with SSE streaming across all of them.
- Typed provider error taxonomy (`ProviderTimeoutError`, `ProviderRateLimitError`, `ProviderServerError`, `ProviderClientError`) driving jittered retries and a per-(provider, model) circuit breaker with a single reserved half-open probe, so a recovering provider takes one trial request rather than the full concurrent load.
- Metering is in-process with a bounded retry buffer, so a failed usage write no longer silently drops billable tokens.

### Observability

- OpenTelemetry **`gen_ai.*`** span attributes emitted on the dispatch span — `gen_ai.system`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens` / `output_tokens`, `gen_ai.response.finish_reason` — asserted against exported spans in tests, with no secrets in span data.

### llama.cpp inference fleet

- Shared model cache backed by a PVC, with an init container that skips download when the model is already present.
- Helm DaemonSet, PVC and Service templates, digest-pinned.

### Memory

- Memory scopes for the mem0-compatible memory API over pgvector.

### Security

- Nine findings from a full security review remediated: unauthenticated gRPC access, management-plane authorization gaps (IDOR and privilege escalation), default credentials, secret handling, a llama.cpp injection path, and a redaction truncation leak.
- `ADMIN_INITIAL_PASSWORD` sourced from the environment; the master key is no longer logged.

### Build, CI and supply chain

- **CI now runs on pull requests targeting `release/**`.** Previously it only ran for `main`, so release-targeted PRs ran no tests and no builds at all — the auto-merge "fully green" gate was passing on branches that had never been built.
- bandit now scans `services/management` rather than a path deleted during consolidation (113 files rather than 63), and a HIGH-severity pass fails the build instead of being swallowed.
- **CodeQL** added for Python, JavaScript/TypeScript and Actions. The repository had never produced code scanning results for any pull request.
- Web UI lint and tests now run in CI, with coverage enforced at the 90% threshold (244 tests).
- Web UI image builds on Node 24 with `npm ci`, so lockfile pins are guaranteed to be what ships; `react-router-dom` pinned to 6.30.4, clearing an open advisory.
- Outstanding Dependabot updates applied across pip, npm and GitHub Actions; the Python base image is deliberately held on 3.13.
- Removed a Cloudflare Pages workflow that deployed a `website/` directory absent from the repository and had failed all 20 of its recorded runs.

## v0.1.0 — 2026-06-22

### AI Content Filtering (4-Tier Pipeline)
- Multi-stage content filtering pipeline: regex patterns → custom organization rules → NER (Named Entity Recognition) → LLM auditor
- Built-in PII/PCI detection: 23 predefined regex patterns (credit cards, SSNs, phone numbers, emails, API keys, etc.)
- Pattern toggles: Enable/disable built-in patterns per organization via management API
- NER entity detection: Presidio + spaCy with transformer fallback for PERSON, LOCATION, NRP, MEDICAL_LICENSE and 10+ entity types
- Entity type toggles: Organizations can selectively disable NER detection for specific entity types
- ShieldGemma 2B default auditor: Lightweight safety classification model (YES/NO policy format)
- Gemma4 2B routing LLM: Efficient model selection and content routing
- Management API: 12 routes for filter configuration, NER settings, auditor administration
- Database support: content_filter_config, content_filter_rules, content_filter_audit_log tables with Alembic migrations
- Comprehensive AI security documentation: OWASP LLM Top 10 coverage, NIST AI RMF alignment, indirect prompt injection mitigation, semantic cache poisoning prevention

### llama.cpp Provider (Local Edge Inference)
- LlamaCppConnector: Full integration with exact tokenization via /tokenize endpoint
- LlamaCppManager: Kubernetes DaemonSet lifecycle management and remote-connect mode
- Management API routes: llama.cpp lifecycle control, model deployment, health checks
- SQLAlchemy model: LlamaCppDeployment with comprehensive Alembic migration
- Configuration: LlamaCppConfig with LLAMACPP provider type and flexible settings
- K8s deployment: DaemonSet pattern for node-local GPU inference, eliminates external API calls for edge/air-gapped deployments
- Helm chart updates: llama.cpp deployment options with configurable GPU layers and model paths

### Ollama Integration
- Ollama provider support: Full integration in proxy and management servers
- Management API: Enhanced routes for Ollama configuration and lifecycle
- Helm chart: Production-ready Ollama deployment templates
- Multi-model support: Seamless routing to Ollama-hosted models

### Multi-Credential Provider Pools
- Multiple API credentials per LLM provider with automatic rotation
- Provider pool management: Add, update, delete credentials with priority ordering
- Failover support: Automatic credential rotation on rate limits or authentication failures
- Alembic migrations: Database schema for credential pool management
- gRPC support: Inter-service communication for credential distribution
- Security hardening: Encrypted credential storage, audit logging of all credential operations

### pgvector Memory Integration
- PostgreSQL pgvector extension support: Semantic vector storage and similarity search
- AILB (AI Load Balancer) memory injection: Automatic context injection from conversation memory
- Read/write splitting: Optimized memory queries with separate read replicas
- Conversation context: Persistent multi-turn conversation state across sessions
- Memory management API: Endpoints for memory configuration, clearing, and administration

### Authentication & Security Hardening
- JWT username in ext claims: Enhanced token transparency for audit logging
- Virtual API keys: FileKeyStore implementation for key management without database queries
- /auth/verify endpoint: Token validation and claims inspection
- Rootless container migration: All services run as non-root user in Kubernetes
- Security context hardening: runAsNonRoot, readOnlyRootFilesystem, capability dropping
- Service-to-service authentication: SPIFFE/SPIRE-compatible mTLS support

### Infrastructure & CI/CD
- Cilium Gateway API HTTPRoute: Modern ingress using Gateway API instead of deprecated Ingress
- GitHub Actions workflows: Comprehensive CI/CD with security scanning and multi-arch builds
- Security scanning integration: Trivy container scanning, CodeQL analysis, dependency audits
- Multi-architecture builds: Native support for amd64 and arm64 platforms
- Kubernetes manifests: Complete alpha/beta environment configuration with proper resource limits
- Image pinning: SHA256 digest pinning for all external base images
- Health checks: Native binary health checks (no curl/wget dependencies)

### Documentation
- MkDocs documentation site: Professional documentation with search and versioning
- API reference: Complete OpenAI-compatible API documentation with examples
- llama.cpp integration guide: Step-by-step setup and deployment instructions
- AI security recommendations: OWASP LLM Top 10 best practices, indirect prompt injection prevention, semantic cache poisoning mitigation strategies, Kubernetes hardening for ML workloads, NIST AI Risk Management Framework alignment
- Production checklist: Pre-deployment validation with AI-specific security audit section
- Integration guides: Setup for Claude, Cursor IDE, VS Code, Open WebUI, Ollama, memory systems
- Troubleshooting: Common issues, performance tuning, security troubleshooting sections
