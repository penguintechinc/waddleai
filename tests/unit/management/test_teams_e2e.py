"""Teams end to end across both planes (teams spec §11 E2E).

Management HTTP (real Quart app, real signed JWTs) on one side; the proxy's REAL
``ApiKeyAuthenticator`` + ``ApiKeyAuthCache`` on the other; one real database and one shared
fake Valkey between them. No mocks of any code under test.
"""

import pytest

from proxy.apps.proxy_server.auth_cache import ApiKeyAuthCache, ApiKeyAuthenticator
from services.management.app.services.key_minting import mint_proxy_key
from shared.auth.rbac import Permission, RBACManager

P = Permission


@pytest.fixture
def proxy(team_world, shared_valkey) -> ApiKeyAuthenticator:
    """The proxy's cache-fronted authenticator over the shared database and Valkey."""
    cache = ApiKeyAuthCache(valkey=shared_valkey)
    return ApiKeyAuthenticator(rbac=RBACManager(team_world.db), cache=cache)


async def _create_key(client, headers, **extra) -> dict:
    """POST /keys and return the JSON body (asserting 201)."""
    resp = await client.post("/api/v1/keys", json={"name": "e2e", **extra}, headers=headers)
    assert resp.status_code == 201, await resp.get_data()
    return await resp.get_json()


async def test_lifecycle_membership_changes_reach_the_proxy(
    team_world, real_db_client, team_headers, proxy
) -> None:
    """Org admin builds a team; a member's keys act in it at the proxy.

    # regression: gh-303 (key works at the proxy) + D7 (every membership change is visible there
    once management invalidates): added -> promoted -> removed, same key throughout.
    """
    w = team_world
    boss, loner = team_headers("boss"), team_headers("loner")

    created = await real_db_client.post("/api/v1/teams", json={"name": "Ops"}, headers=boss)
    assert created.status_code == 201
    ops = (await created.get_json())["id"]

    # loner is in no team, so the API refuses to mint them a key (every key acts in a team).
    refused = await real_db_client.post("/api/v1/keys", json={"name": "e2e"}, headers=loner)
    assert refused.status_code == 409

    # A pre-existing teamless key (e.g. minted before they left their team) still works at the
    # proxy, teamless -- and is adopted when they join a team.
    legacy = mint_proxy_key(
        w.db, user_id=w.u["loner"], org_id=w.org["a"], name="old", owner_role="user",
        team_id=None, expires_at=None, allowed_models=None, allowed_providers=None,
        budget_limit_daily=None, budget_limit_monthly=None, tpm_limit=None, rpm_limit=None,
    )  # fmt: skip
    first = {"api_key": legacy.api_key}
    ctx = await proxy.authenticate(first["api_key"])
    assert (ctx.user_id, ctx.team_id) == (w.u["loner"], None)

    # Added to Ops as member: the SAME key now acts in Ops (adopted + cache invalidated).
    add = await real_db_client.post(
        f"/api/v1/teams/{ops}/members", json={"user_id": w.u["loner"], "team_role": "member"},
        headers=boss,
    )  # fmt: skip
    assert add.status_code == 201
    ctx = await proxy.authenticate(first["api_key"])
    assert (ctx.team_id, ctx.team_role) == (ops, "member")
    assert P.TEAM_USE in ctx.permissions

    # Promoted to admin: visible at the proxy at once.
    promote = await real_db_client.put(
        f"/api/v1/teams/{ops}/members/{w.u['loner']}", json={"team_role": "admin"}, headers=boss
    )
    assert promote.status_code == 200
    ctx = await proxy.authenticate(first["api_key"])
    assert ctx.team_role == "admin"
    assert P.TEAM_MEMBER_MANAGE in ctx.permissions

    # A key minted now defaults into Ops too.
    second = await _create_key(real_db_client, loner)
    assert second["team_id"] == ops
    assert (await proxy.authenticate(second["api_key"])).team_id == ops

    # Removed: the key still authenticates, but teamless -- no team scopes.
    gone = await real_db_client.delete(f"/api/v1/teams/{ops}/members/{w.u['loner']}", headers=boss)
    assert gone.status_code == 200
    ctx = await proxy.authenticate(first["api_key"])
    assert ctx.team_id is None
    assert P.TEAM_USE not in ctx.permissions


async def test_team_admin_manages_only_their_own_team(
    team_world, real_db_client, team_headers
) -> None:
    """Adam (admin of a1): may manage a1's membership; a2 is off limits; cannot delete a1."""
    w = team_world
    a1, a2 = f"/api/v1/teams/{w.t['a1']}", f"/api/v1/teams/{w.t['a2']}"
    adam = team_headers("adam")

    ok = await real_db_client.post(
        f"{a1}/members", json={"user_id": w.u["loner"], "team_role": "viewer"}, headers=adam
    )
    cross_read = await real_db_client.get(a2, headers=adam)
    cross_add = await real_db_client.post(
        f"{a2}/members", json={"user_id": w.u["boss"], "team_role": "member"}, headers=adam
    )
    delete_own = await real_db_client.delete(a1, headers=adam)

    assert (ok.status_code, cross_read.status_code, cross_add.status_code) == (201, 403, 403)
    assert delete_own.status_code == 403


async def test_member_and_viewer_cannot_mutate_anything(
    team_world, real_db_client, team_headers
) -> None:
    """Mia (member) and vic (viewer) can read their team but every mutation is a 403."""
    w = team_world
    base = f"/api/v1/teams/{w.t['a1']}"
    for who in ("mia", "vic"):
        hdr = team_headers(who)
        assert (await real_db_client.get(base, headers=hdr)).status_code == 200
        assert (await real_db_client.put(base, json={"name": "x"}, headers=hdr)).status_code == 403
        assert (await real_db_client.delete(base, headers=hdr)).status_code == 403
        add = await real_db_client.post(
            f"{base}/members", json={"user_id": w.u["loner"], "team_role": "member"}, headers=hdr
        )
        assert add.status_code == 403
    # Only the member can spend: viewer holds no team:key_create, so naming the team is refused.
    named = {"name": "k", "team_id": w.t["a1"]}
    assert (
        await real_db_client.post("/api/v1/keys", json=named, headers=team_headers("vic"))
    ).status_code == 403
    assert (
        await real_db_client.post("/api/v1/keys", json=named, headers=team_headers("mia"))
    ).status_code == 201


async def test_cross_team_member_cannot_issue_or_read_the_other_team(
    team_world, real_db_client, team_headers
) -> None:
    """Nat (member of a2) gets 403 reading a1, listing its members, and issuing a key into it."""
    w = team_world
    nat = team_headers("nat")
    base = f"/api/v1/teams/{w.t['a1']}"
    assert (await real_db_client.get(base, headers=nat)).status_code == 403
    assert (await real_db_client.get(f"{base}/members", headers=nat)).status_code == 403
    issue = await real_db_client.post(
        "/api/v1/keys", json={"name": "k", "team_id": w.t["a1"]}, headers=nat
    )
    assert issue.status_code == 403


async def test_kill_switch_flip_makes_the_proxy_teamless_after_invalidation(
    team_world, real_db_client, team_headers, proxy, monkeypatch
) -> None:
    """Review focus: turning ``waddleai.disable-teams`` ON stops team context at the proxy.

    The auth cache bakes the resolved team, so the flip takes effect when each entry is dropped
    (TTL, or an explicit invalidation as here) -- the key itself keeps authenticating.
    """
    from services.management.app.services.auth_cache_invalidation import (
        invalidate_user_auth_cache,
    )

    w = team_world
    body = await _create_key(real_db_client, team_headers("mia"))
    assert (await proxy.authenticate(body["api_key"])).team_id == w.t["a1"]

    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", "1")
    await invalidate_user_auth_cache(w.db, [w.u["mia"]])

    ctx = await proxy.authenticate(body["api_key"])
    assert ctx.team_id is None
    assert ctx.user_id == w.u["mia"]
