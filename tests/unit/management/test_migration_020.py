"""Migration 020 round-trip test: create the ``graph_instances`` table.

Same technique as ``test_migration_019.py`` -- ``graph_instances`` is a new
Alembic-created table (no ``create_all()`` fallback, no prior revision
creates it), so the scratch DB starts empty and exercises
``upgrade()``/``downgrade()`` directly via an ``Operations`` context bound
to the connection, rather than running the full migration chain.
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
    "020_graph_instances.py",
)


def _load_migration_020():
    """Import ``020_graph_instances.py`` by path (filename isn't an identifier)."""
    spec = importlib.util.spec_from_file_location("migration_020_graph_instances", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scratch_db(tmp_path):
    """A scratch SQLite DB with no ``graph_instances`` table yet (pre-020 shape)."""
    db_path = tmp_path / "migration020.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    yield engine
    engine.dispose()


def test_revision_metadata_chains_off_019() -> None:
    """Revision id and down_revision match the current chain head."""
    module = _load_migration_020()
    assert module.revision == "020_graph_instances"
    assert module.down_revision == "019_code_repos_webhook_secret"


def test_upgrade_creates_graph_instances_table(scratch_db) -> None:
    """upgrade() creates ``graph_instances`` with the expected columns and status default."""
    module = _load_migration_020()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()

        cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(graph_instances)"))}
        assert cols == {"id", "org_id", "status", "bolt_url", "created_at", "updated_at"}

        conn.execute(
            sa.text(
                "INSERT INTO graph_instances (org_id, bolt_url) VALUES (7, 'bolt://neo4j:7687')"
            )
        )
        conn.commit()
        row = conn.execute(
            sa.text("SELECT org_id, status, bolt_url FROM graph_instances WHERE org_id = 7")
        ).one()
        assert row.org_id == 7
        assert row.status == "pending"
        assert row.bolt_url == "bolt://neo4j:7687"


def test_upgrade_enforces_unique_org_id(scratch_db) -> None:
    """``org_id`` is unique -- at most one graph instance row per org."""
    module = _load_migration_020()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()

        conn.execute(sa.text("INSERT INTO graph_instances (org_id) VALUES (7)"))
        conn.commit()
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text("INSERT INTO graph_instances (org_id) VALUES (7)"))
            conn.commit()


def test_downgrade_drops_graph_instances_table(scratch_db) -> None:
    """downgrade() removes ``graph_instances`` entirely."""
    module = _load_migration_020()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()
        with Operations.context(ctx):
            module.downgrade()
        conn.commit()

        tables = {
            row[0]
            for row in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        assert "graph_instances" not in tables


def test_alembic_chain_still_single_head_after_020() -> None:
    """Adding 020 keeps a single resolvable head, no divergent branches.

    Does not pin the exact head revision id -- later migrations (e.g. 021)
    chain off 020 and become the new head; that specific-head assertion
    lives in the newest migration's own test file
    (``test_migration_021.py::test_alembic_chain_still_single_head_after_021``).
    This test only guards against 020 having introduced a branch, while
    still confirming 020 itself remains part of the one resolvable chain.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option(
        "script_location",
        os.path.abspath(os.path.join(os.path.dirname(MIGRATION_PATH), "..")),
    )
    cfg.set_main_option("sqlalchemy.url", "sqlite://")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()

    assert len(heads) == 1
    chain = {rev.revision for rev in script.walk_revisions(base="base", head=heads[0])}
    assert "020_graph_instances" in chain


def test_model_columns() -> None:
    """``GraphInstance`` SQLAlchemy model exposes the columns the migration creates."""
    from app.models_sqlalchemy import GraphInstance

    cols = {c.name for c in GraphInstance.__table__.columns}
    assert {"id", "org_id", "status", "bolt_url", "created_at", "updated_at"} <= cols
    assert GraphInstance.__tablename__ == "graph_instances"


def test_status_values_documented() -> None:
    """``GRAPH_INSTANCE_STATUSES`` enumerates the full status lifecycle, in order."""
    from app.models_sqlalchemy import GRAPH_INSTANCE_STATUSES

    assert GRAPH_INSTANCE_STATUSES == (
        "pending",
        "provisioning",
        "ready",
        "failed",
        "deprovisioning",
        "deprovisioned",
    )
