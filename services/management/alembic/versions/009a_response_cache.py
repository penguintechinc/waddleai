"""Response cache: cache_configs + response_cache_entries + token_usage cols.

Creates the §6 response-cache tables: cache_configs (resolution config at
key > org > global precedence, one seeded global default row) and
response_cache_entries (restricted semantic cache, pgvector + HNSW on
PostgreSQL, JSON-serialized-text fallback on SQLite — mirrors the
MemoryEmbedding pattern in models_sqlalchemy.py). Also adds
token_usage.cache_status / token_usage.tokens_saved for §6.4 accounting.

Revision ID: 009a_response_cache
Revises: 006_add_memory_scope
Create Date: 2026-07-09

NOTE(rebase): §13.1 allocates this migration's true parent as
008_model_registry (feature/aiproxy-migration-completion, not yet merged as
of this writing -- current head is 006_add_memory_scope). down_revision
below points at the current head so this migration is testable in isolation
now; the orchestrator must re-point down_revision at 008_model_registry (and
re-run the round-trip test) when reconciling branches into release/v0.2.X.
Sibling branch feature/proxy-memory-layers owns 009b_proxy_memory chained
off the same parent -- whichever of 009a/009b merges second re-points its
own down_revision at the other's revision id so `alembic heads` stays
single-headed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "009a_response_cache"
down_revision: str | None = "006_add_memory_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    """Create cache_configs + response_cache_entries; add token_usage cache columns."""
    op.create_table(
        "cache_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope_type", sa.String(20), nullable=False),
        sa.Column("scope_ref", sa.String(255), nullable=True),
        sa.Column("exact_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("semantic_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("semantic_threshold", sa.Float(), nullable=False, server_default="0.95"),
        sa.Column("ttl_seconds", sa.Integer(), nullable=False, server_default="86400"),
        sa.Column("max_entry_kb", sa.Integer(), nullable=False, server_default="256"),
        sa.Column(
            "anthropic_cache_control", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=_NOW),
        sa.UniqueConstraint("scope_type", "scope_ref", name="uq_cache_configs_scope"),
    )

    op.create_table(
        "response_cache_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("scope_key", sa.String(255), nullable=True),
        sa.Column("model_class", sa.String(255), nullable=False),
        sa.Column("prompt_embedding_json", sa.Text(), nullable=True),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_NOW),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "idx_rce_org_model_expires",
        "response_cache_entries",
        ["org_id", "model_class", "expires_at"],
    )

    op.add_column("token_usage", sa.Column("cache_status", sa.String(16), nullable=True))
    op.add_column(
        "token_usage",
        sa.Column("tokens_saved", sa.Integer(), nullable=False, server_default="0"),
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Native vector column + HNSW cosine-similarity index. Guarded so
        # SQLite (used by the unit round-trip test) never sees pgvector DDL --
        # SQLite keeps prompt_embedding_json as its only embedding column.
        op.execute("ALTER TABLE response_cache_entries ADD COLUMN prompt_embedding vector(768)")
        op.execute(
            "CREATE INDEX idx_rce_prompt_embedding_hnsw "
            "ON response_cache_entries USING hnsw (prompt_embedding vector_cosine_ops)"
        )

    # Seed one global default row (spec §6.4 defaults) so CacheConfigResolver
    # always has a fallback even before any org/key override is created.
    op.execute(
        "INSERT INTO cache_configs "
        "(scope_type, scope_ref, exact_enabled, semantic_enabled, semantic_threshold, "
        "ttl_seconds, max_entry_kb, anthropic_cache_control, created_at, updated_at) "
        "VALUES ('global', NULL, TRUE, FALSE, 0.95, 86400, 256, TRUE, "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )


def downgrade() -> None:
    """Drop response_cache_entries + cache_configs; remove token_usage cache columns."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS idx_rce_prompt_embedding_hnsw")
        op.execute("ALTER TABLE response_cache_entries DROP COLUMN IF EXISTS prompt_embedding")

    with op.batch_alter_table("token_usage") as batch_op:
        batch_op.drop_column("tokens_saved")
        batch_op.drop_column("cache_status")

    op.drop_index("idx_rce_org_model_expires", table_name="response_cache_entries")
    op.drop_table("response_cache_entries")
    op.drop_table("cache_configs")
