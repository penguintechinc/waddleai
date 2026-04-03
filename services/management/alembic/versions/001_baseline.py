"""Baseline schema snapshot — all tables pre-existing before Alembic was introduced.

Revision ID: 001_baseline
Revises:
Create Date: 2026-04-02
"""
from typing import Sequence, Union

revision: str = "001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No-op: all tables in this baseline were created by SQLAlchemy create_all()
    # before Alembic was introduced. Run `alembic stamp 001_baseline` on existing
    # databases to register this baseline without re-running DDL.
    pass


def downgrade() -> None:
    pass
