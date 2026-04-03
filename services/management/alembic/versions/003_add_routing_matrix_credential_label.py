"""Add credential_label column to routing_matrix table.

Revision ID: 003_add_routing_matrix_credential_label
Revises: 002_add_provider_credentials
Create Date: 2026-04-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_add_routing_matrix_credential_label"
down_revision: Union[str, None] = "002_add_provider_credentials"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "routing_matrix",
        sa.Column("credential_label", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("routing_matrix", "credential_label")
