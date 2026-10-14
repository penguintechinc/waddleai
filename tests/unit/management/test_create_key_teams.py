"""POST /api/v1/keys mints a proxy-authenticable, team-bound key (issue #303; teams spec §7).

HTTP-level against a real database (``team_world`` + ``real_db_client``): the response's
credential is then fed to the proxy's REAL ``RBACManager.authenticate_api_key``.
"""

import pytest

from shared.auth.rbac import RBACManager


def _hdr(mint_token, w, user: str, role: str = "user", org: str = "a", team: str | None = None):
    """Authorization header for ``user`` (token carries their team context when ``team`` set)."""
    team_role = {
        "olive": "owner", "adam": "admin", "mia": "member", "vic": "viewer", "nat": "member",
    }.get(user)  # fmt: skip
    token = mint_token(
        role=role, user_id=w.u[user], org_id=w.org[org], team_id=w.t[team] if team else None,
        team_role=team_role if team else None,
    )  # fmt: skip
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def test_gh303_created_key_authenticates_at_the_proxy_with_the_default_team(
    team_world, real_db_client, mint_token
) -> None:
    """# regression: gh-303 -- a key from the management endpoint works at the proxy, in a1."""
    resp = await real_db_client.post(
        "/api/v1/keys", json={"name": "ci"}, headers=_hdr(mint_token, team_world, "mia", team="a1")
    )
    assert resp.status_code == 201
    body = await resp.get_json()
    assert body["team_id"] == team_world.t["a1"]
    assert body["api_key"].startswith("wa-")
    assert body["key_prefix"] == body["api_key"][:11] + "..."

    ctx = RBACManager(team_world.db).authenticate_api_key(body["api_key"])
    assert ctx.user_id == team_world.u["mia"]
    assert (ctx.team_id, ctx.team_role) == (team_world.t["a1"], "member")


async def test_user_in_no_team_cannot_create_a_key(team_world, real_db_client, mint_token) -> None:
    """Loner is in no team: key creation is refused (409) and nothing is minted."""
    before = team_world.db(team_world.db.api_keys.id > 0).count()
    resp = await real_db_client.post(
        "/api/v1/keys", json={"name": "ci"}, headers=_hdr(mint_token, team_world, "loner")
    )
    assert resp.status_code == 409
    assert team_world.db(team_world.db.api_keys.id > 0).count() == before


@pytest.mark.parametrize(
    ("caller", "caller_team", "target", "team", "status"),
    [
        ("mia", "a1", None, "a1", 201),     # member, own key, own team
        ("vic", "a1", None, "a1", 403),     # viewer: no team:key_create
        ("nat", "a2", None, "a1", 403),     # cross-team: member of a2 asking for a1
        ("mia", "a1", "vic", "a1", 403),    # member issuing for a teammate: needs key_admin
        ("adam", "a1", "vic", "a1", 201),   # team admin issuing for a teammate
        ("adam", "a1", "nat", "a1", 400),   # target not a member of the named team
        ("mia", "a1", None, "b1", 404),     # another tenant's team: no existence leak
    ],
)  # fmt: skip
async def test_explicit_team_authorization_matrix(
    team_world, real_db_client, mint_token, caller, caller_team, target, team, status
) -> None:
    """Who may bind a key to which team (spec §5 role bundles, enforced live)."""
    w = team_world
    payload = {"name": "k", "team_id": w.t[team]}
    if target:
        payload["user_id"] = w.u[target]
        payload["organization_id"] = w.org["a"]
    resp = await real_db_client.post(
        "/api/v1/keys", json=payload, headers=_hdr(mint_token, w, caller, team=caller_team)
    )
    assert resp.status_code == status, await resp.get_data()


async def test_org_admin_may_bind_any_members_key(team_world, real_db_client, mint_token) -> None:
    """Boss (org admin) issues a key for mia into a1 without being a member."""
    w = team_world
    resp = await real_db_client.post(
        "/api/v1/keys",
        json={"name": "k", "user_id": w.u["mia"], "organization_id": w.org["a"],
              "team_id": w.t["a1"]},
        headers=_hdr(mint_token, w, "boss", role="admin"),
    )  # fmt: skip
    assert resp.status_code == 201
    assert (await resp.get_json())["team_id"] == w.t["a1"]


async def test_unauthorized_caller_cannot_probe_user_ids(
    team_world, real_db_client, mint_token
) -> None:
    """A plain member naming a team gets 403 for a real AND a nonexistent target (no oracle)."""
    w = team_world
    codes = set()
    for target in (w.u["carl"], 99999):
        resp = await real_db_client.post(
            "/api/v1/keys",
            json={"name": "k", "user_id": target, "organization_id": w.org["a"],
                  "team_id": w.t["a1"]},
            headers=_hdr(mint_token, w, "mia", team="a1"),
        )  # fmt: skip
        codes.add(resp.status_code)
    assert codes == {403}


async def test_kill_switch_rejects_an_explicit_team_and_mints_teamless(
    team_world, real_db_client, mint_token, monkeypatch
) -> None:
    """``waddleai.disable-teams`` ON: naming a team is a 404; default keys carry no team."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", "1")
    hdr = _hdr(mint_token, team_world, "mia")
    bad = await real_db_client.post(
        "/api/v1/keys", json={"name": "k", "team_id": team_world.t["a1"]}, headers=hdr
    )
    ok = await real_db_client.post("/api/v1/keys", json={"name": "k"}, headers=hdr)
    assert bad.status_code == 404
    assert ok.status_code == 201
    assert (await ok.get_json())["team_id"] is None


@pytest.mark.parametrize("target_role", ["reporter", "resource_manager"])
async def test_team_admin_cannot_mint_above_their_org_role(
    team_world, real_db_client, mint_token, target_role
) -> None:
    """A team key_admin (org role ``user``) may not mint a key for a higher-org-role teammate."""
    w = team_world
    w.db(w.db.users.id == w.u["mia"]).update(role=target_role)
    resp = await real_db_client.post(
        "/api/v1/keys",
        json={"name": "k", "user_id": w.u["mia"], "organization_id": w.org["a"],
              "team_id": w.t["a1"]},
        headers=_hdr(mint_token, w, "adam", team="a1"),
    )  # fmt: skip
    assert resp.status_code == 403, await resp.get_data()


@pytest.mark.parametrize(
    ("role", "target", "status"),
    [
        ("admin", "mia", 201),               # admin: same-org other user
        ("admin", "carl", 201),              # admin: cross-org (apikey:admin bypass)
        ("resource_manager", "mia", 201),    # RM: same-org other user
        ("resource_manager", "carl", 403),   # RM: never cross-org
        ("user", "mia", 403),                # plain user: no teamless cross-user issuance
        ("user", "carl", 403),
    ],
)  # fmt: skip
async def test_cross_user_create_gate_outcomes_are_scope_derived(
    team_world, real_db_client, mint_token, role, target, status
) -> None:
    """Cross-org/cross-user create gate allow/deny matrix (same outcomes as the old role gate)."""
    w = team_world
    target_org = "b" if target == "carl" else "a"
    resp = await real_db_client.post(
        "/api/v1/keys",
        json={"name": "k", "user_id": w.u[target], "organization_id": w.org[target_org]},
        headers=_hdr(mint_token, w, "boss", role=role),
    )
    assert resp.status_code == status, await resp.get_data()
