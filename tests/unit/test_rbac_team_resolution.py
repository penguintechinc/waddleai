"""Acting-team resolution into ``UserContext`` (teams spec D3/D5): keys and logins.

``lookup_key_team`` / ``lookup_user_team`` resolve (team_id, team_role) from the LIVE
``team_members`` row; ``build_user_context`` expands the team role into scopes. Real schema,
no mocks (``team_world``).
"""

import pytest
from passlib.hash import bcrypt

from shared.auth.rbac import (
    ROLE_PERMISSIONS,
    Permission,
    RBACManager,
    Role,
    lookup_key_team,
    lookup_user_team,
)

P = Permission
_SECRET = "wa-k1-secretvalue"  # noqa: S105 -- test fixture credential
_HASH = bcrypt.hash(_SECRET)


def _insert_key(world, user: str, team_key: str | None) -> None:
    """Insert an ``api_keys`` row ``k1`` for ``user`` bound to ``team_key`` (or NULL)."""
    db = world.db
    org = db.users[world.u[user]].organization_id
    db.api_keys.insert(
        key_id="k1", key_hash=_HASH, user_id=world.u[user], organization_id=org, name="k",
        enabled=True, team_id=world.t[team_key] if team_key else None,
    )  # fmt: skip
    db.commit()


def test_key_bound_to_the_users_team_resolves_with_the_live_role(team_world) -> None:
    """Mia's key pinned to a1 authenticates as member of a1 with exactly the member scopes."""
    _insert_key(team_world, "mia", "a1")
    ctx = RBACManager(team_world.db).authenticate_api_key(_SECRET)
    assert (ctx.team_id, ctx.team_role) == (team_world.t["a1"], "member")
    assert {P.TEAM_USE, P.TEAM_KEY_CREATE, P.TEAM_READ} <= ctx.permissions
    assert P.TEAM_ADMIN not in ctx.permissions


def test_key_without_a_team_is_teamless_even_for_a_team_member(team_world) -> None:
    """D3: ``api_keys.team_id`` names the acting team; NULL means no team context at all."""
    _insert_key(team_world, "mia", None)
    ctx = RBACManager(team_world.db).authenticate_api_key(_SECRET)
    assert (ctx.team_id, ctx.team_role) == (None, None)
    assert not any(p.value.startswith("team:") for p in ctx.permissions)


def test_key_pointing_at_a_team_the_user_is_not_in_is_teamless(team_world) -> None:
    """A stale/forged ``team_id`` (nat's team is a2, key says a1) never grants a1 context."""
    _insert_key(team_world, "nat", "a1")
    ctx = RBACManager(team_world.db).authenticate_api_key(_SECRET)
    assert (ctx.team_id, ctx.team_role) == (None, None)


def test_key_whose_owner_was_removed_from_the_team_is_teamless(team_world) -> None:
    """Membership removal is honoured even though the key row still names the team."""
    _insert_key(team_world, "mia", "a1")
    db = team_world.db
    db(db.team_members.user_id == team_world.u["mia"]).delete()
    db.commit()
    ctx = RBACManager(db).authenticate_api_key(_SECRET)
    assert ctx.team_id is None


def test_disabled_team_yields_no_team_context(team_world) -> None:
    """Members of a disabled team act teamless at the proxy."""
    _insert_key(team_world, "mia", "a1")
    db = team_world.db
    db(db.teams.id == team_world.t["a1"]).update(enabled=False)
    db.commit()
    assert RBACManager(db).authenticate_api_key(_SECRET).team_id is None


def test_kill_switch_makes_keys_teamless(team_world, monkeypatch) -> None:
    """``waddleai.disable-teams`` ON: no acting team, no team scopes, auth still succeeds."""
    _insert_key(team_world, "mia", "a1")
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", "1")
    ctx = RBACManager(team_world.db).authenticate_api_key(_SECRET)
    assert ctx.team_id is None
    assert ctx.permissions == ROLE_PERMISSIONS[Role.USER]


def test_login_path_resolves_the_users_single_team(team_world) -> None:
    """No key on a login/JWT: the (user, tenant) pair resolves to its one team, or none."""
    w = team_world
    assert lookup_user_team(w.db, w.u["adam"], w.org["a"]) == (w.t["a1"], "admin")
    assert lookup_user_team(w.db, w.u["loner"], w.org["a"]) == (None, None)
    assert lookup_user_team(w.db, w.u["adam"], w.org["b"]) == (None, None)


def test_key_lookup_requires_the_keys_tenant_to_match(team_world) -> None:
    """A key claiming org b for an org-a member finds no membership there."""
    w = team_world
    assert lookup_key_team(w.db, w.u["mia"], w.org["b"], w.t["a1"]) == (None, None)


def test_build_user_context_unions_org_and_team_scopes_without_mutating_bundles(
    team_world,
) -> None:
    """Org bundle ∪ team bundle; the shared ROLE_PERMISSIONS set must stay untouched."""
    before = set(ROLE_PERMISSIONS[Role.USER])
    ctx = RBACManager(team_world.db).build_user_context(
        user_id=1, username="u", role="user", organization_id=1, managed_orgs=[],
        team_id=7, team_role="owner",
    )  # fmt: skip
    assert ctx.permissions == before | {
        P.TEAM_READ, P.TEAM_USE, P.TEAM_KEY_CREATE, P.TEAM_KEY_ADMIN, P.TEAM_MEMBER_MANAGE,
        P.TEAM_ADMIN, P.TEAM_DELETE, P.TEAM_TRANSFER_OWNER,
    }  # fmt: skip
    assert (ctx.team_id, ctx.team_role) == (7, "owner")
    assert ROLE_PERMISSIONS[Role.USER] == before


def test_unrecognized_team_role_string_grants_no_team_scopes() -> None:
    """A corrupt ``team_role`` value keeps the team id but expands to nothing (fail closed)."""
    ctx = RBACManager(db=None).build_user_context(
        user_id=1, username="u", role="user", organization_id=1, managed_orgs=[],
        team_id=7, team_role="bogus",
    )  # fmt: skip
    assert ctx.permissions == ROLE_PERMISSIONS[Role.USER]


@pytest.mark.parametrize("role", ["admin", "reporter"])
def test_team_scopes_union_with_any_org_role(role) -> None:
    """The union applies to every org role, not just USER."""
    ctx = RBACManager(db=None).build_user_context(
        user_id=1, username="u", role=role, organization_id=1, managed_orgs=[],
        team_id=7, team_role="viewer",
    )  # fmt: skip
    assert P.TEAM_READ in ctx.permissions
    assert ROLE_PERMISSIONS[Role(role)] <= ctx.permissions
