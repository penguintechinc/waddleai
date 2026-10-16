# Elastic Pooled Quota — Phase 2b: Migration 026 Tables + Backfill

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec §5, §9, D2/D3/D6.

Part of the data-model phase (spec §5, §9, D2/D3/D6); earlier tasks of this phase created the tables and migration.

---

### Task 10: Migration `026` tables part + disabled org-pool backfill

**Files:**
- Modify: `services/management/alembic/versions/026_pooled_quota.py` (docstring unchanged; add `_tables_upgrade`, `_tables_downgrade`; extend `upgrade`/`downgrade`)
- Test: `tests/unit/management/test_migration_026_tables.py`

**Interfaces:**
- Consumes: Task 6's `026_pooled_quota.py` (`_has_table`, `_rates_upgrade`); Task 9's table/constraint/index names.
- Produces: `upgrade()` = `_rates_upgrade` then `_tables_upgrade` (create five tables, guarded, then one disabled `quota_org_pool` row per organization); `downgrade()` drops the five tables (reverse FK order) then drops the rates UNIQUE.

- [ ] **Step 1: Write the failing test**

`tests/unit/management/test_migration_026_tables.py`:

```python
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
_TABLES = ("quota_org_pool", "quota_team_pool", "quota_user_reservation", "quota_override", "quota_usage_ledger")
_PRE_026_DDL = (
    "CREATE TABLE organizations (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255) NOT NULL)",
    "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username VARCHAR(255), organization_id INTEGER)",
    "CREATE TABLE teams (id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id INTEGER, name VARCHAR(255))",
    "CREATE TABLE token_conversion_rates (id INTEGER PRIMARY KEY AUTOINCREMENT, provider VARCHAR(100) NOT NULL, "
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


def test_upgrade_creates_tables_indexes_and_backfills_disabled_pools(scratch):
    """Five tables exist, every org gets exactly one DISABLED pool row, indexes are present."""
    url, engine = scratch
    command.upgrade(_cfg(url), "026_pooled_quota")
    insp = sa.inspect(engine)
    assert set(_TABLES) <= set(insp.get_table_names())
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT org_id, enabled, pool_credits, cycle_anchor_date FROM quota_org_pool ORDER BY org_id")).all()
    assert [(r.org_id, r.enabled, r.pool_credits, r.cycle_anchor_date) for r in rows] == [(1, 0, 0, None), (2, 0, 0, None)]
    assert "ix_quota_override_org" in {i["name"] for i in insp.get_indexes("quota_override")}
    assert "ix_quota_ledger_org_cycle" in {i["name"] for i in insp.get_indexes("quota_usage_ledger")}


def test_constraints_are_enforced_after_migration(scratch):
    """The UNIQUE and CHECK constraints exist in the migrated schema, not just the models."""
    url, engine = scratch
    command.upgrade(_cfg(url), "026_pooled_quota")
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO quota_org_pool (org_id, pool_credits, enabled) VALUES (1, 5, 1)"))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO quota_override (organization_id, cap_credits) VALUES (1, 5)"))


def test_migrated_columns_match_the_models(scratch):
    """Drift guard: Alembic and SQLAlchemy must define the same columns for the five tables."""
    url, engine = scratch
    command.upgrade(_cfg(url), "026_pooled_quota")
    insp = sa.inspect(engine)
    for name in _TABLES:
        migrated = {c["name"] for c in insp.get_columns(name)}
        modelled = {c.name for c in Base.metadata.tables[name].columns}
        assert migrated == modelled, name


def test_downgrade_then_upgrade_is_idempotent(scratch):
    """Down drops the five tables; up again recreates them with one pool row per org."""
    url, engine = scratch
    command.upgrade(_cfg(url), "026_pooled_quota")
    command.downgrade(_cfg(url), "025_fk_indexes")
    assert not set(_TABLES) & set(sa.inspect(engine).get_table_names())
    command.upgrade(_cfg(url), "026_pooled_quota")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM quota_org_pool")).scalar() == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/management/test_migration_026_tables.py -v --no-cov`
Expected: FAIL — `quota_org_pool` does not exist after upgrade.

- [ ] **Step 3: Write minimal implementation**

In `026_pooled_quota.py` add (below `_rates_upgrade`):

```python
def _has_index(bind: sa.engine.Connection, table: str, name: str) -> bool:
    """True when ``table`` already carries an index called ``name``."""
    return name in {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def _tables_upgrade(bind: sa.engine.Connection) -> None:
    """Create the five quota tables (guarded), their indexes, and backfill disabled org pools."""
    big = sa.BigInteger()
    if not _has_table(bind, "quota_org_pool"):
        op.create_table(
            "quota_org_pool",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("cycle_anchor_date", sa.Date(), nullable=True),
            sa.Column("pool_credits", big, nullable=False, server_default="0"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.UniqueConstraint("org_id", name="uq_quota_org_pool_org"),
        )  # fmt: skip
    if not _has_table(bind, "quota_team_pool"):
        op.create_table(
            "quota_team_pool",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("pool_credits", big, nullable=False, server_default="0"),
            sa.Column("shared_buffer_credits", big, nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.UniqueConstraint("team_id", name="uq_quota_team_pool_team"),
        )  # fmt: skip
    if not _has_table(bind, "quota_user_reservation"):
        op.create_table(
            "quota_user_reservation",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("guaranteed_credits", big, nullable=False, server_default="0"),
            sa.Column("daily_subcap", big, nullable=True),
            sa.UniqueConstraint("user_id", "organization_id", name="uq_quota_user_res_user_org"),
        )  # fmt: skip
    if not _has_table(bind, "quota_override"):
        op.create_table(
            "quota_override",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
            sa.Column("model", sa.String(255), nullable=True),
            sa.Column("provider", sa.String(100), nullable=True),
            sa.Column("cap_credits", big, nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.CheckConstraint(
                "user_id IS NOT NULL OR model IS NOT NULL OR provider IS NOT NULL",
                name="ck_quota_override_has_dimension",
            ),
        )  # fmt: skip
    if not _has_table(bind, "quota_usage_ledger"):
        op.create_table(
            "quota_usage_ledger",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("scope_type", sa.String(20), nullable=False),
            sa.Column("scope_id", sa.Integer(), nullable=False),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("cycle_index", sa.Integer(), nullable=False),
            sa.Column("credits_used", big, nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
            sa.UniqueConstraint("scope_type", "scope_id", "organization_id", "cycle_index",
                                name="uq_quota_ledger_scope_cycle"),
        )  # fmt: skip
    for name, table, cols in _INDEXES:
        if not _has_index(bind, table, name):
            op.create_index(name, table, cols)
    bind.execute(
        sa.text(
            "INSERT INTO quota_org_pool (org_id, pool_credits, enabled, created_at) "
            "SELECT o.id, 0, :enabled, :now FROM organizations o "
            "WHERE NOT EXISTS (SELECT 1 FROM quota_org_pool p WHERE p.org_id = o.id)"
        ),
        {"enabled": False, "now": datetime.utcnow()},
    )


def _tables_downgrade(bind: sa.engine.Connection) -> None:
    """Drop the five quota tables (children before parents); indexes go with them."""
    for name in (
        "quota_usage_ledger", "quota_override", "quota_user_reservation",
        "quota_team_pool", "quota_org_pool",
    ):  # fmt: skip
        if _has_table(bind, name):
            op.drop_table(name)
```

Add module-level (above `_has_table`):

```python
_INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    ("ix_quota_team_pool_org", "quota_team_pool", ["organization_id"]),
    ("ix_quota_user_res_team", "quota_user_reservation", ["team_id"]),
    ("ix_quota_override_org", "quota_override", ["organization_id"]),
    ("ix_quota_ledger_org_cycle", "quota_usage_ledger", ["organization_id", "cycle_index"]),
)
```

Update the two entry points:

```python
def upgrade() -> None:
    """Apply the conversion-rate cleanup, then create the quota tables."""
    bind = op.get_bind()
    _rates_upgrade(bind)
    _tables_upgrade(bind)


def downgrade() -> None:
    """Drop the quota tables, then the rates UNIQUE; rate rows are intentionally kept."""
    bind = op.get_bind()
    _tables_downgrade(bind)
    if _has_table(bind, "token_conversion_rates") and _has_unique(
        bind, "token_conversion_rates", _UQ_NAME
    ):
        with op.batch_alter_table("token_conversion_rates") as batch:
            batch.drop_constraint(_UQ_NAME, type_="unique")
```

Update the module docstring's first line to `"""Elastic pooled quota: conversion-rate cleanup + the five quota tables.` .

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/management/test_migration_026_tables.py tests/unit/management/test_migration_026_rates.py tests/unit/management/test_migration_019.py -v --no-cov`
Expected: PASS. The rates test's scratch DB has no `organizations` table: `_tables_upgrade` would fail there. Fix in the same step: add `"CREATE TABLE organizations (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255) NOT NULL)"`, a minimal `users` and `teams` table to `_DDL` in `test_migration_026_rates.py`, and change its expected-total assertion to still equal `len(DEFAULT_RATE_ROWS)` (org rows are in another table, so the count is unaffected).

- [ ] **Step 5: Commit**

```bash
git add services/management/alembic/versions/026_pooled_quota.py tests/unit/management/test_migration_026_tables.py tests/unit/management/test_migration_026_rates.py
git commit -m "feat(migrations): 026 quota tables, indexes, disabled org-pool backfill"
git push
```

---
