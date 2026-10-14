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


def _seed_populated(engine) -> None:
    """Insert 3 orgs: acme (admin+user+rm+disabled user), globex (no admin), empty (no users)."""
    stmts = (
        "INSERT INTO organizations (name) VALUES ('acme'), ('globex'), ('empty')",
        "INSERT INTO users (username, role, organization_id, enabled) VALUES "
        "('boss', 'admin', 1, 1), ('dev', 'user', 1, 1), ('mgr', 'resource_manager', 1, 1), "
        "('gone', 'user', 1, 0), ('gx', 'user', 2, 1)",
        # k4's owner (user 5) lives in org 2 but the key claims org 1: no membership there.
        "INSERT INTO api_keys (key_id, user_id, organization_id, name) VALUES "
        "('k1', 1, 1, 'a'), ('k2', 2, 1, 'b'), ('k3', 5, 2, 'c'), ('k4', 5, 1, 'd')",
    )
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(sa.text(stmt))


def _rows(engine, sql: str) -> list[tuple]:
    """Run ``sql`` and return all rows as plain tuples."""
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.text(sql))]


def test_backfill_creates_one_default_team_per_org_including_empty_orgs(scratch_db) -> None:
    """Every org gets exactly one 'Default' team -- even an org with no users."""
    db_url, engine = scratch_db
    _seed_populated(engine)
    command.upgrade(alembic_config(db_url), "024_teams")

    assert _rows(engine, "SELECT organization_id, name FROM teams ORDER BY organization_id") == [
        (1, "Default"), (2, "Default"), (3, "Default"),
    ]  # fmt: skip


def test_backfill_assigns_admin_to_org_admins_and_member_to_everyone_else(scratch_db) -> None:
    """Org-admin (``org:admin_update``) users become team admin; all others member; never owner."""
    db_url, engine = scratch_db
    _seed_populated(engine)
    command.upgrade(alembic_config(db_url), "024_teams")

    roles = dict(
        _rows(
            engine,
            "SELECT u.username, tm.team_role FROM team_members tm "
            "JOIN users u ON u.id = tm.user_id",
        )
    )
    assert roles == {
        "boss": "admin", "dev": "member", "mgr": "member", "gone": "member", "gx": "member",
    }  # fmt: skip
    assert "owner" not in roles.values()


def test_backfill_org_without_an_admin_user_is_left_with_no_admin_or_owner(scratch_db) -> None:
    """An org with no admin user gets a Default team whose members are all plain members."""
    db_url, engine = scratch_db
    _seed_populated(engine)
    command.upgrade(alembic_config(db_url), "024_teams")

    globex_roles = _rows(engine, "SELECT team_role FROM team_members WHERE organization_id = 2")
    assert globex_roles == [("member",)]


def test_backfill_points_each_key_at_its_owners_team_in_the_keys_org(scratch_db) -> None:
    """``api_keys.team_id`` = the owner's team in the key's org; a non-member owner stays NULL."""
    db_url, engine = scratch_db
    _seed_populated(engine)
    command.upgrade(alembic_config(db_url), "024_teams")

    by_key = dict(_rows(engine, "SELECT key_id, team_id FROM api_keys"))
    org1_team = _rows(engine, "SELECT id FROM teams WHERE organization_id = 1")[0][0]
    org2_team = _rows(engine, "SELECT id FROM teams WHERE organization_id = 2")[0][0]
    assert by_key == {"k1": org1_team, "k2": org1_team, "k3": org2_team, "k4": None}


def test_backfill_is_idempotent(scratch_db) -> None:
    """Running the data step again changes nothing (no duplicate teams/members, no re-pointing)."""
    db_url, engine = scratch_db
    _seed_populated(engine)
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "024_teams")
    before = (
        _rows(engine, "SELECT id, organization_id, name FROM teams ORDER BY id"),
        _rows(engine, "SELECT team_id, user_id, team_role FROM team_members ORDER BY id"),
        _rows(engine, "SELECT key_id, team_id FROM api_keys ORDER BY id"),
    )

    module = ScriptDirectory.from_config(cfg).get_revision("024_teams").module
    with engine.begin() as conn:
        module._backfill(conn)

    after = (
        _rows(engine, "SELECT id, organization_id, name FROM teams ORDER BY id"),
        _rows(engine, "SELECT team_id, user_id, team_role FROM team_members ORDER BY id"),
        _rows(engine, "SELECT key_id, team_id FROM api_keys ORDER BY id"),
    )
    assert after == before


def test_backfill_respects_an_existing_membership_and_default_team(tmp_path, monkeypatch) -> None:
    """Over an ORM-built DB that already has a Default team + custom role, nothing is clobbered."""
    from services.management.app.models_sqlalchemy import Base

    db_url = f"sqlite:///{tmp_path / 'preexisting.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = sa.create_engine(db_url)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO organizations (name) VALUES ('acme')"))
        conn.execute(
            sa.text(
                "INSERT INTO users (username, email, password_hash, role, organization_id) "
                "VALUES ('boss', 'b@x.io', 'x', 'admin', 1)"
            )
        )
        conn.execute(sa.text("INSERT INTO teams (organization_id, name) VALUES (1, 'Default')"))
        conn.execute(
            sa.text(
                "INSERT INTO team_members (team_id, user_id, organization_id, team_role) "
                "VALUES (1, 1, 1, 'owner')"
            )
        )
    command.stamp(alembic_config(db_url), "023_schema_drift_gh213")
    command.upgrade(alembic_config(db_url), "024_teams")

    assert _rows(engine, "SELECT name FROM teams") == [("Default",)]
    assert _rows(engine, "SELECT user_id, team_role FROM team_members") == [(1, "owner")]
    engine.dispose()


def test_migration_admin_role_set_matches_the_org_admin_scope_holders() -> None:
    """024's frozen admin-role constant must equal the roles that hold ``org:admin_update``."""
    from shared.auth.rbac import ROLE_PERMISSIONS, Permission

    holders = {
        role.value
        for role, perms in ROLE_PERMISSIONS.items()
        if Permission.ORG_ADMIN_UPDATE in perms
    }
    module = (
        ScriptDirectory.from_config(alembic_config("sqlite://")).get_revision("024_teams").module
    )
    assert set(module._ADMIN_ROLES) == holders


def test_alembic_chain_is_single_headed_with_024() -> None:
    """024 extends 023 and leaves exactly one resolvable head."""
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    assert len(script.get_heads()) == 1
    assert script.get_revision("024_teams").down_revision == "023_schema_drift_gh213"
