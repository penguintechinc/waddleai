"""Migration 006 round-trip test: memory scope columns + backfill.

Creates the pre-006 memory_embeddings shape on a scratch sqlite DB and
runs migration 006's real `upgrade()`/`downgrade()` functions directly via
Alembic's `Operations` API, verifying the backfill (scope_type='user',
author_user_id=user_id) and the column drop on downgrade.

Driven via `Operations.context()` rather than `alembic.command.upgrade()`/
`command.stamp()`: those go through `ScriptDirectory`, which eagerly
resolves the *entire* revision graph in `services/management/alembic/
versions/` on any call, not just the requested target -- and migration
014 (`014_integrations.py`, this worktree's in-flight work) currently
carries a placeholder `down_revision = "013_fleet"` that does not exist
yet (migrations 007-013 land on parallel branches; see the TODO(rebase)
note in that file and `tests/unit/management/test_migration_014.py`).
That makes `ScriptDirectory` raise `KeyError: '013_fleet'` for *any*
command-based Alembic call anywhere in this suite, not just calls that
touch 014 -- including this test, previously. Loading migration 006 by
file path and running it through a standalone `Operations` context
sidesteps `ScriptDirectory` entirely, so this test is unaffected by the
in-progress, not-yet-reconciled chain.
"""

import importlib.util
import os

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

MIGRATION_006_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "..",
    "..",
    "services",
    "management",
    "alembic",
    "versions",
    "006_add_memory_scope.py",
)


def _load_migration_006():
    """Load migration 006."""
    spec = importlib.util.spec_from_file_location(
        "migration_006_add_memory_scope", MIGRATION_006_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(engine: sa.Engine) -> None:
    """Run upgrade."""
    migration = _load_migration_006()
    conn = engine.connect()
    ctx = MigrationContext.configure(conn, opts={"target_metadata": None})
    with Operations.context(ctx):
        migration.upgrade()
    conn.commit()
    conn.close()


def _run_downgrade(engine: sa.Engine) -> None:
    """Run downgrade."""
    migration = _load_migration_006()
    conn = engine.connect()
    ctx = MigrationContext.configure(conn, opts={"target_metadata": None})
    with Operations.context(ctx):
        migration.downgrade()
    conn.commit()
    conn.close()


@pytest.fixture
def scratch_engine():
    """Scratch engine."""
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        # Pre-006 shape of memory_embeddings (005-era)
        conn.execute(
            sa.text(
                "CREATE TABLE memory_embeddings ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id INTEGER NOT NULL, "
                "organization_id INTEGER NOT NULL, "
                "session_id VARCHAR(255) NOT NULL, "
                "content TEXT NOT NULL, "
                "embedding_json TEXT, "
                "role VARCHAR(50) NOT NULL, "
                "created_at DATETIME, "
                "metadata JSON)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO memory_embeddings "
                "(user_id, organization_id, session_id, content, role) "
                "VALUES (42, 7, '', 'legacy personal memory', 'user')"
            )
        )
    yield engine
    engine.dispose()


def test_upgrade_backfills_scope_and_author(scratch_engine):
    """Upgrade backfills scope and author."""
    _run_upgrade(scratch_engine)

    with scratch_engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT scope_type, author_user_id, user_id FROM memory_embeddings")
        ).one()
    assert row.scope_type == "user"
    assert row.author_user_id == 42
    assert row.author_user_id == row.user_id


def test_downgrade_drops_columns(scratch_engine):
    """Downgrade drops columns."""
    _run_upgrade(scratch_engine)
    _run_downgrade(scratch_engine)

    with scratch_engine.connect() as conn:
        cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(memory_embeddings)"))}
    assert "scope_type" not in cols
    assert "author_user_id" not in cols
    # Original columns intact
    assert {"id", "user_id", "organization_id", "content"} <= cols
