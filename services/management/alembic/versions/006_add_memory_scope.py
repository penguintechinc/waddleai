"""Add memory scope columns (personal vs organizational memory).

Adds scope_type ('user' | 'org') and author_user_id to memory_embeddings.
Backfills all existing rows to personal scope with author = owner, so no
memory changes visibility retroactively.

Field names follow the platform spec §9.7 (Memory Scoping & Trust) so the
v0.4.x scope expansion (session/project/repo, trust tiers) extends this
schema without renaming.

Revision ID: 006_add_memory_scope
Revises: 005_add_content_filter_tables
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "006_add_memory_scope"
down_revision: str | None = "005_add_content_filter_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add scope_type/author_user_id to memory_embeddings, backfilling rows to personal scope."""
    # NOT NULL with server_default is safe as a single step on both
    # PostgreSQL and SQLite: existing rows take the default.
    op.add_column(
        "memory_embeddings",
        sa.Column("scope_type", sa.String(20), nullable=False, server_default="user"),
    )
    # author_user_id backfills from user_id, so it is added nullable,
    # backfilled, then tightened (batch_alter_table for SQLite compat).
    op.add_column(
        "memory_embeddings",
        sa.Column("author_user_id", sa.Integer, nullable=True),
    )
    op.execute("UPDATE memory_embeddings SET author_user_id = user_id WHERE author_user_id IS NULL")
    with op.batch_alter_table("memory_embeddings") as batch_op:
        batch_op.alter_column("author_user_id", existing_type=sa.Integer, nullable=False)

    op.create_index("idx_mememb_scope_type", "memory_embeddings", ["scope_type"])
    op.create_index("idx_mememb_author_user", "memory_embeddings", ["author_user_id"])
    # Composite index keeps the merged-view org branch
    # (organization_id, scope_type='org') cheap.
    op.create_index("idx_mememb_org_scope", "memory_embeddings", ["organization_id", "scope_type"])


def downgrade() -> None:
    """Drop the scope_type/author_user_id columns and their indexes from memory_embeddings."""
    op.drop_index("idx_mememb_org_scope", table_name="memory_embeddings")
    op.drop_index("idx_mememb_author_user", table_name="memory_embeddings")
    op.drop_index("idx_mememb_scope_type", table_name="memory_embeddings")
    with op.batch_alter_table("memory_embeddings") as batch_op:
        batch_op.drop_column("author_user_id")
        batch_op.drop_column("scope_type")
