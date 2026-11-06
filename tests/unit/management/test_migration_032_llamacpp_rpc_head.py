"""Migration 032: llamacpp_deployments.rpc_head_node_uid (Plan F)."""

import os

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

ALEMBIC_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "..", "..", "services", "management", "alembic")
)  # fmt: skip
REV = "032_llamacpp_rpc_head"
PREV = "031_llamacpp_owner_org"


def _cfg(url: str) -> Config:
    """Alembic config pointed at the management migrations."""
    cfg = Config()
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _engine(tmp_path, monkeypatch, columns: str = "id INTEGER PRIMARY KEY"):
    """A scratch sqlite DB with a minimal llamacpp_deployments table, stamped at 031."""
    url = f"sqlite:///{tmp_path / 'm032.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text(f"CREATE TABLE llamacpp_deployments ({columns})"))
    command.stamp(_cfg(url), PREV)
    return url, engine


def _cols(engine) -> set[str]:
    """Column names of llamacpp_deployments."""
    return {c["name"] for c in sa.inspect(engine).get_columns("llamacpp_deployments")}


def test_chain_032_follows_031_and_is_head() -> None:
    """032 revises 031 and is the single head."""
    script = ScriptDirectory.from_config(_cfg("sqlite://"))
    assert script.get_revision(REV).down_revision == PREV
    assert script.get_heads() == [REV]


def test_upgrade_adds_nullable_column_and_downgrade_drops_it(tmp_path, monkeypatch) -> None:
    """Upgrade adds rpc_head_node_uid (existing rows NULL); downgrade removes it."""
    url, engine = _engine(tmp_path, monkeypatch)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO llamacpp_deployments (id) VALUES (1)"))
    command.upgrade(_cfg(url), REV)
    assert "rpc_head_node_uid" in _cols(engine)
    with engine.connect() as conn:
        assert (
            conn.execute(sa.text("SELECT rpc_head_node_uid FROM llamacpp_deployments")).scalar()
            is None
        )
    command.downgrade(_cfg(url), PREV)
    assert "rpc_head_node_uid" not in _cols(engine)


def test_upgrade_and_downgrade_are_idempotent_when_state_already_matches(
    tmp_path, monkeypatch
) -> None:
    """A column that already exists is not re-added; a missing one is not re-dropped."""
    url, engine = _engine(
        tmp_path, monkeypatch, "id INTEGER PRIMARY KEY, rpc_head_node_uid VARCHAR(255)"
    )
    command.upgrade(_cfg(url), REV)  # would raise "duplicate column" without the guard
    assert "rpc_head_node_uid" in _cols(engine)
    command.downgrade(_cfg(url), PREV)
    command.upgrade(_cfg(url), REV)
    command.downgrade(_cfg(url), PREV)
    assert "rpc_head_node_uid" not in _cols(engine)
    with engine.begin() as conn:  # downgrade of an absent column is a no-op, not an error
        conn.execute(sa.text("DROP TABLE llamacpp_deployments"))
    command.stamp(_cfg(url), REV)
    command.downgrade(_cfg(url), PREV)
