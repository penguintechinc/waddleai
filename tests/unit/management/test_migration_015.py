"""Migration 015 round-trip test: local_vector_collections + local_vector_points.

Stamps the scratch SQLite DB at 014_integrations (015's down_revision) and
upgrades from there, rather than replaying the full chain from 001 -- some
early migrations (e.g. 002) run data-backfill queries against tables that
require a fully-seeded 001 baseline the scratch fixture doesn't set up,
same reasoning as test_migration_009a_response_cache.py's ``command.stamp``
usage.
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
    """A scratch SQLite DB, migrated from scratch through the full chain."""
    db_path = tmp_path / "migration015.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = sa.create_engine(db_url)
    yield db_url, engine
    engine.dispose()


def test_upgrade_creates_local_vector_tables(scratch_db):
    """Upgrading to head creates both tables with the exact expected column set."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "014_integrations")
    command.upgrade(cfg, "015_local_vector_store")

    with engine.connect() as conn:
        coll_cols = {
            r[1] for r in conn.execute(sa.text("PRAGMA table_info(local_vector_collections)"))
        }
        point_cols = {
            r[1] for r in conn.execute(sa.text("PRAGMA table_info(local_vector_points)"))
        }

    assert coll_cols == {"id", "name", "dimensions", "embedder_id", "distance", "created_at"}
    assert point_cols == {
        "id",
        "collection_id",
        "external_id",
        "vector_json",
        "payload_json",
        "created_at",
    }


def test_unique_constraints_enforced(scratch_db):
    """(name) on collections and (collection_id, external_id) on points are unique."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "014_integrations")
    command.upgrade(cfg, "015_local_vector_store")

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO local_vector_collections (name, dimensions, embedder_id, distance) "
                "VALUES ('memory', 768, 'ollama:nomic-embed-text', 'cosine')"
            )
        )

    with engine.connect() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO local_vector_collections "
                    "(name, dimensions, embedder_id, distance) "
                    "VALUES ('memory', 384, 'sentence-transformers:all-MiniLM-L6-v2', 'cosine')"
                )
            )
            conn.commit()

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO local_vector_points "
                "(collection_id, external_id, vector_json, payload_json) "
                "VALUES (1, 'doc-1', '[0.1, 0.2]', '{}')"
            )
        )

    with engine.connect() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO local_vector_points "
                    "(collection_id, external_id, vector_json, payload_json) "
                    "VALUES (1, 'doc-1', '[0.9, 0.9]', '{}')"
                )
            )
            conn.commit()


def test_downgrade_drops_tables(scratch_db):
    """Downgrade drops both tables cleanly."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "014_integrations")
    command.upgrade(cfg, "015_local_vector_store")
    command.downgrade(cfg, "-1")

    with engine.connect() as conn:
        tables = {
            r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert "local_vector_points" not in tables
    assert "local_vector_collections" not in tables


def test_alembic_heads_single(scratch_db):
    """The revision graph stays single-headed after adding 015."""
    db_url, _engine = scratch_db
    cfg = _alembic_config(db_url)
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    # Assert the invariant that matters -- exactly one head, i.e. no divergent
    # branches -- not that 015 IS the head. The latter is false by construction
    # the moment any later migration lands (016_hooks already has).
    assert len(heads) == 1
    assert "015_local_vector_store" in {sc.revision for sc in script.walk_revisions()}
