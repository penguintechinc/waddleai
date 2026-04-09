"""Phase 7 cleanup: drop deprecated ai_providers.api_key column.

Run this migration only after provider_credentials has been validated in
production and the backward-compat fallback in LLMConnectionManager is removed.

Revision ID: 004_drop_provider_api_key
Revises: 003_add_routing_matrix_credential_label
Create Date: 2026-04-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004_drop_provider_api_key"
down_revision: Union[str, None] = "003_add_routing_matrix_credential_label"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("ai_providers", "api_key")


def downgrade() -> None:
    op.add_column(
        "ai_providers",
        sa.Column("api_key", sa.String(512), nullable=True),
    )
    # NOTE: Restoring the column leaves it NULL for all rows.
    # If you need the data back, restore from provider_credentials manually.
