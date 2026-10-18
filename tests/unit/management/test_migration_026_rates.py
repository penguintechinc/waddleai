"""Migration 026, rates part: dedup, UNIQUE(provider, model), canonical seed (spec §4)."""

import importlib
import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError

from shared.utils.default_conversion_rates import DEFAULT_RATE_ROWS

ALEMBIC_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "..", "..", "services", "management", "alembic")
)  # fmt: skip

_DDL = (
    "CREATE TABLE organizations (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255) NOT NULL)",
    "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username VARCHAR(255))",
    "CREATE TABLE teams (id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id INTEGER)",
    "CREATE TABLE token_conversion_rates (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "provider VARCHAR(100) NOT NULL, model VARCHAR(255) NOT NULL, input_rate FLOAT NOT NULL, "
    "output_rate FLOAT NOT NULL, base_cost_per_waddleai_token FLOAT, effective_date DATETIME, "
    "enabled BOOLEAN)",
    # duplicate (openai, gpt-4o): the lower id is the seed/admin row and must survive
    "INSERT INTO token_conversion_rates (provider, model, input_rate, output_rate, enabled) "
    "VALUES ('openai', 'gpt-4o', 77.0, 77.0, 1), ('openai', 'gpt-4o', 1.0, 1.0, 1)",
)


def _cfg(url: str) -> Config:
    """Alembic config pointed at the management migrations."""
    cfg = Config()
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """Scratch SQLite stamped at 025 with a duplicated rates table; yields (url, engine)."""
    url = f"sqlite:///{tmp_path / 'm026.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        for ddl in _DDL:
            conn.execute(sa.text(ddl))
    command.stamp(_cfg(url), "025_fk_indexes")
    yield url, engine
    engine.dispose()


def test_revision_chain():
    """026 revises 025 and is the single head."""
    script = ScriptDirectory.from_config(_cfg("sqlite://"))
    assert script.get_revision("026_pooled_quota").down_revision == "025_fk_indexes"
    assert script.get_heads() == ["026_pooled_quota"]


def test_upgrade_dedups_keeps_lowest_id_enforces_unique_and_seeds(scratch):
    """Duplicates collapse to the oldest row, UNIQUE bites, every canonical row exists once."""
    url, engine = scratch
    command.upgrade(_cfg(url), "026_pooled_quota")
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT input_rate FROM token_conversion_rates "
                "WHERE provider='openai' AND model='gpt-4o'"
            )
        ).all()
        total = conn.execute(sa.text("SELECT COUNT(*) FROM token_conversion_rates")).scalar()
    assert [r.input_rate for r in rows] == [77.0]
    assert total == len(DEFAULT_RATE_ROWS)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO token_conversion_rates (provider, model, input_rate, output_rate) "
                "VALUES ('openai', 'gpt-4o', 5, 5)"
            )
        )


def test_downgrade_then_upgrade_is_idempotent(scratch):
    """Re-running over an already-migrated DB neither errors nor duplicates seed rows."""
    url, engine = scratch
    command.upgrade(_cfg(url), "026_pooled_quota")
    command.downgrade(_cfg(url), "025_fk_indexes")
    command.upgrade(_cfg(url), "026_pooled_quota")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM token_conversion_rates")).scalar() == len(
            DEFAULT_RATE_ROWS
        )


def test_frozen_seed_matches_the_canonical_module():
    """The migration frozen seed copy must equal DEFAULT_RATE_ROWS (no app-code import)."""
    mig = importlib.import_module("services.management.alembic.versions.026_pooled_quota")
    assert tuple(mig._CANONICAL_RATES) == tuple(tuple(r) for r in DEFAULT_RATE_ROWS)
