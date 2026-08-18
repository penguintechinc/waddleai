"""
WaddleAI Database Models
Shared database models for both proxy and management servers
"""

import os
from datetime import date, datetime

from penguin_dal import DAL, Field


def get_db(db_uri=None, migrate=False):
    """Initialize database connection with all models.

    Args:
        db_uri: Database URI. Defaults to DATABASE_URL env var / local sqlite file.
        migrate: If True, let PyDAL create/alter tables itself (used only by the
            contract-test harness against an empty, ephemeral sqlite file). In every
            other context (including production) this stays False: Alembic
            (services/management/app/models_sqlalchemy.py) is the sole schema
            authority and this module must not auto-migrate against it.
    """
    if db_uri is None:
        db_uri = os.getenv("DATABASE_URL", "sqlite://waddleai.db")

    # penguin_dal.DAL has no fake_migrate_all param (PyDAL-only concept: it
    # skips CREATE TABLE/ALTER and only rewrites PyDAL's .table fingerprint
    # files for adopting an already-existing external schema). penguin_dal's
    # define_table() always does CREATE TABLE IF NOT EXISTS, matching the
    # fake_migrate_all=False behavior this call previously requested.
    db = DAL(db_uri, migrate=migrate)
    define_tables(db)
    return db


def define_tables(db):
    """Define all database tables"""

    # Organizations for Multi-tenancy
    db.define_table(
        "organizations",
        Field("name", unique=True, notnull=True),
        Field("description", "text"),
        Field("token_quota_monthly", "integer", default=1000000),
        Field("token_quota_daily", "integer", default=100000),
        Field("default_model", "string"),  # Default model for organization
        Field("enabled", "boolean", default=True),
        Field("created_at", "datetime", default=datetime.utcnow),
    )

    # Users and Authentication
    db.define_table(
        "users",
        Field("username", unique=True, notnull=True),
        Field("email", unique=True, notnull=True),
        Field("password_hash", "password", notnull=True),
        Field("role", "string", notnull=True),  # admin, resource_manager, reporter, user
        Field("organization_id", "reference organizations", notnull=True),
        Field("managed_orgs", "list:reference organizations"),  # For resource managers
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("token_quota_monthly", "integer", default=100000),
        Field("token_quota_daily", "integer", default=10000),
        Field("default_model", "string"),  # Default model for user
        Field("enabled", "boolean", default=True),
    )

    # API Keys with Usage Limits
    db.define_table(
        "api_keys",
        Field("key_id", unique=True, notnull=True),
        Field("key_hash", "password", notnull=True),  # Hashed API key
        Field("user_id", "reference users", notnull=True),
        Field("organization_id", "reference organizations", notnull=True),
        Field("name", "string", notnull=True),  # Human readable name
        Field("token_quota_monthly", "integer"),  # Override user quota
        Field("token_quota_daily", "integer"),  # Override user quota
        Field("rate_limit_rpm", "integer", default=60),  # Requests per minute
        Field("default_model", "string"),  # Default model for this API key
        Field("enabled", "boolean", default=True),
        Field("expires_at", "datetime"),
        Field("last_used", "datetime"),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("permissions", "json"),  # Scoped permissions
        Field("allowed_endpoints", "list:string"),  # Endpoint restrictions
        Field("api_access_level", "string"),  # admin_api, management_api, proxy_api
    )

    # Connection Links (LLM Providers)
    # TODO: This file uses raw PyDAL and is used by the proxy service.
    # The management service uses penguin-dal (auto-reflects schema from SQLAlchemy models).
    # provider_credentials is NOT defined here because the proxy service should be
    # migrated to penguin-dal in a dedicated ticket — at that point this file is removed.
    # See: services/management/app/models_sqlalchemy.py for the canonical schema.
    db.define_table(
        "connection_links",
        Field("name", unique=True, notnull=True),
        Field("provider", "string", notnull=True),  # ollama, anthropic, openai
        Field("endpoint_url", notnull=True),
        Field("api_key", "password"),
        Field("model_list", "json"),
        Field("rate_limits", "json"),
        Field("enabled", "boolean", default=True),
        Field("tls_config", "json"),
        Field("management_capabilities", "json"),  # For Ollama: pull, remove, list
    )

    # Ollama Model Registry
    db.define_table(
        "ollama_models",
        Field("link_id", "reference connection_links", notnull=True),
        Field("model_name", notnull=True),
        Field("model_tag", default="latest"),
        Field("status", "string", default="unknown"),  # available, pulling, failed, removed
        Field("size_bytes", "bigint"),
        Field("pull_progress", "json"),
        Field("last_updated", "datetime", default=datetime.utcnow),
        Field("auto_pull", "boolean", default=False),
    )

    # Routing Rules
    db.define_table(
        "routing_rules",
        Field("name", notnull=True),
        Field("routing_llm_id", "reference connection_links"),
        Field("conditions", "json"),  # request patterns, user criteria
        Field("target_links", "list:reference connection_links"),
        Field("priority", "integer", default=100),
        Field("enabled", "boolean", default=True),
    )

    # Conversation Memory Configurations
    db.define_table(
        "conversation_memory_configs",
        Field("name", notnull=True),
        Field("provider", "string", default="mem0"),  # mem0, chromadb
        Field("connection_string"),
        Field("api_key", "password"),
        Field("collection_name"),
        Field("embedding_model"),
        Field("config_json", "json"),  # Provider-specific settings
        Field("enabled", "boolean", default=True),
    )

    # RAG/Knowledge Base Configurations
    db.define_table(
        "rag_configs",
        Field("name", notnull=True),
        Field("provider", "string", default="supabase"),  # supabase, qdrant, chromadb
        Field("connection_string"),
        Field("api_key", "password"),
        Field("collection_name"),
        Field("embedding_model", default="all-MiniLM-L6-v2"),
        Field("config_json", "json"),  # Provider-specific settings (host, port, etc.)
        Field("chunk_size", "integer", default=512),
        Field("chunk_overlap", "integer", default=50),
        Field("enabled", "boolean", default=True),
    )

    # Token Conversion Rates (LLM tokens to WaddleAI tokens)
    db.define_table(
        "token_conversion_rates",
        Field("provider", "string", notnull=True),  # openai, anthropic, ollama
        Field("model", "string", notnull=True),
        Field("input_rate", "double", notnull=True),  # LLM tokens per WaddleAI token
        Field("output_rate", "double", notnull=True),  # LLM tokens per WaddleAI token
        Field("base_cost_per_waddleai_token", "double", default=0.001),
        Field("effective_date", "datetime", default=datetime.utcnow),
        Field("enabled", "boolean", default=True),
    )

    # Token Usage Tracking
    db.define_table(
        "token_usage",
        Field("api_key_id", "reference api_keys", notnull=True),
        Field("user_id", "reference users", notnull=True),
        Field("organization_id", "reference organizations", notnull=True),
        Field("date", "date", default=date.today),
        # WaddleAI Tokens (normalized usage units)
        Field("waddleai_tokens", "integer", default=0),
        # Individual LLM Token Counts
        Field("llm_tokens", "json"),  # {"openai_gpt4": {"input": 100, "output": 50}, "claude": {...}}
        Field("tokens_input_total", "integer", default=0),  # Sum across all LLMs
        Field("tokens_output_total", "integer", default=0),  # Sum across all LLMs
        Field("request_count", "integer", default=0),
        Field("last_updated", "datetime", default=datetime.utcnow),
    )

    # Real-time Usage Cache (for quota enforcement)
    db.define_table(
        "usage_cache",
        Field("api_key_id", "reference api_keys", notnull=True),
        Field("organization_id", "reference organizations", notnull=True),
        Field("period", "string", notnull=True),  # daily, monthly
        Field("period_start", "datetime", notnull=True),
        Field("waddleai_tokens_used", "integer", default=0),
        Field("llm_tokens_used", "json"),  # Per-LLM breakdown
        Field("requests_made", "integer", default=0),
        Field("last_updated", "datetime", default=datetime.utcnow),
    )

    # Prompt Security Logs
    db.define_table(
        "security_logs",
        Field("timestamp", "datetime", default=datetime.utcnow),
        Field("api_key_id", "reference api_keys"),
        Field("user_id", "reference users"),
        Field("organization_id", "reference organizations"),
        Field("request_hash"),
        Field("threat_type", "string"),  # injection, jailbreak, data_extraction
        Field("severity", "string"),  # low, medium, high, critical
        Field("blocked", "boolean", default=False),
        Field("prompt_sample", "text"),  # Truncated sample for analysis
        Field("detection_rules", "json"),
        Field("ip_address", "string"),
    )

    # Usage Analytics
    db.define_table(
        "usage_logs",
        Field("timestamp", "datetime", default=datetime.utcnow),
        Field("api_key_id", "reference api_keys"),
        Field("user_id", "reference users"),
        Field("organization_id", "reference organizations"),
        Field("request_hash"),
        Field("routing_rule_id", "reference routing_rules"),
        Field("target_link_id", "reference connection_links"),
        # WaddleAI Token Usage
        Field("waddleai_tokens_used", "integer", default=0),
        # Raw LLM Token Usage
        Field("llm_tokens_input", "integer", default=0),
        Field("llm_tokens_output", "integer", default=0),
        Field("llm_tokens_total", "integer", default=0),
        Field("response_time", "double"),
        Field("status_code", "integer"),
        Field("model_used", "string"),
        Field("provider", "string"),
        Field("cost_estimate_waddleai", "double"),  # Cost in WaddleAI tokens
        Field("cost_estimate_usd", "double"),  # Estimated USD cost
        Field("security_check_passed", "boolean", default=True),
    )

    # Content Filter Rules
    db.define_table(
        "content_filter_rules",
        Field("name", "string", notnull=True),
        Field("description", "text"),
        Field("rule_type", "string", notnull=True),  # 'builtin_pii', 'custom_string', 'custom_regex'
        Field("target", "string", default="both"),  # 'input', 'output', 'both'
        Field("pattern", "text", notnull=True),
        Field("action", "string", default="log"),  # 'block', 'redact', 'log'
        Field("redact_with", "string", default="[REDACTED]"),
        Field("enabled", "boolean", default=True),
        Field("organization_id", "reference organizations"),
        Field("created_by", "reference users"),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow),
    )

    # Content Filter Audit Log
    db.define_table(
        "content_filter_audit_log",
        Field("timestamp", "datetime", default=datetime.utcnow),
        Field("phase", "string", notnull=True),  # 'input', 'output'
        Field("user_id", "reference users"),
        Field("organization_id", "reference organizations"),
        Field("api_key_id", "reference api_keys"),
        Field("ip_address", "string"),
        Field("action_taken", "string", notnull=True),  # 'allow', 'block', 'redact', 'log'
        Field("violations_json", "json"),
        Field("text_sample", "text"),  # First 200 chars for audit
        Field("auditor_used", "boolean", default=False),
        Field("auditor_decision", "string"),  # 'block', 'allow', NULL if not invoked
        Field("request_id", "string"),  # For correlation with proxy logs
    )

    # Content Filter Configuration (key-value store for auditor settings)
    db.define_table(
        "content_filter_config",
        Field("key", "string", notnull=True),
        Field("value", "text"),
        Field("organization_id", "reference organizations"),
        Field("created_by", "reference users"),
        Field("updated_at", "datetime", default=datetime.utcnow),
    )

    # Smart Routing engine (spec §7, migration 010) -- proxy-side read access.
    # Schema is Alembic-authoritative (services/management/app/models_sqlalchemy.py);
    # these definitions bind PyDAL field metadata onto the already-created tables
    # (migrate=False in production), matching the existing dual-definition
    # pattern used above for content_filter_* and other post-baseline tables.
    db.define_table(
        "model_assignments",
        Field("tool_type", "string", notnull=True),
        Field("complexity", "string"),
        Field("region", "string"),
        Field("model_name", "string", notnull=True),
        Field("model_params", "string"),
        Field("vram_gb", "integer"),
        Field("capability_score", "double"),
        Field("enabled", "boolean", default=True),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("credential_label", "string"),
        Field("escalation_model", "string"),
        Field("fallback_models", "json"),
        Field("scope", "string", default="global"),
        Field("scope_ref", "integer"),
    )

    db.define_table(
        "model_configs",
        Field("model_name", unique=True, notnull=True),
        Field("preferred_providers", "json"),
        Field("cost_per_token", "json"),
        Field("max_tokens", "integer"),
        Field("context_length", "integer"),
        Field("capabilities", "json"),
        Field("enabled", "boolean", default=True),
        Field("created_at", "datetime", default=datetime.utcnow),
    )

    db.define_table(
        "model_aliases",
        Field("organization_id", "reference organizations"),
        Field("source_model", "string", notnull=True),
        Field("target_model", "string", notnull=True),
        Field("target_provider", "string"),
        Field("enabled", "boolean", default=True),
        Field("created_at", "datetime", default=datetime.utcnow),
    )

    db.define_table(
        "routing_rules_v2",
        Field("name", "string", notnull=True),
        Field("priority", "integer", default=100),
        Field("match", "json"),
        Field("action", "json"),
        Field("enabled", "boolean", default=True),
        Field("organization_id", "reference organizations"),
        Field("created_at", "datetime", default=datetime.utcnow),
    )

    db.define_table(
        "routing_policies",
        Field("organization_id", "reference organizations", notnull=True),
        Field("mode", "string", default="local_first"),
        Field("escalation_threshold", "integer", default=3),
        Field("escalation_target", "string"),
        Field("classifier_prompt", "text"),
        Field("de_escalation", "string", default="idle_reset"),
        Field("idle_reset_minutes", "integer", default=10),
        Field("sensitivity_routing", "string", default="local_only"),
        Field("budget_pressure_enabled", "boolean", default=True),
        Field("provider_failover", "string", default="off"),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow),
    )

    db.define_table(
        "routing_decision_traces",
        Field("request_id", "string", notnull=True),
        Field("organization_id", "reference organizations", notnull=True),
        Field("timestamp", "datetime", default=datetime.utcnow),
        Field("requirements", "json"),
        Field("tool_type", "string"),
        Field("tool_type_source", "string"),
        Field("rules_fired", "json"),
        Field("classifier_output", "json"),
        Field("assignment_model", "string"),
        Field("capability_veto", "boolean", default=False),
        Field("veto_reason", "string"),
        Field("qualified_candidates", "json"),
        Field("pressure_signals", "json"),
        Field("final_model", "string"),
        Field("routed_from", "json"),
        Field("escalated", "boolean", default=False),
    )

    return db


def init_default_data(db):
    """Initialize default data for the database"""

    # Create default organization
    if not db(db.organizations.name == "default").select():
        org_id = db.organizations.insert(
            name="default",
            description="Default organization for initial setup",
            token_quota_monthly=1000000,
            token_quota_daily=100000,
        )
    else:
        org_id = db(db.organizations.name == "default").select().first().id

    # Create admin user if doesn't exist
    if not db(db.users.username == "admin").select():
        from passlib.hash import bcrypt

        admin_id = db.users.insert(
            username="admin",
            email="admin@waddleai.local",
            password_hash=bcrypt.hash("admin123"),  # Change in production!
            role="admin",
            organization_id=org_id,
            token_quota_monthly=999999999,
            token_quota_daily=999999,
        )

        # Create admin API key
        import secrets

        api_key = "wa-" + secrets.token_urlsafe(32)
        api_key_hash = bcrypt.hash(api_key)

        db.api_keys.insert(
            key_id=f"admin-key-{secrets.token_hex(8)}",
            key_hash=api_key_hash,
            user_id=admin_id,
            organization_id=org_id,
            name="Admin Master Key",
            api_access_level="admin_api",
            permissions={"*": True},
        )

        print(f"Admin API Key (save this!): {api_key}")

    # Default token conversion rates
    default_rates = [
        ("openai", "gpt-4", 10, 20),
        ("openai", "gpt-3.5-turbo", 20, 30),
        ("anthropic", "claude-3-opus", 8, 15),
        ("anthropic", "claude-3-sonnet", 12, 18),
        ("ollama", "llama2", 50, 50),
        ("ollama", "mistral", 45, 45),
    ]

    for provider, model, input_rate, output_rate in default_rates:
        if not db(
            (db.token_conversion_rates.provider == provider) & (db.token_conversion_rates.model == model)
        ).select():
            db.token_conversion_rates.insert(
                provider=provider,
                model=model,
                input_rate=input_rate,
                output_rate=output_rate,
                base_cost_per_waddleai_token=0.001,
            )

    db.commit()
    return db


if __name__ == "__main__":
    # Test database creation
    db = get_db()
    init_default_data(db)
    print("Database initialized successfully!")
    print(f"Tables: {', '.join(db.tables)}")
