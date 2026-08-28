"""Migration 007 round-trip test: drop AILB, fold usage, add native limits.

Creates the pre-007 (baseline + 006) shape of the affected tables on a
scratch sqlite DB, stamps alembic at 006, upgrades to head, and verifies:
the AILB usage fold actually moves rows into token_usage (not just DDL);
the three AILB tables are gone; virtual_keys/token_usage/organizations gain
their new columns; and token_conversion_rates is seeded with the real
(non-truncated) default rates. Then downgrades one revision and verifies
the documented behavior -- folded token_usage rows are NOT unfolded/deleted.
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
    cfg = Config()
    cfg.set_main_option("script_location", os.path.abspath(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _create_pre_007_schema(engine: sa.engine.Engine) -> None:
    """Pre-007 (baseline + 006) shape of every table this migration touches."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE organizations ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name VARCHAR(255) NOT NULL UNIQUE, "
                "description TEXT, "
                "token_quota_monthly INTEGER, "
                "token_quota_daily INTEGER, "
                "default_model VARCHAR(255), "
                "enabled BOOLEAN, "
                "created_at DATETIME)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE virtual_keys ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id INTEGER, "
                "organization_id INTEGER, "
                "name VARCHAR(255) NOT NULL, "
                "key_prefix VARCHAR(20), "
                "key_hash VARCHAR(255), "
                "ailb_key_id VARCHAR(255), "
                "ailb_sync_status VARCHAR(50), "
                "allowed_models JSON, "
                "allowed_providers JSON, "
                "budget_limit_daily INTEGER, "
                "budget_limit_monthly INTEGER, "
                "tpm_limit INTEGER, "
                "rpm_limit INTEGER, "
                "enabled BOOLEAN, "
                "expires_at DATETIME, "
                "last_used DATETIME, "
                "created_at DATETIME)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE token_usage ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "virtual_key_id INTEGER, "
                "user_id INTEGER, "
                "organization_id INTEGER, "
                "date DATETIME, "
                "waddleai_tokens INTEGER, "
                "llm_tokens JSON, "
                "tokens_input_total INTEGER, "
                "tokens_output_total INTEGER, "
                "request_count INTEGER, "
                "cost_usd_total INTEGER, "
                "last_updated DATETIME)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE token_conversion_rates ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "provider VARCHAR(100) NOT NULL, "
                "model VARCHAR(255) NOT NULL, "
                "input_rate INTEGER NOT NULL, "
                "output_rate INTEGER NOT NULL, "
                "base_cost_per_waddleai_token INTEGER, "
                "effective_date DATETIME, "
                "enabled BOOLEAN)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE ailb_usage_events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "event_id VARCHAR(255) UNIQUE, "
                "virtual_key_id INTEGER, "
                "ailb_key_id VARCHAR(255), "
                "request_id VARCHAR(255), "
                "model VARCHAR(255), "
                "provider VARCHAR(100), "
                "input_tokens INTEGER, "
                "output_tokens INTEGER, "
                "cost_usd INTEGER, "
                "latency_ms INTEGER, "
                "status VARCHAR(50), "
                "error_message TEXT, "
                "timestamp DATETIME, "
                "processed BOOLEAN, "
                "created_at DATETIME)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE ailb_usage_records ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id VARCHAR(255) NOT NULL, "
                "api_key_id VARCHAR(255), "
                "model VARCHAR(255) NOT NULL, "
                "provider VARCHAR(100) NOT NULL, "
                "input_tokens INTEGER, "
                "output_tokens INTEGER, "
                "total_tokens INTEGER, "
                "latency_ms INTEGER, "
                "request_id VARCHAR(255), "
                "timestamp DATETIME NOT NULL, "
                "created_at DATETIME)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE marchproxy_ailb_sync ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "provider_id INTEGER, "
                "ailb_instance_id VARCHAR(255), "
                "ailb_route_id VARCHAR(255), "
                "sync_status VARCHAR(50), "
                "last_synced DATETIME, "
                "sync_error TEXT, "
                "config_hash VARCHAR(64), "
                "created_at DATETIME)"
            )
        )

        conn.execute(sa.text("INSERT INTO organizations (id, name, enabled) VALUES (1, 'acme', 1)"))
        conn.execute(
            sa.text(
                "INSERT INTO virtual_keys "
                "(id, organization_id, name, ailb_key_id, ailb_sync_status) "
                "VALUES (1, 1, 'acme-key', 'ailb-abc123', 'synced')"
            )
        )


def _table_names(engine: sa.engine.Engine) -> set:
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))
        return {r[0] for r in rows}


def _columns(engine: sa.engine.Engine, table: str) -> set:
    with engine.connect() as conn:
        return {r[1] for r in conn.execute(sa.text(f"PRAGMA table_info({table})"))}


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """Build a fresh SQLite DB at the pre-007 schema and point DATABASE_URL at it."""
    db_path = tmp_path / "migration007.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = sa.create_engine(db_url)
    _create_pre_007_schema(engine)
    yield db_url, engine
    engine.dispose()


def _seed_ailb_usage(engine: sa.engine.Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO ailb_usage_events "
                "(event_id, virtual_key_id, ailb_key_id, model, provider, "
                " input_tokens, output_tokens, cost_usd, timestamp, created_at) "
                "VALUES ('evt-1', 1, 'ailb-abc123', 'gpt-4', 'openai', 100, 50, 45, "
                " '2026-06-01 00:00:00', '2026-06-01 00:00:00')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO ailb_usage_records "
                "(user_id, api_key_id, model, provider, input_tokens, output_tokens, "
                " total_tokens, timestamp, created_at) "
                "VALUES ('42', 'key-xyz', 'claude-3-haiku', 'anthropic', 200, 80, 280, "
                " '2026-06-02 00:00:00', '2026-06-02 00:00:00')"
            )
        )
        # Non-numeric external user_id -- must fold with user_id=NULL, not crash.
        conn.execute(
            sa.text(
                "INSERT INTO ailb_usage_records "
                "(user_id, api_key_id, model, provider, input_tokens, output_tokens, "
                " total_tokens, timestamp, created_at) "
                "VALUES ('external-user-abc', 'key-abc', 'llama2', 'ollama', 10, 5, 15, "
                " '2026-06-03 00:00:00', '2026-06-03 00:00:00')"
            )
        )


def test_upgrade_folds_ailb_usage_into_token_usage(scratch_db):
    """The fold must actually move rows, not silently drop them (Q#1 continuity)."""
    db_url, engine = scratch_db
    _seed_ailb_usage(engine)
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "006_add_memory_scope")
    command.upgrade(cfg, "007_drop_ailb_add_native_limits")

    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT virtual_key_id, user_id, tokens_input_total, "
                "tokens_output_total, source FROM token_usage"
            )
        ).fetchall()

    assert len(rows) == 3
    assert all(r.source == "ailb_import" for r in rows)

    # ailb_usage_events row (virtual_key_id=1, 100/50 tokens)
    event_rows = [r for r in rows if r.virtual_key_id == 1]
    assert len(event_rows) == 1
    assert event_rows[0].tokens_input_total == 100
    assert event_rows[0].tokens_output_total == 50

    # ailb_usage_records row with a resolvable numeric user_id
    numeric_rows = [r for r in rows if r.user_id == 42]
    assert len(numeric_rows) == 1
    assert numeric_rows[0].tokens_input_total == 200
    assert numeric_rows[0].tokens_output_total == 80

    # ailb_usage_records row with a non-numeric user_id folds with user_id=NULL
    unresolved_rows = [r for r in rows if r.virtual_key_id is None and r.user_id is None]
    assert len(unresolved_rows) == 1
    assert unresolved_rows[0].tokens_input_total == 10


def test_upgrade_fold_safe_on_empty_ailb_tables(scratch_db):
    """No AILB usage rows at all -- fold must be a safe no-op, not an error."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "006_add_memory_scope")
    command.upgrade(cfg, "007_drop_ailb_add_native_limits")

    with engine.connect() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM token_usage")).scalar()
    assert count == 0


def test_upgrade_drops_ailb_tables_and_columns(scratch_db):
    """Upgrade drops the AILB tables/columns and adds the new virtual_keys budget columns."""
    db_url, engine = scratch_db
    _seed_ailb_usage(engine)
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "006_add_memory_scope")
    command.upgrade(cfg, "007_drop_ailb_add_native_limits")

    tables = _table_names(engine)
    assert "ailb_usage_events" not in tables
    assert "ailb_usage_records" not in tables
    assert "marchproxy_ailb_sync" not in tables

    vkey_cols = _columns(engine, "virtual_keys")
    assert "ailb_key_id" not in vkey_cols
    assert "ailb_sync_status" not in vkey_cols
    assert {"budget_monthly_tokens", "budget_monthly_usd", "tpm_limit", "rpm_limit"} <= vkey_cols


def test_upgrade_adds_native_limit_columns(scratch_db):
    """Upgrade adds token_usage.source/estimated and organizations.rpm_limit columns."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "006_add_memory_scope")
    command.upgrade(cfg, "007_drop_ailb_add_native_limits")

    assert "source" in _columns(engine, "token_usage")
    assert "estimated" in _columns(engine, "token_usage")
    assert "rpm_limit" in _columns(engine, "organizations")


def test_upgrade_seeds_conversion_rates_without_truncation(scratch_db):
    """Seeded conversion rates keep full float precision after the column widens to Float."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "006_add_memory_scope")
    command.upgrade(cfg, "007_drop_ailb_add_native_limits")

    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT input_rate, output_rate, base_cost_per_waddleai_token "
                "FROM token_conversion_rates WHERE provider='openai' AND model='gpt-4o-mini'"
            )
        ).one()
        count = conn.execute(sa.text("SELECT COUNT(*) FROM token_conversion_rates")).scalar()

    assert count == 12
    # The whole point of widening the column to Float: this must survive
    # exactly, not truncate to 0.
    assert row.base_cost_per_waddleai_token == pytest.approx(0.00015)
    assert row.input_rate == pytest.approx(10.0)
    assert row.output_rate == pytest.approx(10.0)


def test_downgrade_restores_ailb_tables_but_keeps_folded_usage(scratch_db):
    """Downgrade restores empty AILB tables/columns without unfolding already-folded usage rows."""
    db_url, engine = scratch_db
    _seed_ailb_usage(engine)
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "006_add_memory_scope")
    command.upgrade(cfg, "007_drop_ailb_add_native_limits")
    command.downgrade(cfg, "-1")

    tables = _table_names(engine)
    assert {"ailb_usage_events", "ailb_usage_records", "marchproxy_ailb_sync"} <= tables

    # Restored tables are empty -- the fold is one-directional.
    with engine.connect() as conn:
        events_count = conn.execute(sa.text("SELECT COUNT(*) FROM ailb_usage_events")).scalar()
        records_count = conn.execute(sa.text("SELECT COUNT(*) FROM ailb_usage_records")).scalar()
        token_usage_count = conn.execute(sa.text("SELECT COUNT(*) FROM token_usage")).scalar()
    assert events_count == 0
    assert records_count == 0
    # Documented behavior: folded rows are NOT unfolded/deleted on downgrade.
    assert token_usage_count == 3

    vkey_cols = _columns(engine, "virtual_keys")
    assert "ailb_key_id" in vkey_cols
    assert "ailb_sync_status" in vkey_cols
    assert "budget_monthly_tokens" not in vkey_cols
    assert "budget_monthly_usd" not in vkey_cols

    assert "source" not in _columns(engine, "token_usage")
    assert "rpm_limit" not in _columns(engine, "organizations")

    with engine.connect() as conn:
        rates_count = conn.execute(sa.text("SELECT COUNT(*) FROM token_conversion_rates")).scalar()
    assert rates_count == 0
