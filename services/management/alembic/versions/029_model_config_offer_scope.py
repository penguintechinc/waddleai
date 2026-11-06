"""Scope ``model_configs`` offers to the deployment (and org) that created them.

vLLM deployments publish a routable offer in ``model_configs``. Without an owner, deleting one
deployment could remove an offer another deployment (or a commercial provider) still uses.
``owner_org_id`` and ``deployment_public_id`` are NULL for every pre-existing/global offer, so
existing rows keep their meaning; the management API only ever deletes an offer whose
``deployment_public_id`` matches the deployment being removed.

Revision ID: 029_model_config_offer_scope
Revises: 028_vllm_ray_fleet
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "029_model_config_offer_scope"
down_revision: str | None = "028_vllm_ray_fleet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_model_configs_deployment_public_id"


def upgrade() -> None:
    """Add the nullable scope columns and an index for the delete-by-owner lookup."""
    with op.batch_alter_table("model_configs") as batch:
        batch.add_column(sa.Column("owner_org_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("deployment_public_id", sa.Uuid(), nullable=True))
        batch.create_index(_INDEX, ["deployment_public_id"])


def downgrade() -> None:
    """Drop the scope columns."""
    with op.batch_alter_table("model_configs") as batch:
        batch.drop_index(_INDEX)
        batch.drop_column("deployment_public_id")
        batch.drop_column("owner_org_id")
