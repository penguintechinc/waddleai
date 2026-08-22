"""Migration 017 round-trip test: ``namespace`` column on ``ollama_deployments``.

``ollama_deployments`` is created by SQLAlchemy's ``Base.metadata.create_all()``
at app startup, not by any Alembic migration (see the absence of a
``create_table("ollama_deployments", ...)`` anywhere in ``alembic/versions/``),
so -- same as ``test_migration_013.py`` -- the scratch DB pre-creates the
013-era shape by hand before exercising ``upgrade()``/``downgrade()`` via a
direct ``Operations`` context bound to the connection.
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
    "017_ollama_deployment_namespace.py",
)


def _load_migration_017():
    """Import ``017_ollama_deployment_namespace.py`` by path (filename isn't an identifier)."""
    spec = importlib.util.spec_from_file_location("migration_017_ollama_namespace", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pre_017_shape(conn: sa.Connection) -> None:
    """Create the post-013 shape of ``ollama_deployments`` (no ``namespace`` yet)."""
    conn.execute(
        sa.text(
            "CREATE TABLE ollama_deployments ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name VARCHAR(255) UNIQUE NOT NULL, "
            "endpoint_url VARCHAR(512) NOT NULL, "
            "deployment_type VARCHAR(50), "
            "status VARCHAR(50), "
            "pool_mode BOOLEAN NOT NULL DEFAULT 0)"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO ollama_deployments (name, endpoint_url, deployment_type, status) "
            "VALUES ('pool-a', 'http://ollama-pool-a:11434', 'kubernetes-daemonset', 'running')"
        )
    )


@pytest.fixture
def scratch_db(tmp_path):
    """A scratch SQLite DB pre-loaded with the pre-017 ``ollama_deployments`` shape."""
    db_path = tmp_path / "migration017.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _pre_017_shape(conn)
    yield engine
    engine.dispose()


def test_revision_metadata_chains_off_016_hooks() -> None:
    """Revision id and down_revision match the current chain head."""
    module = _load_migration_017()
    assert module.revision == "017_ollama_deployment_namespace"
    assert module.down_revision == "016_hooks"


def test_upgrade_adds_namespace_column_with_default(scratch_db) -> None:
    """upgrade() adds `namespace`, defaulting existing rows to 'waddleai'."""
    module = _load_migration_017()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()

        cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(ollama_deployments)"))}
        assert "namespace" in cols

        row = conn.execute(
            sa.text("SELECT namespace, status FROM ollama_deployments WHERE name = 'pool-a'")
        ).one()
        assert row.namespace == "waddleai"
        assert row.status == "running"


def test_downgrade_drops_namespace_column(scratch_db) -> None:
    """downgrade() restores the pre-017 shape, preserving other data."""
    module = _load_migration_017()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
            conn.commit()
            module.downgrade()
        conn.commit()

        cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(ollama_deployments)"))}
        assert "namespace" not in cols

        row = conn.execute(sa.text("SELECT name, status FROM ollama_deployments")).one()
        assert row.name == "pool-a"
        assert row.status == "running"


def test_alembic_chain_still_single_head_after_017() -> None:
    """Adding 017 keeps a single resolvable head, no divergent branches."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    alembic_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "..",
        "..",
        "services",
        "management",
        "alembic",
    )
    cfg = Config()
    cfg.set_main_option("script_location", os.path.abspath(alembic_dir))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()

    assert len(heads) == 1
    assert heads[0] == "017_ollama_deployment_namespace"
