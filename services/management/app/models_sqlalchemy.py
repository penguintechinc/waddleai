"""SQLAlchemy models for database schema initialization and migrations.

Use SQLAlchemy for schema creation and Alembic for migrations. Use PyDAL for
runtime database operations.
"""

import logging
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

logger = logging.getLogger(__name__)

Base = declarative_base()


class Organization(Base):
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    description = Column(Text)
    token_quota_monthly = Column(Integer, default=1000000)
    token_quota_daily = Column(Integer, default=100000)
    default_model = Column(String(255))
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    rpm_limit = Column(Integer, nullable=True)  # Per-org Cilium edge RPM (§12.1); None = unlimited


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(255), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(
        String(50), nullable=False, default="user"
    )  # admin, resource_manager, reporter, user
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    managed_orgs = Column(JSON)  # List of organization IDs for resource managers
    created_at = Column(DateTime, default=datetime.utcnow)
    token_quota_monthly = Column(Integer, default=100000)
    token_quota_daily = Column(Integer, default=10000)
    default_model = Column(String(255))
    enabled = Column(Boolean, default=True)
    # Login tracking fields
    last_login_at = Column(DateTime)
    current_login_at = Column(DateTime)
    last_login_ip = Column(String(50))
    current_login_ip = Column(String(50))
    login_count = Column(Integer, default=0)


class APIKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key_id = Column(String(255), unique=True, nullable=False)
    key_hash = Column(String(255), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    name = Column(String(255), nullable=False)
    token_quota_monthly = Column(Integer)
    token_quota_daily = Column(Integer)
    rate_limit_rpm = Column(Integer, default=60)
    default_model = Column(String(255))
    enabled = Column(Boolean, default=True)
    expires_at = Column(DateTime)
    last_used = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    permissions = Column(JSON)
    allowed_endpoints = Column(JSON)
    api_access_level = Column(String(50))
    # §6A.5 per-key proxy-memory config block (scratchpad/summarization/
    # embedding_cache/schema_dedup). NULL = feature defaults apply.
    # See shared/memory/config.py::resolve_proxy_memory_config.
    proxy_memory = Column(JSON, nullable=True)


class AIProvider(Base):
    __tablename__ = "ai_providers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    provider_type = Column(String(50), nullable=False)  # openai, anthropic, ollama, etc
    endpoint_url = Column(String(512), nullable=False)
    # DEPRECATED: use provider_credentials table instead.
    # Retained for backward-compat fallback until migration 004 runs.
    api_key = Column(String(512))
    model_list = Column(JSON)
    rate_limits = Column(JSON)
    enabled = Column(Boolean, default=True)
    tls_config = Column(JSON)
    extra_config = Column(JSON)  # Provider-specific settings (replaces management_capabilities)
    ailb_sync_enabled = Column(Boolean, default=False)
    priority = Column(Integer, default=100)
    created_at = Column(DateTime, default=datetime.utcnow)

    credentials = relationship(
        "ProviderCredential",
        back_populates="provider",
        cascade="all, delete-orphan",
        lazy="select",
    )


class ProviderCredential(Base):
    """One row per API token/account for an AIProvider.

    Replaces the single api_key field on AIProvider. The pool of enabled
    credentials for a provider is selected from by LLMConnectionManager
    using a CredentialSelector strategy (round-robin by default).
    """

    __tablename__ = "provider_credentials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(
        Integer,
        ForeignKey("ai_providers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label = Column(String(255), nullable=False)
    # Fernet-encrypted with enc: prefix via shared.security.credential_encryption
    api_key = Column(String(512))
    org_id = Column(String(255))  # Optional: OpenAI org, Anthropic workspace
    account_meta = Column(JSON)  # Arbitrary provider-specific account fields
    weight = Column(Integer, nullable=False, default=100)
    enabled = Column(Boolean, nullable=False, default=True)
    request_count = Column(BigInteger, nullable=False, default=0)
    token_count = Column(BigInteger, nullable=False, default=0)
    last_used_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    plan_budget = Column(JSON, nullable=True)  # §7.3 window-based plan budget config

    provider = relationship("AIProvider", back_populates="credentials")


class OllamaDeployment(Base):
    __tablename__ = "ollama_deployments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    endpoint_url = Column(String(512), nullable=False)
    deployment_type = Column(String(50))  # docker, kubernetes, external
    docker_compose_config = Column(JSON)
    gpu_config = Column(JSON)
    resource_limits = Column(JSON)
    status = Column(String(50))  # running, stopped, pulling, error
    health_status = Column(String(50))
    last_health_check = Column(DateTime)
    auto_start = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class LlamaCppDeployment(Base):
    __tablename__ = "llamacpp_deployments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    deployment_type = Column(String(50), nullable=False, default="kubernetes")  # kubernetes, remote
    status = Column(
        String(50), nullable=False, default="pending"
    )  # pending, deploying, running, stopped, error
    status_message = Column(Text)

    # Model
    model_name = Column(String(255), nullable=False)
    model_url = Column(String(512))  # GGUF download URL (kubernetes mode)
    model_filename = Column(String(255))  # filename inside volume

    # Inference params
    n_ctx = Column(Integer, default=4096)
    n_gpu_layers = Column(Integer, default=-1)  # -1 = all layers on GPU
    gpu_count = Column(Integer, default=1)

    # Connection
    endpoint_url = Column(
        String(512)
    )  # set by manager after deploy, or provided directly for remote

    # Kubernetes
    k8s_namespace = Column(String(255), default="waddleai")
    k8s_daemonset_name = Column(String(255))
    node_selector = Column(JSON)  # e.g. {"waddleai/gpu-tier": "a100"}
    node_affinity = Column(JSON)  # optional advanced scheduling
    model_cache_claim = Column(String(255))  # PVC name for model cache; None = emptyDir

    # Resource limits for containers
    cpu_request = Column(String(50))  # e.g. "2000m", "2"
    cpu_limit = Column(String(50))  # e.g. "4000m", "4"
    memory_request = Column(String(50))  # e.g. "8Gi", "8192Mi"
    memory_limit = Column(String(50))  # e.g. "16Gi", "16384Mi"

    created_at = Column(DateTime, default=datetime.utcnow)
    modified_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OllamaModel(Base):
    __tablename__ = "ollama_models"

    id = Column(Integer, primary_key=True, autoincrement=True)
    deployment_id = Column(Integer, ForeignKey("ollama_deployments.id"), nullable=False)
    model_name = Column(String(255), nullable=False)
    model_tag = Column(String(50), default="latest")
    status = Column(String(50), default="unknown")  # available, pulling, failed, removed
    size_bytes = Column(BigInteger)
    pull_progress = Column(JSON)
    last_updated = Column(DateTime, default=datetime.utcnow)
    auto_pull = Column(Boolean, default=False)


class VirtualKey(Base):
    __tablename__ = "virtual_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    organization_id = Column(Integer, ForeignKey("organizations.id"))
    name = Column(String(255), nullable=False)
    key_prefix = Column(String(20))  # wa-xxx for display
    key_hash = Column(String(255))  # Hashed full key
    allowed_models = Column(JSON)
    allowed_providers = Column(JSON)
    budget_limit_daily = Column(Integer)
    budget_limit_monthly = Column(Integer)
    tpm_limit = Column(Integer)  # Tokens per minute
    rpm_limit = Column(Integer)  # Requests per minute
    budget_monthly_tokens = Column(Integer, nullable=True)  # Monthly token limit; None = unlimited
    budget_monthly_usd = Column(
        Integer, nullable=True
    )  # Monthly USD limit in micro-USD; None = unlimited
    enabled = Column(Boolean, default=True)
    expires_at = Column(DateTime)
    last_used = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)


class TokenUsage(Base):
    __tablename__ = "token_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    virtual_key_id = Column(Integer, ForeignKey("virtual_keys.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    organization_id = Column(Integer, ForeignKey("organizations.id"))
    date = Column(DateTime)  # Date for this usage record
    waddleai_tokens = Column(Integer, default=0)
    llm_tokens = Column(JSON)  # Breakdown by model/provider
    tokens_input_total = Column(Integer, default=0)
    tokens_output_total = Column(Integer, default=0)
    request_count = Column(Integer, default=0)
    cost_usd_total = Column(Integer, default=0)  # Store as cents
    last_updated = Column(DateTime, default=datetime.utcnow)
    source = Column(String(50), default="aiproxy")  # aiproxy, ailb_import (migration 007 fold), etc
    estimated = Column(
        Boolean, default=False
    )  # True if usage was estimated (missing from provider)
    # Response cache accounting (spec §6.4, migration 009a). cache_status is
    # one of exact|semantic|upstream|miss (None for rows predating the cache
    # feature). tokens_saved is 0 for misses and non-cache-aware rows.
    cache_status = Column(String(16), nullable=True)
    tokens_saved = Column(Integer, default=0)


class UsageCache(Base):
    __tablename__ = "usage_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    virtual_key_id = Column(Integer, ForeignKey("virtual_keys.id"))
    organization_id = Column(Integer, ForeignKey("organizations.id"))
    period = Column(String(20), nullable=False)  # daily, monthly
    period_start = Column(DateTime, nullable=False)
    waddleai_tokens_used = Column(Integer, default=0)
    llm_tokens_used = Column(JSON)
    requests_made = Column(Integer, default=0)
    last_updated = Column(DateTime, default=datetime.utcnow)


class TokenConversionRate(Base):
    __tablename__ = "token_conversion_rates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(100), nullable=False)
    model = Column(String(255), nullable=False)
    # Float, not Integer: real rates carry sub-cent decimals (e.g. 0.00015).
    # Widened from Integer by migration 007 immediately before seeding.
    input_rate = Column(Float, nullable=False)  # Cost per token
    output_rate = Column(Float, nullable=False)  # Cost per token
    base_cost_per_waddleai_token = Column(Float, default=0.001)  # Base cost multiplier
    effective_date = Column(DateTime, default=datetime.utcnow)
    enabled = Column(Boolean, default=True)


class ModelRegistry(Base):
    """Catalog of admissible model weights (§2.2/§2.3 platform spec).

    One row per selectable model: carries the license/origin metadata the
    §2.2 PRC-origin deny-list and §2.3 dual-default license-admissibility
    test are checked against, at both registration and fleet `place_model`
    time. `is_utility` marks internal-function models (routing classifier,
    security auditor, embeddings) that are excluded from Free-tier model
    count caps (§2.4 Q#7). Seeded by migration 008 with the §2.3
    dual-default set; §7.1 `model_assignments` (migration 010) is what
    later maps a tool type to one of these rows as its active default.
    """

    __tablename__ = "model_registry"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    role = Column(String(100), nullable=False)  # routing_classifier, security_auditor, embeddings
    license = Column(String(100), nullable=False)
    origin = Column(String(100), nullable=False)  # Org/country; checked vs. the §2.2 deny-list
    min_vram = Column(Integer, nullable=True)  # Approx. min VRAM in GB; operator-adjustable
    ollama_tag = Column(String(255), nullable=True)
    resolved_digest = Column(String(128), nullable=True)  # sha256, set once resolved at first pull
    is_utility = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class RoutingRule(Base):
    __tablename__ = "routing_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    priority = Column(Integer, default=100)
    match_conditions = Column(JSON)  # Model patterns, user groups, etc
    target_provider_id = Column(Integer, ForeignKey("ai_providers.id"))
    fallback_provider_id = Column(Integer, ForeignKey("ai_providers.id"))
    load_balancing_strategy = Column(String(50))  # round_robin, least_latency, cost_optimized
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class OllamaModelRoute(Base):
    __tablename__ = "ollama_model_routes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    deployment_id = Column(Integer, ForeignKey("ollama_deployments.id"), nullable=False)
    model_pattern = Column(String(255), nullable=False)  # Regex pattern for model matching
    priority = Column(Integer, default=100)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class UsageLog(Base):
    __tablename__ = "usage_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    api_key_id = Column(Integer, ForeignKey("api_keys.id"))
    organization_id = Column(Integer, ForeignKey("organizations.id"))
    request_id = Column(String(255))
    endpoint = Column(String(512))
    model = Column(String(255))
    provider = Column(String(100))
    input_tokens = Column(Integer)
    output_tokens = Column(Integer)
    total_tokens = Column(Integer)
    response_time_ms = Column(Integer)
    status_code = Column(Integer)
    error_message = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)


class SecurityLog(Base):
    __tablename__ = "security_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    virtual_key_id = Column(Integer, ForeignKey("virtual_keys.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    organization_id = Column(Integer, ForeignKey("organizations.id"))
    request_hash = Column(String(255))
    threat_type = Column(String(100))
    severity = Column(String(50))
    blocked = Column(Boolean, default=False)
    prompt_sample = Column(Text)
    detection_rules = Column(JSON)
    ip_address = Column(String(50))


class EmbeddingSettings(Base):
    __tablename__ = "embedding_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, nullable=True)  # NULL = global default
    backend = Column(String(50), nullable=False, default="ollama")  # ollama, openai, anthropic
    model = Column(String(255), nullable=False, default="nomic-embed-text")
    ollama_host = Column(String(500), default="http://localhost:11434")
    dimensions = Column(Integer, default=768)  # nomic=768, openai-3-small=1536
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class MemoryEmbedding(Base):
    __tablename__ = "memory_embeddings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    organization_id = Column(Integer, nullable=False, index=True)
    session_id = Column(String(255), nullable=False, index=True)
    content = Column(Text, nullable=False)
    embedding_json = Column(
        Text
    )  # JSON-serialized float array; replaced by pgvector column after extension is confirmed
    role = Column(String(50), nullable=False)  # user, assistant
    created_at = Column(DateTime, default=datetime.utcnow)
    metadata_ = Column("metadata", JSON, default=dict)
    # Memory access-control scope: 'user' (personal, default) | 'org' (shared).
    # Spec §9.7 field names; v0.4 adds more scope values without renaming.
    scope_type = Column(
        String(20), nullable=False, default="user", server_default="user", index=True
    )
    author_user_id = Column(Integer, nullable=False, index=True)
    # §9.7 retrofit (migration 009b) -- the remaining scope/trust/attribution
    # columns; scope_type/author_user_id above predate this (migration 006).
    scope_ref = Column(String(255), nullable=True)
    trust_tier = Column(
        String(20), nullable=False, default="unverified", server_default="unverified"
    )
    version = Column(Integer, nullable=False, default=1, server_default="1")
    superseded_by = Column(Integer, nullable=True)
    status = Column(
        String(20), nullable=False, default="active", server_default="active", index=True
    )
    expires_at = Column(DateTime, nullable=True)


class SessionScratchpad(Base):
    """Per-(org, session, user) KV working set (§6A.1).

    Valkey holds the hot path; this table is the durable spill/re-warm
    tier. All rows are session-scoped, trust `unverified`, and pass
    through shared.memory.provenance.filter_on_write before persist.
    """

    __tablename__ = "session_scratchpad"

    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    session_id = Column(String(255), nullable=False, index=True)
    key = Column(String(255), nullable=False)
    value = Column(Text, nullable=True)
    scope_type = Column(String(20), nullable=False, default="session", server_default="session")
    scope_ref = Column(String(255), nullable=True)
    author_user_id = Column(Integer, nullable=True)
    trust_tier = Column(
        String(20), nullable=False, default="unverified", server_default="unverified"
    )
    version = Column(Integer, nullable=False, default=1, server_default="1")
    superseded_by = Column(Integer, nullable=True)
    status = Column(String(20), nullable=False, default="active", server_default="active")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("org_id", "session_id", "user_id", "key", name="uq_scratchpad_key"),
    )


class ConversationSummary(Base):
    """Threshold-triggered rolling conversation summary (§6A.2).

    Versioned via ``superseded_by`` -- a repeat summarization never mutates
    a prior row; it writes a new version and points the old one forward.
    """

    __tablename__ = "conversation_summaries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String(255), nullable=False, index=True)
    org_id = Column(Integer, nullable=False, index=True)
    summary = Column(Text, nullable=False)
    covers_through_turn = Column(Integer, nullable=False)
    tokens_summarized = Column(Integer, nullable=False, default=0, server_default="0")
    model_used = Column(String(255), nullable=True)
    scope_type = Column(String(20), nullable=False, default="session", server_default="session")
    scope_ref = Column(String(255), nullable=True)
    author_user_id = Column(Integer, nullable=True)
    trust_tier = Column(
        String(20), nullable=False, default="unverified", server_default="unverified"
    )
    version = Column(Integer, nullable=False, default=1, server_default="1")
    superseded_by = Column(Integer, nullable=True)
    status = Column(String(20), nullable=False, default="active", server_default="active")
    updated_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("org_id", "conversation_id", "version", name="uq_convsum_version"),
    )


class EmbeddingCacheEntry(Base):
    """Content-addressed embedding cache (§6A.3): (model, content_hash) -> vector.

    Deterministic function cache only -- no org column, no plaintext. A
    caller must already possess the content to compute the key, so no
    org-readable data can leak from this table (see shared/memory/embedding_cache.py).
    """

    __tablename__ = "embedding_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model = Column(String(255), nullable=False)
    content_hash = Column(String(64), nullable=False)
    # SQLite/ORM fallback; native vector(768) added on postgres only.
    embedding_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("model", "content_hash", name="uq_embcache_model_hash"),)


class RAGDocument(Base):
    __tablename__ = "rag_documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, nullable=False, index=True)
    collection = Column(String(255), nullable=False, index=True)
    content = Column(Text, nullable=False)
    embedding_json = Column(Text)  # JSON-serialized float array
    source = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    metadata_ = Column("metadata", JSON, default=dict)


class ConversationMemoryConfig(Base):
    __tablename__ = "conversation_memory_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, unique=True, nullable=False)
    enabled = Column(Boolean, default=True)
    max_messages = Column(Integer, default=20)
    similarity_threshold = Column(Float, default=0.7)
    created_at = Column(DateTime, default=datetime.utcnow)


class RAGConfig(Base):
    __tablename__ = "rag_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, unique=True, nullable=False)
    enabled = Column(Boolean, default=True)
    collection = Column(String(255), nullable=False, default="default")
    top_k = Column(Integer, default=5)
    similarity_threshold = Column(Float, default=0.7)
    created_at = Column(DateTime, default=datetime.utcnow)


class ModelAssignment(Base):
    """Tool-type -> model assignment (evolves the legacy routing_matrix, §7.1/§7.6).

    The admin's steering wheel: maps a tool type to a default model plus an
    optional escalation model, scoped global or per-org. Pre-declared rows
    seed WaddleAI's own internal functions (security-audit, routing-classifier,
    embeddings, docs-fetch, summarize) per the §2.3 dual-default pattern.
    complexity/region are retained (now nullable) for backward-compat with the
    original routing_matrix lookup shape; new rows written by the smart-routing
    engine leave them unset.
    """

    __tablename__ = "model_assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tool_type = Column(String(50), nullable=False)
    complexity = Column(String(10), nullable=True)
    region = Column(String(5), nullable=True)
    model_name = Column(String(255), nullable=False)
    model_params = Column(String(50))
    vram_gb = Column(Integer)
    capability_score = Column(Float)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Optional: pin this route to a specific provider credential by label.
    # When set, LLMConnectionManager bypasses the pool selector and uses this
    # credential directly. When None, pool selection applies normally.
    credential_label = Column(String(255), nullable=True)
    # §7.1/§7.3: escalation target for this tool type (overrides the org
    # policy's escalation_target when set); ordered cross-provider equivalents
    # for §7.3 availability failover (provider_failover=same_class).
    escalation_model = Column(String(255), nullable=True)
    fallback_models = Column(JSON, nullable=True)
    # 'global' (scope_ref NULL) or 'org' (scope_ref = organizations.id).
    scope = Column(String(10), nullable=False, default="global", server_default="global")
    scope_ref = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("tool_type", "scope", "scope_ref", name="uq_model_assignments_lookup"),
    )


class ModelConfig(Base):
    """Per-model routing metadata (§7.6: replaces the hardcoded model_configs dict).

    Mirrors shared.utils.request_router.ModelConfig: preferred providers, cost
    per token, context/max tokens, and capability tags. Read by the smart
    routing engine and (once migration 010 is live) by request_router itself
    instead of the in-process dict.
    """

    __tablename__ = "model_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model_name = Column(String(255), unique=True, nullable=False)
    preferred_providers = Column(JSON, nullable=False, default=list)
    cost_per_token = Column(JSON, nullable=False, default=dict)
    max_tokens = Column(Integer, nullable=False)
    context_length = Column(Integer, nullable=False)
    capabilities = Column(JSON, nullable=False, default=list)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ModelAlias(Base):
    """Admin-controlled model aliasing (§7.2 stage 0).

    Redirects a client-supplied model name (e.g. gpt-4o) to a target model,
    optionally pinning a target provider. NULL organization_id is a global
    rule; an org-scoped row overrides the global one for that org.
    """

    __tablename__ = "model_aliases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    source_model = Column(String(255), nullable=False)
    target_model = Column(String(255), nullable=False)
    target_provider = Column(String(100), nullable=True)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "source_model", name="uq_model_aliases_org_source"),
    )


class RoutingRuleV2(Base):
    """Stage-1 heuristic routing rules (§7.2): cheap, deterministic, no LLM.

    Rules are evaluated in priority order; the first whose ``match`` predicate
    fits the request fires its ``action`` (tool_type and/or route). Replaces
    ad-hoc keyword matching in the legacy routing agent.
    """

    __tablename__ = "routing_rules_v2"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    priority = Column(Integer, nullable=False, default=100)
    match = Column(JSON, nullable=False, default=dict)
    action = Column(JSON, nullable=False, default=dict)
    enabled = Column(Boolean, default=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_at = Column(DateTime, default=datetime.utcnow)


class RoutingPolicy(Base):
    """Per-org smart-routing policy (§7.3): mode, escalation, sensitivity, budgets.

    One row per organization; a missing row means engine defaults apply. Also
    absorbs the legacy Valkey ``routing:instructions`` NL-routing UX via
    classifier_prompt (§7.6).
    """

    __tablename__ = "routing_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    mode = Column(String(20), nullable=False, default="local_first")
    escalation_threshold = Column(Integer, nullable=False, default=3)
    escalation_target = Column(String(255), nullable=True)
    classifier_prompt = Column(Text, nullable=True)
    de_escalation = Column(String(20), nullable=False, default="idle_reset")
    idle_reset_minutes = Column(Integer, nullable=False, default=10)
    sensitivity_routing = Column(String(20), nullable=False, default="local_only")
    budget_pressure_enabled = Column(Boolean, nullable=False, default=True)
    provider_failover = Column(String(20), nullable=False, default="off")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RoutingDecisionTrace(Base):
    """First-class routing decision trace (§7.4): the durable corpus.

    One row per routed request: requirements vector, tool-type source, rules
    fired, classifier output, assignment applied, capability vetoes, qualified
    candidates + scores, pressure signals, and the final choice. Powers the
    per-request WebUI view, aggregate tuning views, and future heuristics
    training data.
    """

    __tablename__ = "routing_decision_traces"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    request_id = Column(String(64), nullable=False, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    requirements = Column(JSON, nullable=True)
    tool_type = Column(String(50), nullable=True)
    tool_type_source = Column(String(20), nullable=True)  # explicit | heuristic | classifier
    rules_fired = Column(JSON, nullable=True)
    classifier_output = Column(JSON, nullable=True)
    assignment_model = Column(String(255), nullable=True)
    capability_veto = Column(Boolean, default=False)
    veto_reason = Column(String(255), nullable=True)
    qualified_candidates = Column(JSON, nullable=True)
    pressure_signals = Column(JSON, nullable=True)
    final_model = Column(String(255), nullable=True)
    routed_from = Column(JSON, nullable=True)
    escalated = Column(Boolean, default=False)

    __table_args__ = (
        Index("idx_rdt_org_timestamp", "organization_id", "timestamp"),
    )


class AILBUsageRecord(Base):
    __tablename__ = "ailb_usage_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(255), nullable=False, index=True)
    api_key_id = Column(String(255), index=True)
    model = Column(String(255), nullable=False)
    provider = Column(String(100), nullable=False)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    latency_ms = Column(Integer)
    request_id = Column(String(255))
    timestamp = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class ContentFilterRule(Base):
    __tablename__ = "content_filter_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    rule_type = Column(String(20), nullable=False)  # 'builtin_pii', 'custom_string', 'custom_regex'
    target = Column(String(10), nullable=False, default="both")  # 'input', 'output', 'both'
    pattern = Column(Text, nullable=False)
    action = Column(String(10), nullable=False, default="log")  # 'block', 'redact', 'log'
    redact_with = Column(String(100), nullable=True, default="[REDACTED]")
    enabled = Column(Boolean, nullable=False, default=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_cfr_org_enabled", "organization_id", "enabled"),
        Index("idx_cfr_target", "target"),
    )


class ContentFilterAuditLog(Base):
    __tablename__ = "content_filter_audit_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)
    phase = Column(String(10), nullable=False)  # 'input', 'output'
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    api_key_id = Column(
        Integer, ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ip_address = Column(String(45), nullable=True)
    action_taken = Column(String(10), nullable=False)  # 'allow', 'block', 'redact', 'log'
    violations_json = Column(JSON, nullable=True)
    text_sample = Column(Text, nullable=True)  # First 200 chars for audit
    auditor_used = Column(Boolean, nullable=False, default=False)
    auditor_decision = Column(String(10), nullable=True)  # 'block', 'allow', NULL if not invoked
    request_id = Column(String(64), nullable=True)  # For correlation with proxy logs
    # Security v2 (§8.9) -- migration 011
    policy_id = Column(
        Integer,
        ForeignKey("security_policies.id", ondelete="SET NULL", name="fk_cfal_policy_id"),
        nullable=True,
    )
    intent_categories = Column(JSON, nullable=True)  # Intent-classifier per-category verdicts
    degraded = Column(Boolean, nullable=False, default=False)  # True if fail_mode=degrade fired
    bypass_grant_id = Column(
        Integer,
        ForeignKey("security_bypass_grants.id", ondelete="SET NULL", name="fk_cfal_bypass_grant"),
        nullable=True,
    )
    redaction_counts = Column(JSON, nullable=True)  # Per-category counts for metering (§8.7/§8.9)

    __table_args__ = (
        Index("idx_cfal_timestamp", "timestamp"),
        Index("idx_cfal_user", "user_id", "timestamp"),
        Index("idx_cfal_org", "organization_id", "timestamp"),
        Index("idx_cfal_action", "action_taken"),
    )


class CacheConfig(Base):
    """Response-cache configuration, resolved at key > org > global precedence.

    One global default row is seeded by migration 009a (scope_type='global',
    scope_ref=NULL); org- and key-scoped rows override it. See
    shared.cache.config.CacheConfigResolver for the resolution logic and
    services.management.app.api.v1.cache_configs for the CRUD surface (§6.4).
    """

    __tablename__ = "cache_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope_type = Column(String(20), nullable=False)  # 'global' | 'org' | 'key'
    scope_ref = Column(String(255), nullable=True)  # org_id/vkey_id as string; NULL for global
    exact_enabled = Column(Boolean, nullable=False, default=True)
    semantic_enabled = Column(Boolean, nullable=False, default=False)
    semantic_threshold = Column(Float, nullable=False, default=0.95)
    ttl_seconds = Column(Integer, nullable=False, default=86400)
    max_entry_kb = Column(Integer, nullable=False, default=256)
    anthropic_cache_control = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("scope_type", "scope_ref", name="uq_cache_configs_scope"),)


class ResponseCacheEntry(Base):
    """Restricted semantic response-cache row (spec §6.2).

    prompt_embedding_json is the portable (SQLite-safe) JSON-serialized float
    array, following the MemoryEmbedding pattern. init_schema() additionally
    adds a native pgvector `prompt_embedding vector(768)` column + HNSW index
    when running against PostgreSQL; migration 009a does the equivalent for
    already-provisioned databases.
    """

    __tablename__ = "response_cache_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, nullable=False, index=True)
    scope_key = Column(String(255), nullable=True)
    model_class = Column(String(255), nullable=False)
    prompt_embedding_json = Column(Text)  # JSON-serialized float array; SQLite-safe fallback
    context_hash = Column(String(64), nullable=False)
    response = Column(JSON, nullable=False)
    hit_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)

    __table_args__ = (Index("idx_rce_org_model_expires", "org_id", "model_class", "expires_at"),)
class SecurityPolicy(Base):
    """Scoped security policy row (§8.1).

    Resolution chain is global -> org -> model -> tool/function, most-specific
    field wins (per-field merge, not whole-row replace -- see
    shared/security/policy_resolver.py::PolicyResolver).
    """

    __tablename__ = "security_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope_type = Column(String(10), nullable=False)  # 'global' | 'org' | 'model' | 'tool'
    scope_ref = Column(String(255), nullable=True)  # NULL=global; else org id / model / tool name
    # Configurable columns are nullable: NULL = inherit from the
    # next-more-general scope (see migration 011 docstring). Only the
    # seeded 'global' row is fully populated.
    tier1_enabled = Column(Boolean, nullable=True)
    tier2_enabled = Column(Boolean, nullable=True)
    tier3_enabled = Column(Boolean, nullable=True)
    tier4_enabled = Column(Boolean, nullable=True)
    tier4_model = Column(String(100), nullable=True)
    intent_classifier_enabled = Column(Boolean, nullable=True)
    intent_categories = Column(JSON, nullable=True)
    direction = Column(String(10), nullable=False, default="both")  # 'input'|'output'|'both'
    block_action = Column(String(10), nullable=True)  # 'block' | 'redact' | 'flag'
    fail_mode = Column(String(10), nullable=True)  # 'open' | 'closed' | 'degrade'
    on_unclassifiable = Column(String(10), nullable=True)  # 'reject' | 'degrade'
    auditor_timeout_ms = Column(Integer, nullable=True)
    latency_budget_ms = Column(Integer, nullable=True)
    sample_rate = Column(Integer, nullable=True)
    upstream_filters = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_secpol_scope", "scope_type", "scope_ref", "direction", unique=True),
    )


class SecurityBypassGrant(Base):
    """Scope-based authorized bypass grant (§8.6) -- researchers/red teams.

    Grantable per user or virtual key; never a role check. Enforcement lives
    in shared/security/bypass.py::BypassResolver, which additionally requires
    the `security:bypass` OIDC scope on the caller's token.
    """

    __tablename__ = "security_bypass_grants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subject_type = Column(String(10), nullable=False)  # 'user' | 'vkey'
    subject_ref = Column(String(255), nullable=False)
    mode = Column(String(10), nullable=False, default="shadow")  # 'shadow' | 'skip'
    scope_narrow = Column(JSON, nullable=True)
    include_upstream = Column(Boolean, nullable=False, default=False)
    granted_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_secbypass_subject", "subject_type", "subject_ref"),
        Index("idx_secbypass_expires", "expires_at"),
    )


def init_schema(database_url: str):
    """Initialize database schema using SQLAlchemy.

    Enables the pgvector extension and creates all tables. After table creation,
    adds native vector columns (requires pgvector) and IVFFlat indexes for fast
    similarity search.
    """
    # SQLAlchemy 2.0+ requires 'postgresql://' not 'postgres://'
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    engine = create_engine(database_url)

    # Enable pgvector extension (optional — degrades gracefully without it)
    vector_available = False
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        vector_available = True
    except Exception as e:
        logger.warning(
            "pgvector extension not available: %s. "
            "Vector search features (memory embeddings, RAG) will be disabled.",
            e,
        )

    # Create all tables if they don't exist
    Base.metadata.create_all(engine, checkfirst=True)

    # Add native vector columns and IVFFlat indexes (pgvector-specific, idempotent)
    if vector_available:
        with engine.connect() as conn:
            # Add vector columns if not present
            conn.execute(
                text(
                    "ALTER TABLE memory_embeddings "
                    "ADD COLUMN IF NOT EXISTS embedding vector(768)"
                )
            )
            conn.execute(
                text("ALTER TABLE rag_documents " "ADD COLUMN IF NOT EXISTS embedding vector(768)")
            )
            # IVFFlat indexes for cosine similarity search
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS memory_embeddings_emb_idx "
                    "ON memory_embeddings USING ivfflat (embedding vector_cosine_ops) "
                    "WITH (lists = 100)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS rag_documents_emb_idx "
                    "ON rag_documents USING ivfflat (embedding vector_cosine_ops) "
                    "WITH (lists = 100)"
                )
            )
            # Response cache: native vector column + HNSW (spec §6.2). HNSW
            # (not IVFFlat) matches migration 009a's index type exactly.
            conn.execute(
                text(
                    "ALTER TABLE response_cache_entries "
                    "ADD COLUMN IF NOT EXISTS prompt_embedding vector(768)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_rce_prompt_embedding_hnsw "
                    "ON response_cache_entries USING hnsw (prompt_embedding vector_cosine_ops)"
                )
            )
            conn.commit()

    return engine
