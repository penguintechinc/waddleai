"""Add users.is_service_account / users.service_kind (headless auth H1).

Enables headless/machine auth: an API-key owner can now be a NON-HUMAN
service account, so it can be issued a scoped RS256 JWT (H2 does the token
exchange; this migration only adds the identity flag H2/H3 and seat
metering consume).

``is_service_account`` is ``NOT NULL DEFAULT false`` -- every pre-existing
row is a human user, which is the correct default and requires no backfill.
``service_kind`` is nullable free-text (documented set:
``ci``/``agent``/``health-check``/``migration-runner``, see
``shared/licensing/seats.py::INTERNAL_SERVICE_KINDS``) describing which kind
of non-human identity this is; ``NULL`` for ordinary human users.

Scope/claims derivation is unchanged by this migration --
``shared/auth/rbac.py::_build_user_context`` builds ``UserContext`` purely
from ``(role, organization_id, managed_orgs)`` and never reads either new
column, so a service-account owner yields the same claim shape as a human
owner (verified in ``tests/unit/test_rbac_service_account.py``).

Revision ID: 022_service_account_fields
Revises: 021_audit_log
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "022_service_account_fields"
down_revision: str | None = "021_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(bind: sa.engine.Connection, table: str, column: str) -> bool:
    """True if `column` already exists on `table` (guard for pre-migration ORM drift)."""
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    """Add users.is_service_account (NOT NULL, default false) and users.service_kind."""
    bind = op.get_bind()

    if not _has_column(bind, "users", "is_service_account"):
        op.add_column(
            "users",
            sa.Column(
                "is_service_account",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    if not _has_column(bind, "users", "service_kind"):
        op.add_column("users", sa.Column("service_kind", sa.String(50), nullable=True))


def downgrade() -> None:
    """Drop users.service_kind and users.is_service_account."""
    bind = op.get_bind()

    if _has_column(bind, "users", "service_kind"):
        op.drop_column("users", "service_kind")

    if _has_column(bind, "users", "is_service_account"):
        op.drop_column("users", "is_service_account")
