"""Management token mint/decode carry the acting team (teams spec D3/D6/D7).

Real database (``team_world``), real signed tokens: login, refresh and key-exchange each
re-resolve the live team, and ``_decode_token`` hands ``g.user`` the team context.
"""

import jwt as _jwt
from passlib.hash import bcrypt

from services.management.app.api.v1 import auth as auth_mod
from shared.auth.rbac import Permission

P = Permission


def _claims(token: str) -> dict:
    """Decode a token's claims without verifying (the signature is checked by the app)."""
    return _jwt.decode(token, options={"verify_signature": False})


def _login_user(world, name: str, org: str, team_key: str | None, team_role: str | None) -> None:
    """Insert a user with a real bcrypt password (and optional team membership)."""
    db = world.db
    uid = db.users.insert(
        username=name, email=f"{name}@example.com", password_hash=bcrypt.hash("pw-12345-xyz"),
        role="user", enabled=True, organization_id=world.org[org],
    )  # fmt: skip
    if team_key:
        db.team_members.insert(
            team_id=world.t[team_key], user_id=uid, organization_id=world.org[org],
            team_role=team_role,
        )  # fmt: skip
    db.commit()


async def _login(client, name: str) -> dict:
    """POST /auth/login and return the JSON body (asserting success)."""
    resp = await client.post(
        "/api/v1/auth/login", json={"username": name, "password": "pw-12345-xyz"}
    )
    assert resp.status_code == 200, await resp.get_data()
    return await resp.get_json()


def test_issue_access_token_unions_team_scopes_and_sets_ext() -> None:
    """A team role adds its scopes and rides in ``ext``; the org bundle is unchanged."""
    issued = auth_mod.issue_access_token(
        user_id=1, username="u", role="user", organization_id=1, team_id=7, team_role="admin"
    )
    claims = _claims(issued.access_token)
    assert claims["ext"]["team_id"] == "7"
    assert claims["ext"]["team_role"] == "admin"
    assert {P.TEAM_ADMIN.value, P.TEAM_MEMBER_MANAGE.value, P.TEAM_USE.value} <= set(
        claims["scope"]
    )
    assert P.TEAM_DELETE.value not in claims["scope"]


def test_teamless_token_has_no_team_scopes() -> None:
    """No acting team -> no ``team:*`` scope and null ext team fields."""
    claims = _claims(auth_mod.issue_access_token(1, "u", "user", 1).access_token)
    assert claims["ext"]["team_id"] is None
    assert not [s for s in claims["scope"] if s.startswith("team:")]


def test_decode_token_exposes_team_context() -> None:
    """``g.user`` (built from ``_decode_token``) carries team_id/team_role for downstream use."""
    token = auth_mod.issue_access_token(1, "u", "user", 1, team_id=7, team_role="viewer")
    payload = auth_mod._decode_token(token.access_token)
    assert payload is not None
    assert (payload["team_id"], payload["team_role"]) == (7, "viewer")


async def test_acting_team_resolves_live_membership(team_world, real_db_client) -> None:
    """``_acting_team`` reads the DB: mia -> (a1, member); loner -> teamless."""
    org = team_world.org["a"]
    assert await auth_mod._acting_team(team_world.u["mia"], org) == (team_world.t["a1"], "member")
    assert await auth_mod._acting_team(team_world.u["loner"], org) == (None, None)


async def test_login_mints_the_users_team_into_the_token(team_world, real_db_client) -> None:
    """A team member's login token carries ext.team_* and the team role's scopes."""
    _login_user(team_world, "tina", "a", "a1", "admin")
    body = await _login(real_db_client, "tina")
    claims = _claims(body["access_token"])
    assert claims["ext"]["team_id"] == str(team_world.t["a1"])
    assert claims["ext"]["team_role"] == "admin"
    assert P.TEAM_MEMBER_MANAGE.value in claims["scope"]


async def test_refresh_picks_up_a_membership_change(team_world, real_db_client) -> None:
    """Review focus: a refresh after a demotion re-resolves the live role, not the old token's."""
    _login_user(team_world, "tina", "a", "a1", "admin")
    body = await _login(real_db_client, "tina")
    db = team_world.db
    uid = db(db.users.username == "tina").select().first().id
    db(db.team_members.user_id == uid).update(team_role="viewer")
    db.commit()

    resp = await real_db_client.post(
        "/api/v1/auth/refresh", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert resp.status_code == 200
    claims = _claims((await resp.get_json())["access_token"])
    assert claims["ext"]["team_role"] == "viewer"
    assert P.TEAM_MEMBER_MANAGE.value not in claims["scope"]
    assert P.TEAM_READ.value in claims["scope"]


async def test_kill_switch_login_is_teamless(team_world, real_db_client, monkeypatch) -> None:
    """``waddleai.disable-teams`` ON: login still works and mints no team context."""
    _login_user(team_world, "tina", "a", "a1", "admin")
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", "1")
    claims = _claims((await _login(real_db_client, "tina"))["access_token"])
    assert claims["ext"]["team_id"] is None
    assert not [s for s in claims["scope"] if s.startswith("team:")]
