"""Migration 006 round-trip test: memory scope columns + backfill.

Creates the pre-006 memory_embeddings shape on a scratch sqlite DB, stamps
alembic at 005, upgrades to head, and verifies the backfill
(scope_type='user', author_user_id=user_id). Then downgrades one revision
and verifies both columns are dropped.

This module upgrades to "head", which now resolves through
009b_proxy_memory's parent, 009a_response_cache (feature/response-cache,
not yet present in this worktree) -- the alembic revision graph can't be
built at all until that file lands, so these tests are collaterally
skipped alongside tests/unit/management/test_migration_009b.py rather than
reporting a false failure. See the coordination note in
services/management/alembic/versions/009b_proxy_memory.py.
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

_VERSIONS_DIR = os.path.join(ALEMBIC_DIR, "versions")
_HAS_009A_RESPONSE_CACHE = os.path.isdir(_VERSIONS_DIR) and any(
    fn.startswith("009a_response_cache") for fn in os.listdir(_VERSIONS_DIR)
)

pytestmark = pytest.mark.skipif(
    not _HAS_009A_RESPONSE_CACHE,
    reason=(
        "'head' now chains through 009a_response_cache (feature/response-cache), "
        "not yet present in this worktree -- the alembic revision graph can't "
        "resolve until it lands. Reconciled at merge."
    ),
)


def _alembic_config(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.abspath(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    db_path = tmp_path / "migration006.db"
    db_url = f"sqlite:///{db_path}"
    # env.py reads DATABASE_URL; point it at the scratch DB
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = sa.create_engine(db_url)
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
        # Minimal api_keys shape -- required by migration 009b (head, as of
        # this test running the full chain), which adds api_keys.proxy_memory.
        conn.execute(
            sa.text(
                "CREATE TABLE api_keys ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "key_id VARCHAR(255) NOT NULL, "
                "key_hash VARCHAR(255) NOT NULL, "
                "user_id INTEGER NOT NULL, "
                "organization_id INTEGER NOT NULL, "
                "name VARCHAR(255) NOT NULL)"
            )
        )
    yield db_url, engine
    engine.dispose()


def test_upgrade_backfills_scope_and_author(scratch_db):
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "005_add_content_filter_tables")
    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT scope_type, author_user_id, user_id FROM memory_embeddings")
        ).one()
    assert row.scope_type == "user"
    assert row.author_user_id == 42
    assert row.author_user_id == row.user_id


def test_downgrade_drops_columns(scratch_db):
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "005_add_content_filter_tables")
    command.upgrade(cfg, "head")
    # Explicit target rather than "-1": head has grown past 006 (009b and
    # later chain off it), so a relative downgrade no longer lands on 005.
    command.downgrade(cfg, "005_add_content_filter_tables")

    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(memory_embeddings)"))}
    assert "scope_type" not in cols
    assert "author_user_id" not in cols
    # Original columns intact
    assert {"id", "user_id", "organization_id", "content"} <= cols
