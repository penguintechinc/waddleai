"""Teams subsystem: ``teams``, ``team_members``, ``api_keys.team_id`` (design spec §4).

Teams nest under an organization (the tenant). ``team_members`` carries a denormalized
``organization_id`` so ``UNIQUE(user_id, organization_id)`` enforces D2 -- one team per
(user, tenant) -- in the database, not just the service layer.

Also adds ``virtual_keys.api_key_id`` (nullable): the link from a management-issued key to
the ``api_keys`` row the proxy actually authenticates (issue #303 / D10), so revoke, rotate
and disable on the virtual key can reach the proxy credential.

Safe on a populated database: nullable additions and new tables only, every step guarded so
it is a no-op where ``init_schema()``/``create_all`` already built the object.

Revision ID: 024_teams
Revises: 023_schema_drift_gh213
Create Date: 2026-10-13
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision: str = "024_teams"
down_revision: str | None = "023_schema_drift_gh213"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_TEAM_NAME = "Default"

# Roles holding the org-admin scope (``org:admin_update``) -- they become team ``admin`` in the
# backfill. Frozen here (a historical migration must not import live app code); a unit test
# asserts it equals the roles that hold that scope in ``ROLE_PERMISSIONS``.
_ADMIN_ROLES = ("admin",)


def _has_table(bind: sa.engine.Connection, table: str) -> bool:
    """True if ``table`` already exists (guard for create_all-bootstrapped databases)."""
    return table in set(sa.inspect(bind).get_table_names())


def _has_column(bind: sa.engine.Connection, table: str, column: str) -> bool:
    """True if ``column`` already exists on ``table``."""
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _add_nullable_fk_column(
    bind: sa.engine.Connection, table: str, column: str, ref_table: str, fk_name: str
) -> None:
    """Add a nullable integer FK column; SQLite cannot ALTER-add a constraint, so skip the FK there.

    SQLite does not enforce foreign keys by default and rejects ``ALTER ... ADD CONSTRAINT``
    (Alembic raises ``NotImplementedError``), so on SQLite the column is added bare. Postgres
    gets the real ``ON DELETE SET NULL`` constraint.
    """
    if _has_column(bind, table, column):
        return
    op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))
    if bind.dialect.name != "sqlite":
        op.create_foreign_key(fk_name, table, ref_table, [column], ["id"], ondelete="SET NULL")


def _backfill(bind: sa.engine.Connection) -> None:
    """Give every org a Default team, seat its users, and point each key at its owner's team.

    Set-based and idempotent (every INSERT/UPDATE is guarded by NOT EXISTS / ``IS NULL``), so a
    re-run, or a run over a DB that already has teams, neither duplicates nor overwrites. The
    migration never assigns ``owner`` -- an org admin designates one afterwards (spec §9).
    """
    now = datetime.utcnow()
    bind.execute(
        sa.text(
            "INSERT INTO teams (organization_id, name, enabled, created_at) "
            "SELECT o.id, :name, :enabled, :now FROM organizations o "
            "WHERE NOT EXISTS (SELECT 1 FROM teams t "
            "WHERE t.organization_id = o.id AND t.name = :name)"
        ),
        {"name": _DEFAULT_TEAM_NAME, "enabled": True, "now": now},
    )
    bind.execute(
        sa.text(
            "INSERT INTO team_members (team_id, user_id, organization_id, team_role, created_at) "
            "SELECT t.id, u.id, u.organization_id, "
            "CASE WHEN u.role IN :admin_roles THEN 'admin' ELSE 'member' END, :now "
            "FROM users u JOIN teams t "
            "ON t.organization_id = u.organization_id AND t.name = :name "
            "WHERE NOT EXISTS (SELECT 1 FROM team_members tm "
            "WHERE tm.user_id = u.id AND tm.organization_id = u.organization_id)"
        ).bindparams(sa.bindparam("admin_roles", expanding=True)),
        {"name": _DEFAULT_TEAM_NAME, "admin_roles": list(_ADMIN_ROLES), "now": now},
    )
    bind.execute(
        sa.text(
            "UPDATE api_keys SET team_id = (SELECT tm.team_id FROM team_members tm "
            "WHERE tm.user_id = api_keys.user_id "
            "AND tm.organization_id = api_keys.organization_id) "
            "WHERE team_id IS NULL"
        )
    )


def upgrade() -> None:
    """Create the team tables, add the link columns, then backfill the Default teams."""
    bind = op.get_bind()

    if not _has_table(bind, "teams"):
        op.create_table(
            "teams",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False
            ),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("enabled", sa.Boolean(), server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.UniqueConstraint("organization_id", "name", name="uq_teams_org_name"),
        )

    if not _has_table(bind, "team_members"):
        op.create_table(
            "team_members",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "team_id",
                sa.Integer(),
                sa.ForeignKey("teams.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column(
                "organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False
            ),
            sa.Column("team_role", sa.String(20), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.UniqueConstraint("user_id", "organization_id", name="uq_team_members_user_org"),
            sa.UniqueConstraint("team_id", "user_id", name="uq_team_members_team_user"),
        )

    _add_nullable_fk_column(bind, "api_keys", "team_id", "teams", "fk_api_keys_team_id")
    _add_nullable_fk_column(
        bind, "virtual_keys", "api_key_id", "api_keys", "fk_virtual_keys_api_key_id"
    )
    _backfill(bind)


def downgrade() -> None:
    """Drop the link columns, then ``team_members``, then ``teams`` (FK order), tolerating gaps."""
    bind = op.get_bind()

    if _has_column(bind, "virtual_keys", "api_key_id"):
        op.drop_column("virtual_keys", "api_key_id")
    if _has_column(bind, "api_keys", "team_id"):
        op.drop_column("api_keys", "team_id")
    if _has_table(bind, "team_members"):
        op.drop_table("team_members")
    if _has_table(bind, "teams"):
        op.drop_table("teams")
