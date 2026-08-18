"""Migration 013 round-trip test: fleet_backends + deployment interface columns.

Migrations 007-012 (the intended chain ancestors — see the # TODO(rebase)
note in ``013_fleet.py``) are authored on sibling branches and do not exist
in this worktree, so alembic's revision-chain commands (``command.upgrade``,
``command.heads``) cannot resolve ``down_revision="012_knowledge"`` and
would fail with "Can't locate revision identified by '012_knowledge'" for
reasons that have nothing to do with migration 013 itself.

Instead of a chain-based round-trip, this binds ``upgrade()``/``downgrade()``
directly to a scratch SQLite connection via ``alembic.operations.Operations``
context — the same DDL the real migration would emit, without depending on
the missing chain. A chain-based test is left as a module-level skip so the
orchestrating agent has a marker to replace once 007-012 land.
"""

import importlib.util
import os

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "..",
    "..",
    "services",
    "management",
    "alembic",
    "versions",
    "013_fleet.py",
)


def _load_migration_013():
    """Import ``013_fleet.py`` by path (its filename isn't a valid identifier)."""
    spec = importlib.util.spec_from_file_location("migration_013_fleet", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pre_013_shape(conn: sa.Connection) -> None:
    """Create the 006-era shape of the two deployment tables + organizations."""
    conn.execute(
        sa.text(
            "CREATE TABLE organizations (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255))"
        )
    )
    conn.execute(sa.text("INSERT INTO organizations (id, name) VALUES (1, 'acme')"))
    conn.execute(
        sa.text(
            "CREATE TABLE ollama_deployments ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name VARCHAR(255) UNIQUE NOT NULL, "
            "endpoint_url VARCHAR(512) NOT NULL, "
            "deployment_type VARCHAR(50), "
            "status VARCHAR(50))"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO ollama_deployments (name, endpoint_url, deployment_type, status) "
            "VALUES ('pool-a', 'http://ollama-pool-a:11434', 'kubernetes-daemonset', 'running')"
        )
    )
    conn.execute(
        sa.text(
            "CREATE TABLE llamacpp_deployments ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name VARCHAR(255) UNIQUE NOT NULL, "
            "deployment_type VARCHAR(50) NOT NULL, "
            "status VARCHAR(50) NOT NULL, "
            "model_name VARCHAR(255) NOT NULL)"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO llamacpp_deployments (name, deployment_type, status, model_name) "
            "VALUES ('gguf-a', 'kubernetes', 'running', 'llama-3.2-3b')"
        )
    )


@pytest.fixture
def scratch_db(tmp_path):
    """A scratch SQLite DB pre-loaded with the 006-era deployment table shape."""
    db_path = tmp_path / "migration013.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _pre_013_shape(conn)
    yield engine
    engine.dispose()


def test_revision_metadata_points_at_intended_parent() -> None:
    """Revision id and the (provisional) down_revision match the plan's chain."""
    module = _load_migration_013()
    assert module.revision == "013_fleet"
    assert module.down_revision == "012_knowledge"


def test_upgrade_creates_fleet_backends_and_extends_deployments(scratch_db) -> None:
    """upgrade() creates fleet_backends and adds interface columns, preserving data."""
    module = _load_migration_013()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()

        fb_cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(fleet_backends)"))}
        assert fb_cols == {
            "id",
            "org_id",
            "name",
            "type",
            "mode",
            "management_scope",
            "config",
            "credentials_ref",
            "status",
            "created_at",
            "updated_at",
        }

        for table in ("ollama_deployments", "llamacpp_deployments"):
            cols = {row[1] for row in conn.execute(sa.text(f"PRAGMA table_info({table})"))}
            assert {"fleet_backend_id", "management_scope", "node_uid", "pool_mode"} <= cols

        # Existing rows preserved with the safe default management_scope.
        row = conn.execute(
            sa.text(
                "SELECT management_scope, fleet_backend_id, pool_mode "
                "FROM ollama_deployments WHERE name = 'pool-a'"
            )
        ).one()
        assert row.management_scope == "full_lifecycle"
        assert row.fleet_backend_id is None
        assert row.pool_mode in (0, False)

        row = conn.execute(
            sa.text("SELECT management_scope FROM llamacpp_deployments WHERE name = 'gguf-a'")
        ).one()
        assert row.management_scope == "full_lifecycle"


def test_fleet_backends_org_name_unique(scratch_db) -> None:
    """The (org_id, name) unique index rejects a duplicate registration."""
    module = _load_migration_013()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()

        conn.execute(
            sa.text(
                "INSERT INTO fleet_backends (org_id, name, type, management_scope, status) "
                "VALUES (1, 'prod-ollama', 'ollama', 'full_lifecycle', 'active')"
            )
        )
        conn.commit()

        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO fleet_backends (org_id, name, type, management_scope, status) "
                    "VALUES (1, 'prod-ollama', 'llamacpp', 'full_lifecycle', 'active')"
                )
            )
            conn.commit()


def test_downgrade_drops_fleet_backends_and_columns(scratch_db) -> None:
    """downgrade() restores the pre-013 shape exactly."""
    module = _load_migration_013()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
            conn.commit()
            module.downgrade()
        conn.commit()

        tables = {
            row[0]
            for row in conn.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        assert "fleet_backends" not in tables

        for table in ("ollama_deployments", "llamacpp_deployments"):
            cols = {row[1] for row in conn.execute(sa.text(f"PRAGMA table_info({table})"))}
            assert not ({"fleet_backend_id", "management_scope", "node_uid", "pool_mode"} & cols)

        # Original data intact.
        row = conn.execute(sa.text("SELECT name, status FROM ollama_deployments")).one()
        assert row.name == "pool-a"
        assert row.status == "running"


@pytest.mark.skip(
    reason=(
        "Chain-based round-trip (command.stamp('012_knowledge') -> "
        "command.upgrade(cfg, 'head')) requires migrations 007-012, which "
        "are being authored on sibling branches and do not exist on "
        "feature/inference-fleet. Re-enable once the chain is reconciled "
        "at merge time (see # TODO(rebase) in 013_fleet.py)."
    )
)
def test_upgrade_via_full_alembic_chain() -> None:
    """Placeholder for the real chain-based test once 007-012 land."""
    raise AssertionError("should never run — see skip reason")
