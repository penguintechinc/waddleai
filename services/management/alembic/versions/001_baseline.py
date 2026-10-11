"""Baseline schema snapshot — all tables pre-existing before Alembic was introduced.

Revision ID: 001_baseline
Revises:
Create Date: 2026-04-02
"""

from collections.abc import Sequence

revision: str = "001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op — baseline tables were already created via `create_all()`, stamp only."""
    # No-op: all tables in this baseline were created by SQLAlchemy create_all()
    # before Alembic was introduced. Run `alembic stamp 001_baseline` on existing
    # databases to register this baseline without re-running DDL. A completely
    # EMPTY database never reaches this chain: alembic/env.py bootstraps it from
    # the models and stamps head instead (gh-215).
    pass


def downgrade() -> None:
    """No-op — there is no prior revision to downgrade to."""
    pass
