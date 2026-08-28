"""WaddleAI Database Models.

Shared database models for both proxy and management servers.
"""

import os
from datetime import date, datetime

from penguin_dal import DAL, Field
from sqlalchemy import text

# Fixed key for the Postgres session-level advisory lock taken in
# _define_tables_serialized() below. Any int64 works here -- it has no
# meaning beyond being a private mutex ID for this one call site, chosen to
# be unlikely to collide with any other advisory lock this cluster's other
# services might take (waddleai-proxy-schema-bootstrap, ASCII-summed and
# truncated to fit a signed 64-bit key).
_SCHEMA_BOOTSTRAP_LOCK_KEY = 8_711_942_003_501


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
    #
    # DAL() defaults to reflect=True, which reflects every table already
    # present in db_uri's schema into its SQLAlchemy MetaData *before*
    # define_tables() runs. In production (and any environment where
    # Alembic/management has already created the schema -- e.g. the
    # integration-test compose stack, where management's migrations land
    # before the proxy's first connect) that means "organizations" and
    # friends are already registered in the MetaData by the time
    # define_tables() calls db.define_table("organizations", ...), which
    # tries to register a second SQLAlchemy Table for the same name and
    # raises sqlalchemy.exc.InvalidRequestError ("... already defined for
    # this MetaData instance"). define_tables()'s per-table guard (see
    # below) makes this idempotent: reuse the already-reflected,
    # Alembic-authoritative Table when present, only fall through to
    # define_table() to create it from these PyDAL Field defs against a
    # fresh, empty database (contract-test sqlite, migrate=True).
    db = DAL(db_uri, migrate=migrate)

    # The proxy is a DB CLIENT, not the schema authority (Alembic/management
    # owns the schema in every real environment -- see the module docstring
    # above). But proxy/Dockerfile's CMD runs `hypercorn ... --workers 4`,
    # spawning 4 independent OS processes that each call get_db() during
    # their own lifespan startup. On a fresh/empty database every process's
    # own DAL(reflect=True) call above sees no tables yet, so all 4 race
    # define_tables() -> SQLAlchemy's create_all(checkfirst=True): the
    # existence check and the CREATE TABLE are two separate round-trips, not
    # atomic across processes, so more than one process's check can pass
    # before any of them commits. Every loser after the first then crashes
    # hypercorn's lifespan startup with psycopg2.errors.UniqueViolation on
    # pg_type_typname_nsp_index (confirmed via local repro against a fresh
    # Postgres 15 container using the exact Dockerfile CMD -- see
    # .claude/agent-memory/penguintech-dev/proxy_worker_race_startup_bug.md).
    # A Postgres advisory lock serializes define_tables() across processes
    # without touching the Dockerfile/entrypoint or the contract-test path:
    # only postgresql (the dialect that actually has advisory locks and the
    # only one that runs multi-worker) takes the locked path; sqlite (the
    # contract-test harness's `--workers 1`, no concurrent siblings) has no
    # such primitive and no race to guard against, so it keeps calling
    # define_tables() directly, unchanged from before this fix.
    if db.engine.dialect.name == "postgresql":
        _define_tables_serialized(db)
    else:
        define_tables(db)
    return db


def _define_tables_serialized(db):
    """Run define_tables() while holding a cluster-wide Postgres advisory lock.

    Only the first of N concurrently-starting proxy worker processes to
    acquire the lock actually creates any missing tables; the rest block on
    pg_advisory_lock() until it releases, then run define_tables() -> each
    define_table() call's create_all(checkfirst=True) does a live catalog
    check (not a check against this process's own possibly-stale reflected
    metadata) and correctly no-ops on every table the lock holder already
    committed -- closing the boot-time race described in get_db()'s
    docstring above. The lock is released explicitly (not left to the
    connection-pool checkin, which returns the connection for reuse rather
    than actually closing the socket, so it would not free a session-level
    advisory lock).
    """
    with db.engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _SCHEMA_BOOTSTRAP_LOCK_KEY})
        try:
            define_tables(db)
        finally:
            conn.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _SCHEMA_BOOTSTRAP_LOCK_KEY}
            )


def _define_table_if_absent(db, name, *fields, **kwargs):
    """Define a table only if it is not already present in db's MetaData.

    Guards every define_tables() call site against the reflect-then-define
    collision described in get_db()'s docstring above. None of this
    module's Field(...) calls pass `requires=` (Python-side validators), so
    skipping define_table() for an already-reflected table loses no
    validation behavior -- DB-level constraints from Alembic's schema still
    apply.
    """
    if name in db.tables:
        return
    db.define_table(name, *fields, **kwargs)


def define_tables(db):
    """Define all database tables."""
    # Organizations for Multi-tenancy
    _define_table_if_absent(
        db,
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
    _define_table_if_absent(
        db,
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
    _define_table_if_absent(
        db,
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
    _define_table_if_absent(
        db,
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
    _define_table_if_absent(
        db,
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
    _define_table_if_absent(
        db,
        "routing_rules",
        Field("name", notnull=True),
        Field("routing_llm_id", "reference connection_links"),
        Field("conditions", "json"),  # request patterns, user criteria
        Field("target_links", "list:reference connection_links"),
        Field("priority", "integer", default=100),
        Field("enabled", "boolean", default=True),
    )

    # Conversation Memory Configurations
    _define_table_if_absent(
        db,
        "conversation_memory_configs",
        Field("name", notnull=True),
        Field("provider", "string", default="mem0"),  # mem0, pgvector
        Field("connection_string"),
        Field("api_key", "password"),
        Field("collection_name"),
        Field("embedding_model"),
        Field("config_json", "json"),  # Provider-specific settings
        Field("enabled", "boolean", default=True),
    )

    # RAG/Knowledge Base Configurations
    _define_table_if_absent(
        db,
        "rag_configs",
        Field("name", notnull=True),
        Field("provider", "string", default="supabase"),  # supabase, qdrant, pgvector
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
    _define_table_if_absent(
        db,
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
    _define_table_if_absent(
        db,
        "token_usage",
        Field("api_key_id", "reference api_keys", notnull=True),
        Field("user_id", "reference users", notnull=True),
        Field("organization_id", "reference organizations", notnull=True),
        Field("date", "date", default=date.today),
        # WaddleAI Tokens (normalized usage units)
        Field("waddleai_tokens", "integer", default=0),
        # Individual LLM Token Counts
        Field(
            "llm_tokens", "json"
        ),  # {"openai_gpt4": {"input": 100, "output": 50}, "claude": {...}}
        Field("tokens_input_total", "integer", default=0),  # Sum across all LLMs
        Field("tokens_output_total", "integer", default=0),  # Sum across all LLMs
        Field("request_count", "integer", default=0),
        Field("last_updated", "datetime", default=datetime.utcnow),
    )

    # Real-time Usage Cache (for quota enforcement)
    _define_table_if_absent(
        db,
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
    _define_table_if_absent(
        db,
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
    _define_table_if_absent(
        db,
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
    _define_table_if_absent(
        db,
        "content_filter_rules",
        Field("name", "string", notnull=True),
        Field("description", "text"),
        Field(
            "rule_type", "string", notnull=True
        ),  # 'builtin_pii', 'custom_string', 'custom_regex'
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
    _define_table_if_absent(
        db,
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
    _define_table_if_absent(
        db,
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
    _define_table_if_absent(
        db,
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

    _define_table_if_absent(
        db,
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

    _define_table_if_absent(
        db,
        "model_aliases",
        Field("organization_id", "reference organizations"),
        Field("source_model", "string", notnull=True),
        Field("target_model", "string", notnull=True),
        Field("target_provider", "string"),
        Field("enabled", "boolean", default=True),
        Field("created_at", "datetime", default=datetime.utcnow),
    )

    _define_table_if_absent(
        db,
        "routing_rules_v2",
        Field("name", "string", notnull=True),
        Field("priority", "integer", default=100),
        Field("match", "json"),
        Field("action", "json"),
        Field("enabled", "boolean", default=True),
        Field("organization_id", "reference organizations"),
        Field("created_at", "datetime", default=datetime.utcnow),
    )

    _define_table_if_absent(
        db,
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

    _define_table_if_absent(
        db,
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


# NOTE: default-data bootstrap (default org, admin user, admin API key, token
# conversion rates) lives solely in
# services/management/app/extensions.py::init_default_data now -- it sources
# the admin password from ADMIN_INITIAL_PASSWORD (fail-closed random password
# if unset) and never prints/logs the plaintext admin API key. This module
# used to carry a duplicate bootstrap (reachable only via `python3 -m
# shared.database.models`) that hardcoded "admin123" and printed the master
# API key to stdout (CodeQL alert #2507, py/clear-text-logging-sensitive-data)
# -- removed rather than fixed in place since nothing but that script called
# it and the hardened version is a strict superset.
