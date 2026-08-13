"""Migration 009a round-trip test: response cache tables + token_usage columns.

Runs on a scratch SQLite DB from the pre-009a (006-era) shape through to
head, verifying cache_configs + response_cache_entries exist with the exact
column set, the seeded global default row, and the token_usage additions.
Downgrade restores the 006 schema exactly. HNSW index existence is
PostgreSQL-only and isn't asserted here (see module docstring in the
migration itself) -- SQLite keeps prompt_embedding_json as its only
embedding column, matching the MemoryEmbedding pattern.
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
    """Alembic config."""
    cfg = Config()
    cfg.set_main_option("script_location", os.path.abspath(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """Scratch db."""
    db_path = tmp_path / "migration009a.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = sa.create_engine(db_url)
    with engine.begin() as conn:
        # Pre-009a shape of token_usage (enough columns to exercise add_column)
        conn.execute(
            sa.text(
                "CREATE TABLE token_usage ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "virtual_key_id INTEGER, "
                "user_id INTEGER, "
                "organization_id INTEGER, "
                "date DATETIME, "
                "waddleai_tokens INTEGER, "
                "tokens_input_total INTEGER, "
                "tokens_output_total INTEGER, "
                "request_count INTEGER, "
                "cost_usd_total INTEGER)"
            )
        )
    yield db_url, engine
    engine.dispose()


def test_upgrade_creates_cache_tables_and_seeds_default(scratch_db):
    """Upgrade creates cache tables and seeds default."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "006_add_memory_scope")
    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        cc_cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(cache_configs)"))}
        rce_cols = {
            r[1] for r in conn.execute(sa.text("PRAGMA table_info(response_cache_entries)"))
        }
        tu_cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(token_usage)"))}

    assert cc_cols == {
        "id",
        "scope_type",
        "scope_ref",
        "exact_enabled",
        "semantic_enabled",
        "semantic_threshold",
        "ttl_seconds",
        "max_entry_kb",
        "anthropic_cache_control",
        "created_at",
        "updated_at",
    }
    assert rce_cols == {
        "id",
        "org_id",
        "scope_key",
        "model_class",
        "prompt_embedding_json",
        "context_hash",
        "response",
        "hit_count",
        "created_at",
        "expires_at",
    }
    assert {"cache_status", "tokens_saved"} <= tu_cols

    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT scope_type, scope_ref, exact_enabled, semantic_enabled, "
                "semantic_threshold, ttl_seconds, max_entry_kb, anthropic_cache_control "
                "FROM cache_configs"
            )
        ).one()
    assert row.scope_type == "global"
    assert row.scope_ref is None
    assert bool(row.exact_enabled) is True
    assert bool(row.semantic_enabled) is False
    assert row.semantic_threshold == 0.95
    assert row.ttl_seconds == 86400
    assert row.max_entry_kb == 256
    assert bool(row.anthropic_cache_control) is True


def test_downgrade_drops_tables_and_columns(scratch_db):
    """Downgrade drops tables and columns."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "006_add_memory_scope")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")

    with engine.connect() as conn:
        tables = {
            r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        tu_cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(token_usage)"))}

    assert "cache_configs" not in tables
    assert "response_cache_entries" not in tables
    assert "cache_status" not in tu_cols
    assert "tokens_saved" not in tu_cols
    # Original token_usage columns intact
    assert {"id", "virtual_key_id", "organization_id"} <= tu_cols


def test_alembic_heads_single(scratch_db):
    """Alembic heads single."""
    db_url, _engine = scratch_db
    cfg = _alembic_config(db_url)
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    assert heads[0] == "009a_response_cache"
