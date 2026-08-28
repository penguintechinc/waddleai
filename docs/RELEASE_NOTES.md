# WaddleAI Release Notes

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
