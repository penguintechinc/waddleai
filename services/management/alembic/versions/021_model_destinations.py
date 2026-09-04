"""owner_org_id on provider_credentials + model_destinations (spec §3).

``provider_credentials.owner_org_id`` (NULL = platform pool, unchanged;
non-null = tenant-owned/BYOK). ``model_destinations`` is the ordered
active/standby destination list for one (org, logical model). Enum-like
columns are plain strings validated in the app (house style, mig 018).
SQLAlchemy models are the schema authority; the proxy reads these via
parameterized executesql (spec §5.2).

The ``provider_credentials`` alterations (``owner_org_id``, its FK to
``organizations.id``, and ``updated_at``) are wrapped in
``op.batch_alter_table`` so this migration runs on SQLite as well as
Postgres -- SQLite has no ``ALTER TABLE ... ADD CONSTRAINT`` support, which
``op.create_foreign_key`` needs outside batch mode. ``model_destinations``
is a brand-new table, so its creation stays a plain ``op.create_table``.

Revision ID: 021_model_destinations
Revises: 020_graph_instances
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "021_model_destinations"
down_revision: str | None = "020_graph_instances"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add owner_org_id + updated_at to provider_credentials; create model_destinations."""
    with op.batch_alter_table("provider_credentials") as batch_op:
        batch_op.add_column(sa.Column("owner_org_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now())
        )
        batch_op.create_foreign_key(
            "fk_provider_credentials_owner_org",
            "organizations",
            ["owner_org_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index("ix_provider_credentials_owner_org_id", ["owner_org_id"])

    op.create_table(
        "model_destinations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("credential_id", sa.Integer(), nullable=True),
        sa.Column("provider_model_id", sa.String(255), nullable=True),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_id"], ["ai_providers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["credential_id"], ["provider_credentials.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint("priority >= 0", name="ck_model_destinations_priority"),
        sa.CheckConstraint(
            "timeout_seconds IS NULL OR (timeout_seconds >= 1 AND timeout_seconds <= 600)",
            name="ck_model_destinations_timeout",
        ),
        sa.UniqueConstraint(
            "organization_id", "model", "priority", name="uq_model_destinations_org_model_priority"
        ),
    )
    op.create_index(
        "ix_model_destinations_org_model", "model_destinations", ["organization_id", "model"]
    )


def downgrade() -> None:
    """Drop model_destinations and provider_credentials.owner_org_id/updated_at."""
    op.drop_index("ix_model_destinations_org_model", table_name="model_destinations")
    op.drop_table("model_destinations")

    with op.batch_alter_table("provider_credentials") as batch_op:
        batch_op.drop_index("ix_provider_credentials_owner_org_id")
        batch_op.drop_constraint("fk_provider_credentials_owner_org", type_="foreignkey")
        batch_op.drop_column("owner_org_id")
        batch_op.drop_column("updated_at")
