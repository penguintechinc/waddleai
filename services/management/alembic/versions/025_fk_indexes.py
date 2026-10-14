"""Index the two unindexed teams-era FK columns: api_keys.team_id, virtual_keys.api_key_id.

Both columns were added by 024_teams as plain FKs; neither backend auto-indexes an FK, so
team-scoped key lookups and the virtual-key -> api-key revoke/rotate join were full scans.

Idempotent: each index is created only when absent and the column exists, and dropped only
when present, so re-running on a drifted schema is a no-op.

Revision ID: 025_fk_indexes
Revises: 024_teams
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "025_fk_indexes"
down_revision: str | None = "024_teams"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_api_keys_team_id", "api_keys", "team_id"),
    ("ix_virtual_keys_api_key_id", "virtual_keys", "api_key_id"),
)


def _has_index(bind: sa.engine.Connection, table: str, name: str) -> bool:
    """True when ``table`` already carries an index called ``name``."""
    return name in {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def _has_column(bind: sa.engine.Connection, table: str, column: str) -> bool:
    """True when ``column`` exists on ``table`` (guards tables that drifted pre-024)."""
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    """Create both FK indexes where the column exists and the index does not."""
    bind = op.get_bind()
    for name, table, column in _INDEXES:
        if _has_column(bind, table, column) and not _has_index(bind, table, name):
            op.create_index(name, table, [column])


def downgrade() -> None:
    """Drop both FK indexes when present."""
    bind = op.get_bind()
    for name, table, _column in _INDEXES:
        if sa.inspect(bind).has_table(table) and _has_index(bind, table, name):
            op.drop_index(name, table_name=table)
