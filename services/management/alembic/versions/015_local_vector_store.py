"""Add local_vector_collections + local_vector_points (spec §17, local-only profile).

Backing store for ``shared.vectorstore.pgvector_backend.PgvectorVectorStore``
— the generic ``VectorStoreBackend`` interface's default implementation, not
a change to any existing pgvector table. Vectors are stored as JSON text
(``vector_json``) rather than a native pgvector ``vector(n)`` column: this
interface supports many independently-dimensioned collections through one
pair of tables, and pgvector's column type is fixed-width per column, so a
JSON-text + Python-cosine-ranking approach (mirroring
``response_cache_entries.prompt_embedding_json`` from migration 009a) is
what makes that portable across SQLite (tests) and PostgreSQL (production)
without per-collection schema changes or runtime DDL.

``local_vector_collections`` records each collection's explicit
``dimensions``/``embedder_id`` so a mismatched reopen is refused rather than
silently accepted (see ``shared.vectorstore.base.VectorCollectionMismatchError``).

Revision ID: 015_local_vector_store
Revises: 014_integrations
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "015_local_vector_store"
down_revision: str | None = "014_integrations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    """Create local_vector_collections + local_vector_points."""
    op.create_table(
        "local_vector_collections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("embedder_id", sa.String(255), nullable=False),
        sa.Column("distance", sa.String(20), nullable=False, server_default="cosine"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_NOW),
    )

    op.create_table(
        "local_vector_points",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "collection_id",
            sa.Integer(),
            sa.ForeignKey("local_vector_collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("vector_json", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_NOW),
        sa.UniqueConstraint(
            "collection_id", "external_id", name="uq_local_vector_points_collection_external"
        ),
    )
    op.create_index("idx_local_vector_points_collection", "local_vector_points", ["collection_id"])


def downgrade() -> None:
    """Drop local_vector_points + local_vector_collections."""
    op.drop_index("idx_local_vector_points_collection", table_name="local_vector_points")
    op.drop_table("local_vector_points")
    op.drop_table("local_vector_collections")
