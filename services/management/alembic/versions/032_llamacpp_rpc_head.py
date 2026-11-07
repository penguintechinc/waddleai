"""Head node identity for llama.cpp RPC deployments (Plan F).

An RPC deployment's cost depends on how many distinct nodes it spans, and the head can share a
node with some workers, so ``rpc_workers`` alone cannot give the node count. This adds the head's
inventory ``node_uid`` (NULL for single-node rows) so repricing and re-deploys never guess.

Revision ID: 032_llamacpp_rpc_head
Revises: 031_llamacpp_owner_org
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "032_llamacpp_rpc_head"
down_revision: str | None = "031_llamacpp_owner_org"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "llamacpp_deployments"
_COLUMN = "rpc_head_node_uid"


def _has_column() -> bool:
    """True when the column already exists (re-run safe; fresh DBs built from the chain add it)."""
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return False
    return _COLUMN in {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    """Add the nullable head node uid column."""
    if _has_column():
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column(_COLUMN, sa.String(255), nullable=True))


def downgrade() -> None:
    """Drop the head node uid column."""
    if not _has_column():
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
