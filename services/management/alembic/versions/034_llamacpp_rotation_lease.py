"""Per-deployment rotation lease on ``llamacpp_deployments``.

Concurrent token rotations could interleave their Secret and connection-link writes and leave the
two holding different tokens. A nullable lease timestamp, taken with a conditional UPDATE, makes
rotation single-flight across management replicas without holding a DB connection open.

Revision ID: 034_llamacpp_rotation_lease
Revises: 033_llamacpp_name_per_org
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "034_llamacpp_rotation_lease"
down_revision: str | None = "033_llamacpp_name_per_org"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "llamacpp_deployments"
_COLUMN = "rotation_lease_until"


def _has_column() -> bool:
    """True when the table exists and already holds the column (re-run safe, fresh DBs)."""
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return False
    return _COLUMN in {c["name"] for c in inspector.get_columns(_TABLE)}


def _table_present() -> bool:
    """True when the table exists."""
    return _TABLE in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    """Add the nullable lease column."""
    if not _table_present() or _has_column():
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column(_COLUMN, sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Drop the lease column."""
    if not _has_column():
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
