"""New users are seated in their organization's Default team (024 seats existing ones only).

``POST /api/v1/users`` against a real database (``team_world`` + ``real_db_client``).
"""

import secrets

import pytest

from services.management.app.services.teams import DEFAULT_TEAM_NAME, seat_new_user

_NEW_PASSWORD = secrets.token_urlsafe(16)  # random per run; the route only needs a non-empty one


def _membership(db, user_id: int):
    """Return the user's single ``team_members`` row, or ``None``."""
    return db(db.team_members.user_id == user_id).select().first()


async def _create_user(client, headers, **over) -> dict:
    """POST /users with sane defaults and return the JSON body (asserting 201)."""
    payload = {"username": "nina", "email": "nina@example.com", "password": _NEW_PASSWORD}
    payload.update(over)
    resp = await client.post("/api/v1/users", json=payload, headers=headers)
    assert resp.status_code == 201, await resp.get_data()
    return await resp.get_json()


@pytest.mark.parametrize(("role", "team_role"), [("user", "member"), ("admin", "admin")])
async def test_new_user_joins_the_default_team(
    team_world, real_db_client, team_headers, role, team_role
) -> None:
    """A user created in org a joins a Default team there: member, or admin for an org admin."""
    w = team_world
    body = await _create_user(real_db_client, team_headers("boss"), role=role)
    row = _membership(w.db, body["id"])
    team = w.db.teams[row.team_id]
    assert (team.name, team.organization_id) == (DEFAULT_TEAM_NAME, w.org["a"])
    assert (row.organization_id, row.team_role) == (w.org["a"], team_role)


async def test_user_created_in_another_org_joins_that_orgs_default_team(
    team_world, real_db_client, team_headers
) -> None:
    """The org admin creating a user in org b seats them in b's Default team (created on demand)."""
    w = team_world
    body = await _create_user(
        real_db_client, team_headers("boss"), organization_id=w.org["b"], username="bob2",
        email="bob2@example.com",
    )  # fmt: skip
    team = w.db.teams[_membership(w.db, body["id"]).team_id]
    assert (team.name, team.organization_id) == (DEFAULT_TEAM_NAME, w.org["b"])


async def test_two_new_users_share_one_default_team(
    team_world, real_db_client, team_headers
) -> None:
    """Seating is idempotent on the team: the second user reuses the first user's Default team."""
    w = team_world
    one = await _create_user(real_db_client, team_headers("boss"))
    two = await _create_user(
        real_db_client, team_headers("boss"), username="omar", email="omar@example.com"
    )
    assert _membership(w.db, one["id"]).team_id == _membership(w.db, two["id"]).team_id
    named = w.db((w.db.teams.organization_id == w.org["a"]) & (w.db.teams.name == "Default"))
    assert named.count() == 1


async def test_a_rejected_create_seats_nobody(team_world, real_db_client, team_headers) -> None:
    """A 409 (duplicate username) must not leave a stray membership behind."""
    w = team_world
    before = w.db(w.db.team_members.id > 0).count()
    resp = await real_db_client.post(
        "/api/v1/users",
        json={"username": "mia", "email": "other@example.com", "password": _NEW_PASSWORD},
        headers=team_headers("boss"),
    )
    assert resp.status_code == 409
    assert w.db(w.db.team_members.id > 0).count() == before


async def test_seating_is_idempotent(team_world, real_db_client, team_headers) -> None:
    """A repeated seat_new_user (retried create) keeps ONE membership and returns its team."""
    w = team_world
    body = await _create_user(real_db_client, team_headers("boss"))
    first = _membership(w.db, body["id"])
    again = seat_new_user(w.db, body["id"], w.org["a"], "user")
    assert again == first.team_id
    assert w.db(w.db.team_members.user_id == body["id"]).count() == 1


async def test_seating_happens_with_the_kill_switch_on(
    team_world, real_db_client, team_headers, monkeypatch
) -> None:
    """``disable-teams`` ON only hides the team features; new users are still seated."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", "1")
    w = team_world
    body = await _create_user(real_db_client, team_headers("boss"))
    row = _membership(w.db, body["id"])
    assert row is not None
    assert w.db.teams[row.team_id].name == DEFAULT_TEAM_NAME
