# Teams Subsystem — Phase 6d: Membership and Ownership Routes

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §10 ("Membership: add/remove member, set team role (org-admin or team Owner/Admin)"), §5, §3 D7.
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Phase 6c (the blueprint module and the `team_headers`/`redis_spy` fixtures).

---

<!-- TASK 24 -->
## Task 25: `/teams/<id>/members` and `/transfer-ownership`

**Files:**
- Modify: `services/management/app/api/v1/teams.py`
- Test: `tests/unit/management/test_teams_member_routes.py` (create)

**Interfaces:**
- Consumes: `authorize_team`, `team_svc.add_member/set_member_role/remove_member/transfer_ownership`, `invalidate_user_auth_cache`.
- Produces routes: `GET /teams/<id>/members` (`team:read`; entries are exactly `user_id`, `team_role`, `created_at` -- no PII), `POST /teams/<id>/members` (`team:member_manage`; body `{user_id, team_role="member"}`), `PUT /teams/<id>/members/<user_id>` (`team:member_manage`; body `{team_role}`), `DELETE /teams/<id>/members/<user_id>` (`team:member_manage`), `POST /teams/<id>/transfer-ownership` (`team:transfer_owner`; body `{new_owner_user_id}`). `_can_manage_owner(access)` = the org admin, or a live Owner (`team:transfer_owner`); it is passed to the service so a team Admin cannot grant/touch/remove an owner.
- The test file owns the **blueprint-wide route inventory** (`EXPECTED_ROUTES`): a new team route must be added there, which forces an authorization test for it. It also asserts, for every membership route, 401 unauthenticated, 403 for a teamless user, and 404 for another tenant's team.
- Review Focus coverage: D2 (`nat` already in a2 is a 409 when added to a1), the owner guard (admin cannot mint/edit/remove an owner; the sole owner cannot self-demote or self-remove), and **cache invalidation on every membership change** (`test_membership_change_invalidates_the_affected_users_cache`).
- mypy note: inside the `lambda` the narrowed `data.user_id` is `int | None` again, so bind `new_member_id: int = data.user_id` after the `None` check.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/management/test_teams_member_routes.py --no-cov -q -x`
Expected: FAIL `AssertionError` in `test_blueprint_exposes_exactly_the_expected_team_routes` (the member routes do not exist yet).

- [ ] **Step 3: Implement**

```diff
--- a/services/management/app/api/v1/teams.py
+++ b/services/management/app/api/v1/teams.py
@@ -1,6 +1,6 @@
 """WaddleAI Management API v1 - Teams (teams spec §10).

-Team CRUD (membership and ownership routes are added in the next task). Authorization:
+Team CRUD plus membership and ownership. Authorization:

 * Org-layer routes (create, list) use ``team:org_admin`` / authentication + a tenant filter.
 * Team-layer routes call :func:`._team_authz.authorize_team`, which re-derives the caller's team
@@ -19,7 +19,7 @@
 from quart import g, jsonify
 from quart_schema import security_scheme, tag, validate_request, validate_response

-from shared.auth.rbac import Permission
+from shared.auth.rbac import Permission, expand_team_permissions
 from shared.auth.team_flags import teams_enabled

 from ...extensions import db
@@ -28,7 +28,7 @@
 from ...services.teams import TeamError
 from . import api_v1_bp
 from ._pagination import PageRequest
-from ._team_authz import authorize_team
+from ._team_authz import TeamAccess, authorize_team
 from .auth import require_auth, require_scope

 _BEARER_AUTH: list[dict[str, list[str]]] = [{"bearerAuth": []}]
@@ -74,6 +74,28 @@


 @dataclass(slots=True)
+class AddMemberRequest:
+    """Request body for POST /api/v1/teams/<team_id>/members."""
+
+    user_id: int | None = None
+    team_role: str | None = "member"
+
+
+@dataclass(slots=True)
+class SetMemberRoleRequest:
+    """Request body for PUT /api/v1/teams/<team_id>/members/<user_id>."""
+
+    team_role: str | None = None
+
+
+@dataclass(slots=True)
+class TransferOwnershipRequest:
+    """Request body for POST /api/v1/teams/<team_id>/transfer-ownership."""
+
+    new_owner_user_id: int | None = None
+
+
+@dataclass(slots=True)
 class TeamItem:
     """A team as returned by the API -- exactly these fields, never the raw row."""

@@ -103,12 +125,30 @@


 @dataclass(slots=True)
+class MemberItem:
+    """A membership: the user id (never a name or email) and their team role."""
+
+    user_id: int
+    team_role: str
+    created_at: str | None
+
+
+@dataclass(slots=True)
 class TeamDetailResponse:
     """Response body for GET /api/v1/teams/<team_id>."""

     team: TeamItem
     member_count: int
     your_team_role: str | None
+
+
+@dataclass(slots=True)
+class MemberListResponse:
+    """Response body for GET /api/v1/teams/<team_id>/members."""
+
+    team_id: int
+    members: list[MemberItem]
+    total: int


 def _team_item(team: Any) -> dict[str, Any]:
@@ -120,6 +160,13 @@
         "enabled": team.enabled is not False,
         "created_at": team.created_at.isoformat() if team.created_at else None,
     }
+
+
+def _can_manage_owner(access: TeamAccess) -> bool:
+    """True for the org admin or a live team Owner (``team:transfer_owner``)."""
+    return access.is_org_admin or Permission.TEAM_TRANSFER_OWNER in expand_team_permissions(
+        access.team_role
+    )


 def _error(exc: TeamError) -> tuple[Any, int]:
@@ -235,3 +282,135 @@
     stale = await asyncio.to_thread(team_svc.delete_team, _db(), access.team)
     await invalidate_user_auth_cache(_db(), stale)
     return {"message": "Team deleted"}
+
+
+@api_v1_bp.route("/teams/<int:team_id>/members", methods=["GET"])
+@tag(["Teams"])
+@security_scheme(_BEARER_AUTH)
+@require_auth
+@validate_response(MemberListResponse, 200)
+async def list_members(team_id: int):
+    """List a team's members (any member or the org admin): user ids and roles only."""
+    access, error = await authorize_team(_db(), team_id, Permission.TEAM_READ)
+    if error is not None or access is None:
+        return error
+    database = _db()
+    page = PageRequest.from_request()
+    rows = await asyncio.to_thread(
+        lambda: list(
+            database(database.team_members.team_id == team_id).select(
+                limitby=page.limitby, orderby=database.team_members.id
+            )
+        )
+    )
+    members = [
+        {
+            "user_id": r.user_id,
+            "team_role": r.team_role,
+            "created_at": r.created_at.isoformat() if r.created_at else None,
+        }
+        for r in rows
+    ]
+    return {"team_id": team_id, "members": members, "total": len(members)}
+
+
+@api_v1_bp.route("/teams/<int:team_id>/members", methods=["POST"])
+@tag(["Teams"])
+@security_scheme(_BEARER_AUTH)
+@require_auth
+@validate_response(MessageResponse, 201)
+@validate_request(AddMemberRequest)
+async def add_team_member(team_id: int, data: AddMemberRequest):
+    """Add a user to the team (team Admin+ or org admin); 409 if they already have a team here."""
+    access, error = await authorize_team(_db(), team_id, Permission.TEAM_MEMBER_MANAGE)
+    if error is not None or access is None:
+        return error
+    if data.user_id is None:
+        return jsonify({"error": "user_id is required"}), 400
+    new_member_id: int = data.user_id
+    try:
+        stale = await asyncio.to_thread(
+            lambda: team_svc.add_member(
+                _db(),
+                access.team,
+                new_member_id,
+                data.team_role,
+                can_manage_owner=_can_manage_owner(access),
+            )  # fmt: skip
+        )
+    except TeamError as exc:
+        return _error(exc)
+    await invalidate_user_auth_cache(_db(), stale)
+    return {"message": "Member added"}, 201
+
+
+@api_v1_bp.route("/teams/<int:team_id>/members/<int:user_id>", methods=["PUT"])
+@tag(["Teams"])
+@security_scheme(_BEARER_AUTH)
+@require_auth
+@validate_response(MessageResponse, 200)
+@validate_request(SetMemberRoleRequest)
+async def set_member_role(team_id: int, user_id: int, data: SetMemberRoleRequest):
+    """Change a member's team role (team Admin+ or org admin; owner changes need an owner)."""
+    access, error = await authorize_team(_db(), team_id, Permission.TEAM_MEMBER_MANAGE)
+    if error is not None or access is None:
+        return error
+    try:
+        stale = await asyncio.to_thread(
+            lambda: team_svc.set_member_role(
+                _db(),
+                access.team,
+                user_id,
+                data.team_role,
+                can_manage_owner=_can_manage_owner(access),
+            )  # fmt: skip
+        )
+    except TeamError as exc:
+        return _error(exc)
+    await invalidate_user_auth_cache(_db(), stale)
+    return {"message": "Member role updated"}
+
+
+@api_v1_bp.route("/teams/<int:team_id>/members/<int:user_id>", methods=["DELETE"])
+@tag(["Teams"])
+@security_scheme(_BEARER_AUTH)
+@require_auth
+@validate_response(MessageResponse, 200)
+async def remove_team_member(team_id: int, user_id: int):
+    """Remove a member (team Admin+ or org admin; removing an owner needs an owner)."""
+    access, error = await authorize_team(_db(), team_id, Permission.TEAM_MEMBER_MANAGE)
+    if error is not None or access is None:
+        return error
+    try:
+        stale = await asyncio.to_thread(
+            lambda: team_svc.remove_member(
+                _db(), access.team, user_id, can_manage_owner=_can_manage_owner(access)
+            )
+        )
+    except TeamError as exc:
+        return _error(exc)
+    await invalidate_user_auth_cache(_db(), stale)
+    return {"message": "Member removed"}
+
+
+@api_v1_bp.route("/teams/<int:team_id>/transfer-ownership", methods=["POST"])
+@tag(["Teams"])
+@security_scheme(_BEARER_AUTH)
+@require_auth
+@validate_response(MessageResponse, 200)
+@validate_request(TransferOwnershipRequest)
+async def transfer_team_ownership(team_id: int, data: TransferOwnershipRequest):
+    """Make an existing member the sole owner (team Owner or org admin)."""
+    access, error = await authorize_team(_db(), team_id, Permission.TEAM_TRANSFER_OWNER)
+    if error is not None or access is None:
+        return error
+    if data.new_owner_user_id is None:
+        return jsonify({"error": "new_owner_user_id is required"}), 400
+    try:
+        stale = await asyncio.to_thread(
+            team_svc.transfer_ownership, _db(), access.team, data.new_owner_user_id
+        )
+    except TeamError as exc:
+        return _error(exc)
+    await invalidate_user_auth_cache(_db(), stale)
+    return {"message": "Ownership transferred"}
```

- [ ] **Step 4: Run it with the CRUD suite**

Run: `.venv/bin/python -m pytest tests/unit/management/test_teams_member_routes.py tests/unit/management/test_teams_routes.py --no-cov -q`
Expected: `63 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/teams.py tests/unit/management/test_teams_member_routes.py
git commit -m "feat(teams): membership and ownership API with cache invalidation"
```

- [ ] **Phase 6 gate:** `.venv/bin/python -m pytest tests/unit/management/test_team_service.py tests/unit/management/test_team_service_roles.py tests/unit/management/test_teams_routes.py tests/unit/management/test_teams_member_routes.py tests/unit/management/test_scope_authz.py --no-cov -q` -- all pass.
