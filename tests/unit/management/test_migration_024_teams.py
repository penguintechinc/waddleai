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
