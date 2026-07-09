"""
SQLAlchemy models for database schema initialization and migrations
Use SQLAlchemy for schema creation and Alembic for migrations
Use PyDAL for runtime database operations
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


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(255), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(50), nullable=False, default="user")  # admin, resource_manager, reporter, user
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
    status = Column(String(50), nullable=False, default="pending")  # pending, deploying, running, stopped, error
    status_message = Column(Text)

    # Model
    model_name = Column(String(255), nullable=False)
    model_url = Column(String(512))       # GGUF download URL (kubernetes mode)
    model_filename = Column(String(255))  # filename inside volume

    # Inference params
    n_ctx = Column(Integer, default=4096)
    n_gpu_layers = Column(Integer, default=-1)  # -1 = all layers on GPU
    gpu_count = Column(Integer, default=1)

    # Connection
    endpoint_url = Column(String(512))    # set by manager after deploy, or provided directly for remote

    # Kubernetes
    k8s_namespace = Column(String(255), default="waddleai")
    k8s_daemonset_name = Column(String(255))
    node_selector = Column(JSON)   # e.g. {"waddleai/gpu-tier": "a100"}
    node_affinity = Column(JSON)   # optional advanced scheduling

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
    ailb_key_id = Column(String(255))  # Synced AILB key ID
    ailb_sync_status = Column(String(50))  # synced, pending, failed
    allowed_models = Column(JSON)
    allowed_providers = Column(JSON)
    budget_limit_daily = Column(Integer)
    budget_limit_monthly = Column(Integer)
    tpm_limit = Column(Integer)  # Tokens per minute
    rpm_limit = Column(Integer)  # Requests per minute
    enabled = Column(Boolean, default=True)
    expires_at = Column(DateTime)
    last_used = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)


class MarchProxyAILBSync(Base):
    __tablename__ = "marchproxy_ailb_sync"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("ai_providers.id"))
    ailb_instance_id = Column(String(255))
    ailb_route_id = Column(String(255))
    sync_status = Column(String(50))  # synced, pending, failed, deleted
    last_synced = Column(DateTime)
    sync_error = Column(Text)
    config_hash = Column(String(64))  # Hash to detect config changes
    created_at = Column(DateTime, default=datetime.utcnow)


class AILBUsageEvent(Base):
    __tablename__ = "ailb_usage_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(255), unique=True)
    virtual_key_id = Column(Integer, ForeignKey("virtual_keys.id"))
    ailb_key_id = Column(String(255))
    request_id = Column(String(255))
    model = Column(String(255))
    provider = Column(String(100))
    input_tokens = Column(Integer)
    output_tokens = Column(Integer)
    cost_usd = Column(Integer)  # Store as cents to avoid floating point
    latency_ms = Column(Integer)
    status = Column(String(50))  # success, error, rate_limited
    error_message = Column(Text)
    timestamp = Column(DateTime)
    processed = Column(Boolean, default=False)
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
    input_rate = Column(Integer, nullable=False)  # Cost per token
    output_rate = Column(Integer, nullable=False)  # Cost per token
    base_cost_per_waddleai_token = Column(Integer, default=1)  # Base cost multiplier
    effective_date = Column(DateTime, default=datetime.utcnow)
    enabled = Column(Boolean, default=True)


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


class RoutingMatrixEntry(Base):
    __tablename__ = "routing_matrix"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tool_type = Column(String(50), nullable=False)
    complexity = Column(String(10), nullable=False)
    region = Column(String(5), nullable=False)
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

    __table_args__ = (UniqueConstraint("tool_type", "complexity", "region", name="uq_routing_matrix_lookup"),)


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
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
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
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    api_key_id = Column(Integer, ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True, index=True)
    ip_address = Column(String(45), nullable=True)
    action_taken = Column(String(10), nullable=False)  # 'allow', 'block', 'redact', 'log'
    violations_json = Column(JSON, nullable=True)
    text_sample = Column(Text, nullable=True)  # First 200 chars for audit
    auditor_used = Column(Boolean, nullable=False, default=False)
    auditor_decision = Column(String(10), nullable=True)  # 'block', 'allow', NULL if not invoked
    request_id = Column(String(64), nullable=True)  # For correlation with proxy logs

    __table_args__ = (
        Index("idx_cfal_timestamp", "timestamp"),
        Index("idx_cfal_user", "user_id", "timestamp"),
        Index("idx_cfal_org", "organization_id", "timestamp"),
        Index("idx_cfal_action", "action_taken"),
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
            conn.execute(text("ALTER TABLE memory_embeddings " "ADD COLUMN IF NOT EXISTS embedding vector(768)"))
            conn.execute(text("ALTER TABLE rag_documents " "ADD COLUMN IF NOT EXISTS embedding vector(768)"))
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
            conn.commit()

    return engine
