"""Migration 019 round-trip test: ``webhook_secret`` column on ``code_repos``.

Same technique as ``test_migration_017.py`` -- ``code_repos`` is an
Alembic-created table (migration 012), not a ``create_all()`` table, so the
scratch DB pre-creates the post-012 shape by hand and exercises
``upgrade()``/``downgrade()`` via a direct ``Operations`` context.
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
    "019_code_repos_webhook_secret.py",
)


def _load_migration_019():
    """Import ``019_code_repos_webhook_secret.py`` by path (filename isn't an identifier)."""
    spec = importlib.util.spec_from_file_location(
        "migration_019_code_repos_webhook_secret", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pre_019_shape(conn: sa.Connection) -> None:
    """Create the post-012 shape of ``code_repos`` (no ``webhook_secret`` yet)."""
    conn.execute(
        sa.text(
            "CREATE TABLE code_repos ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "org_id INTEGER NOT NULL, "
            "name VARCHAR(255) NOT NULL, "
            "source_url VARCHAR(1024) NOT NULL, "
            "credentials_ref VARCHAR(255), "
            "index_status VARCHAR(50) NOT NULL DEFAULT 'pending', "
            "last_commit VARCHAR(64))"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO code_repos (org_id, name, source_url) "
            "VALUES (7, 'waddleai', 'https://github.com/penguintechinc/waddleai.git')"
        )
    )


@pytest.fixture
def scratch_db(tmp_path):
    """A scratch SQLite DB pre-loaded with the pre-019 ``code_repos`` shape."""
    db_path = tmp_path / "migration019.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _pre_019_shape(conn)
    yield engine
    engine.dispose()


def test_revision_metadata_chains_off_018() -> None:
    """Revision id and down_revision match the current chain head."""
    module = _load_migration_019()
    assert module.revision == "019_code_repos_webhook_secret"
    assert module.down_revision == "018_model_access_policies"


def test_upgrade_adds_webhook_secret_column(scratch_db) -> None:
    """upgrade() adds ``webhook_secret``, nullable, existing rows default to NULL."""
    module = _load_migration_019()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()

        cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(code_repos)"))}
        assert "webhook_secret" in cols

        row = conn.execute(
            sa.text("SELECT name, webhook_secret FROM code_repos WHERE name = 'waddleai'")
        ).one()
        assert row.name == "waddleai"
        assert row.webhook_secret is None


def test_downgrade_drops_webhook_secret_column(scratch_db) -> None:
    """downgrade() restores the pre-019 shape, preserving other data."""
    module = _load_migration_019()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()
        with Operations.context(ctx):
            module.downgrade()
        conn.commit()

        cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(code_repos)"))}
        assert "webhook_secret" not in cols

        row = conn.execute(sa.text("SELECT name FROM code_repos WHERE name = 'waddleai'")).one()
        assert row.name == "waddleai"


def test_alembic_chain_still_single_head_after_019() -> None:
    """Adding 019 keeps a single resolvable head, no divergent branches.

    Does not pin the exact head revision id -- later migrations (e.g. 020)
    chain off 019 and become the new head; that specific-head assertion
    lives in the newest migration's own test file
    (``test_migration_020.py::test_alembic_chain_still_single_head_after_020``).
    This test only guards against 019 having introduced a branch.
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
