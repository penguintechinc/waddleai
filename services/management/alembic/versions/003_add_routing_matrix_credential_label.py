"""Add credential_label column to routing_matrix table.

Revision ID: 003_add_routing_matrix_credential_label
Revises: 002_add_provider_credentials
Create Date: 2026-04-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "003_add_routing_matrix_credential_label"
down_revision: str | None = "002_add_provider_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Widen alembic_version.version_num, then add nullable routing_matrix.credential_label.

    Alembic creates ``alembic_version.version_num`` as VARCHAR(32) but this revision's id is
    39 chars, so a DB stamped at 001/002 (still VARCHAR(32)) died with StringDataRightTruncation
    when Alembic recorded it (gh-215). Widen first, idempotently (255 matches 001_baseline; an
    already-widened column is left as-is). SQLite's column is TEXT, so Postgres only.
    """
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)")
    op.add_column(
        "routing_matrix",
        sa.Column("credential_label", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    """Drop routing_matrix.credential_label column."""
    op.drop_column("routing_matrix", "credential_label")
