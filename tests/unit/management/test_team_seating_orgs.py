"""New organizations get a Default team (migration 024 only seats data that exists at upgrade).

Covers the service helpers, ``POST /api/v1/organizations`` and the first-boot bootstrap
(``init_default_data``) against a real database (``team_world`` / ``real_schema_db``).
"""

import pytest

from services.management.app.extensions import init_default_data
from services.management.app.services.teams import (
    DEFAULT_TEAM_NAME,
    default_team_role,
    ensure_default_team,
    seat_new_user,
)

_PLACEHOLDER_HASH = "x"  # noqa: S105 -- placeholder, never a loginable hash


def _team_rows(db, org_id: int) -> list:
    """Return every team row of ``org_id``."""
    return list(db(db.teams.organization_id == org_id).select())


def test_ensure_default_team_creates_it_once(team_world) -> None:
    """The first call creates ``Default`` (enabled); a second call returns the same id."""
    w = team_world
    first = ensure_default_team(w.db, w.org["a"])
    second = ensure_default_team(w.db, w.org["a"])
    assert first == second
    named = [t for t in _team_rows(w.db, w.org["a"]) if t.name == DEFAULT_TEAM_NAME]
    assert len(named) == 1
    assert named[0].enabled is True


def test_ensure_default_team_reuses_an_existing_default(team_world) -> None:
    """A Default team created by migration 024 (or an admin) is reused, never duplicated."""
    w = team_world
    existing = w.db.teams.insert(organization_id=w.org["b"], name=DEFAULT_TEAM_NAME, enabled=True)
    w.db.commit()
    assert ensure_default_team(w.db, w.org["b"]) == existing


@pytest.mark.parametrize(
    ("org_role", "team_role"),
    [("admin", "admin"), ("resource_manager", "member"), ("reporter", "member"), ("user", "member"),
     ("bogus", "member")],
)  # fmt: skip
def test_default_team_role_follows_the_org_admin_scope(org_role, team_role) -> None:
    """Same rule as migration 024: org-admin-scope holders become team admin, never owner."""
    assert default_team_role(org_role) == team_role


def test_seat_new_user_puts_them_in_the_orgs_default_team(team_world) -> None:
    """A user created after 024 lands in Default as member (admin for an org admin)."""
    w = team_world
    new_user = w.db.users.insert(
        username="newbie", email="newbie@example.com", password_hash=_PLACEHOLDER_HASH, role="user",
        organization_id=w.org["a"], enabled=True,
    )  # fmt: skip
    w.db.commit()
    team_id = seat_new_user(w.db, new_user, w.org["a"], "user")
    row = w.db(w.db.team_members.user_id == new_user).select().first()
    assert (row.team_id, row.team_role, row.organization_id) == (team_id, "member", w.org["a"])


async def test_creating_an_org_creates_its_default_team(
    team_world, real_db_client, team_headers
) -> None:
    """POST /organizations: the new org has exactly one enabled Default team."""
    resp = await real_db_client.post(
        "/api/v1/organizations", json={"name": "newco"}, headers=team_headers("boss")
    )
    assert resp.status_code == 201
    org_id = (await resp.get_json())["id"]
    teams = _team_rows(team_world.db, org_id)
    assert [(t.name, t.enabled) for t in teams] == [(DEFAULT_TEAM_NAME, True)]


def test_bootstrap_seats_the_admin_and_binds_the_master_key(real_schema_db) -> None:
    """First boot: Default org gets a Default team, the admin is its team admin, the key is bound.

    The bootstrap admin key is minted straight into ``api_keys`` (it never goes through
    ``POST /keys``), so it must be bound to the team here or it would act teamless forever.
    """
    db = real_schema_db
    init_default_data(db, config={"ADMIN_INITIAL_PASSWORD": "bootstrap-pw-123"})

    org = db(db.organizations.name == "default").select().first()
    admin = db(db.users.username == "admin").select().first()
    team = db(db.teams.organization_id == org.id).select().first()
    member = db(db.team_members.user_id == admin.id).select().first()
    key = db(db.api_keys.user_id == admin.id).select().first()
    assert team.name == DEFAULT_TEAM_NAME
    assert (member.team_id, member.team_role) == (team.id, "admin")
    assert key.team_id == team.id


def test_bootstrap_is_idempotent_for_teams(real_schema_db) -> None:
    """A second boot creates no second Default team and no second membership."""
    db = real_schema_db
    config = {"ADMIN_INITIAL_PASSWORD": "bootstrap-pw-123"}
    init_default_data(db, config=config)
    init_default_data(db, config=config)
    assert db(db.teams.id > 0).count() == 1
    assert db(db.team_members.id > 0).count() == 1
