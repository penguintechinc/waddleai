"""Migration 010 round-trip test: routing engine tables + routing_matrix evolution.

Creates the pre-010 (post-006) shape of routing_matrix on a scratch sqlite DB
with a sample row, stamps alembic at 006, upgrades to head, and verifies:
model_assignments carries the migrated row plus new columns; the internal
function assignment rows are seeded; model_configs is seeded from the
formerly-hardcoded dict; model_aliases/routing_rules_v2/routing_policies/
routing_decision_traces all exist. Then downgrades one revision and verifies
the schema folds back to routing_matrix.

Down_revision is currently pinned to 006_add_memory_scope (the actual head in
this worktree) rather than 009b_proxy_memory -- see the TODO(rebase) comment
in the migration file for why. This test therefore exercises the migration
for real rather than skipping.
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
    """Build an Alembic Config pointed at the real script directory + a scratch DB URL."""
    cfg = Config()
    cfg.set_main_option("script_location", os.path.abspath(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _q(conn, sql: str):
    """Execute raw SQL against the scratch connection (shorthand for readability)."""
    return conn.execute(sa.text(sql))


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """A scratch sqlite DB seeded with the post-006 organizations + routing_matrix shape."""
    db_path = tmp_path / "migration010.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = sa.create_engine(db_url)
    with engine.begin() as conn:
        # Post-006 shape of organizations + routing_matrix (003-era + FK target).
        _q(
            conn,
            "CREATE TABLE organizations ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name VARCHAR(255) UNIQUE NOT NULL)",
        )
        _q(conn, "INSERT INTO organizations (id, name) VALUES (1, 'acme')")
        _q(
            conn,
            "CREATE TABLE routing_matrix ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "tool_type VARCHAR(50) NOT NULL, "
            "complexity VARCHAR(10) NOT NULL, "
            "region VARCHAR(5) NOT NULL, "
            "model_name VARCHAR(255) NOT NULL, "
            "model_params VARCHAR(50), "
            "vram_gb INTEGER, "
            "capability_score FLOAT, "
            "enabled BOOLEAN, "
            "created_at DATETIME, "
            "credential_label VARCHAR(255), "
            "CONSTRAINT uq_routing_matrix_lookup UNIQUE (tool_type, complexity, region))",
        )
        _q(
            conn,
            "INSERT INTO routing_matrix "
            "(tool_type, complexity, region, model_name, enabled) "
            "VALUES ('chat', 'medium', 'us', 'gpt-4o', 1)",
        )
    yield db_url, engine
    engine.dispose()


def test_upgrade_evolves_routing_matrix_and_seeds_tables(scratch_db):
    """Upgrade renames routing_matrix, adds routing-engine tables, and seeds them."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "006_add_memory_scope")
    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        # routing_matrix renamed to model_assignments; migrated row intact.
        tables = {r[0] for r in _q(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "model_assignments" in tables
        assert "routing_matrix" not in tables

        row = _q(
            conn,
            "SELECT tool_type, model_name, scope, scope_ref, escalation_model "
            "FROM model_assignments WHERE tool_type = 'chat'",
        ).one()
        assert row.model_name == "gpt-4o"
        assert row.scope == "global"
        assert row.scope_ref is None
        assert row.escalation_model is None

        # Internal-function rows seeded.
        internal_types = {
            r[0]
            for r in _q(
                conn,
                "SELECT tool_type FROM model_assignments WHERE tool_type IN "
                "('security-audit', 'routing-classifier', 'embeddings', 'docs-fetch', 'summarize')",
            )
        }
        assert internal_types == {
            "security-audit",
            "routing-classifier",
            "embeddings",
            "docs-fetch",
            "summarize",
        }
        classifier_row = _q(
            conn,
            "SELECT model_name FROM model_assignments WHERE tool_type = 'routing-classifier'",
        ).one()
        assert classifier_row.model_name == "gemma4:e2b"

        # model_configs seeded from the formerly-hardcoded dict.
        config_names = {r[0] for r in _q(conn, "SELECT model_name FROM model_configs")}
        expected = {"gpt-4", "claude-3-opus-20240229", "claude-3-sonnet-20240229", "llama3"}
        assert expected <= config_names

        # New tables exist (schema-only check).
        new_tables = (
            "model_aliases",
            "routing_rules_v2",
            "routing_policies",
            "routing_decision_traces",
        )
        for table_name in new_tables:
            assert table_name in tables


def test_downgrade_folds_back_to_routing_matrix(scratch_db):
    """Downgrade drops the routing-engine tables and renames model_assignments back."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "006_add_memory_scope")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")

    with engine.connect() as conn:
        tables = {r[0] for r in _q(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "routing_matrix" in tables
        assert "model_assignments" not in tables
        dropped_tables = (
            "model_configs",
            "model_aliases",
            "routing_rules_v2",
            "routing_policies",
            "routing_decision_traces",
        )
        for table_name in dropped_tables:
            assert table_name not in tables

        cols = {r[1] for r in _q(conn, "PRAGMA table_info(routing_matrix)")}
        assert "escalation_model" not in cols
        assert "scope" not in cols
        assert "scope_ref" not in cols
        assert "fallback_models" not in cols
        assert {"id", "tool_type", "complexity", "region", "model_name"} <= cols

        # Original migrated row survived the round trip.
        row = _q(conn, "SELECT model_name FROM routing_matrix WHERE tool_type = 'chat'").one()
        assert row.model_name == "gpt-4o"
