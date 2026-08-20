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
    """Add nullable routing_matrix.credential_label column."""
    op.add_column(
        "routing_matrix",
        sa.Column("credential_label", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    """Drop routing_matrix.credential_label column."""
    op.drop_column("routing_matrix", "credential_label")
