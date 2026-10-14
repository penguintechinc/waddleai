"""Teams HTTP API, membership and ownership (teams spec §5, §10; D2/D7).

Complements ``test_teams_routes.py`` (CRUD). Also owns the blueprint-wide route inventory: a new
team route must be added to ``EXPECTED_ROUTES`` here, which forces an authorization test for it.
"""

import pytest

from shared.auth.rbac import auth_cache_key

EXPECTED_ROUTES = {
    ("POST", "/api/v1/teams"),
    ("GET", "/api/v1/teams"),
    ("GET", "/api/v1/teams/<int:team_id>"),
    ("PUT", "/api/v1/teams/<int:team_id>"),
    ("DELETE", "/api/v1/teams/<int:team_id>"),
    ("GET", "/api/v1/teams/<int:team_id>/members"),
    ("POST", "/api/v1/teams/<int:team_id>/members"),
    ("PUT", "/api/v1/teams/<int:team_id>/members/<int:user_id>"),
    ("DELETE", "/api/v1/teams/<int:team_id>/members/<int:user_id>"),
    ("POST", "/api/v1/teams/<int:team_id>/transfer-ownership"),
}
MEMBER_ROUTES = sorted(r for r in EXPECTED_ROUTES if "members" in r[1] or "transfer" in r[1])


def _path(rule: str, w, team: str = "a1", user: str = "mia") -> str:
    """Fill a rule's converters with the given team and user."""
    return rule.replace("<int:team_id>", str(w.t[team])).replace("<int:user_id>", str(w.u[user]))


def test_blueprint_exposes_exactly_the_expected_team_routes(flask_app) -> None:
    """Inventory guard: no team route ships without being listed (and so tested) here."""
    found = {
        (m, rule.rule)
        for rule in flask_app.url_map.iter_rules()
        if rule.rule.startswith("/api/v1/teams")
        for m in (rule.methods or set()) - {"HEAD", "OPTIONS"}
    }
    assert found == EXPECTED_ROUTES


@pytest.mark.parametrize(("method", "rule"), MEMBER_ROUTES)
async def test_member_routes_require_authentication(real_db_client, team_world, method, rule):
    """No credential -> 401."""
    resp = await real_db_client.open(_path(rule, team_world), method=method, json={})
    assert resp.status_code == 401


@pytest.mark.parametrize(("method", "rule"), MEMBER_ROUTES)
async def test_member_routes_refuse_a_teamless_user(
    team_world, real_db_client, team_headers, method, rule
):
    """A signed-in org user with no team gets 403 on every membership route."""
    resp = await real_db_client.open(
        _path(rule, team_world), method=method,
        json={"user_id": team_world.u["loner"], "team_role": "member", "new_owner_user_id": 1},
        headers=team_headers("loner"),
    )  # fmt: skip
    assert resp.status_code == 403, f"{method} {rule}"


@pytest.mark.parametrize(("method", "rule"), MEMBER_ROUTES)
async def test_member_routes_hide_another_tenants_team(
    team_world, real_db_client, team_headers, method, rule
):
    """Org-a's admin addressing org b's team gets 404 on every membership route."""
    resp = await real_db_client.open(
        _path(rule, team_world, team="b1", user="carl"), method=method,
        json={"user_id": team_world.u["loner"], "team_role": "member", "new_owner_user_id": 1},
        headers=team_headers("boss"),
    )  # fmt: skip
    assert resp.status_code == 404, f"{method} {rule}"


async def test_member_list_exposes_ids_and_roles_only(team_world, real_db_client, team_headers):
    """No PII: every entry has exactly user_id/team_role/created_at."""
    resp = await real_db_client.get(
        f"/api/v1/teams/{team_world.t['a1']}/members", headers=team_headers("vic")
    )
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["total"] == 4
    assert all(set(m) == {"user_id", "team_role", "created_at"} for m in body["members"])


@pytest.mark.parametrize(
    ("actor", "target", "role", "status"),
    [
        ("adam", "loner", "member", 201),  # team admin adds an unassigned org user
        ("mia", "loner", "member", 403),   # a member cannot manage membership
        ("adam", "nat", "member", 409),    # D2: nat already has a team (a2) in this org
        ("adam", "loner", "owner", 403),   # an admin cannot mint an owner
        ("boss", "loner", "owner", 201),   # the org admin can
        ("adam", "carl", "member", 400),   # a user from another tenant
        ("adam", "loner", "root", 400),    # unknown role
    ],
)  # fmt: skip
async def test_add_member_matrix(
    team_world, real_db_client, team_headers, actor, target, role, status
):
    """POST /members: who may add whom, with the D2 one-team rule and the owner guard."""
    w = team_world
    resp = await real_db_client.post(
        f"/api/v1/teams/{w.t['a1']}/members",
        json={"user_id": w.u[target], "team_role": role}, headers=team_headers(actor),
    )  # fmt: skip
    assert resp.status_code == status, await resp.get_data()


async def test_add_member_requires_user_id(team_world, real_db_client, team_headers):
    """A body without user_id is a 400, not a 500."""
    resp = await real_db_client.post(
        f"/api/v1/teams/{team_world.t['a1']}/members", json={}, headers=team_headers("adam")
    )
    assert resp.status_code == 400


async def test_membership_change_invalidates_the_affected_users_cache(
    team_world, real_db_client, team_headers, redis_spy
):
    """Review focus (D7): adding loner (who owns key ``kl``) deletes exactly kl's cache entry."""
    w = team_world
    w.db.api_keys.insert(
        key_id="kl", key_hash="h", user_id=w.u["loner"], organization_id=w.org["a"], name="k",
        enabled=True,
    )  # fmt: skip
    w.db.commit()
    resp = await real_db_client.post(
        f"/api/v1/teams/{w.t['a1']}/members",
        json={"user_id": w.u["loner"], "team_role": "viewer"}, headers=team_headers("adam"),
    )  # fmt: skip
    assert resp.status_code == 201
    redis_spy.delete.assert_called_once_with(auth_cache_key("kl"))


@pytest.mark.parametrize(
    ("actor", "target", "role", "status"),
    [
        ("adam", "mia", "admin", 200),      # a team admin promotes a member
        ("adam", "olive", "member", 403),   # ...but cannot touch the owner
        ("adam", "mia", "owner", 403),      # ...or mint one
        ("olive", "mia", "owner", 200),     # an owner can grant owner
        ("olive", "olive", "member", 409),  # the sole owner cannot self-demote
        ("mia", "vic", "member", 403),      # a member cannot manage roles
        ("adam", "nat", "member", 404),     # target is not in this team
    ],
)  # fmt: skip
async def test_set_role_matrix(
    team_world, real_db_client, team_headers, actor, target, role, status
):
    """PUT /members/<uid>: role-edit authorization, the owner guard and the last-owner rule."""
    w = team_world
    resp = await real_db_client.put(
        f"/api/v1/teams/{w.t['a1']}/members/{w.u[target]}",
        json={"team_role": role}, headers=team_headers(actor),
    )  # fmt: skip
    assert resp.status_code == status, await resp.get_data()


@pytest.mark.parametrize(
    ("actor", "target", "status"),
    [("adam", "vic", 200), ("adam", "olive", 403), ("olive", "olive", 409), ("mia", "vic", 403)],
)
async def test_remove_member_matrix(
    team_world, real_db_client, team_headers, actor, target, status
):
    """DELETE /members/<uid>: admin removes a viewer; only an owner may touch the sole owner."""
    w = team_world
    resp = await real_db_client.delete(
        f"/api/v1/teams/{w.t['a1']}/members/{w.u[target]}", headers=team_headers(actor)
    )
    assert resp.status_code == status


async def test_transfer_ownership(team_world, real_db_client, team_headers):
    """Owner transfers to adam: adam owns, olive is admin; a team admin cannot transfer."""
    w = team_world
    url = f"/api/v1/teams/{w.t['a1']}/transfer-ownership"
    body = {"new_owner_user_id": w.u["adam"]}
    denied = await real_db_client.post(url, json=body, headers=team_headers("adam"))
    done = await real_db_client.post(url, json=body, headers=team_headers("olive"))
    assert (denied.status_code, done.status_code) == (403, 200)
    roles = {r.user_id: r.team_role for r in w.db(w.db.team_members.team_id == w.t["a1"]).select()}
    assert (roles[w.u["adam"]], roles[w.u["olive"]]) == ("owner", "admin")


async def test_transfer_requires_the_new_owner_id(team_world, real_db_client, team_headers):
    """A transfer body without new_owner_user_id is a 400."""
    resp = await real_db_client.post(
        f"/api/v1/teams/{team_world.t['a1']}/transfer-ownership", json={},
        headers=team_headers("olive"),
    )  # fmt: skip
    assert resp.status_code == 400


async def test_kill_switch_turns_member_routes_off(
    team_world, real_db_client, team_headers, monkeypatch
):
    """``waddleai.disable-teams`` ON: membership routes answer 404 for everyone."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", "1")
    resp = await real_db_client.get(
        f"/api/v1/teams/{team_world.t['a1']}/members", headers=team_headers("boss")
    )
    assert resp.status_code == 404


@pytest.mark.parametrize(
    ("verb", "actor", "target", "body"),
    [
        ("put", "adam", "mia", {"team_role": "admin"}),
        ("delete", "adam", "vic", None),
    ],
)
async def test_role_change_and_removal_invalidate_the_affected_users_cache(
    team_world, real_db_client, team_headers, redis_spy, verb, actor, target, body
):
    """Set-role and remove both drop the target's cached proxy auth entry."""
    w = team_world
    w.db.api_keys.insert(
        key_id="kt", key_hash="h", user_id=w.u[target], organization_id=w.org["a"], name="k",
        enabled=True,
    )  # fmt: skip
    w.db.commit()
    kwargs = {"json": body} if body is not None else {}
    resp = await getattr(real_db_client, verb)(
        f"/api/v1/teams/{w.t['a1']}/members/{w.u[target]}", headers=team_headers(actor), **kwargs
    )
    assert resp.status_code == 200
    redis_spy.delete.assert_any_call(auth_cache_key("kt"))


async def test_transfer_invalidates_both_owners_caches(
    team_world, real_db_client, team_headers, redis_spy
):
    """Transfer changes two users' acting role: both keys' cache entries are deleted."""
    w = team_world
    for kid, who in (("ko", "olive"), ("ka", "adam")):
        w.db.api_keys.insert(
            key_id=kid, key_hash="h", user_id=w.u[who], organization_id=w.org["a"], name="k",
            enabled=True,
        )  # fmt: skip
    w.db.commit()
    resp = await real_db_client.post(
        f"/api/v1/teams/{w.t['a1']}/transfer-ownership",
        json={"new_owner_user_id": w.u["adam"]}, headers=team_headers("olive"),
    )  # fmt: skip
    assert resp.status_code == 200
    deleted = {a for c in redis_spy.delete.call_args_list for a in c.args}
    assert {auth_cache_key("ko"), auth_cache_key("ka")} <= deleted


async def test_demoted_owners_stale_token_cannot_transfer_or_manage_owners(
    team_world, real_db_client, team_headers
):
    """Authority is the LIVE row: olive's pre-demotion token loses owner powers at once."""
    w = team_world
    stale_headers = team_headers("olive")  # minted while olive is still the owner
    w.db(
        (w.db.team_members.team_id == w.t["a1"]) & (w.db.team_members.user_id == w.u["olive"])
    ).update(team_role="member")
    w.db.commit()
    base = f"/api/v1/teams/{w.t['a1']}"
    transfer = await real_db_client.post(
        f"{base}/transfer-ownership", json={"new_owner_user_id": w.u["mia"]}, headers=stale_headers
    )
    grant = await real_db_client.put(
        f"{base}/members/{w.u['mia']}", json={"team_role": "owner"}, headers=stale_headers
    )
    assert (transfer.status_code, grant.status_code) == (403, 403)
