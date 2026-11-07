"""Migration 033: llamacpp_deployments.name unique per owner org (not global)."""

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

ALEMBIC_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "..", "..", "services", "management", "alembic")
)  # fmt: skip
REV = "033_llamacpp_name_per_org"
PREV = "032_llamacpp_rpc_head"
OLD_TABLE = (
    "CREATE TABLE llamacpp_deployments (id INTEGER PRIMARY KEY, "
    "name VARCHAR(255) NOT NULL UNIQUE, owner_org_id INTEGER)"
)


def _cfg(url: str) -> Config:
    """Alembic config pointed at the management migrations."""
    cfg = Config()
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _engine(tmp_path, monkeypatch, ddl: str = OLD_TABLE):
    """A scratch sqlite DB holding the pre-033 table (global UNIQUE name), stamped at 032."""
    url = f"sqlite:///{tmp_path / 'm033.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text(ddl))
    command.stamp(_cfg(url), PREV)
    return url, engine


def _insert(engine, name: str, org: int | None) -> None:
    """Insert one deployment row."""
    with engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO llamacpp_deployments (name, owner_org_id) VALUES (:n, :o)"),
            {"n": name, "o": org},
        )


def test_chain_033_follows_032() -> None:
    """033 revises 032 (034 now sits above it)."""
    script = ScriptDirectory.from_config(_cfg("sqlite://"))
    assert script.get_revision(REV).down_revision == PREV
    assert script.get_heads() == ["034_llamacpp_rotation_lease"]


def test_global_unique_replaced_by_per_org_unique(tmp_path, monkeypatch) -> None:
    """Before: org 2 cannot reuse org 1's name. After: it can; the same org still cannot."""
    url, engine = _engine(tmp_path, monkeypatch)
    _insert(engine, "shared", 1)
    with pytest.raises(sa.exc.IntegrityError):
        _insert(engine, "shared", 2)
    command.upgrade(_cfg(url), REV)
    _insert(engine, "shared", 2)
    with pytest.raises(sa.exc.IntegrityError):
        _insert(engine, "shared", 2)
    with pytest.raises(sa.exc.IntegrityError):  # legacy unowned rows share one bucket
        _insert(engine, "legacy", None)
        _insert(engine, "legacy", None)


def test_downgrade_restores_global_unique_and_reupgrade_is_idempotent(
    tmp_path, monkeypatch
) -> None:
    """Downgrade puts the global constraint back; a second upgrade/downgrade cycle works."""
    url, engine = _engine(tmp_path, monkeypatch)
    command.upgrade(_cfg(url), REV)
    command.upgrade(_cfg(url), REV)  # re-run safe (stamped no-op)
    command.downgrade(_cfg(url), PREV)
    _insert(engine, "n", 1)
    with pytest.raises(sa.exc.IntegrityError):
        _insert(engine, "n", 2)
    command.upgrade(_cfg(url), REV)
    _insert(engine, "n", 2)


def test_fresh_db_without_the_table_is_a_noop(tmp_path, monkeypatch) -> None:
    """A DB that has no llamacpp_deployments yet (guard, like 031/032) neither fails nor adds it."""
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.stamp(_cfg(url), PREV)
    command.upgrade(_cfg(url), REV)
    assert "llamacpp_deployments" not in sa.inspect(sa.create_engine(url)).get_table_names()
    command.downgrade(_cfg(url), PREV)
