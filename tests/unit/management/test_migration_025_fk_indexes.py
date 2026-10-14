"""Migration 025 round-trip test: FK indexes on ``api_keys.team_id`` / ``virtual_keys.api_key_id``.

Scratch SQLite DB with minimal post-024 tables, stamped at the real ``024_teams`` head so
exactly one step (025) runs.
"""

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

ALEMBIC_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "..", "..", "services", "management", "alembic",
    )
)  # fmt: skip

_POST_024_DDL = (
    "CREATE TABLE api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, team_id INTEGER)",
    "CREATE TABLE virtual_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, api_key_id INTEGER)",
)


def _cfg(db_url: str) -> Config:
    """Alembic config pointed at the management service's migration scripts."""
    cfg = Config()
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """Scratch DB with minimal post-024 tables, stamped at 024; yields (url, engine)."""
    db_url = f"sqlite:///{tmp_path / 'migration025.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = sa.create_engine(db_url)
    with engine.begin() as conn:
        for ddl in _POST_024_DDL:
            conn.execute(sa.text(ddl))
    command.stamp(_cfg(db_url), "024_teams")
    yield db_url, engine
    engine.dispose()


def _indexes(engine, table: str) -> set[str]:
    """Return the index names SQLite reports on ``table``."""
    return {i["name"] for i in sa.inspect(engine).get_indexes(table)}


def test_upgrade_creates_both_indexes_and_downgrade_drops_them(scratch_db) -> None:
    """Upgrade adds both FK indexes; downgrade removes them again."""
    db_url, engine = scratch_db
    command.upgrade(_cfg(db_url), "025_fk_indexes")
    assert "ix_api_keys_team_id" in _indexes(engine, "api_keys")
    assert "ix_virtual_keys_api_key_id" in _indexes(engine, "virtual_keys")

    command.downgrade(_cfg(db_url), "024_teams")
    assert "ix_api_keys_team_id" not in _indexes(engine, "api_keys")
    assert "ix_virtual_keys_api_key_id" not in _indexes(engine, "virtual_keys")


def test_upgrade_is_idempotent_when_indexes_already_exist(scratch_db) -> None:
    """A pre-existing index (drifted schema) makes 025 a no-op rather than an error."""
    db_url, engine = scratch_db
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE INDEX ix_api_keys_team_id ON api_keys (team_id)"))
    command.upgrade(_cfg(db_url), "025_fk_indexes")
    assert "ix_virtual_keys_api_key_id" in _indexes(engine, "virtual_keys")


def test_models_declare_the_same_indexes() -> None:
    """The SQLAlchemy models carry the indexes, so create_all schemas match the migration."""
    from services.management.app.models_sqlalchemy import Base

    def names(table: str) -> set[str]:
        """Index names declared on the model table."""
        return {str(i.name) for i in Base.metadata.tables[table].indexes}

    assert "ix_api_keys_team_id" in names("api_keys")
    assert "ix_virtual_keys_api_key_id" in names("virtual_keys")


def test_025_chains_off_024() -> None:
    """025's parent is 024_teams (single linear history)."""
    script = ScriptDirectory.from_config(_cfg("sqlite://"))
    assert script.get_revision("025_fk_indexes").down_revision == "024_teams"
