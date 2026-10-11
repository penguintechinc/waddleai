"""Add security_logs.api_key_id and memory_embeddings.embedding (gh-213).

Two more PyDAL-runtime vs Alembic/SQLAlchemy drifts, same root cause as
gh-207 (``020_token_usage_api_key_id``):

1. ``security_logs``: the runtime write/read path
   (``shared/security/prompt_security.py`` ``_log_threat`` and the
   rate-limit query) and the PyDAL model (``shared/database/models.py``)
   use ``api_key_id`` (FK ``api_keys``); the production schema only had
   ``virtual_key_id`` (FK ``virtual_keys``, which no runtime path reads).
   Every threat-log insert failed, swallowed by ``_log_threat``'s broad
   ``except``. ``api_key_id`` is the canonical name (it is what every
   write-site uses); ``virtual_key_id`` is left in place.

2. ``memory_embeddings.embedding``: ``PgvectorMemoryStore.store_memory``
   inserts into a native pgvector ``embedding`` column. No migration ever
   created it -- only ``init_schema()`` did, and only when pgvector was
   available at bootstrap -- so migration-provisioned (or pgvector-less
   at init) databases failed every memory write with ``column "embedding"
   of relation "memory_embeddings" does not exist``, swallowed by
   ``store_memory``'s broad ``except``. Added as ``vector(768)`` (the
   dimension used by every other vector column: nomic-embed-text), plus the
   same ivfflat cosine index ``init_schema()`` creates. Postgres+pgvector
   only; elsewhere (SQLite/no extension) this is skipped, matching
   ``init_schema()``'s degrade-gracefully behaviour.

Safe on a populated database: nullable additions only, all guarded
(idempotent) so it is a no-op where ``init_schema()`` already created them.

Revision ID: 023_schema_drift_gh213
Revises: 022_service_account_fields
Create Date: 2026-10-11
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "023_schema_drift_gh213"
down_revision: str | None = "022_service_account_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger(__name__)


def _has_column(bind: sa.engine.Connection, table: str, column: str) -> bool:
    """True if `column` already exists on `table` (guard for pre-migration ORM drift)."""
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _has_index(bind: sa.engine.Connection, table: str, index: str) -> bool:
    """True if `index` exists on `table` (create_all/bootstrapped DBs never created ours)."""
    return index in {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def _pgvector_available(bind: sa.engine.Connection) -> bool:
    """Create the vector extension on Postgres; False if not Postgres or unavailable."""
    if bind.dialect.name != "postgresql":
        return False
    try:
        with bind.begin_nested():
            bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    except Exception as exc:  # noqa: BLE001 -- no pgvector installed: degrade like init_schema()
        logger.warning(
            "pgvector extension not available, memory_embeddings.embedding column skipped: %s",
            exc,
        )
        return False
    return True


def upgrade() -> None:
    """Add security_logs.api_key_id and (pgvector only) memory_embeddings.embedding."""
    bind = op.get_bind()

    if not _has_column(bind, "security_logs", "api_key_id"):
        op.add_column(
            "security_logs",
            sa.Column(
                "api_key_id",
                sa.Integer(),
                sa.ForeignKey("api_keys.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index("idx_security_logs_api_key_id", "security_logs", ["api_key_id"])

    if not _has_column(bind, "memory_embeddings", "embedding") and _pgvector_available(bind):
        op.execute("ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS embedding vector(768)")
        op.execute(
            "CREATE INDEX IF NOT EXISTS memory_embeddings_emb_idx ON memory_embeddings "
            "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
        )


def downgrade() -> None:
    """Drop the guard-added columns (and their indexes), tolerating create_all-built DBs.

    A database built by ``init_schema()``/``create_all`` (dev/e2e, gh-215 bootstrap)
    has the columns but not ``idx_security_logs_api_key_id`` (the ORM column carries
    no ``index=True``), so every drop is IF EXISTS / inspector-guarded rather than
    assumed present. Dropping the columns is lossy by nature; it just must not crash.
    """
    bind = op.get_bind()

    if bind.dialect.name == "postgresql" and _has_column(bind, "memory_embeddings", "embedding"):
        op.execute("DROP INDEX IF EXISTS memory_embeddings_emb_idx")
        op.execute("ALTER TABLE memory_embeddings DROP COLUMN IF EXISTS embedding")

    if _has_column(bind, "security_logs", "api_key_id"):
        if _has_index(bind, "security_logs", "idx_security_logs_api_key_id"):
            op.drop_index("idx_security_logs_api_key_id", table_name="security_logs")
        op.drop_column("security_logs", "api_key_id")
