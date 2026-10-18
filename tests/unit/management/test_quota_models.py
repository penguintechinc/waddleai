"""Quota schema (spec §5): column shape and DB-enforced uniqueness/check constraints."""

import pytest
from sqlalchemy.exc import IntegrityError

from services.management.app.models_sqlalchemy import (
    Base,
    QuotaOrgPool,
    QuotaOverride,
    QuotaTeamPool,
    QuotaUsageLedger,
    QuotaUserReservation,
)

QUOTA_TABLES = (
    "quota_org_pool",
    "quota_team_pool",
    "quota_user_reservation",
    "quota_override",
    "quota_usage_ledger",
)


def _cols(model) -> set[str]:
    """Column-name set of a declarative model's table."""
    return {c.name for c in model.__table__.columns}


def _seed(db):
    """One org, one team, one user; returns (org, team, user) ids."""
    org = int(db.organizations.insert(name="acme"))
    team = int(db.teams.insert(organization_id=org, name="Alpha"))
    user = int(
        db.users.insert(
            username="u",
            email="u@x.io",
            password_hash="x",  # noqa: S106
            role="user",
            organization_id=org,
        )
    )
    db.commit()
    return org, team, user


def test_tables_have_exactly_the_spec_columns():
    """Each table carries the spec §5 columns and nothing else."""
    assert _cols(QuotaOrgPool) == {
        "id",
        "org_id",
        "cycle_anchor_date",
        "pool_credits",
        "enabled",
        "created_at",
    }
    assert _cols(QuotaTeamPool) == {
        "id",
        "team_id",
        "organization_id",
        "pool_credits",
        "shared_buffer_credits",
        "created_at",
    }
    assert _cols(QuotaUserReservation) == {
        "id",
        "team_id",
        "user_id",
        "organization_id",
        "guaranteed_credits",
        "daily_subcap",
    }
    assert _cols(QuotaOverride) == {
        "id",
        "organization_id",
        "user_id",
        "model",
        "provider",
        "cap_credits",
        "created_at",
    }
    assert _cols(QuotaUsageLedger) == {
        "id",
        "scope_type",
        "scope_id",
        "organization_id",
        "cycle_index",
        "credits_used",
        "updated_at",
    }


def test_pydal_and_sqlalchemy_agree(tmp_path):
    """Runtime (PyDAL) definitions match the schema authority table-for-table, column-for-column."""
    from shared.database.models import get_db

    db = get_db(f"sqlite://{tmp_path}/quota_agree.db", migrate=True)
    for name in QUOTA_TABLES:
        sa_cols = {c.name for c in Base.metadata.tables[name].columns}
        assert name in db.tables, f"PyDAL missing {name}"
        assert {c.name for c in getattr(db, name).table.columns} == sa_cols, name


def test_named_constraints_and_indexes_present():
    """Task 10's migration matches these exact constraint/index names."""
    names = set()
    for name in QUOTA_TABLES:
        t = Base.metadata.tables[name]
        names |= {c.name for c in t.constraints if c.name} | {i.name for i in t.indexes}
    assert {
        "uq_quota_org_pool_org",
        "uq_quota_team_pool_team",
        "uq_quota_user_res_user_org",
        "ck_quota_override_has_dimension",
        "uq_quota_ledger_scope_cycle",
        "ix_quota_team_pool_org",
        "ix_quota_user_res_team",
        "ix_quota_override_org",
        "ix_quota_ledger_org_cycle",
    } <= names


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
    db.quota_team_pool.insert(
        team_id=team, organization_id=org, pool_credits=10, shared_buffer_credits=2
    )
    with pytest.raises(IntegrityError):
        db.quota_team_pool.insert(
            team_id=team, organization_id=org, pool_credits=1, shared_buffer_credits=1
        )


def test_one_reservation_per_user_per_tenant(real_schema_db):
    """D2 mirror: UNIQUE(user_id, organization_id) on quota_user_reservation."""
    db = real_schema_db
    org, team, user = _seed(db)
    db.quota_user_reservation.insert(
        team_id=team, user_id=user, organization_id=org, guaranteed_credits=5
    )
    with pytest.raises(IntegrityError):
        db.quota_user_reservation.insert(
            team_id=team, user_id=user, organization_id=org, guaranteed_credits=9
        )


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
    db.quota_usage_ledger.insert(
        scope_type="user", scope_id=user, organization_id=org, cycle_index=3, credits_used=1
    )
    with pytest.raises(IntegrityError):
        db.quota_usage_ledger.insert(
            scope_type="user", scope_id=user, organization_id=org, cycle_index=3, credits_used=2
        )
    db.quota_usage_ledger.insert(
        scope_type="user", scope_id=user, organization_id=org, cycle_index=4, credits_used=2
    )
