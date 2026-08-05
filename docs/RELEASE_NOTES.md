# WaddleAI Release Notes

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
