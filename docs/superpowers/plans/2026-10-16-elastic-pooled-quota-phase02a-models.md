# Elastic Pooled Quota — Phase 2a: Quota Tables (SQLAlchemy + PyDAL)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec §5, §9, D2/D3/D6.

Five new tables (spec §5), dual-defined: SQLAlchemy is the authority (composite UNIQUE + CHECK live only there and in Alembic); PyDAL field defs let the proxy read config and write the ledger. `quota_org_pool` uses `org_id` (spec-literal); every other table uses `organization_id`.

---

---

### Task 9: SQLAlchemy models + PyDAL definitions for the five quota tables

**Files:**
- Modify: `services/management/app/models_sqlalchemy.py` (add `CheckConstraint` to the `sqlalchemy` import block; append five models directly after `class TeamMember`)
- Modify: `shared/database/models.py` (add five `_define_table_if_absent` calls directly after the `team_members` definition, ~line 182)
- Test: `tests/unit/management/test_quota_models.py`

**Interfaces:**
- Consumes: `real_schema_db` (SQLAlchemy `create_all` schema reflected by penguin-dal).
- Produces (tables/columns the later tasks query):
  - `quota_org_pool(id, org_id UNIQUE FK, cycle_anchor_date Date NULL, pool_credits BigInt NOT NULL default 0, enabled Bool default False, created_at)`
  - `quota_team_pool(id, team_id UNIQUE FK, organization_id FK, pool_credits BigInt default 0, shared_buffer_credits BigInt default 0, created_at)`
  - `quota_user_reservation(id, team_id FK, user_id FK, organization_id FK, guaranteed_credits BigInt default 0, daily_subcap BigInt NULL, UNIQUE(user_id, organization_id) = uq_quota_user_res_user_org)`
  - `quota_override(id, organization_id FK, user_id FK NULL, model NULL, provider NULL, cap_credits BigInt NOT NULL, created_at, CHECK ck_quota_override_has_dimension)`
  - `quota_usage_ledger(id, scope_type, scope_id, organization_id, cycle_index, credits_used BigInt default 0, updated_at, UNIQUE(scope_type, scope_id, organization_id, cycle_index) = uq_quota_ledger_scope_cycle)`
  - Index names: `ix_quota_team_pool_org`, `ix_quota_user_res_team`, `ix_quota_override_org`, `ix_quota_ledger_org_cycle`.
  - Ledger `scope_type` values: `"user"` (reservation consumption), `"team_buffer"`, `"override"`.

- [ ] **Step 1: Write the failing test**

`tests/unit/management/test_quota_models.py`:

```python
"""Quota schema (spec §5): column shape and DB-enforced uniqueness/check constraints."""

import pytest
from sqlalchemy.exc import IntegrityError

from services.management.app.models_sqlalchemy import (
    QuotaOrgPool,
    QuotaOverride,
    QuotaTeamPool,
    QuotaUsageLedger,
    QuotaUserReservation,
)


def _cols(model) -> set[str]:
    """Column-name set of a declarative model's table."""
    return {c.name for c in model.__table__.columns}


def _seed(db):
    """One org, one team, one user; returns (org, team, user) ids."""
    org = int(db.organizations.insert(name="acme"))
    team = int(db.teams.insert(organization_id=org, name="Alpha"))
    user = int(db.users.insert(username="u", email="u@x.io", password_hash="x", role="user", organization_id=org))
    db.commit()
    return org, team, user


def test_tables_have_exactly_the_spec_columns():
    """Each table carries the spec §5 columns and nothing else."""
    assert _cols(QuotaOrgPool) == {"id", "org_id", "cycle_anchor_date", "pool_credits", "enabled", "created_at"}
    assert _cols(QuotaTeamPool) == {"id", "team_id", "organization_id", "pool_credits", "shared_buffer_credits", "created_at"}
    assert _cols(QuotaUserReservation) == {"id", "team_id", "user_id", "organization_id", "guaranteed_credits", "daily_subcap"}
    assert _cols(QuotaOverride) == {"id", "organization_id", "user_id", "model", "provider", "cap_credits", "created_at"}
    assert _cols(QuotaUsageLedger) == {"id", "scope_type", "scope_id", "organization_id", "cycle_index", "credits_used", "updated_at"}


def test_one_org_pool_per_org(real_schema_db):
    """UNIQUE(org_id) on quota_org_pool."""
    db = real_schema_db
    org, _team, _user = _seed(db)
    db.quota_org_pool.insert(org_id=org, pool_credits=100, enabled=True)
    with pytest.raises(IntegrityError):
        db.quota_org_pool.insert(org_id=org, pool_credits=5, enabled=False)


def test_one_team_pool_per_team(real_schema_db):
    """UNIQUE(team_id) on quota_team_pool."""
    db = real_schema_db
    org, team, _user = _seed(db)
    db.quota_team_pool.insert(team_id=team, organization_id=org, pool_credits=10, shared_buffer_credits=2)
    with pytest.raises(IntegrityError):
        db.quota_team_pool.insert(team_id=team, organization_id=org, pool_credits=1, shared_buffer_credits=1)


def test_one_reservation_per_user_per_tenant(real_schema_db):
    """D2 mirror: UNIQUE(user_id, organization_id) on quota_user_reservation."""
    db = real_schema_db
    org, team, user = _seed(db)
    db.quota_user_reservation.insert(team_id=team, user_id=user, organization_id=org, guaranteed_credits=5)
    with pytest.raises(IntegrityError):
        db.quota_user_reservation.insert(team_id=team, user_id=user, organization_id=org, guaranteed_credits=9)


def test_override_needs_at_least_one_dimension(real_schema_db):
    """CHECK: an override with no user, model, or provider is meaningless and is refused."""
    db = real_schema_db
    org, _team, user = _seed(db)
    with pytest.raises(IntegrityError):
        db.quota_override.insert(organization_id=org, cap_credits=10)
    db.quota_override.insert(organization_id=org, user_id=user, cap_credits=10)
    db.quota_override.insert(organization_id=org, model="gpt-4o", cap_credits=10)
    db.quota_override.insert(organization_id=org, provider="openai", cap_credits=10)


def test_ledger_row_is_unique_per_scope_and_cycle(real_schema_db):
    """UNIQUE(scope_type, scope_id, organization_id, cycle_index) makes the rollup upsertable."""
    db = real_schema_db
    org, _team, user = _seed(db)
    db.quota_usage_ledger.insert(scope_type="user", scope_id=user, organization_id=org, cycle_index=3, credits_used=1)
    with pytest.raises(IntegrityError):
        db.quota_usage_ledger.insert(scope_type="user", scope_id=user, organization_id=org, cycle_index=3, credits_used=2)
    db.quota_usage_ledger.insert(scope_type="user", scope_id=user, organization_id=org, cycle_index=4, credits_used=2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/management/test_quota_models.py -v --no-cov`
Expected: FAIL — `ImportError: cannot import name 'QuotaOrgPool'`.

- [ ] **Step 3: Write minimal implementation**

`models_sqlalchemy.py`: add `CheckConstraint,` to the `from sqlalchemy import (...)` block (alphabetical: before `Column`) and `Date,` (after `Column`). Insert after `class TeamMember`:

```python
class QuotaOrgPool(Base):
    """Per-tenant credit pool + anchored 30-day cycle start (pooled-quota spec §5, D3/D6).

    A row exists per org (migration 026 backfills it ``enabled = false``); enforcement is a
    no-op until an admin sets the anchor, pool, and enables it.
    """

    __tablename__ = "quota_org_pool"
    __table_args__ = (UniqueConstraint("org_id", name="uq_quota_org_pool_org"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    cycle_anchor_date = Column(Date, nullable=True)
    pool_credits = Column(BigInteger, nullable=False, default=0)
    enabled = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class QuotaTeamPool(Base):
    """A team's sub-pool and lendable shared buffer, in WaddleAI-token credits (spec §5, D3/D4)."""

    __tablename__ = "quota_team_pool"
    __table_args__ = (
        UniqueConstraint("team_id", name="uq_quota_team_pool_team"),
        Index("ix_quota_team_pool_org", "organization_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    pool_credits = Column(BigInteger, nullable=False, default=0)
    shared_buffer_credits = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class QuotaUserReservation(Base):
    """A user's private guaranteed credits (never lent) and optional daily sub-cap (spec §5, D4)."""

    __tablename__ = "quota_user_reservation"
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="uq_quota_user_res_user_org"),
        Index("ix_quota_user_res_team", "team_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    guaranteed_credits = Column(BigInteger, nullable=False, default=0)
    daily_subcap = Column(BigInteger, nullable=True)


class QuotaOverride(Base):
    """A hard cap keyed by user and/or model and/or provider; further-restricts only (spec D5)."""

    __tablename__ = "quota_override"
    __table_args__ = (
        CheckConstraint(
            "user_id IS NOT NULL OR model IS NOT NULL OR provider IS NOT NULL",
            name="ck_quota_override_has_dimension",
        ),
        Index("ix_quota_override_org", "organization_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    model = Column(String(255), nullable=True)
    provider = Column(String(100), nullable=True)
    cap_credits = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class QuotaUsageLedger(Base):
    """Durable per-scope, per-cycle credit rollup: reporting + Valkey counter rebuild (spec §5).

    ``scope_type`` is ``user`` (reservation consumption), ``team_buffer`` or ``override``.
    """

    __tablename__ = "quota_usage_ledger"
    __table_args__ = (
        UniqueConstraint(
            "scope_type", "scope_id", "organization_id", "cycle_index",
            name="uq_quota_ledger_scope_cycle",
        ),
        Index("ix_quota_ledger_org_cycle", "organization_id", "cycle_index"),
    )  # fmt: skip

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope_type = Column(String(20), nullable=False)
    scope_id = Column(Integer, nullable=False)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    cycle_index = Column(Integer, nullable=False)
    credits_used = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow)
```

`shared/database/models.py`, after the `team_members` definition:

```python
    # Elastic pooled quota (spec §5). The proxy reads the four config tables and writes the
    # ledger; SQLAlchemy stays the schema authority (composite UNIQUE/CHECK live only there
    # and in Alembic 026_pooled_quota).
    _define_table_if_absent(
        db,
        "quota_org_pool",
        Field("org_id", "reference organizations", notnull=True),
        Field("cycle_anchor_date", "date"),
        Field("pool_credits", "bigint", default=0),
        Field("enabled", "boolean", default=False),
        Field("created_at", "datetime", default=datetime.utcnow),
    )
    _define_table_if_absent(
        db,
        "quota_team_pool",
        Field("team_id", "reference teams", notnull=True),
        Field("organization_id", "reference organizations", notnull=True),
        Field("pool_credits", "bigint", default=0),
        Field("shared_buffer_credits", "bigint", default=0),
        Field("created_at", "datetime", default=datetime.utcnow),
    )
    _define_table_if_absent(
        db,
        "quota_user_reservation",
        Field("team_id", "reference teams", notnull=True),
        Field("user_id", "reference users", notnull=True),
        Field("organization_id", "reference organizations", notnull=True),
        Field("guaranteed_credits", "bigint", default=0),
        Field("daily_subcap", "bigint"),
    )
    _define_table_if_absent(
        db,
        "quota_override",
        Field("organization_id", "reference organizations", notnull=True),
        Field("user_id", "reference users"),
        Field("model", "string"),
        Field("provider", "string"),
        Field("cap_credits", "bigint", notnull=True),
        Field("created_at", "datetime", default=datetime.utcnow),
    )
    _define_table_if_absent(
        db,
        "quota_usage_ledger",
        Field("scope_type", "string", notnull=True),
        Field("scope_id", "integer", notnull=True),
        Field("organization_id", "reference organizations", notnull=True),
        Field("cycle_index", "integer", notnull=True),
        Field("credits_used", "bigint", default=0),
        Field("updated_at", "datetime", default=datetime.utcnow),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/management/test_quota_models.py tests/unit/management/test_team_models.py -v --no-cov`
Expected: PASS. If `get_db` raises on the PyDAL `"bigint"` type name, switch those fields to `"integer"` in `shared/database/models.py` only (SQLite/Postgres reflection keeps the SQLAlchemy BigInteger).

- [ ] **Step 5: Commit**

```bash
git add services/management/app/models_sqlalchemy.py shared/database/models.py tests/unit/management/test_quota_models.py
git commit -m "feat(quota): five quota tables, dual-defined (SQLAlchemy authority + PyDAL)"
git push
```

---
