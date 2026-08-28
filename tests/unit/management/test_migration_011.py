"""Migration 011 round-trip test: security_policies, bypass grants, extended audit log.

Creates the pre-011 shape (content_filter_config with sample org rows +
content_filter_audit_log without the new columns) on a scratch sqlite DB
and runs migration 011's real `upgrade()`/`downgrade()` functions directly
via Alembic's `Operations` API.

Driven via `Operations.context()` rather than `alembic.command.upgrade()`/
`command.stamp()`: those go through `ScriptDirectory`, which eagerly
resolves the *entire* revision graph in `services/management/alembic/
versions/` on any call. Migration 011's own `down_revision =
"010_routing_engine"` is a placeholder (see the # TODO(rebase) note in that
file -- migrations 007-010 land on parallel branches and are reconciled at
merge), so `ScriptDirectory` cannot resolve it yet. Loading migration 011
by file path and running it through a standalone `Operations` context
sidesteps `ScriptDirectory` entirely, so this suite runs -- and actually
verifies the real upgrade()/downgrade() -- regardless of whether the parent
chain has landed.
"""

import importlib.util
import os

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

MIGRATION_011_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "..",
    "..",
    "services",
    "management",
    "alembic",
    "versions",
    "011_security_v2.py",
)


def _load_migration_011():
    """Load migration 011 by file path, bypassing package/ScriptDirectory resolution."""
    spec = importlib.util.spec_from_file_location("migration_011_security_v2", MIGRATION_011_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(engine: sa.Engine) -> None:
    """Run migration 011's upgrade() against a standalone Operations context."""
    migration = _load_migration_011()
    conn = engine.connect()
    ctx = MigrationContext.configure(conn, opts={"target_metadata": None})
    with Operations.context(ctx):
        migration.upgrade()
    conn.commit()
    conn.close()


def _run_downgrade(engine: sa.Engine) -> None:
    """Run migration 011's downgrade() against a standalone Operations context."""
    migration = _load_migration_011()
    conn = engine.connect()
    ctx = MigrationContext.configure(conn, opts={"target_metadata": None})
    with Operations.context(ctx):
        migration.downgrade()
    conn.commit()
    conn.close()


_LIST_TABLES_SQL = "SELECT name FROM sqlite_master WHERE type='table'"
_AUDIT_LOG_COLS_SQL = "PRAGMA table_info(content_filter_audit_log)"


def _table_names(conn) -> set:
    """All table names in a sqlite scratch DB."""
    return {r[0] for r in conn.execute(sa.text(_LIST_TABLES_SQL))}


def _audit_log_cols(conn) -> set:
    """Column names of content_filter_audit_log."""
    return {r[1] for r in conn.execute(sa.text(_AUDIT_LOG_COLS_SQL))}


@pytest.fixture
def scratch_engine():
    """Scratch in-memory sqlite engine carrying the pre-011 (006-era) schema shape."""
    engine = sa.create_engine("sqlite:///:memory:")
    orgs_sql = (
        "CREATE TABLE organizations (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255))"
    )
    users_sql = "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username VARCHAR(255))"
    with engine.begin() as conn:
        conn.execute(sa.text(orgs_sql))
        conn.execute(sa.text("INSERT INTO organizations (id, name) VALUES (7, 'acme')"))
        conn.execute(sa.text(users_sql))
        conn.execute(
            sa.text(
                "CREATE TABLE content_filter_config ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "key VARCHAR(100) NOT NULL, "
                "value TEXT, "
                "organization_id INTEGER, "
                "created_by INTEGER, "
                "updated_at DATETIME)"
            )
        )
        idx_sql = (
            "CREATE UNIQUE INDEX idx_cfc_key_org ON content_filter_config (key, organization_id)"
        )
        conn.execute(sa.text(idx_sql))
        conn.execute(
            sa.text(
                "INSERT INTO content_filter_config (key, value, organization_id) "
                "VALUES ('auditor_system_prompt', 'custom prompt', 7)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO content_filter_config (key, value, organization_id) "
                "VALUES ('disabled_builtins', '[\"ip_address_public\"]', NULL)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE content_filter_audit_log ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "timestamp DATETIME NOT NULL, "
                "phase VARCHAR(10) NOT NULL, "
                "action_taken VARCHAR(10) NOT NULL, "
                "auditor_used BOOLEAN NOT NULL DEFAULT 0)"
            )
        )
    yield engine
    engine.dispose()


def test_upgrade_creates_scoped_policies_and_seeds_global(scratch_engine):
    """Upgrade seeds a fully-populated global row + an inheriting org row."""
    _run_upgrade(scratch_engine)

    select_sql = (
        "SELECT scope_type, scope_ref, fail_mode, auditor_timeout_ms, tier1_enabled "
        "FROM security_policies"
    )
    with scratch_engine.connect() as conn:
        rows = conn.execute(sa.text(select_sql)).fetchall()

    by_scope = {(r.scope_type, r.scope_ref): r for r in rows}
    assert ("global", None) in by_scope
    assert ("org", "7") in by_scope
    # Global row is fully populated (the resolution floor).
    assert by_scope[("global", None)].fail_mode == "degrade"
    assert by_scope[("global", None)].auditor_timeout_ms == 5000
    # Migrated org row inherits everything (NULL) -- see migration docstring.
    assert by_scope[("org", "7")].fail_mode is None
    assert by_scope[("org", "7")].tier1_enabled is None


def test_upgrade_extends_audit_log_and_creates_bypass_grants(scratch_engine):
    """Upgrade adds the five audit-log columns and the bypass-grants table."""
    _run_upgrade(scratch_engine)

    expected_cols = {
        "policy_id",
        "intent_categories",
        "degraded",
        "bypass_grant_id",
        "redaction_counts",
    }
    with scratch_engine.connect() as conn:
        assert expected_cols <= _audit_log_cols(conn)

        tables = _table_names(conn)
        assert "security_bypass_grants" in tables
        assert "content_filter_config" not in tables


def test_downgrade_restores_content_filter_config_and_drops_new_tables(scratch_engine):
    """Downgrade restores content_filter_config and drops the v2 tables/columns."""
    _run_upgrade(scratch_engine)
    _run_downgrade(scratch_engine)

    with scratch_engine.connect() as conn:
        tables = _table_names(conn)
        assert "content_filter_config" in tables
        assert "security_policies" not in tables
        assert "security_bypass_grants" not in tables

        cols = _audit_log_cols(conn)
        assert "policy_id" not in cols
        assert "redaction_counts" not in cols
