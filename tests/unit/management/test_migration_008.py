"""Migration 008 round-trip test: model_registry dual-default seed + plan_budget.

Creates the pre-008 (post-007) shape of provider_credentials on a scratch
sqlite DB, stamps alembic at 007, upgrades to head, and verifies the
model_registry table is created and seeded with the §2.3 dual-default set
(no PRC-origin entries) and that provider_credentials.plan_budget exists.
Then downgrades one revision and verifies both are removed. Also asserts
the full migration script directory resolves to a single head.
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

# Same-organization deny-list as spec §2.2 -- keep in sync manually since
# this asserts against a fixed migration seed, not the live spec text.
_PRC_ORIGIN_MARKERS = (
    "qwen",
    "deepseek",
    "glm",
    "chatglm",
    "zhipu",
    "yi",
    "kimi",
    "minimax",
    "kuaishou",
    "alibaba",
    "baidu",
    "tencent",
    "huawei",
    "beijing",
    "china",
    "prc",
)


def _alembic_config(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.abspath(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _create_pre_008_schema(engine: sa.engine.Engine) -> None:
    """Pre-008 (post-007) shape of provider_credentials -- the only table 008 touches."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE provider_credentials ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "provider_id INTEGER NOT NULL, "
                "label VARCHAR(255) NOT NULL, "
                "api_key VARCHAR(512), "
                "org_id VARCHAR(255), "
                "account_meta JSON, "
                "weight INTEGER NOT NULL, "
                "enabled BOOLEAN NOT NULL, "
                "request_count INTEGER NOT NULL, "
                "token_count INTEGER NOT NULL, "
                "last_used_at DATETIME, "
                "created_at DATETIME)"
            )
        )


def _columns(engine: sa.engine.Engine, table: str) -> set:
    with engine.connect() as conn:
        return {r[1] for r in conn.execute(sa.text(f"PRAGMA table_info({table})"))}


def _table_names(engine: sa.engine.Engine) -> set:
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))
        return {r[0] for r in rows}


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    db_path = tmp_path / "migration008.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = sa.create_engine(db_url)
    _create_pre_008_schema(engine)
    yield db_url, engine
    engine.dispose()


def test_upgrade_creates_and_seeds_model_registry(scratch_db):
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "007_drop_ailb_add_native_limits")
    command.upgrade(cfg, "008_model_registry")

    assert "model_registry" in _table_names(engine)

    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT name, role, license, origin, is_utility FROM model_registry")
        ).fetchall()

    names = {r.name for r in rows}
    assert {"gemma4:e2b", "shieldgemma:2b", "nomic-embed-text"} <= names
    # Stale plan-doc example names must NOT appear -- spec §2.3 superseded them.
    assert "gemma3:1b" not in names
    assert "granite3.3:2b" not in names

    assert all(r.is_utility for r in rows)
    roles = {r.role for r in rows}
    assert roles == {"routing_classifier", "security_auditor", "embeddings"}

    for row in rows:
        origin_lower = row.origin.lower()
        assert not any(marker in origin_lower for marker in _PRC_ORIGIN_MARKERS), (
            f"model {row.name} has a PRC-origin marker in origin={row.origin!r}"
        )


def test_upgrade_adds_plan_budget_column(scratch_db):
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "007_drop_ailb_add_native_limits")
    command.upgrade(cfg, "008_model_registry")

    assert "plan_budget" in _columns(engine, "provider_credentials")


def test_downgrade_drops_model_registry_and_plan_budget(scratch_db):
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "007_drop_ailb_add_native_limits")
    command.upgrade(cfg, "008_model_registry")
    command.downgrade(cfg, "-1")

    assert "model_registry" not in _table_names(engine)
    assert "plan_budget" not in _columns(engine, "provider_credentials")


def test_single_head():
    """The versions/ directory must resolve to exactly one head -- no branch/fork."""
    cfg = _alembic_config("sqlite:///:memory:")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    # Assert the single-head invariant, not that this revision IS the head:
    # hardcoding that breaks the moment any later migration lands.
    assert "008_model_registry" in {sc.revision for sc in script.walk_revisions()}
