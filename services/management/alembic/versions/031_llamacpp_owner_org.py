"""Durable owning org on ``llamacpp_deployments`` (fail-closed tenant isolation).

``llamacpp_deployments`` had no org column, so ownership was inferred from the fleet backend or
the key link and unowned legacy rows were visible to every org admin. This adds a nullable
``owner_org_id`` and backfills it from the fleet backend's org, else the ``llamacpp-<public_id>``
connection link's ``owner_org_id``. Rows with neither stay NULL (system-org visible only).

Revision ID: 031_llamacpp_owner_org
Revises: 030_local_generation_samples
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "031_llamacpp_owner_org"
down_revision: str | None = "030_local_generation_samples"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "llamacpp_deployments"


def upgrade() -> None:
    """Add ``owner_org_id`` and backfill it from backend org, then link owner."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(
            sa.Column(
                "owner_org_id",
                sa.Integer(),
                sa.ForeignKey(
                    "organizations.id", ondelete="SET NULL", name="fk_llamacpp_owner_org"
                ),
                nullable=True,
            )
        )
    op.create_index("ix_llamacpp_deployments_owner_org", _TABLE, ["owner_org_id"])
    op.execute(
        """
        UPDATE llamacpp_deployments SET owner_org_id = (
            SELECT fb.org_id FROM fleet_backends fb
            WHERE fb.id = llamacpp_deployments.fleet_backend_id
        ) WHERE owner_org_id IS NULL AND fleet_backend_id IS NOT NULL
        """
    )
    # connection_links is PyDAL-created (not Alembic): on a fresh DB it does not exist yet, and
    # there are then no legacy rows to backfill from it.
    if "connection_links" in sa.inspect(op.get_bind()).get_table_names():
        op.execute(
            """
            UPDATE llamacpp_deployments SET owner_org_id = (
                SELECT cl.owner_org_id FROM connection_links cl
                WHERE cl.name = 'llamacpp-' || CAST(llamacpp_deployments.public_id AS VARCHAR(36))
            ) WHERE owner_org_id IS NULL AND public_id IS NOT NULL
            """
        )


def downgrade() -> None:
    """Drop the owner column."""
    op.drop_index("ix_llamacpp_deployments_owner_org", table_name=_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column("owner_org_id")
