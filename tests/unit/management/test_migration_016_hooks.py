"""Migration 015 round-trip test (§18 platform spec).

Covers hook_rules, hook_denylist_entries, hook_configs, hook_telemetry_events.
Stamps a scratch SQLite DB at the real `014_integrations` parent (already
the current head at time of writing) and upgrades exactly one step to
`016_hooks`, then verifies the exact column set of each new table.
Downgrade restores the pre-015 (no hooks tables) shape.
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


# 016 chains off 015_local_vector_store, which lands on feature/local-only-profile.
# Alembic resolves the whole revision graph on any command, so until that file is
# present these cannot run at all. Skips itself away automatically once it merges.


def _alembic_config(db_url: str) -> Config:
    """Alembic config pointed at the management service's migration scripts."""
    cfg = Config()
    cfg.set_main_option("script_location", os.path.abspath(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """Scratch db stamped (not built) at 014_integrations -- no DDL run for prior revisions."""
    db_path = tmp_path / "migration015.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = sa.create_engine(db_url)
    yield db_url, engine
    engine.dispose()


def test_upgrade_creates_hooks_tables(scratch_db):
    """Upgrading to 016_hooks creates all four tables with the expected columns."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "014_integrations")
    command.upgrade(cfg, "016_hooks")

    with engine.connect() as conn:
        hr_cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(hook_rules)"))}
        hd_cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(hook_denylist_entries)"))}
        hc_cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(hook_configs)"))}
        ht_cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(hook_telemetry_events)"))}

    assert hr_cols == {
        "id", "scope_type", "scope_ref", "ecosystem", "event", "tool_name_pattern",
        "match_pattern", "decision", "reason", "enabled", "priority", "created_by",
        "created_at", "updated_at",
    }
    assert hd_cols == {
        "id", "scope_type", "scope_ref", "pattern", "reason", "enabled", "created_by",
        "created_at", "updated_at",
    }
    assert hc_cols == {
        "id", "scope_type", "scope_ref", "remote_eval_enabled", "remote_eval_timeout_ms",
        "remote_eval_fail_mode", "capture_raw_payloads", "created_at", "updated_at",
    }
    assert ht_cols == {
        "id", "organization_id", "ecosystem", "event", "tool_name", "session_id",
        "tool_input_hash", "tool_input_raw", "occurred_at", "received_at",
    }


def test_hook_rules_row_round_trips(scratch_db):
    """A hook_rules row inserted post-upgrade reads back with the values it was given."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "014_integrations")
    command.upgrade(cfg, "016_hooks")

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO hook_rules "
                "(scope_type, scope_ref, decision, reason, enabled, priority) "
                "VALUES ('org', '7', 'deny', 'no rm -rf', 1, 5)"
            )
        )

    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT scope_type, scope_ref, decision, priority FROM hook_rules")
        ).fetchone()

    assert row == ("org", "7", "deny", 5)


def test_downgrade_drops_hooks_tables(scratch_db):
    """Downgrading one step removes all four tables cleanly."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "014_integrations")
    command.upgrade(cfg, "016_hooks")
    command.downgrade(cfg, "014_integrations")

    with engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }

    assert "hook_rules" not in tables
    assert "hook_denylist_entries" not in tables
    assert "hook_configs" not in tables
    assert "hook_telemetry_events" not in tables
