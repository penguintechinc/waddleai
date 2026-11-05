"""Persist Wave-5 managed MCP ownership and DAC revisions/apply runs.

Revision ID: 027_configuration_as_code
Revises: 026_pooled_quota
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "027_configuration_as_code"
down_revision: str | None = "026_pooled_quota"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the configuration-as-code revision and plan tables.

    Supports applying reviewed DaC manifests with an auditable history.
    """
    op.create_table(
        "managed_mcp_servers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("endpoint_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("namespace", sa.String(255), nullable=False),
        sa.Column("image", sa.String(1024), nullable=False),
        sa.Column("transport", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("discovered_tools", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["endpoint_id"], ["mcp_endpoints.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("org_id", "name", name="uq_managed_mcp_servers_org_name"),
    )
    op.create_table(
        "configuration_revisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("manifest_redacted", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "org_id", "name", "manifest_digest", name="uq_configuration_revisions_identity"
        ),
    )
    op.create_table(
        "manifest_apply_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("revision_id", sa.Integer(), nullable=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.String(128), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("outcomes", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["revision_id"], ["configuration_revisions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "idx_manifest_apply_runs_org_created", "manifest_apply_runs", ["org_id", "created_at"]
    )


def downgrade() -> None:
    """Drop the configuration-as-code tables created by upgrade().

    Reverses revision 027 so the schema returns to its prior state.
    """
    op.drop_index("idx_manifest_apply_runs_org_created", table_name="manifest_apply_runs")
    op.drop_table("manifest_apply_runs")
    op.drop_table("configuration_revisions")
    op.drop_table("managed_mcp_servers")
