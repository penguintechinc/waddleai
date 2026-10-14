"""Teams schema (spec §4): SQLAlchemy model shape and the D2 uniqueness guarantee.

Runs against ``real_schema_db`` -- the SQLAlchemy ``create_all`` schema reflected by
penguin-dal, no mocks -- so the UNIQUE constraints are enforced by a real engine.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from services.management.app.models_sqlalchemy import APIKey, Team, TeamMember, VirtualKey

_PLACEHOLDER_HASH = "x"  # noqa: S105 -- placeholder, never a loginable hash


def _cols(model) -> set[str]:
    """Return the column-name set of a declarative model's table."""
    return {c.name for c in model.__table__.columns}


def _org(db, name: str) -> int:
    """Insert an organization and return its id."""
    org_id = db.organizations.insert(name=name)
    db.commit()
    return int(org_id)


def _user(db, name: str, org_id: int) -> int:
    """Insert a minimal user in ``org_id`` and return its id."""
    user_id = db.users.insert(
        username=name, email=f"{name}@example.com", password_hash=_PLACEHOLDER_HASH, role="user",
        organization_id=org_id,
    )  # fmt: skip
    db.commit()
    return int(user_id)


def test_team_tables_have_exactly_the_spec_columns() -> None:
    """``teams``/``team_members`` carry the spec §4 columns and nothing else."""
    assert _cols(Team) == {"id", "organization_id", "name", "enabled", "created_at"}
    assert _cols(TeamMember) == {
        "id", "team_id", "user_id", "organization_id", "team_role", "created_at",
    }  # fmt: skip


def test_api_keys_gain_team_id_and_virtual_keys_gain_api_key_id() -> None:
    """``api_keys.team_id`` (spec §4) and ``virtual_keys.api_key_id`` (revoke-parity link)."""
    assert "team_id" in _cols(APIKey)
    assert "api_key_id" in _cols(VirtualKey)


def test_second_team_in_same_org_is_rejected(real_schema_db) -> None:
    """D2: UNIQUE(user_id, organization_id) blocks a second team for a user in one tenant."""
    db = real_schema_db
    org = _org(db, "acme")
    user = _user(db, "alice", org)
    t1 = db.teams.insert(organization_id=org, name="Alpha")
    t2 = db.teams.insert(organization_id=org, name="Beta")
    db.team_members.insert(team_id=t1, user_id=user, organization_id=org, team_role="member")
    db.commit()
    with pytest.raises(IntegrityError):
        db.team_members.insert(team_id=t2, user_id=user, organization_id=org, team_role="member")


def test_user_may_have_one_team_in_each_of_two_tenants(real_schema_db) -> None:
    """D2 is per (user, tenant): a user managing two orgs may join one team in each."""
    db = real_schema_db
    org_a, org_b = _org(db, "acme"), _org(db, "globex")
    user = _user(db, "mgr", org_a)
    ta = db.teams.insert(organization_id=org_a, name="Alpha")
    tb = db.teams.insert(organization_id=org_b, name="Alpha")
    db.team_members.insert(team_id=ta, user_id=user, organization_id=org_a, team_role="member")
    db.team_members.insert(team_id=tb, user_id=user, organization_id=org_b, team_role="member")
    db.commit()
    assert db(db.team_members.user_id == user).count() == 2


def test_team_name_is_unique_per_org_not_globally(real_schema_db) -> None:
    """UNIQUE(organization_id, name): same name in two orgs is fine, twice in one is not."""
    db = real_schema_db
    org_a, org_b = _org(db, "acme"), _org(db, "globex")
    db.teams.insert(organization_id=org_a, name="Default")
    db.teams.insert(organization_id=org_b, name="Default")
    db.commit()
    with pytest.raises(IntegrityError):
        db.teams.insert(organization_id=org_a, name="Default")
