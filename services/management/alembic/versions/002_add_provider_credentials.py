"""Add provider_credentials table and migrate existing api_key values.

Revision ID: 002_add_provider_credentials
Revises: 001_baseline
Create Date: 2026-04-02
"""

from datetime import datetime
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002_add_provider_credentials"
down_revision: Union[str, None] = "001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create provider_credentials table
    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("api_key", sa.String(512), nullable=True),
        sa.Column("org_id", sa.String(255), nullable=True),
        sa.Column("account_meta", sa.JSON(), nullable=True),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("request_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("token_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["ai_providers.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_provider_credentials_provider_id",
        "provider_credentials",
        ["provider_id"],
    )

    # 2. Migrate existing api_key values into provider_credentials as label='default'
    connection = op.get_bind()
    providers = connection.execute(
        sa.text("SELECT id, api_key FROM ai_providers " "WHERE api_key IS NOT NULL AND api_key != ''")
    ).fetchall()
    now = datetime.utcnow()
    for row in providers:
        connection.execute(
            sa.text(
                "INSERT INTO provider_credentials "
                "(provider_id, label, api_key, weight, enabled, "
                " request_count, token_count, created_at) "
                "VALUES (:pid, 'default', :key, 100, true, 0, 0, :now)"
            ),
            {"pid": row[0], "key": row[1], "now": now},
        )

    # NOTE: ai_providers.api_key is intentionally NOT dropped here.
    # It is kept for backward-compatibility fallback in LLMConnectionManager
    # until migration 004 (cleanup) is run after production validation.


def downgrade() -> None:
    # Copy first credential back to ai_providers.api_key before dropping table
    connection = op.get_bind()
    # PostgreSQL: DISTINCT ON; for portability use subquery
    rows = connection.execute(
        sa.text(
            "SELECT pc.provider_id, pc.api_key "
            "FROM provider_credentials pc "
            "INNER JOIN ("
            "  SELECT provider_id, MIN(id) AS min_id "
            "  FROM provider_credentials GROUP BY provider_id"
            ") first ON pc.id = first.min_id"
        )
    ).fetchall()
    for row in rows:
        connection.execute(
            sa.text("UPDATE ai_providers SET api_key = :key WHERE id = :pid"),
            {"key": row[1], "pid": row[0]},
        )
    op.drop_index("ix_provider_credentials_provider_id", "provider_credentials")
    op.drop_table("provider_credentials")
