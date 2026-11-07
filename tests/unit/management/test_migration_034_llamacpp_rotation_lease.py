"""Migration 034: llamacpp_deployments.rotation_lease_until (single-flight token rotation)."""

import os

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

ALEMBIC_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "..", "..", "services", "management", "alembic")
)  # fmt: skip
REV = "034_llamacpp_rotation_lease"
PREV = "033_llamacpp_name_per_org"


def _cfg(url: str) -> Config:
    """Alembic config pointed at the management migrations."""
    cfg = Config()
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _cols(engine) -> set[str]:
    """Column names of llamacpp_deployments."""
    return {c["name"] for c in sa.inspect(engine).get_columns("llamacpp_deployments")}


def test_chain_034_follows_033_and_is_head() -> None:
    """034 revises 033 and is the single head."""
    script = ScriptDirectory.from_config(_cfg("sqlite://"))
    assert script.get_revision(REV).down_revision == PREV
    assert script.get_heads() == [REV]


def test_upgrade_downgrade_and_guards(tmp_path, monkeypatch) -> None:
    """Adds a nullable column; re-run and missing-table cases are no-ops; downgrade drops it."""
    url = f"sqlite:///{tmp_path / 'm034.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE llamacpp_deployments (id INTEGER PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO llamacpp_deployments (id) VALUES (1)"))
    command.stamp(_cfg(url), PREV)
    command.upgrade(_cfg(url), REV)
    assert "rotation_lease_until" in _cols(engine)
    with engine.connect() as conn:
        assert (
            conn.execute(sa.text("SELECT rotation_lease_until FROM llamacpp_deployments")).scalar()
            is None
        )
    command.downgrade(_cfg(url), PREV)
    assert "rotation_lease_until" not in _cols(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE llamacpp_deployments"))
    command.stamp(_cfg(url), PREV)
    command.upgrade(_cfg(url), REV)  # no table: guarded no-op
    command.downgrade(_cfg(url), PREV)
