"""Admin-action audit trail table (audit G10).

Adds ``audit_log``: one row per state-changing ``/api/v1`` request
(``POST``/``PUT``/``PATCH``/``DELETE``), written by the ``app.audit``
after-request middleware. Records who (``user_id`` -- a reference to the
identity table, never the raw username, per the PII-tokenization boundary),
what (``method`` + ``path`` + ``resource_id``), when (``created_at``), and the
outcome (``status_code``). Request bodies are never stored -- they can carry
secrets/PII and the path + status already answer what changed.

FK columns are nullable with ``ON DELETE SET NULL`` so purging a user or org
never destroys the audit history of their actions. ``created_at`` carries a
``server_default`` because the row is inserted through PyDAL against the
reflected table, where a Python-side default would not fire (same rationale as
``content_filter_audit_log``, gh-207).

Revision ID: 021_audit_log
Revises: 020_token_usage_api_key_id
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "021_audit_log"
down_revision: str | None = "020_token_usage_api_key_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(bind: sa.engine.Connection, table: str) -> bool:
    """True if ``table`` already exists (guard for the create_all()/stamp path)."""
    return table in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    """Create ``audit_log`` with indexes on created_at, (user_id, created_at), (org, created_at)."""
    bind = op.get_bind()
    if _has_table(bind, "audit_log"):
        return

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("resource_id", sa.String(255), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_audit_log_created_at", "audit_log", ["created_at"])
    op.create_index("idx_audit_log_user", "audit_log", ["user_id", "created_at"])
    op.create_index("idx_audit_log_org", "audit_log", ["organization_id", "created_at"])


def downgrade() -> None:
    """Drop ``audit_log`` and its indexes."""
    bind = op.get_bind()
    if not _has_table(bind, "audit_log"):
        return
    op.drop_index("idx_audit_log_org", table_name="audit_log")
    op.drop_index("idx_audit_log_user", table_name="audit_log")
    op.drop_index("idx_audit_log_created_at", table_name="audit_log")
    op.drop_table("audit_log")
