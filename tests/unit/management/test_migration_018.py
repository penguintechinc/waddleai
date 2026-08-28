"""Migration 018 round-trip test: ``model_access_policies`` (design spec §3.3).

Same technique as ``test_migration_016_hooks.py`` -- stamps a scratch SQLite
DB at the real ``017_ollama_deployment_namespace`` head and upgrades exactly
one step, then verifies the exact column set, a row round-trip, and that
downgrade cleanly removes the table.
"""

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

ALEMBIC_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "..",
    "..",
    "services",
    "management",
    "alembic",
)


def _alembic_config(db_url: str) -> Config:
    """Alembic config pointed at the management service's migration scripts."""
    cfg = Config()
    cfg.set_main_option("script_location", os.path.abspath(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """Scratch db stamped (not built) at 017 -- no DDL run for prior revisions."""
    db_path = tmp_path / "migration018.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = sa.create_engine(db_url)
    yield db_url, engine
    engine.dispose()


def test_upgrade_creates_model_access_policies_table(scratch_db) -> None:
    """Upgrading to 018 creates ``model_access_policies`` with the expected columns."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "017_ollama_deployment_namespace")
    command.upgrade(cfg, "018_model_access_policies")

    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(model_access_policies)"))}

    assert cols == {
        "id",
        "scope_type",
        "scope_ref",
        "model_pattern",
        "action",
        "fallback_model",
        "reason",
        "enabled",
        "created_by",
        "created_at",
        "updated_at",
    }


def test_row_round_trips_with_defaults(scratch_db) -> None:
    """A minimal insert reads back with the server-side defaults applied."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "017_ollama_deployment_namespace")
    command.upgrade(cfg, "018_model_access_policies")

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO model_access_policies (scope_type, scope_ref, model_pattern) "
                "VALUES ('org', '7', 'claude-opus-5*')"
            )
        )

    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT scope_type, scope_ref, model_pattern, action, enabled "
                "FROM model_access_policies"
            )
        ).one()

    assert row.scope_type == "org"
    assert row.scope_ref == "7"
    assert row.model_pattern == "claude-opus-5*"
    assert row.action == "reject"
    # SQLite has no native boolean type -- a text server_default round-trips
    # as the literal string, unlike Postgres's real boolean. Truthy either way.
    assert row.enabled in (1, "true", True)


def test_row_round_trips_reroute_with_fallback(scratch_db) -> None:
    """A reroute row keeps its fallback_model and reason intact."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "017_ollama_deployment_namespace")
    command.upgrade(cfg, "018_model_access_policies")

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO model_access_policies "
                "(scope_type, scope_ref, model_pattern, action, fallback_model, reason) "
                "VALUES ('org', '7', 'claude-opus-5*', 'reroute', 'claude-opus-4.8', "
                "'org prefers 4.8')"
            )
        )

    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT action, fallback_model, reason FROM model_access_policies")
        ).one()

    assert row.action == "reroute"
    assert row.fallback_model == "claude-opus-4.8"
    assert row.reason == "org prefers 4.8"


def test_downgrade_drops_model_access_policies_table(scratch_db) -> None:
    """Downgrading one step removes the table cleanly."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "017_ollama_deployment_namespace")
    command.upgrade(cfg, "018_model_access_policies")
    command.downgrade(cfg, "017_ollama_deployment_namespace")

    with engine.connect() as conn:
        tables = {
            r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))
        }

    assert "model_access_policies" not in tables


def test_alembic_chain_still_single_head_after_018() -> None:
    """Adding 018 keeps a single resolvable head, no divergent branches."""
    from alembic.script import ScriptDirectory

    cfg = _alembic_config("sqlite://")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()

    assert len(heads) == 1
    assert heads[0] == "018_model_access_policies"
