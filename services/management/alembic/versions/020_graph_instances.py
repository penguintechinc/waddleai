"""graph_instances: per-org graph-store instance registry (spec Section 2, Phase 1 dev-mode).

One row per org. Phase 1 dev-mode resolves every org to ONE shared Neo4j
(env ``WADDLEAI_GRAPH_BOLT_URL``); the StatefulSet-per-tenant provisioning
that populates ``status``/``bolt_url`` per org is deferred to a later
slice. The org->instance resolver (Task 7) treats anything but
``status='ready'`` as feature-unavailable (clean 503), never a hang.

Revision ID: 020_graph_instances
Revises: 019_code_repos_webhook_secret
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "020_graph_instances"
down_revision: str | None = "019_code_repos_webhook_secret"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``graph_instances`` (one row per org; status-gated resolution)."""
    op.create_table(
        "graph_instances",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        # pending|provisioning|ready|failed|deprovisioning|deprovisioned
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("bolt_url", sa.String(512), nullable=True),  # NULL until ready
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", name="uq_graph_instances_org"),
    )


def downgrade() -> None:
    """Drop ``graph_instances``."""
    op.drop_table("graph_instances")
