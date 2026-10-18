"""Migration 026, tables part: five tables, constraints, indexes, disabled org-pool backfill."""

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from services.management.app.models_sqlalchemy import Base

ALEMBIC_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "..", "..", "services", "management", "alembic")
)  # fmt: skip
_TABLES = (
    "quota_org_pool",
    "quota_team_pool",
    "quota_user_reservation",
    "quota_override",
    "quota_usage_ledger",
)
_UQ = "uq_token_conversion_rates_provider_model"
_PRE_026_DDL = (
    "CREATE TABLE organizations (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255) NOT NULL)",
    "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username VARCHAR(255), "
    "organization_id INTEGER)",
    "CREATE TABLE teams (id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id INTEGER, "
    "name VARCHAR(255))",
    "CREATE TABLE token_conversion_rates (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "provider VARCHAR(100) NOT NULL, "
    "model VARCHAR(255) NOT NULL, input_rate FLOAT NOT NULL, output_rate FLOAT NOT NULL, "
    "base_cost_per_waddleai_token FLOAT, effective_date DATETIME, enabled BOOLEAN)",
    "INSERT INTO organizations (name) VALUES ('acme'), ('globex')",
)


def _cfg(url: str) -> Config:
    """Alembic config pointed at the management migrations."""
    cfg = Config()
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """Scratch SQLite with the minimal pre-026 tables, stamped at 025; yields (url, engine)."""
    url = f"sqlite:///{tmp_path / 'm026t.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        for ddl in _PRE_026_DDL:
            conn.execute(sa.text(ddl))
    command.stamp(_cfg(url), "025_fk_indexes")
    yield url, engine
    engine.dispose()


def _insp(engine):
    """Inspector on a fresh pool (a pooled connection can serve stale reflection)."""
    engine.dispose()
    return sa.inspect(engine)


def _pool_rows(engine):
    """Return (org_id, enabled, pool_credits, cycle_anchor_date) per org pool, ordered."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT org_id, enabled, pool_credits, cycle_anchor_date "
                "FROM quota_org_pool ORDER BY org_id"
            )
        ).all()
    return [tuple(r) for r in rows]


def test_upgrade_creates_tables_indexes_and_backfills_disabled_pools(scratch):
    """Five tables exist, every org gets exactly one DISABLED pool row, indexes are present."""
    url, engine = scratch
    command.upgrade(_cfg(url), "026_pooled_quota")
    insp = _insp(engine)
    assert set(_TABLES) <= set(insp.get_table_names())
    assert _pool_rows(engine) == [(1, 0, 0, None), (2, 0, 0, None)]
    assert "ix_quota_team_pool_org" in {i["name"] for i in insp.get_indexes("quota_team_pool")}
    assert "ix_quota_user_res_team" in {
        i["name"] for i in insp.get_indexes("quota_user_reservation")
    }
    assert "ix_quota_override_org" in {i["name"] for i in insp.get_indexes("quota_override")}
    assert "ix_quota_ledger_org_cycle" in {
        i["name"] for i in insp.get_indexes("quota_usage_ledger")
    }


def test_named_constraints_match_the_models(scratch):
    """Every UNIQUE/CHECK name in the SQLAlchemy authority exists in the migrated schema."""
    url, engine = scratch
    command.upgrade(_cfg(url), "026_pooled_quota")
    insp = _insp(engine)
    for name in _TABLES:
        want = {
            c.name
            for c in Base.metadata.tables[name].constraints
            if isinstance(c, sa.UniqueConstraint)
        }
        got = {u["name"] for u in insp.get_unique_constraints(name)}
        assert want <= got, (name, want, got)
    checks = {c["name"] for c in insp.get_check_constraints("quota_override")}
    assert "ck_quota_override_has_dimension" in checks


def test_constraints_are_enforced_after_migration(scratch):
    """The UNIQUE and CHECK constraints bite in the migrated schema, not just the models."""
    url, engine = scratch
    command.upgrade(_cfg(url), "026_pooled_quota")
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO quota_org_pool (org_id, pool_credits, enabled) VALUES (1, 5, 1)")
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO quota_override (organization_id, cap_credits) VALUES (1, 5)")
        )


def test_migrated_columns_and_types_match_the_models(scratch):
    """Drift guard: Alembic and SQLAlchemy define the same columns (and BigInteger credits)."""
    url, engine = scratch
    command.upgrade(_cfg(url), "026_pooled_quota")
    insp = _insp(engine)
    for name in _TABLES:
        migrated = {c["name"]: c["type"] for c in insp.get_columns(name)}
        modelled = {c.name: c.type for c in Base.metadata.tables[name].columns}
        assert set(migrated) == set(modelled), name
        for col, typ in modelled.items():
            if isinstance(typ, sa.BigInteger):
                assert isinstance(migrated[col], sa.BigInteger), (name, col)
            if isinstance(typ, sa.Date) and not isinstance(typ, sa.DateTime):
                assert isinstance(migrated[col], sa.Date), (name, col)


def test_reapply_over_migrated_db_hits_skip_branches(scratch):
    """Re-stamp to 025 over an already-migrated DB: UNIQUE/tables/indexes skip, no duplicates."""
    url, engine = scratch
    command.upgrade(_cfg(url), "026_pooled_quota")
    command.stamp(_cfg(url), "025_fk_indexes")
    command.upgrade(_cfg(url), "026_pooled_quota")
    assert _pool_rows(engine) == [(1, 0, 0, None), (2, 0, 0, None)]
    insp = _insp(engine)
    assert _UQ in {u["name"] for u in insp.get_unique_constraints("token_conversion_rates")}


def test_backfill_does_not_touch_an_existing_pool_row(scratch):
    """A pre-existing (admin-configured) pool row survives a re-run; only missing orgs backfill."""
    url, engine = scratch
    command.upgrade(_cfg(url), "026_pooled_quota")
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE quota_org_pool SET enabled = 1, pool_credits = 99 WHERE org_id = 1")
        )
        conn.execute(sa.text("INSERT INTO organizations (name) VALUES ('initech')"))
    command.stamp(_cfg(url), "025_fk_indexes")
    command.upgrade(_cfg(url), "026_pooled_quota")
    assert _pool_rows(engine) == [(1, 1, 99, None), (2, 0, 0, None), (3, 0, 0, None)]


def test_downgrade_drops_tables_and_unique_then_upgrade_recreates(scratch):
    """Down drops the five tables AND the rates UNIQUE; up recreates both, one pool per org."""
    url, engine = scratch
    command.upgrade(_cfg(url), "026_pooled_quota")
    assert _UQ in {
        u["name"] for u in _insp(engine).get_unique_constraints("token_conversion_rates")
    }
    command.downgrade(_cfg(url), "025_fk_indexes")
    insp = _insp(engine)
    assert not set(_TABLES) & set(insp.get_table_names())
    assert _UQ not in {u["name"] for u in insp.get_unique_constraints("token_conversion_rates")}
    command.upgrade(_cfg(url), "026_pooled_quota")
    assert _pool_rows(engine) == [(1, 0, 0, None), (2, 0, 0, None)]
