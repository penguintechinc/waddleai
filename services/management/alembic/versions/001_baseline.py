"""Baseline schema -- the pre-Alembic database, frozen; builds it on an EMPTY database.

``001_baseline`` is the schema as it existed when Alembic was introduced
(2026-04-02; the models of commit 169615c2 minus the tables ``002``+ create, and
minus ``routing_matrix.credential_label``, which ``003`` adds): the 23 tables
``create_all()`` produced, plus the pgvector columns/indexes the old
``init_schema()`` added.
Migrations ``002``+ build on exactly that, so the chain can only run from a
database that already holds it.

* **Existing databases** were created by ``create_all()`` and are stamped
  (``alembic stamp 001_baseline`` / ``head``); this revision never runs there.
* **A completely EMPTY database** (a first ``helm install``; gh-215) used to die
  at ``002`` because nothing created the baseline. ``upgrade()`` now creates it,
  then the REAL chain (``002``..head) runs on top in the same transaction -- so
  every migration-owned column, index, pgvector column, generated column and
  SEED ROW exists exactly as on a migrated database. There is deliberately NO
  ``create_all()``-of-the-current-models shortcut: the models are not the head
  schema (migration-only DDL, seed data) and a stamped-but-incomplete database
  can never be repaired because the migrations would never re-run.

The table definitions below are a FROZEN historical snapshot, independent of
``app/models_sqlalchemy.py``; never edit them to track model changes -- add a
new migration instead.

Revision ID: 001_baseline
Revises:
Create Date: 2026-04-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# pgvector tables the pre-Alembic init_schema() gave a native vector(768) column + ivfflat index.
_VECTOR_TABLES: tuple[str, ...] = ("memory_embeddings", "rag_documents")


def _baseline_metadata() -> sa.MetaData:
    """Return the frozen pre-Alembic table definitions (no later migration's columns)."""
    metadata = sa.MetaData()
    sa.Table(
        "ai_providers",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), unique=True, nullable=False),
        sa.Column("provider_type", sa.String(50), nullable=False),
        sa.Column("endpoint_url", sa.String(512), nullable=False),
        sa.Column("api_key", sa.String(512), nullable=True),
        sa.Column("model_list", sa.JSON(), nullable=True),
        sa.Column("rate_limits", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("tls_config", sa.JSON(), nullable=True),
        sa.Column("extra_config", sa.JSON(), nullable=True),
        sa.Column("ailb_sync_enabled", sa.Boolean(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    sa.Table(
        "ailb_usage_records",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(255), index=True, nullable=False),
        sa.Column("api_key_id", sa.String(255), index=True, nullable=True),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("request_id", sa.String(255), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    sa.Table(
        "conversation_memory_configs",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("organization_id", sa.Integer(), unique=True, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("max_messages", sa.Integer(), nullable=True),
        sa.Column("similarity_threshold", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    sa.Table(
        "embedding_settings",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("backend", sa.String(50), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("ollama_host", sa.String(500), nullable=True),
        sa.Column("dimensions", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    sa.Table(
        "memory_embeddings",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), index=True, nullable=False),
        sa.Column("organization_id", sa.Integer(), index=True, nullable=False),
        sa.Column("session_id", sa.String(255), index=True, nullable=False),
        sa.Column("content", sa.String(None), nullable=False),
        sa.Column("embedding_json", sa.String(None), nullable=True),
        sa.Column("role", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
    )
    sa.Table(
        "ollama_deployments",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), unique=True, nullable=False),
        sa.Column("endpoint_url", sa.String(512), nullable=False),
        sa.Column("deployment_type", sa.String(50), nullable=True),
        sa.Column("docker_compose_config", sa.JSON(), nullable=True),
        sa.Column("gpu_config", sa.JSON(), nullable=True),
        sa.Column("resource_limits", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(50), nullable=True),
        sa.Column("health_status", sa.String(50), nullable=True),
        sa.Column("last_health_check", sa.DateTime(), nullable=True),
        sa.Column("auto_start", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    sa.Table(
        "organizations",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), unique=True, nullable=False),
        sa.Column("description", sa.String(None), nullable=True),
        sa.Column("token_quota_monthly", sa.Integer(), nullable=True),
        sa.Column("token_quota_daily", sa.Integer(), nullable=True),
        sa.Column("default_model", sa.String(255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    sa.Table(
        "rag_configs",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("organization_id", sa.Integer(), unique=True, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("collection", sa.String(255), nullable=False),
        sa.Column("top_k", sa.Integer(), nullable=True),
        sa.Column("similarity_threshold", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    sa.Table(
        "rag_documents",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("organization_id", sa.Integer(), index=True, nullable=False),
        sa.Column("collection", sa.String(255), index=True, nullable=False),
        sa.Column("content", sa.String(None), nullable=False),
        sa.Column("embedding_json", sa.String(None), nullable=True),
        sa.Column("source", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
    )
    sa.Table(
        "routing_matrix",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tool_type", sa.String(50), nullable=False),
        sa.Column("complexity", sa.String(10), nullable=False),
        sa.Column("region", sa.String(5), nullable=False),
        sa.Column("model_name", sa.String(255), nullable=False),
        sa.Column("model_params", sa.String(50), nullable=True),
        sa.Column("vram_gb", sa.Integer(), nullable=True),
        sa.Column("capability_score", sa.Float(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("tool_type", "complexity", "region", name="uq_routing_matrix_lookup"),
    )
    sa.Table(
        "token_conversion_rates",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("input_rate", sa.Integer(), nullable=False),
        sa.Column("output_rate", sa.Integer(), nullable=False),
        sa.Column("base_cost_per_waddleai_token", sa.Integer(), nullable=True),
        sa.Column("effective_date", sa.DateTime(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
    )
    sa.Table(
        "marchproxy_ailb_sync",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider_id", sa.Integer(), sa.ForeignKey("ai_providers.id"), nullable=True),
        sa.Column("ailb_instance_id", sa.String(255), nullable=True),
        sa.Column("ailb_route_id", sa.String(255), nullable=True),
        sa.Column("sync_status", sa.String(50), nullable=True),
        sa.Column("last_synced", sa.DateTime(), nullable=True),
        sa.Column("sync_error", sa.String(None), nullable=True),
        sa.Column("config_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    sa.Table(
        "ollama_model_routes",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "deployment_id", sa.Integer(), sa.ForeignKey("ollama_deployments.id"), nullable=False
        ),
        sa.Column("model_pattern", sa.String(255), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    sa.Table(
        "ollama_models",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "deployment_id", sa.Integer(), sa.ForeignKey("ollama_deployments.id"), nullable=False
        ),
        sa.Column("model_name", sa.String(255), nullable=False),
        sa.Column("model_tag", sa.String(50), nullable=True),
        sa.Column("status", sa.String(50), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("pull_progress", sa.JSON(), nullable=True),
        sa.Column("last_updated", sa.DateTime(), nullable=True),
        sa.Column("auto_pull", sa.Boolean(), nullable=True),
    )
    sa.Table(
        "routing_rules",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), unique=True, nullable=False),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("match_conditions", sa.JSON(), nullable=True),
        sa.Column(
            "target_provider_id", sa.Integer(), sa.ForeignKey("ai_providers.id"), nullable=True
        ),
        sa.Column(
            "fallback_provider_id", sa.Integer(), sa.ForeignKey("ai_providers.id"), nullable=True
        ),
        sa.Column("load_balancing_strategy", sa.String(50), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    sa.Table(
        "users",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(255), unique=True, nullable=False),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(50), nullable=False),
        sa.Column(
            "organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False
        ),
        sa.Column("managed_orgs", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("token_quota_monthly", sa.Integer(), nullable=True),
        sa.Column("token_quota_daily", sa.Integer(), nullable=True),
        sa.Column("default_model", sa.String(255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("current_login_at", sa.DateTime(), nullable=True),
        sa.Column("last_login_ip", sa.String(50), nullable=True),
        sa.Column("current_login_ip", sa.String(50), nullable=True),
        sa.Column("login_count", sa.Integer(), nullable=True),
    )
    sa.Table(
        "api_keys",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key_id", sa.String(255), unique=True, nullable=False),
        sa.Column("key_hash", sa.String(255), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("token_quota_monthly", sa.Integer(), nullable=True),
        sa.Column("token_quota_daily", sa.Integer(), nullable=True),
        sa.Column("rate_limit_rpm", sa.Integer(), nullable=True),
        sa.Column("default_model", sa.String(255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_used", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("permissions", sa.JSON(), nullable=True),
        sa.Column("allowed_endpoints", sa.JSON(), nullable=True),
        sa.Column("api_access_level", sa.String(50), nullable=True),
    )
    sa.Table(
        "virtual_keys",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key_prefix", sa.String(20), nullable=True),
        sa.Column("key_hash", sa.String(255), nullable=True),
        sa.Column("ailb_key_id", sa.String(255), nullable=True),
        sa.Column("ailb_sync_status", sa.String(50), nullable=True),
        sa.Column("allowed_models", sa.JSON(), nullable=True),
        sa.Column("allowed_providers", sa.JSON(), nullable=True),
        sa.Column("budget_limit_daily", sa.Integer(), nullable=True),
        sa.Column("budget_limit_monthly", sa.Integer(), nullable=True),
        sa.Column("tpm_limit", sa.Integer(), nullable=True),
        sa.Column("rpm_limit", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_used", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    sa.Table(
        "ailb_usage_events",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(255), unique=True, nullable=True),
        sa.Column("virtual_key_id", sa.Integer(), sa.ForeignKey("virtual_keys.id"), nullable=True),
        sa.Column("ailb_key_id", sa.String(255), nullable=True),
        sa.Column("request_id", sa.String(255), nullable=True),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("provider", sa.String(100), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(50), nullable=True),
        sa.Column("error_message", sa.String(None), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.Column("processed", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    sa.Table(
        "security_logs",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.Column("virtual_key_id", sa.Integer(), sa.ForeignKey("virtual_keys.id"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True
        ),
        sa.Column("request_hash", sa.String(255), nullable=True),
        sa.Column("threat_type", sa.String(100), nullable=True),
        sa.Column("severity", sa.String(50), nullable=True),
        sa.Column("blocked", sa.Boolean(), nullable=True),
        sa.Column("prompt_sample", sa.String(None), nullable=True),
        sa.Column("detection_rules", sa.JSON(), nullable=True),
        sa.Column("ip_address", sa.String(50), nullable=True),
    )
    sa.Table(
        "token_usage",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("virtual_key_id", sa.Integer(), sa.ForeignKey("virtual_keys.id"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True
        ),
        sa.Column("date", sa.DateTime(), nullable=True),
        sa.Column("waddleai_tokens", sa.Integer(), nullable=True),
        sa.Column("llm_tokens", sa.JSON(), nullable=True),
        sa.Column("tokens_input_total", sa.Integer(), nullable=True),
        sa.Column("tokens_output_total", sa.Integer(), nullable=True),
        sa.Column("request_count", sa.Integer(), nullable=True),
        sa.Column("cost_usd_total", sa.Integer(), nullable=True),
        sa.Column("last_updated", sa.DateTime(), nullable=True),
    )
    sa.Table(
        "usage_cache",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("virtual_key_id", sa.Integer(), sa.ForeignKey("virtual_keys.id"), nullable=True),
        sa.Column(
            "organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True
        ),
        sa.Column("period", sa.String(20), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("waddleai_tokens_used", sa.Integer(), nullable=True),
        sa.Column("llm_tokens_used", sa.JSON(), nullable=True),
        sa.Column("requests_made", sa.Integer(), nullable=True),
        sa.Column("last_updated", sa.DateTime(), nullable=True),
    )
    sa.Table(
        "usage_logs",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("api_key_id", sa.Integer(), sa.ForeignKey("api_keys.id"), nullable=True),
        sa.Column(
            "organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True
        ),
        sa.Column("request_id", sa.String(255), nullable=True),
        sa.Column("endpoint", sa.String(512), nullable=True),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("provider", sa.String(100), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("response_time_ms", sa.Integer(), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.String(None), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
    )
    return metadata


def _create_pgvector_columns(bind: sa.Connection) -> None:
    """Enable pgvector and add the vector columns + indexes; raises if pgvector is unavailable.

    Fail-loud by design: a database stamped at head without its vector columns
    could never be repaired (the migrations would not re-run), so a missing or
    unprivileged pgvector aborts the whole migration transaction instead.
    """
    bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    for table in _VECTOR_TABLES:
        bind.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS embedding vector(768)"))
        bind.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS {table}_emb_idx ON {table} "
                "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
            )
        )


def upgrade() -> None:
    """Create the baseline on an empty database; no-op on a legacy unstamped baseline database.

    Refuses (loudly) a database that has application tables beyond the baseline
    but no recorded revision: it is ahead of the baseline, so running the chain
    from 001 would collide with objects it already holds. That database needs
    ``alembic stamp <revision matching its schema>``.
    """
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Alembic creates alembic_version.version_num as VARCHAR(32); revision
        # 003_add_routing_matrix_credential_label is 39 chars, so walking the chain
        # from scratch died with StringDataRightTruncation at 002 -> 003. Widen it
        # (stamped databases never hit this: `stamp head` writes a short id).
        bind.execute(
            sa.text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)")
        )
    metadata = _baseline_metadata()
    existing = set(sa.inspect(bind).get_table_names()) - {"alembic_version"}
    if not existing:
        metadata.create_all(bind)
        if bind.dialect.name == "postgresql":
            _create_pgvector_columns(bind)
        return
    unexpected = sorted(existing - set(metadata.tables))
    if unexpected:
        raise RuntimeError(
            "refusing to run 001_baseline: the database has application tables "
            f"beyond the baseline ({', '.join(unexpected[:5])}"
            f"{', ...' if len(unexpected) > 5 else ''}) but no alembic_version row, so its "
            "schema revision is unknown. Run `alembic stamp <revision>` for the revision "
            "that matches its schema (e.g. `alembic stamp head` for a create_all() database)."
        )
    # Legacy pre-Alembic database holding (a subset of) the baseline: leave it untouched.


def downgrade() -> None:
    """No-op -- there is no prior revision; the baseline tables are left in place."""
    pass
