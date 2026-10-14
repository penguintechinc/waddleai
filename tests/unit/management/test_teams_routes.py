"""Teams HTTP API, team CRUD (teams spec §10): create, list, get, update, delete.

Real database (``team_world``) behind the real Quart app (``real_db_client``); tokens are real
signed JWTs from ``team_headers``. Team-layer routes authorize on LIVE membership.
"""

import pytest

from shared.auth.rbac import auth_cache_key

#: The CRUD (method, rule) pairs this module covers; the member routes are covered (and the full
#: blueprint inventory guarded) in ``test_teams_member_routes.py``.
CRUD_ROUTES = {
    ("POST", "/api/v1/teams"),
    ("GET", "/api/v1/teams"),
    ("GET", "/api/v1/teams/<int:team_id>"),
    ("PUT", "/api/v1/teams/<int:team_id>"),
    ("DELETE", "/api/v1/teams/<int:team_id>"),
}
_TEAM_SCOPED = sorted(CRUD_ROUTES - {("POST", "/api/v1/teams"), ("GET", "/api/v1/teams")})


def _path(rule: str, w) -> str:
    """Fill a rule's converters with org a's team a1 / user mia."""
    return rule.replace("<int:team_id>", str(w.t["a1"])).replace("<int:user_id>", str(w.u["mia"]))


@pytest.mark.parametrize(("method", "rule"), sorted(CRUD_ROUTES))
async def test_crud_routes_require_authentication(real_db_client, team_world, method, rule) -> None:
    """No credential -> 401 (deny by default)."""
    resp = await real_db_client.open(_path(rule, team_world), method=method, json={})
    assert resp.status_code == 401


@pytest.mark.parametrize(("method", "rule"), sorted(CRUD_ROUTES - {("GET", "/api/v1/teams")}))
async def test_crud_routes_refuse_a_teamless_user(
    team_world, real_db_client, team_headers, method, rule
) -> None:
    """A signed-in org user with no team gets 403 (GET /teams is filtered, not refused)."""
    resp = await real_db_client.open(
        _path(rule, team_world), method=method, json={"name": "x"}, headers=team_headers("loner")
    )
    assert resp.status_code == 403, f"{method} {rule}"


@pytest.mark.parametrize(("method", "rule"), _TEAM_SCOPED)
async def test_team_routes_hide_another_tenants_team(
    team_world, real_db_client, team_headers, method, rule
) -> None:
    """The org-a admin addressing org b's team gets 404 -- tenant isolation, no id leak."""
    w = team_world
    path = rule.replace("<int:team_id>", str(w.t["b1"]))
    resp = await real_db_client.open(
        path, method=method, json={"name": "x"}, headers=team_headers("boss")
    )
    assert resp.status_code == 404, f"{method} {rule}"


async def test_org_admin_creates_a_team_in_its_own_tenant(team_world, real_db_client, team_headers):
    """POST /teams: 201, and the row lands in the caller's org (tenant is never from the body)."""
    w = team_world
    resp = await real_db_client.post(
        "/api/v1/teams", json={"name": " Ops "}, headers=team_headers("boss")
    )
    assert resp.status_code == 201
    body = await resp.get_json()
    assert w.db.teams[body["id"]].organization_id == w.org["a"]
    assert body["name"] == "Ops"


async def test_create_requires_the_org_admin_scope(real_db_client, team_headers):
    """Even a team Owner cannot create teams: that is an org-layer scope."""
    resp = await real_db_client.post(
        "/api/v1/teams", json={"name": "Ops"}, headers=team_headers("olive")
    )
    assert resp.status_code == 403


@pytest.mark.parametrize(
    ("payload", "status"), [({"name": "Alpha"}, 409), ({"name": " "}, 400), ({}, 400)]
)
async def test_create_validates_and_rejects_duplicates(
    real_db_client, team_headers, payload, status
):
    """Duplicate name in the org -> 409; blank or missing name -> 400."""
    resp = await real_db_client.post("/api/v1/teams", json=payload, headers=team_headers("boss"))
    assert resp.status_code == status


async def test_list_is_filtered_to_what_the_caller_may_see(real_db_client, team_headers):
    """Org admin sees both org-a teams (not b's); a member only theirs; a teamless user none."""

    async def names(user):
        resp = await real_db_client.get("/api/v1/teams", headers=team_headers(user))
        assert resp.status_code == 200
        return sorted(t["name"] for t in (await resp.get_json())["teams"])

    assert await names("boss") == ["Alpha", "Beta"]
    assert await names("mia") == ["Alpha"]
    assert await names("loner") == []


async def test_get_team_allows_viewers_and_hides_other_teams(
    team_world, real_db_client, team_headers
):
    """Viewer reads own team (with their role); a member of another team is refused."""
    url = f"/api/v1/teams/{team_world.t['a1']}"
    ok = await real_db_client.get(url, headers=team_headers("vic"))
    assert ok.status_code == 200
    body = await ok.get_json()
    assert (body["your_team_role"], body["member_count"]) == ("viewer", 4)
    assert set(body["team"]) == {"id", "organization_id", "name", "enabled", "created_at"}
    assert (await real_db_client.get(url, headers=team_headers("nat"))).status_code == 403


async def test_team_admin_renames_but_member_cannot(team_world, real_db_client, team_headers):
    """PUT /teams/<id>: admin 200, member 403; the rename persists."""
    w = team_world
    url = f"/api/v1/teams/{w.t['a1']}"
    ok = await real_db_client.put(url, json={"name": "Alpha2"}, headers=team_headers("adam"))
    no = await real_db_client.put(url, json={"name": "Nope"}, headers=team_headers("mia"))
    assert (ok.status_code, no.status_code) == (200, 403)
    assert w.db.teams[w.t["a1"]].name == "Alpha2"


async def test_disabling_a_team_invalidates_its_members_cached_keys(
    team_world, real_db_client, team_headers, redis_spy
):
    """Review focus: an enabled change stales every member's proxy auth-cache entry (D7)."""
    w = team_world
    w.db.api_keys.insert(
        key_id="kmia", key_hash="h", user_id=w.u["mia"], organization_id=w.org["a"], name="k",
        enabled=True, team_id=w.t["a1"],
    )  # fmt: skip
    w.db.commit()
    resp = await real_db_client.put(
        f"/api/v1/teams/{w.t['a1']}", json={"enabled": False}, headers=team_headers("olive")
    )
    assert resp.status_code == 200
    redis_spy.delete.assert_called_once_with(auth_cache_key("kmia"))


async def test_only_owner_or_org_admin_may_delete(team_world, real_db_client, team_headers):
    """DELETE: team admin 403, owner 200; the team and its memberships are gone."""
    w = team_world
    url = f"/api/v1/teams/{w.t['a1']}"
    assert (await real_db_client.delete(url, headers=team_headers("adam"))).status_code == 403
    assert (await real_db_client.delete(url, headers=team_headers("olive"))).status_code == 200
    assert w.db.teams[w.t["a1"]] is None
    assert w.db(w.db.team_members.team_id == w.t["a1"]).count() == 0


async def test_kill_switch_turns_team_crud_off(
    real_db_client, team_world, team_headers, monkeypatch
):
    """``waddleai.disable-teams`` ON: create, list and get answer 404, org admin included."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", "1")
    hdr = team_headers("boss")
    created = await real_db_client.post("/api/v1/teams", json={"name": "z"}, headers=hdr)
    listed = await real_db_client.get("/api/v1/teams", headers=hdr)
    url = f"/api/v1/teams/{team_world.t['a1']}"
    got = await real_db_client.get(url, headers=hdr)
    put = await real_db_client.put(url, json={"name": "z"}, headers=hdr)
    deleted = await real_db_client.delete(url, headers=hdr)
    statuses = [r.status_code for r in (created, listed, got, put, deleted)]
    assert statuses == [404] * 5
    assert team_world.db.teams[team_world.t["a1"]] is not None


async def test_deleting_a_team_invalidates_its_members_cached_keys(
    team_world, real_db_client, team_headers, redis_spy
):
    """Review focus: DELETE unbinds the team's keys, so the owners' cached auth entries go."""
    w = team_world
    w.db.api_keys.insert(
        key_id="kmia", key_hash="h", user_id=w.u["mia"], organization_id=w.org["a"], name="k",
        enabled=True, team_id=w.t["a1"],
    )  # fmt: skip
    w.db.commit()
    resp = await real_db_client.delete(f"/api/v1/teams/{w.t['a1']}", headers=team_headers("olive"))
    assert resp.status_code == 200
    redis_spy.delete.assert_any_call(auth_cache_key("kmia"))


async def test_create_ignores_a_body_organization_id(team_world, real_db_client, team_headers):
    """Tenancy comes from the token: a body ``organization_id`` naming org b is ignored."""
    w = team_world
    resp = await real_db_client.post(
        "/api/v1/teams",
        json={"name": "Sneaky", "organization_id": w.org["b"]},
        headers=team_headers("boss"),
    )
    assert resp.status_code == 201
    assert w.db.teams[(await resp.get_json())["id"]].organization_id == w.org["a"]


async def test_team_list_total_is_the_full_count_not_the_page_size(real_db_client, team_headers):
    """Page 1 with limit=1 of org a's 2 teams: 1 item, but total (and pagination) say 2."""
    resp = await real_db_client.get("/api/v1/teams?limit=1&page=1", headers=team_headers("boss"))
    body = await resp.get_json()
    assert (len(body["teams"]), body["total"], body["pagination"]["total"]) == (1, 2, 2)
