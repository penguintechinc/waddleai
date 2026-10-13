# Teams Subsystem — Phase 1b: Alembic 024 Schema

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §4, §9 (migration step 1).
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q` (targeted runs skip the coverage addopts; the 90% coverage gate runs in Task 32).
**Prerequisite:** Phase 1a (models exist; the `real_schema_db` fixture exists).

---

<!-- TASK 3 -->
## Task 3: Alembic `024_teams` -- tables and columns

**Files:**
- Create: `services/management/alembic/versions/024_teams.py`
- Modify: `tests/unit/management/test_migration_019.py` (pins the single head by name -- bump it)
- Test: `tests/unit/management/test_migration_024_teams.py` (create)

**Interfaces:**
- Consumes: head `023_schema_drift_gh213`.
- Produces: revision `024_teams`; module attributes `_has_table`, `_has_column`, `_add_nullable_fk_column(bind, table, column, ref_table, fk_name)`. SQLite cannot `ALTER ... ADD CONSTRAINT` (Alembic raises `NotImplementedError`), so on SQLite the link columns are added bare; Postgres gets the real `ON DELETE SET NULL` FKs. Every step is guarded so the migration is a no-op over a `create_all`-built DB.

- [ ] **Step 1: Write the failing test**

Same technique as `test_migration_022.py`: a scratch SQLite DB with hand-built minimal pre-024 tables, stamped at 023.

```python
"""Migration 024 round-trip test: ``teams``, ``team_members``, ``api_keys.team_id``.

Same technique as ``test_migration_022.py``: a scratch SQLite DB holding hand-built minimal
pre-024 tables (the only tables 024 touches or reads), stamped at the real
``023_schema_drift_gh213`` head so exactly one step runs.
"""

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError

ALEMBIC_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "..", "..", "services", "management", "alembic",
    )
)  # fmt: skip

_PRE_024_DDL = (
    "CREATE TABLE organizations (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255) NOT NULL)",
    "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username VARCHAR(255) NOT NULL, "
    "role VARCHAR(50) NOT NULL, organization_id INTEGER NOT NULL, enabled BOOLEAN)",
    "CREATE TABLE api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, key_id VARCHAR(255) NOT NULL, "
    "user_id INTEGER NOT NULL, organization_id INTEGER NOT NULL, name VARCHAR(255) NOT NULL)",
    "CREATE TABLE virtual_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, "
    "organization_id INTEGER, name VARCHAR(255) NOT NULL)",
)


def alembic_config(db_url: str) -> Config:
    """Alembic config pointed at the management service's migration scripts."""
    cfg = Config()
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """A scratch DB with minimal pre-024 tables, stamped at 023; yields (url, engine)."""
    db_url = f"sqlite:///{tmp_path / 'migration024.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = sa.create_engine(db_url)
    with engine.begin() as conn:
        for ddl in _PRE_024_DDL:
            conn.execute(sa.text(ddl))
    command.stamp(alembic_config(db_url), "023_schema_drift_gh213")
    yield db_url, engine
    engine.dispose()


def _columns(engine, table: str) -> set[str]:
    """Return the column names of ``table`` as SQLite reports them."""
    with engine.connect() as conn:
        return {r[1] for r in conn.execute(sa.text(f"PRAGMA table_info({table})"))}


def test_upgrade_creates_team_tables_and_link_columns(scratch_db) -> None:
    """024 creates both tables with the spec §4 columns and adds the two nullable link columns."""
    db_url, engine = scratch_db
    command.upgrade(alembic_config(db_url), "024_teams")

    assert _columns(engine, "teams") == {"id", "organization_id", "name", "enabled", "created_at"}
    assert _columns(engine, "team_members") == {
        "id", "team_id", "user_id", "organization_id", "team_role", "created_at",
    }  # fmt: skip
    assert "team_id" in _columns(engine, "api_keys")
    assert "api_key_id" in _columns(engine, "virtual_keys")


def test_upgraded_schema_enforces_one_team_per_user_per_org(scratch_db) -> None:
    """D2 holds at the DB level on a migrated (not create_all) schema."""
    db_url, engine = scratch_db
    command.upgrade(alembic_config(db_url), "024_teams")

    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO organizations (name) VALUES ('acme')"))
        conn.execute(
            sa.text("INSERT INTO users (username, role, organization_id) VALUES ('a', 'user', 1)")
        )
        conn.execute(
            sa.text("INSERT INTO teams (organization_id, name) VALUES (1, 'Alpha'), (1, 'Beta')")
        )
        conn.execute(
            sa.text(
                "INSERT INTO team_members (team_id, user_id, organization_id, team_role) "
                "VALUES (1, 1, 1, 'member')"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO team_members (team_id, user_id, organization_id, team_role) "
                "VALUES (2, 1, 1, 'member')"
            )
        )


def test_upgrade_is_a_noop_over_an_orm_created_schema(tmp_path, monkeypatch) -> None:
    """A dev DB built by ``create_all`` already has everything: 024 must not fail."""
    from services.management.app.models_sqlalchemy import Base

    db_url = f"sqlite:///{tmp_path / 'orm_built.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = sa.create_engine(db_url)
    Base.metadata.create_all(engine)
    command.stamp(alembic_config(db_url), "023_schema_drift_gh213")

    command.upgrade(alembic_config(db_url), "024_teams")

    assert "team_id" in _columns(engine, "api_keys")
    engine.dispose()


def test_downgrade_removes_tables_and_columns(scratch_db) -> None:
    """Downgrading one step drops both tables and both link columns."""
    db_url, engine = scratch_db
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "024_teams")
    command.downgrade(cfg, "023_schema_drift_gh213")

    with engine.connect() as conn:
        tables = {r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master"))}
    assert "teams" not in tables
    assert "team_members" not in tables
    assert "team_id" not in _columns(engine, "api_keys")
    assert "api_key_id" not in _columns(engine, "virtual_keys")


def test_alembic_chain_is_single_headed_with_024() -> None:
    """024 extends 023 and leaves exactly one resolvable head."""
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    assert len(script.get_heads()) == 1
    assert script.get_revision("024_teams").down_revision == "023_schema_drift_gh213"
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/management/test_migration_024_teams.py --no-cov -q -x`
Expected: FAIL `alembic.util.exc.CommandError: Can't locate revision identified by '024_teams'`.

- [ ] **Step 3: Write the migration (schema only -- the data backfill is Task 4)**

```python
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

import sqlalchemy as sa
from alembic import op

revision: str = "024_teams"
down_revision: str | None = "023_schema_drift_gh213"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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


def upgrade() -> None:
    """Create ``teams``/``team_members`` and add ``api_keys.team_id``/``virtual_keys.api_key_id``."""
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
```

- [ ] **Step 4: Run it, then bump the head pin**

Run: `.venv/bin/python -m pytest tests/unit/management/test_migration_024_teams.py tests/unit/management/test_migration_019.py --no-cov -q`
Expected: `test_migration_024_teams.py` passes; `test_migration_019.py::test_single_alembic_head_no_divergent_branches` FAILS with `assert '024_teams' == '023_schema_drift_gh213'` (it pins the head by name on purpose). Update the pin:

```diff
--- a/tests/unit/management/test_migration_019.py
+++ b/tests/unit/management/test_migration_019.py
@@ -192,4 +192,4 @@
     heads = script.get_heads()

     assert len(heads) == 1
-    assert heads[0] == "023_schema_drift_gh213"
+    assert heads[0] == "024_teams"
```

Re-run both files: all pass.

- [ ] **Step 5: Commit**

```bash
git add services/management/alembic/versions/024_teams.py tests/unit/management/test_migration_024_teams.py tests/unit/management/test_migration_019.py
git commit -m "feat(teams): alembic 024_teams creates teams, team_members and key link columns"
```
