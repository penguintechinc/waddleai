"""PyDAL (proxy runtime) definitions for the teams tables must match SQLAlchemy (schema authority).

Real PyDAL<->SQLAlchemy drift already shipped once (migrations 020/023); this is the guard that
``teams``, ``team_members`` and ``api_keys.team_id`` cannot drift the same way (spec §4).
"""

import pytest

from services.management.app.models_sqlalchemy import APIKey, Team, TeamMember
from shared.database.models import get_db

_PLACEHOLDER_HASH = "x"  # noqa: S105 -- placeholder, never a loginable hash


@pytest.fixture
def pydal_db():
    """A fresh in-memory PyDAL schema (``migrate=True``), as the contract harness builds it."""
    return get_db(db_uri="sqlite://", migrate=True)


def _pydal_cols(db, table: str) -> set[str]:
    """Return the column-name set penguin-dal holds for ``table``."""
    return set(getattr(db, table).table.columns.keys())


@pytest.mark.parametrize(("table", "model"), [("teams", Team), ("team_members", TeamMember)])
def test_pydal_columns_match_sqlalchemy(pydal_db, table, model) -> None:
    """The proxy's PyDAL table has exactly the SQLAlchemy model's columns."""
    assert _pydal_cols(pydal_db, table) == {c.name for c in model.__table__.columns}


def test_pydal_api_keys_has_team_id(pydal_db) -> None:
    """``api_keys.team_id`` exists on the proxy's PyDAL definition (the proxy reads it)."""
    assert "team_id" in _pydal_cols(pydal_db, "api_keys")
    assert {c.name for c in APIKey.__table__.columns} == _pydal_cols(pydal_db, "api_keys") | {
        "proxy_memory"
    }, "api_keys drift beyond the known pre-existing proxy_memory column"


def test_team_member_round_trips_through_pydal(pydal_db) -> None:
    """A team + membership written through the proxy's definitions reads back intact."""
    db = pydal_db
    org = db.organizations.insert(name="acme")
    user = db.users.insert(
        username="a",
        email="a@example.com",
        password_hash=_PLACEHOLDER_HASH,
        role="user",
        organization_id=org,
    )
    team = db.teams.insert(organization_id=org, name="Default")
    db.team_members.insert(team_id=team, user_id=user, organization_id=org, team_role="admin")
    db.commit()
    row = db(db.team_members.user_id == user).select().first()
    assert (row.team_id, row.team_role, row.organization_id) == (team, "admin", org)
