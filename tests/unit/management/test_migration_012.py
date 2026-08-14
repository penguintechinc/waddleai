"""Migration 012 round-trip test: knowledge tables + §9.7 scope/trust columns.

012's ``down_revision`` currently points at "006_add_memory_scope" -- a
placeholder for the real intended parent "011_security_v2", which is being
written on another branch and does not exist in this worktree yet (see the
migration's module docstring for why a dangling literal reference was
rejected: it breaks Alembic's revision-graph resolution for every migration
in the directory, not just this one).

Two verification strategies are used:

1. ``test_upgrade_*`` / ``test_downgrade_*`` bind the migration's ``upgrade()``/
   ``downgrade()`` functions directly to an Alembic ``Operations`` object on a
   scratch SQLite DB (bypassing the revision-chain walk entirely). This
   verifies the actual SQL the migration issues is correct regardless of
   where down_revision ends up pointing at merge time.
2. ``test_alembic_chain_resolves_single_head`` exercises the normal
   ``alembic upgrade head`` command path used by every other migration test
   in this repo (see ``test_migration_006_memory_scope.py``) against the
   current (006-rooted) chain. It will need re-pointing, not skipping, once
   down_revision is reconciled to 011_security_v2 at merge.
"""

import importlib.util
import os

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "..",
    "..",
    "services",
    "management",
    "alembic",
    "versions",
    "012_knowledge.py",
)
VERSIONS_DIR = os.path.dirname(MIGRATION_PATH)
ALEMBIC_DIR = os.path.dirname(VERSIONS_DIR)


def _load_migration_012():
    """Import 012_knowledge.py as a standalone module (no package needed)."""
    spec = importlib.util.spec_from_file_location("migration_012_knowledge", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _create_pre_012_schema(conn: sa.Connection) -> None:
    """Create the pre-012 shape of rag_documents/memory_embeddings (post-006)."""
    conn.execute(
        sa.text(
            "CREATE TABLE rag_documents ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "organization_id INTEGER NOT NULL, "
            "collection VARCHAR(255) NOT NULL, "
            "content TEXT NOT NULL, "
            "embedding_json TEXT, "
            "source VARCHAR(500), "
            "created_at DATETIME, "
            "metadata JSON)"
        )
    )
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
            "metadata JSON, "
            "scope_type VARCHAR(20) NOT NULL DEFAULT 'user', "
            "author_user_id INTEGER NOT NULL)"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO rag_documents (organization_id, collection, content) "
            "VALUES (7, 'default', 'doc content')"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO memory_embeddings "
            "(user_id, organization_id, session_id, content, role, scope_type, author_user_id) "
            "VALUES (1, 7, 's1', 'hello', 'user', 'user', 1)"
        )
    )


@pytest.fixture
def scratch_engine(tmp_path):
    """A scratch SQLite engine pre-seeded with the pre-012 (post-006) schema shape."""
    db_path = tmp_path / "migration012.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _create_pre_012_schema(conn)
    yield engine
    engine.dispose()


def _bind_migration_ops(migration, conn: sa.Connection):
    """Bind the migration module's ``op`` global to a real Operations object.

    This is the direct-Operations technique described in the module
    docstring: it lets ``migration.upgrade()``/``migration.downgrade()`` run
    for real against ``conn`` without Alembic's revision-chain resolution.
    """
    ctx = MigrationContext.configure(conn)
    migration.op = Operations(ctx)
    return migration.op


def test_upgrade_creates_knowledge_tables(scratch_engine):
    """upgrade() creates all four new knowledge tables."""
    migration = _load_migration_012()
    with scratch_engine.connect() as conn:
        _bind_migration_ops(migration, conn)
        migration.upgrade()
        conn.commit()

    inspector = sa.inspect(scratch_engine)
    tables = set(inspector.get_table_names())
    assert {"code_repos", "code_chunks", "docs_sources", "docs_cache_pages"} <= tables


def test_upgrade_seeds_docs_sources_with_attribution_flags(scratch_engine):
    """upgrade() seeds docs_sources with the §9.2 rows, MDN/cpp flagged attribution_required."""
    migration = _load_migration_012()
    with scratch_engine.connect() as conn:
        _bind_migration_ops(migration, conn)
        migration.upgrade()
        conn.commit()

    with scratch_engine.connect() as conn:
        query = sa.text("SELECT ecosystem, attribution_required FROM docs_sources")
        rows = conn.execute(query).fetchall()
    seeded = {r[0]: bool(r[1]) for r in rows}

    assert seeded["mdn"] is True
    assert seeded["cpp"] is True
    assert seeded["python"] is False
    assert seeded["go"] is False
    assert len(seeded) == 8


def test_upgrade_extends_rag_documents_and_memory_embeddings(scratch_engine):
    """upgrade() adds the remaining §9.7 columns without disturbing existing rows/columns."""
    migration = _load_migration_012()
    with scratch_engine.connect() as conn:
        _bind_migration_ops(migration, conn)
        migration.upgrade()
        conn.commit()

    inspector = sa.inspect(scratch_engine)
    rag_cols = {c["name"] for c in inspector.get_columns("rag_documents")}
    expected_rag_cols = {
        "scope_type",
        "scope_ref",
        "trust_tier",
        "version",
        "superseded_by",
        "status",
        "expires_at",
        "provenance",
    }
    assert expected_rag_cols <= rag_cols

    mem_cols = {c["name"] for c in inspector.get_columns("memory_embeddings")}
    # New in 012.
    expected_mem_cols = {
        "scope_ref",
        "trust_tier",
        "version",
        "superseded_by",
        "status",
        "expires_at",
        "provenance",
    }
    assert expected_mem_cols <= mem_cols
    # Shipped in 006 -- must survive untouched, not be redefined.
    assert {"scope_type", "author_user_id"} <= mem_cols

    with scratch_engine.connect() as conn:
        rag_query = sa.text("SELECT content, trust_tier, status FROM rag_documents WHERE id = 1")
        rag_row = conn.execute(rag_query).one()
        mem_query = sa.text(
            "SELECT content, scope_type, author_user_id, trust_tier "
            "FROM memory_embeddings WHERE id = 1"
        )
        mem_row = conn.execute(mem_query).one()

    # Pre-existing rows preserved, not clobbered by the ALTER TABLEs.
    assert rag_row.content == "doc content"
    assert rag_row.trust_tier == "verified"
    assert rag_row.status == "active"
    assert mem_row.content == "hello"
    assert mem_row.scope_type == "user"  # 006 backfill untouched
    assert mem_row.author_user_id == 1
    assert mem_row.trust_tier == "unverified"


def test_downgrade_drops_knowledge_tables_and_columns(scratch_engine):
    """downgrade() drops the new tables/columns and preserves 006-era rows."""
    migration = _load_migration_012()
    with scratch_engine.connect() as conn:
        _bind_migration_ops(migration, conn)
        migration.upgrade()
        conn.commit()
        migration.downgrade()
        conn.commit()

    inspector = sa.inspect(scratch_engine)
    tables = set(inspector.get_table_names())
    assert not ({"code_repos", "code_chunks", "docs_sources", "docs_cache_pages"} & tables)

    rag_cols = {c["name"] for c in inspector.get_columns("rag_documents")}
    assert "scope_type" not in rag_cols
    assert "trust_tier" not in rag_cols

    mem_cols = {c["name"] for c in inspector.get_columns("memory_embeddings")}
    assert "trust_tier" not in mem_cols
    # 006 columns must survive the 012 downgrade -- 012 must never drop them.
    assert {"scope_type", "author_user_id"} <= mem_cols

    with scratch_engine.connect() as conn:
        rag_row = conn.execute(sa.text("SELECT content FROM rag_documents WHERE id = 1")).one()
        mem_row = conn.execute(sa.text("SELECT content FROM memory_embeddings WHERE id = 1")).one()
    assert rag_row.content == "doc content"
    assert mem_row.content == "hello"


def test_alembic_chain_resolves_single_head():
    """The normal ``alembic upgrade head`` path against the current chain.

    down_revision is currently the placeholder "006_add_memory_scope", so
    this resolves for real today. Once merge-time reconciliation repoints it
    at "011_security_v2", this assertion still holds (still a single head
    named "012_knowledge") -- only the chain underneath it grows longer.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", os.path.abspath(ALEMBIC_DIR))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert heads == ["012_knowledge"]
