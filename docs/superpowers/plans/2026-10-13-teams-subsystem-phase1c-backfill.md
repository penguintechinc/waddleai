# Teams Subsystem — Phase 1c: Default-Team Backfill and Postgres Verification

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §9, §11 (migration).
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q` (targeted runs skip the coverage addopts; the 90% coverage gate runs in Task 32).
**Prerequisite:** Phase 1b (`024_teams` creates the tables and columns).

---

<!-- TASK 4 -->
## Task 4: Migration 024 -- default-team backfill

**Files:**
- Modify: `services/management/alembic/versions/024_teams.py`
- Test: `tests/unit/management/test_migration_024_teams.py` (append)

**Interfaces:**
- Consumes: Task 3's `upgrade()`.
- Produces: `_backfill(bind)` (set-based, idempotent) and `_ADMIN_ROLES = ("admin",)` -- frozen in the migration (a historical migration must not import live app code); a test asserts it equals the roles that hold `org:admin_update`. Rules (spec §9): one `Default` team per org (even an org with no users); every user seated, `admin` for org-admin roles else `member`, **never `owner`**; each `api_keys.team_id` = its owner's team *in the key's org*, left NULL when the owner is not a member there.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/management/test_migration_024_teams.py` (these cover the Review Focus cases: an org with no admin user, an org with no users, a key whose owner lives in another org, a re-run, and a DB that already has a Default team with a custom role):

```diff
--- a/tests/unit/management/test_migration_024_teams.py
+++ b/tests/unit/management/test_migration_024_teams.py
@@ -132,6 +132,150 @@
     assert "api_key_id" not in _columns(engine, "virtual_keys")


+def _seed_populated(engine) -> None:
+    """Insert 3 orgs: acme (admin+user+rm+disabled user), globex (no admin), empty (no users)."""
+    stmts = (
+        "INSERT INTO organizations (name) VALUES ('acme'), ('globex'), ('empty')",
+        "INSERT INTO users (username, role, organization_id, enabled) VALUES "
+        "('boss', 'admin', 1, 1), ('dev', 'user', 1, 1), ('mgr', 'resource_manager', 1, 1), "
+        "('gone', 'user', 1, 0), ('gx', 'user', 2, 1)",
+        # k4's owner (user 5) lives in org 2 but the key claims org 1: no membership there.
+        "INSERT INTO api_keys (key_id, user_id, organization_id, name) VALUES "
+        "('k1', 1, 1, 'a'), ('k2', 2, 1, 'b'), ('k3', 5, 2, 'c'), ('k4', 5, 1, 'd')",
+    )
+    with engine.begin() as conn:
+        for stmt in stmts:
+            conn.execute(sa.text(stmt))
+
+
+def _rows(engine, sql: str) -> list[tuple]:
+    """Run ``sql`` and return all rows as plain tuples."""
+    with engine.connect() as conn:
+        return [tuple(r) for r in conn.execute(sa.text(sql))]
+
+
+def test_backfill_creates_one_default_team_per_org_including_empty_orgs(scratch_db) -> None:
+    """Every org gets exactly one 'Default' team -- even an org with no users."""
+    db_url, engine = scratch_db
+    _seed_populated(engine)
+    command.upgrade(alembic_config(db_url), "024_teams")
+
+    assert _rows(engine, "SELECT organization_id, name FROM teams ORDER BY organization_id") == [
+        (1, "Default"), (2, "Default"), (3, "Default"),
+    ]  # fmt: skip
+
+
+def test_backfill_assigns_admin_to_org_admins_and_member_to_everyone_else(scratch_db) -> None:
+    """Org-admin (``org:admin_update``) users become team admin; all others member; never owner."""
+    db_url, engine = scratch_db
+    _seed_populated(engine)
+    command.upgrade(alembic_config(db_url), "024_teams")
+
+    roles = dict(
+        _rows(
+            engine,
+            "SELECT u.username, tm.team_role FROM team_members tm "
+            "JOIN users u ON u.id = tm.user_id",
+        )
+    )
+    assert roles == {
+        "boss": "admin", "dev": "member", "mgr": "member", "gone": "member", "gx": "member",
+    }  # fmt: skip
+    assert "owner" not in roles.values()
+
+
+def test_backfill_org_without_an_admin_user_is_left_with_no_admin_or_owner(scratch_db) -> None:
+    """An org with no admin user gets a Default team whose members are all plain members."""
+    db_url, engine = scratch_db
+    _seed_populated(engine)
+    command.upgrade(alembic_config(db_url), "024_teams")
+
+    globex_roles = _rows(engine, "SELECT team_role FROM team_members WHERE organization_id = 2")
+    assert globex_roles == [("member",)]
+
+
+def test_backfill_points_each_key_at_its_owners_team_in_the_keys_org(scratch_db) -> None:
+    """``api_keys.team_id`` = the owner's team in the key's org; a non-member owner stays NULL."""
+    db_url, engine = scratch_db
+    _seed_populated(engine)
+    command.upgrade(alembic_config(db_url), "024_teams")
+
+    by_key = dict(_rows(engine, "SELECT key_id, team_id FROM api_keys"))
+    org1_team = _rows(engine, "SELECT id FROM teams WHERE organization_id = 1")[0][0]
+    org2_team = _rows(engine, "SELECT id FROM teams WHERE organization_id = 2")[0][0]
+    assert by_key == {"k1": org1_team, "k2": org1_team, "k3": org2_team, "k4": None}
+
+
+def test_backfill_is_idempotent(scratch_db) -> None:
+    """Running the data step again changes nothing (no duplicate teams/members, no re-pointing)."""
+    db_url, engine = scratch_db
+    _seed_populated(engine)
+    cfg = alembic_config(db_url)
+    command.upgrade(cfg, "024_teams")
+    before = (
+        _rows(engine, "SELECT id, organization_id, name FROM teams ORDER BY id"),
+        _rows(engine, "SELECT team_id, user_id, team_role FROM team_members ORDER BY id"),
+        _rows(engine, "SELECT key_id, team_id FROM api_keys ORDER BY id"),
+    )
+
+    module = ScriptDirectory.from_config(cfg).get_revision("024_teams").module
+    with engine.begin() as conn:
+        module._backfill(conn)
+
+    after = (
+        _rows(engine, "SELECT id, organization_id, name FROM teams ORDER BY id"),
+        _rows(engine, "SELECT team_id, user_id, team_role FROM team_members ORDER BY id"),
+        _rows(engine, "SELECT key_id, team_id FROM api_keys ORDER BY id"),
+    )
+    assert after == before
+
+
+def test_backfill_respects_an_existing_membership_and_default_team(tmp_path, monkeypatch) -> None:
+    """Over an ORM-built DB that already has a Default team + custom role, nothing is clobbered."""
+    from services.management.app.models_sqlalchemy import Base
+
+    db_url = f"sqlite:///{tmp_path / 'preexisting.db'}"
+    monkeypatch.setenv("DATABASE_URL", db_url)
+    engine = sa.create_engine(db_url)
+    Base.metadata.create_all(engine)
+    with engine.begin() as conn:
+        conn.execute(sa.text("INSERT INTO organizations (name) VALUES ('acme')"))
+        conn.execute(
+            sa.text(
+                "INSERT INTO users (username, email, password_hash, role, organization_id) "
+                "VALUES ('boss', 'b@x.io', 'x', 'admin', 1)"
+            )
+        )
+        conn.execute(sa.text("INSERT INTO teams (organization_id, name) VALUES (1, 'Default')"))
+        conn.execute(
+            sa.text(
+                "INSERT INTO team_members (team_id, user_id, organization_id, team_role) "
+                "VALUES (1, 1, 1, 'owner')"
+            )
+        )
+    command.stamp(alembic_config(db_url), "023_schema_drift_gh213")
+    command.upgrade(alembic_config(db_url), "024_teams")
+
+    assert _rows(engine, "SELECT name FROM teams") == [("Default",)]
+    assert _rows(engine, "SELECT user_id, team_role FROM team_members") == [(1, "owner")]
+    engine.dispose()
+
+
+def test_migration_admin_role_set_matches_the_org_admin_scope_holders() -> None:
+    """024's frozen admin-role constant must equal the roles that hold ``org:admin_update``."""
+    from shared.auth.rbac import ROLE_PERMISSIONS, Permission
+
+    holders = {
+        role.value
+        for role, perms in ROLE_PERMISSIONS.items()
+        if Permission.ORG_ADMIN_UPDATE in perms
+    }
+    module = (
+        ScriptDirectory.from_config(alembic_config("sqlite://")).get_revision("024_teams").module
+    )
+    assert set(module._ADMIN_ROLES) == holders
+
+
 def test_alembic_chain_is_single_headed_with_024() -> None:
     """024 extends 023 and leaves exactly one resolvable head."""
     script = ScriptDirectory.from_config(alembic_config("sqlite://"))
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `.venv/bin/python -m pytest tests/unit/management/test_migration_024_teams.py --no-cov -q`
Expected: the new backfill tests FAIL (`assert [] == [(1, 'Default'), (2, 'Default'), (3, 'Default')]`).

- [ ] **Step 3: Implement the backfill**

```diff
--- a/services/management/alembic/versions/024_teams.py
+++ b/services/management/alembic/versions/024_teams.py
@@ -17,6 +17,7 @@
 """

 from collections.abc import Sequence
+from datetime import datetime

 import sqlalchemy as sa
 from alembic import op
@@ -25,6 +26,14 @@
 down_revision: str | None = "023_schema_drift_gh213"
 branch_labels: str | Sequence[str] | None = None
 depends_on: str | Sequence[str] | None = None
+
+
+_DEFAULT_TEAM_NAME = "Default"
+
+# Roles holding the org-admin scope (``org:admin_update``) -- they become team ``admin`` in the
+# backfill. Frozen here (a historical migration must not import live app code); a unit test
+# asserts it equals the roles that hold that scope in ``ROLE_PERMISSIONS``.
+_ADMIN_ROLES = ("admin",)


 def _has_table(bind: sa.engine.Connection, table: str) -> bool:
@@ -53,8 +62,47 @@
         op.create_foreign_key(fk_name, table, ref_table, [column], ["id"], ondelete="SET NULL")


+def _backfill(bind: sa.engine.Connection) -> None:
+    """Give every org a Default team, seat its users, and point each key at its owner's team.
+
+    Set-based and idempotent (every INSERT/UPDATE is guarded by NOT EXISTS / ``IS NULL``), so a
+    re-run, or a run over a DB that already has teams, neither duplicates nor overwrites. The
+    migration never assigns ``owner`` -- an org admin designates one afterwards (spec §9).
+    """
+    now = datetime.utcnow()
+    bind.execute(
+        sa.text(
+            "INSERT INTO teams (organization_id, name, enabled, created_at) "
+            "SELECT o.id, :name, :enabled, :now FROM organizations o "
+            "WHERE NOT EXISTS (SELECT 1 FROM teams t "
+            "WHERE t.organization_id = o.id AND t.name = :name)"
+        ),
+        {"name": _DEFAULT_TEAM_NAME, "enabled": True, "now": now},
+    )
+    bind.execute(
+        sa.text(
+            "INSERT INTO team_members (team_id, user_id, organization_id, team_role, created_at) "
+            "SELECT t.id, u.id, u.organization_id, "
+            "CASE WHEN u.role IN :admin_roles THEN 'admin' ELSE 'member' END, :now "
+            "FROM users u JOIN teams t "
+            "ON t.organization_id = u.organization_id AND t.name = :name "
+            "WHERE NOT EXISTS (SELECT 1 FROM team_members tm "
+            "WHERE tm.user_id = u.id AND tm.organization_id = u.organization_id)"
+        ).bindparams(sa.bindparam("admin_roles", expanding=True)),
+        {"name": _DEFAULT_TEAM_NAME, "admin_roles": list(_ADMIN_ROLES), "now": now},
+    )
+    bind.execute(
+        sa.text(
+            "UPDATE api_keys SET team_id = (SELECT tm.team_id FROM team_members tm "
+            "WHERE tm.user_id = api_keys.user_id "
+            "AND tm.organization_id = api_keys.organization_id) "
+            "WHERE team_id IS NULL"
+        )
+    )
+
+
 def upgrade() -> None:
-    """Create ``teams``/``team_members`` and add ``api_keys.team_id``/``virtual_keys.api_key_id``."""
+    """Create the team tables, add the link columns, then backfill the Default teams."""
     bind = op.get_bind()

     if not _has_table(bind, "teams"):
@@ -94,6 +142,7 @@
     _add_nullable_fk_column(
         bind, "virtual_keys", "api_key_id", "api_keys", "fk_virtual_keys_api_key_id"
     )
+    _backfill(bind)


 def downgrade() -> None:
```

- [ ] **Step 4: Run them and confirm they pass**

Run: `.venv/bin/python -m pytest tests/unit/management/test_migration_024_teams.py --no-cov -q`
Expected: `12 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/management/alembic/versions/024_teams.py tests/unit/management/test_migration_024_teams.py
git commit -m "feat(teams): 024_teams backfills a Default team per org and binds existing keys"
```

---

<!-- TASK 5 -->
## Task 5: Migration 024 on real Postgres

**Files:**
- Test: `tests/integration/test_teams_migration_pg.py` (create)

**Interfaces:**
- Consumes: Tasks 3-4. Needs Docker and the `postgres:17-bookworm` image; **skips** (not fails) when Docker is unavailable, like `test_gh213_schema_drift.py`.
- Produces: proof of the Postgres-only paths SQLite cannot see: the two real `ON DELETE SET NULL` FK constraints from `op.create_foreign_key`, the `IN :admin_roles` expanding bind, boolean parameters, and the real UNIQUE enforcement.

- [ ] **Step 1: Write the test**

```python
"""Migration 024_teams against real Postgres (FK constraints, expanding IN, boolean params).

The SQLite unit test (``tests/unit/management/test_migration_024_teams.py``) cannot see the
Postgres-only paths: the real ``ON DELETE SET NULL`` foreign keys 024 adds via
``op.create_foreign_key``, the ``IN :admin_roles`` expanding bind, and boolean parameters.
Skips (not fails) when Docker is unavailable -- same convention as ``test_gh213_schema_drift.py``.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from tests.e2e.conftest import _free_port

MANAGEMENT_DIR = Path(__file__).resolve().parents[2] / "services" / "management"
_PG_IMAGE = "postgres:17-bookworm"
_PG_PASSWORD = "teams-migration"  # noqa: S105 -- ephemeral local test container password

_PRE_024_DDL = (
    "CREATE TABLE organizations (id SERIAL PRIMARY KEY, name VARCHAR(255) NOT NULL)",
    "CREATE TABLE users (id SERIAL PRIMARY KEY, username VARCHAR(255) NOT NULL, "
    "role VARCHAR(50) NOT NULL, organization_id INTEGER NOT NULL REFERENCES organizations(id))",
    "CREATE TABLE api_keys (id SERIAL PRIMARY KEY, key_id VARCHAR(255) NOT NULL, "
    "user_id INTEGER NOT NULL REFERENCES users(id), "
    "organization_id INTEGER NOT NULL REFERENCES organizations(id), name VARCHAR(255) NOT NULL)",
    "CREATE TABLE virtual_keys (id SERIAL PRIMARY KEY, user_id INTEGER, organization_id INTEGER, "
    "name VARCHAR(255) NOT NULL)",
    "INSERT INTO organizations (name) VALUES ('acme'), ('globex')",
    "INSERT INTO users (username, role, organization_id) VALUES "
    "('boss', 'admin', 1), ('dev', 'user', 1), ('gx', 'user', 2)",
    "INSERT INTO api_keys (key_id, user_id, organization_id, name) VALUES "
    "('k1', 1, 1, 'a'), ('k3', 3, 2, 'c')",
)


@pytest.fixture(scope="module")
def pg_admin_dsn() -> Iterator[str]:
    """DSN to the maintenance DB of a module-lived Postgres container."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not available -- teams migration test needs a real Postgres")
    port = _free_port()
    name = f"waddleai-teams024-{uuid.uuid4().hex[:8]}"
    run_argv = [
        "docker", "run", "--rm", "-d", "--name", name,
        "-e", f"POSTGRES_PASSWORD={_PG_PASSWORD}", "-p", f"{port}:5432", _PG_IMAGE,
    ]  # fmt: skip
    try:
        subprocess.run(run_argv, check=True, capture_output=True, timeout=120, text=True)  # noqa: S603
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"could not start Postgres: {exc.stderr}")
        return
    dsn = f"postgresql://postgres:{_PG_PASSWORD}@127.0.0.1:{port}/postgres"
    try:
        for _ in range(60):
            try:
                eng = sa.create_engine(dsn)
                with eng.connect() as conn:
                    conn.execute(sa.text("SELECT 1"))
                eng.dispose()
                break
            except sa.exc.OperationalError:
                time.sleep(1)
        else:
            pytest.fail(f"Postgres container {name!r} never accepted connections")
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)  # noqa: S603, S607


@pytest.fixture
def migrated(pg_admin_dsn, monkeypatch):
    """A fresh DB with the pre-024 schema seeded, stamped 023 and upgraded to 024; yields engine."""
    db_name = f"t024_{uuid.uuid4().hex[:10]}"
    admin = sa.create_engine(pg_admin_dsn, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{db_name}"'))
    admin.dispose()
    url = f"{pg_admin_dsn.rsplit('/', 1)[0]}/{db_name}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        for ddl in _PRE_024_DDL:
            conn.execute(sa.text(ddl))
    cfg = Config()
    cfg.set_main_option("script_location", str(MANAGEMENT_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.stamp(cfg, "023_schema_drift_gh213")
    command.upgrade(cfg, "024_teams")
    yield engine, cfg
    engine.dispose()


def test_upgrade_backfills_and_adds_real_foreign_keys(migrated) -> None:
    """On Postgres 024 seats users, points keys at teams and creates the two real FKs."""
    engine, _cfg = migrated
    with engine.connect() as conn:
        roles = dict(
            conn.execute(
                sa.text(
                    "SELECT u.username, tm.team_role FROM team_members tm "
                    "JOIN users u ON u.id = tm.user_id"
                )
            ).all()
        )
        keyed = dict(
            conn.execute(sa.text("SELECT key_id, team_id IS NOT NULL FROM api_keys")).all()
        )
        fks = {
            r[0]
            for r in conn.execute(
                sa.text(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE constraint_type = 'FOREIGN KEY'"
                )
            )
        }
    assert roles == {"boss": "admin", "dev": "member", "gx": "member"}
    assert keyed == {"k1": True, "k3": True}
    assert {"fk_api_keys_team_id", "fk_virtual_keys_api_key_id"} <= fks


def test_unique_user_org_is_enforced_by_postgres(migrated) -> None:
    """A second team for the same (user, org) is rejected by the real UNIQUE constraint."""
    engine, _cfg = migrated
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO teams (organization_id, name) VALUES (1, 'Beta')"))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO team_members (team_id, user_id, organization_id, team_role) "
                "SELECT t.id, 2, 1, 'member' FROM teams t WHERE t.name = 'Beta'"
            )
        )


def test_downgrade_then_reupgrade_round_trips(migrated) -> None:
    """Down then up again leaves the same Default teams (the data step is re-runnable)."""
    engine, cfg = migrated
    command.downgrade(cfg, "023_schema_drift_gh213")
    command.upgrade(cfg, "024_teams")
    with engine.connect() as conn:
        names = conn.execute(sa.text("SELECT name FROM teams ORDER BY organization_id")).all()
    assert names == [("Default",), ("Default",)]
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/integration/test_teams_migration_pg.py --no-cov -q`
Expected: `3 passed` (a skip means Docker is unavailable -- run it on a host that has it before merging; do not treat a skip as verification).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_teams_migration_pg.py
git commit -m "test(teams): verify migration 024 against real Postgres"
```

- [ ] **Phase 1 gate:** `.venv/bin/python -m pytest tests/unit/management/test_team_models.py tests/unit/test_team_pydal_definitions.py tests/unit/management/test_migration_024_teams.py tests/unit/management/test_migration_019.py --no-cov -q` -- all pass.
