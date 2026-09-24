"""Migration 019 round-trip test: the Gemma 4 ``e2b`` -> ``e4b`` floor bump.

Same technique as ``test_migration_018.py`` -- a scratch SQLite DB stamped at
the real ``018_model_access_policies`` head so exactly one step runs. 019 is a
data migration over tables created back at 008/010, so the two tables it
touches are built here directly (only the columns 019 reads or writes) and
seeded with the pre-bump rows.
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

_REGISTRY_DDL = """
CREATE TABLE model_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(255) NOT NULL UNIQUE,
    role VARCHAR(100) NOT NULL,
    license VARCHAR(100) NOT NULL,
    origin VARCHAR(100) NOT NULL,
    min_vram INTEGER,
    ollama_tag VARCHAR(255),
    is_utility BOOLEAN NOT NULL DEFAULT 0
)
"""

_ASSIGNMENTS_DDL = """
CREATE TABLE model_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_type VARCHAR(100) NOT NULL,
    model_name VARCHAR(255) NOT NULL,
    scope VARCHAR(10) NOT NULL DEFAULT 'global'
)
"""


def _alembic_config(db_url: str) -> Config:
    """Alembic config pointed at the management service's migration scripts."""
    cfg = Config()
    cfg.set_main_option("script_location", os.path.abspath(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """Scratch db stamped at 018, with 008/010's two affected tables seeded pre-bump."""
    db_path = tmp_path / "migration019.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = sa.create_engine(db_url)

    with engine.begin() as conn:
        conn.execute(sa.text(_REGISTRY_DDL))
        conn.execute(sa.text(_ASSIGNMENTS_DDL))
        conn.execute(
            sa.text(
                "INSERT INTO model_registry "
                "(name, role, license, origin, min_vram, ollama_tag, is_utility) VALUES "
                "('gemma4:e2b', 'routing_classifier', 'Apache-2.0', 'Google', 2, "
                "'gemma4:e2b', 1), "
                "('smollm2:1.7b', 'routing_classifier', 'Apache-2.0', 'HuggingFace', 2, "
                "'smollm2:1.7b', 1)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO model_assignments (tool_type, model_name, scope) VALUES "
                "('routing-classifier', 'gemma4:e2b', 'global'), "
                "('docs-fetch', 'gemma4:e2b', 'global'), "
                "('summarize', 'gemma4:e2b', 'global'), "
                "('security-audit', 'shieldgemma:2b', 'global'), "
                "('chat', 'gemma4:12b', 'org')"
            )
        )

    yield db_url, engine
    engine.dispose()


def _registry(conn) -> dict[str, tuple[str | None, int | None]]:
    """Map registry name -> (ollama_tag, min_vram)."""
    rows = conn.execute(sa.text("SELECT name, ollama_tag, min_vram FROM model_registry"))
    return {r[0]: (r[1], r[2]) for r in rows}


def _assignments(conn) -> dict[str, str]:
    """Map tool_type -> model_name."""
    rows = conn.execute(sa.text("SELECT tool_type, model_name FROM model_assignments"))
    return {r[0]: r[1] for r in rows}


def test_upgrade_retags_registry_and_assignments(scratch_db) -> None:
    """019 moves every gemma4:e2b row to e4b and raises the registry min_vram to 4."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "018_model_access_policies")
    command.upgrade(cfg, "019_gemma4_e4b_minimum")

    with engine.connect() as conn:
        registry = _registry(conn)
        assignments = _assignments(conn)

    assert "gemma4:e2b" not in registry
    assert registry["gemma4:e4b"] == ("gemma4:e4b", 4)
    assert assignments["routing-classifier"] == "gemma4:e4b"
    assert assignments["docs-fetch"] == "gemma4:e4b"
    assert assignments["summarize"] == "gemma4:e4b"


def test_upgrade_leaves_other_models_untouched(scratch_db) -> None:
    """Rows on any other tag -- including an operator's own 12b pick -- are not rewritten."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "018_model_access_policies")
    command.upgrade(cfg, "019_gemma4_e4b_minimum")

    with engine.connect() as conn:
        registry = _registry(conn)
        assignments = _assignments(conn)

    assert registry["smollm2:1.7b"] == ("smollm2:1.7b", 2)
    assert assignments["security-audit"] == "shieldgemma:2b"
    assert assignments["chat"] == "gemma4:12b"


def test_upgrade_is_idempotent(scratch_db) -> None:
    """Re-running the update statements finds nothing left to change."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "018_model_access_policies")
    command.upgrade(cfg, "019_gemma4_e4b_minimum")

    with engine.connect() as conn:
        first = (_registry(conn), _assignments(conn))

    command.downgrade(cfg, "018_model_access_policies")
    command.upgrade(cfg, "019_gemma4_e4b_minimum")

    with engine.connect() as conn:
        second = (_registry(conn), _assignments(conn))

    assert first == second


def test_downgrade_restores_e2b(scratch_db) -> None:
    """Downgrade puts the seeded rows back on e2b at min_vram 2."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "018_model_access_policies")
    command.upgrade(cfg, "019_gemma4_e4b_minimum")
    command.downgrade(cfg, "018_model_access_policies")

    with engine.connect() as conn:
        registry = _registry(conn)
        assignments = _assignments(conn)

    assert "gemma4:e4b" not in registry
    assert registry["gemma4:e2b"] == ("gemma4:e2b", 2)
    assert assignments["routing-classifier"] == "gemma4:e2b"
    assert assignments["chat"] == "gemma4:12b"


def test_single_alembic_head_no_divergent_branches() -> None:
    """Exactly one resolvable Alembic head -- no divergent branches introduced.

    The `len(heads) == 1` assertion is the durable invariant: two heads mean
    two migrations claim the same `down_revision`, which `alembic upgrade head`
    cannot resolve.

    The identity assertion is deliberately pinned so that adding a migration is
    an explicit, reviewed act rather than a silent one -- bump it in the same
    commit that adds the migration.
    """
    from alembic.script import ScriptDirectory

    cfg = _alembic_config("sqlite://")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()

    assert len(heads) == 1
    assert heads[0] == "021_audit_log"
