"""Migration 009b: proxy memory tables with §9.7 scope/trust columns.

Creates the §6A.5 tables backing the proxy memory layers (session
scratchpad, rolling conversation summarization, embedding cache), retrofits
the §9.7 scoping/trust/attribution columns onto the existing
``memory_embeddings`` table (``scope_type``/``author_user_id`` already exist
from migration 006 — this adds the remaining ``scope_ref``, ``trust_tier``,
``version``, ``superseded_by``, ``status``, ``expires_at``), and adds the
``api_keys.proxy_memory`` per-key config block.

``embedding_cache`` is a pure content-addressed function cache: no org
column and no plaintext — a caller must already possess the content to
compute ``(model, content_hash)``, so nothing readable can leak from it. The
org boundary for readable cached content lives on the retrieval-result
cache instead (Valkey-only, no durable table — see shared/memory/retrieval_cache.py).

# COORDINATION: §13.1 allocates one migration "009" slot to both this branch
# (§6A memory tables, this file) and feature/response-cache (§6 tables,
# 009a_response_cache). feature/response-cache merges into release/v0.2.X
# first, so this file chains off its head: down_revision =
# "009a_response_cache". That revision does not exist in this worktree yet
# (feature/response-cache is landing concurrently) -- the orchestrator
# reconciles at merge time and verifies `alembic heads` is single-headed on
# the merged result.

Revision ID: 009b_proxy_memory
Revises: 009a_response_cache
Create Date: 2026-08-12
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision: str = "009b_proxy_memory"
down_revision: str | None = "009a_response_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# §9.7 scope/trust/attribution columns shared by session_scratchpad and
# conversation_summaries (memory_embeddings already has scope_type/
# author_user_id from 006 -- only the remaining six are added there).
_SCOPE_TRUST_COLUMNS = (
    sa.Column("scope_type", sa.String(20), nullable=False, server_default="session"),
    sa.Column("scope_ref", sa.String(255), nullable=True),
    sa.Column("author_user_id", sa.Integer, nullable=True),
    sa.Column("trust_tier", sa.String(20), nullable=False, server_default="unverified"),
    sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("superseded_by", sa.Integer, nullable=True),
    sa.Column("status", sa.String(20), nullable=False, server_default="active"),
    sa.Column("expires_at", sa.DateTime, nullable=True),
)


def upgrade() -> None:
    """Create the §6A memory tables and retrofit §9.7 columns onto memory_embeddings/api_keys."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # ------------------------------------------------------------------
    # session_scratchpad (§6A.1)
    # ------------------------------------------------------------------
    op.create_table(
        "session_scratchpad",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer, nullable=False, index=True),
        sa.Column("user_id", sa.Integer, nullable=False, index=True),
        sa.Column("session_id", sa.String(255), nullable=False, index=True),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value", sa.Text, nullable=True),
        *_SCOPE_TRUST_COLUMNS,
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "uq_scratchpad_key",
        "session_scratchpad",
        ["org_id", "session_id", "user_id", "key"],
        unique=True,
    )

    # ------------------------------------------------------------------
    # conversation_summaries (§6A.2)
    # ------------------------------------------------------------------
    op.create_table(
        "conversation_summaries",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(255), nullable=False, index=True),
        sa.Column("org_id", sa.Integer, nullable=False, index=True),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("covers_through_turn", sa.Integer, nullable=False),
        sa.Column("tokens_summarized", sa.Integer, nullable=False, server_default="0"),
        sa.Column("model_used", sa.String(255), nullable=True),
        *_SCOPE_TRUST_COLUMNS,
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "uq_convsum_version",
        "conversation_summaries",
        ["org_id", "conversation_id", "version"],
        unique=True,
    )

    # ------------------------------------------------------------------
    # embedding_cache (§6A.3) -- deterministic function cache, no org
    # column, no plaintext. embedding_json is the SQLite-compatible/ORM
    # fallback column; a native pgvector column is added below on postgres
    # only, mirroring the existing memory_embeddings/rag_documents pattern
    # in services/management/app/models_sqlalchemy.py::init_schema.
    # ------------------------------------------------------------------
    op.create_table(
        "embedding_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("embedding_json", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "uq_embcache_model_hash",
        "embedding_cache",
        ["model", "content_hash"],
        unique=True,
    )

    if is_postgres:
        try:
            op.execute("CREATE EXTENSION IF NOT EXISTS vector")
            op.execute("ALTER TABLE embedding_cache ADD COLUMN IF NOT EXISTS embedding vector(768)")
        except Exception as exc:  # pragma: no cover - degrades gracefully, matches init_schema()
            logger.warning(
                "pgvector extension not available, embedding_cache stays TEXT-only: %s", exc
            )

    # ------------------------------------------------------------------
    # memory_embeddings §9.7 retrofit -- scope_type/author_user_id already
    # exist (migration 006); add the remaining six columns.
    # ------------------------------------------------------------------
    op.add_column(
        "memory_embeddings",
        sa.Column("scope_ref", sa.String(255), nullable=True),
    )
    op.add_column(
        "memory_embeddings",
        sa.Column("trust_tier", sa.String(20), nullable=False, server_default="unverified"),
    )
    op.add_column(
        "memory_embeddings",
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    )
    op.add_column(
        "memory_embeddings",
        sa.Column("superseded_by", sa.Integer, nullable=True),
    )
    op.add_column(
        "memory_embeddings",
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
    )
    op.add_column(
        "memory_embeddings",
        sa.Column("expires_at", sa.DateTime, nullable=True),
    )

    # ------------------------------------------------------------------
    # api_keys.proxy_memory (§6A.5 per-key config block)
    # ------------------------------------------------------------------
    op.add_column(
        "api_keys",
        sa.Column("proxy_memory", sa.JSON, nullable=True),
    )


def downgrade() -> None:
    """Drop the §6A memory tables and revert the §9.7 retrofit, restoring the exact 006 shape."""
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.drop_column("proxy_memory")

    with op.batch_alter_table("memory_embeddings") as batch_op:
        batch_op.drop_column("expires_at")
        batch_op.drop_column("status")
        batch_op.drop_column("superseded_by")
        batch_op.drop_column("version")
        batch_op.drop_column("trust_tier")
        batch_op.drop_column("scope_ref")

    op.drop_index("uq_embcache_model_hash", table_name="embedding_cache")
    op.drop_table("embedding_cache")

    op.drop_index("uq_convsum_version", table_name="conversation_summaries")
    op.drop_table("conversation_summaries")

    op.drop_index("uq_scratchpad_key", table_name="session_scratchpad")
    op.drop_table("session_scratchpad")
