"""Migration 009b round-trip test: proxy-memory tables + §9.7 scope/trust columns.

Creates the pre-009b (006-era) memory_embeddings/api_keys shape on a scratch
sqlite DB, stamps alembic at 006, upgrades to head, and verifies the three
new tables plus the §9.7 retrofit columns and their defaults. Then downgrades
one revision and verifies the exact 006 shape is restored.

009b_proxy_memory chains off 009a_response_cache (feature/response-cache,
merging into release/v0.2.X first per the migration-009 coordination note in
services/management/alembic/versions/009b_proxy_memory.py). That revision
does not exist in this worktree -- the whole module is skipped until it's
present (post-merge, or in CI on the merged tree) rather than reporting a
false failure against a revision graph that can't resolve.
"""

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

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
        "009b_proxy_memory's down_revision is 009a_response_cache "
        "(feature/response-cache), not yet present in this worktree -- "
        "the alembic revision graph can't resolve until it lands. "
        "Reconciled at merge; see the coordination note in "
        "services/management/alembic/versions/009b_proxy_memory.py."
    ),
)


def _alembic_config(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.abspath(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """Scratch sqlite DB seeded with the post-006 memory_embeddings/api_keys shape."""
    db_path = tmp_path / "migration009b.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = sa.create_engine(db_url)
    with engine.begin() as conn:
        # Post-006 shape of memory_embeddings.
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
                "INSERT INTO memory_embeddings "
                "(user_id, organization_id, session_id, content, role, scope_type, author_user_id) "
                "VALUES (42, 7, '', 'legacy personal memory', 'user', 'user', 42)"
            )
        )
        # Minimal pre-009b api_keys shape (only the columns this test touches).
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


def _cfg_for(db_url: str) -> Config:
    return _alembic_config(db_url)


def test_upgrade_creates_new_tables(scratch_db):
    """Upgrade creates session_scratchpad, conversation_summaries, and embedding_cache."""
    db_url, engine = scratch_db
    cfg = _cfg_for(db_url)
    command.stamp(cfg, "009a_response_cache")
    command.upgrade(cfg, "009b_proxy_memory")

    table_names = set(sa.inspect(engine).get_table_names())
    assert {"session_scratchpad", "conversation_summaries", "embedding_cache"} <= table_names


def test_upgrade_session_scratchpad_shape(scratch_db):
    """session_scratchpad has the expected columns, unique index, and §9.7 defaults."""
    db_url, engine = scratch_db
    cfg = _cfg_for(db_url)
    command.stamp(cfg, "009a_response_cache")
    command.upgrade(cfg, "009b_proxy_memory")

    cols = {c["name"] for c in sa.inspect(engine).get_columns("session_scratchpad")}
    expected = {
        "id",
        "org_id",
        "user_id",
        "session_id",
        "key",
        "value",
        "scope_type",
        "scope_ref",
        "author_user_id",
        "trust_tier",
        "version",
        "superseded_by",
        "status",
        "created_at",
        "updated_at",
        "expires_at",
    }
    assert expected <= cols

    indexes = sa.inspect(engine).get_indexes("session_scratchpad")
    uq = next(i for i in indexes if i["name"] == "uq_scratchpad_key")
    assert bool(uq["unique"]) is True
    assert uq["column_names"] == ["org_id", "session_id", "user_id", "key"]

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO session_scratchpad (org_id, user_id, session_id, key, value) "
                "VALUES (1, 1, 's1', 'k1', 'v1')"
            )
        )
        row = conn.execute(
            sa.text(
                "SELECT scope_type, trust_tier, version, status "
                "FROM session_scratchpad WHERE key='k1'"
            )
        ).one()
    assert row.scope_type == "session"
    assert row.trust_tier == "unverified"
    assert row.version == 1
    assert row.status == "active"


def test_upgrade_conversation_summaries_shape(scratch_db):
    """conversation_summaries has the expected columns and unique index."""
    db_url, engine = scratch_db
    cfg = _cfg_for(db_url)
    command.stamp(cfg, "009a_response_cache")
    command.upgrade(cfg, "009b_proxy_memory")

    cols = {c["name"] for c in sa.inspect(engine).get_columns("conversation_summaries")}
    expected = {
        "id",
        "conversation_id",
        "org_id",
        "summary",
        "covers_through_turn",
        "tokens_summarized",
        "model_used",
        "scope_type",
        "scope_ref",
        "author_user_id",
        "trust_tier",
        "version",
        "superseded_by",
        "status",
        "updated_at",
        "expires_at",
    }
    assert expected <= cols

    indexes = sa.inspect(engine).get_indexes("conversation_summaries")
    uq = next(i for i in indexes if i["name"] == "uq_convsum_version")
    assert bool(uq["unique"]) is True
    assert uq["column_names"] == ["org_id", "conversation_id", "version"]


def test_upgrade_embedding_cache_shape_sqlite_text_fallback(scratch_db):
    """embedding_cache falls back to TEXT embedding_json on SQLite and enforces uniqueness."""
    db_url, engine = scratch_db
    cfg = _cfg_for(db_url)
    command.stamp(cfg, "009a_response_cache")
    command.upgrade(cfg, "009b_proxy_memory")

    cols = {c["name"]: c for c in sa.inspect(engine).get_columns("embedding_cache")}
    assert {"id", "model", "content_hash", "embedding_json", "created_at"} <= set(cols)
    # SQLite fallback: no native vector column, TEXT-serialized embedding_json instead.
    assert "vector" not in str(cols["embedding_json"]["type"]).lower()

    indexes = sa.inspect(engine).get_indexes("embedding_cache")
    uq = next(i for i in indexes if i["name"] == "uq_embcache_model_hash")
    assert bool(uq["unique"]) is True
    assert uq["column_names"] == ["model", "content_hash"]

    # Sequential transactions, deliberately not nested: SQLite takes a write
    # lock for the duration of the first INSERT's transaction, so opening a
    # second engine.begin() inside it fails with "database is locked" before
    # the unique constraint is ever evaluated -- masking what this test exists
    # to prove.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO embedding_cache (model, content_hash, embedding_json) "
                "VALUES ('nomic-embed-text', 'abc123', '[0.1, 0.2]')"
            )
        )

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO embedding_cache (model, content_hash, embedding_json) "
                    "VALUES ('nomic-embed-text', 'abc123', '[0.9]')"
                )
            )


def test_upgrade_memory_embeddings_retrofit_and_backfill(scratch_db):
    """memory_embeddings gains the §9.7 retrofit columns without touching 006-era columns."""
    db_url, engine = scratch_db
    cfg = _cfg_for(db_url)
    command.stamp(cfg, "009a_response_cache")
    command.upgrade(cfg, "009b_proxy_memory")

    cols = {c["name"] for c in sa.inspect(engine).get_columns("memory_embeddings")}
    assert {"scope_ref", "trust_tier", "version", "superseded_by", "status", "expires_at"} <= cols

    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT scope_type, author_user_id, trust_tier, version, status "
                "FROM memory_embeddings"
            )
        ).one()
    # Pre-existing 006 columns are untouched by this migration.
    assert row.scope_type == "user"
    assert row.author_user_id == 42
    # New §9.7 columns backfill to their documented defaults.
    assert row.trust_tier == "unverified"
    assert row.version == 1
    assert row.status == "active"


def test_upgrade_api_keys_proxy_memory_column(scratch_db):
    """api_keys.proxy_memory exists and defaults to NULL."""
    db_url, engine = scratch_db
    cfg = _cfg_for(db_url)
    command.stamp(cfg, "009a_response_cache")
    command.upgrade(cfg, "009b_proxy_memory")

    cols = {c["name"] for c in sa.inspect(engine).get_columns("api_keys")}
    assert "proxy_memory" in cols

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO api_keys (key_id, key_hash, user_id, organization_id, name) "
                "VALUES ('k1', 'h1', 1, 1, 'test key')"
            )
        )
        row = conn.execute(sa.text("SELECT proxy_memory FROM api_keys WHERE key_id='k1'")).one()
    assert row.proxy_memory is None


def test_downgrade_restores_006_shape(scratch_db):
    """Downgrade drops the new tables/columns and restores the exact 006 shape."""
    db_url, engine = scratch_db
    cfg = _cfg_for(db_url)
    command.stamp(cfg, "009a_response_cache")
    command.upgrade(cfg, "009b_proxy_memory")
    command.downgrade(cfg, "-1")

    table_names = set(sa.inspect(engine).get_table_names())
    assert "session_scratchpad" not in table_names
    assert "conversation_summaries" not in table_names
    assert "embedding_cache" not in table_names

    mem_cols = {c["name"] for c in sa.inspect(engine).get_columns("memory_embeddings")}
    assert not (
        {"scope_ref", "trust_tier", "version", "superseded_by", "status", "expires_at"} & mem_cols
    )
    # 006-era columns remain intact.
    assert {"scope_type", "author_user_id"} <= mem_cols

    api_key_cols = {c["name"] for c in sa.inspect(engine).get_columns("api_keys")}
    assert "proxy_memory" not in api_key_cols


def test_alembic_heads_single_headed():
    """The alembic revision graph has exactly one head."""
    cfg = _alembic_config("sqlite:///:memory:")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"Expected a single alembic head, got {heads}"
