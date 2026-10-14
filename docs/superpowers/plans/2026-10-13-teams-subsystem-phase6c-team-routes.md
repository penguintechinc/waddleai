# Teams Subsystem — Phase 6c: Team CRUD Routes

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §10 ("Team CRUD ... `@api_v1_bp.route` + `@require_auth` + `@require_scope(...)` + `@validate_response`"; "All responses go through explicit response models (no raw ORM); PII stays tenant-side"), §11 (E2E: team-admin manages only own team).
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Phases 3b (`authorize_team`), 4c (`real_db_client`, `mint_token`), 4d (`invalidate_user_auth_cache`), 6a-6b (service layer).

---

<!-- TASK 23 -->
## Task 24: `POST/GET /teams`, `GET/PUT/DELETE /teams/<id>`

**Files:**
- Create: `services/management/app/api/v1/teams.py`
- Modify: `services/management/app/api/v1/__init__.py` (register the module; append at the end of the import list -- the file documents why order is append-only)
- Modify: `tests/unit/management/conftest.py` (add `teams` to `ROUTE_MODULES` and to `_REAL_DB_ROUTE_MODULES`; add the `team_headers` and `redis_spy` fixtures)
- Modify: `tests/unit/management/test_scope_authz.py` (one new scoped route)
- Test: `tests/unit/management/test_teams_routes.py` (create)

**Interfaces:**
- Consumes: `authorize_team`, service functions, `invalidate_user_auth_cache`, `teams_enabled`, `PageRequest`.
- Produces routes under `/api/v1`: `POST /teams` (`@require_scope(Permission.TEAM_ORG_ADMIN)`; the tenant is taken from the token, never the body), `GET /teams` (any authenticated user: the org admin sees every team in the tenant, others only teams they belong to), `GET /teams/<id>` (`team:read`: any member incl. Viewer), `PUT /teams/<id>` (`team:admin`), `DELETE /teams/<id>` (`team:delete`). Team-layer routes call `authorize_team` (live membership) rather than `require_scope`. Every route has `@validate_response` with an explicit dataclass (`TeamItem`, `TeamListResponse`, `CreateTeamResponse`, `TeamDetailResponse`, `MessageResponse`) -- field sets are asserted, so a raw row can never leak; responses carry ids only, no names/emails (PII boundary).
- Produces fixtures: `team_headers("mia")` (bearer headers whose token carries the user's real team context and scopes) and `redis_spy` (a recording stand-in for management's Valkey).
- `POST /teams` is the only route carrying `_required_scopes`, so `test_scope_authz.py` (which enumerates scoped routes from the live URL map and pins an exact count + scope set) must learn about it: route count 113 -> 114 and `team:org_admin` joins `_MIGRATED_SCOPES` (the suite's other tests then also prove no non-admin role holds it).
- Every mutation that stales cached keys calls `invalidate_user_auth_cache` (Valkey failure never fails the request).
- Review Focus: cross-team access (`nat` reading a1 -> 403), another tenant's team (404, no id leak), a teamless user (403), the kill switch (404 for everyone).

- [ ] **Step 1: Write the failing tests and fixtures**

```diff
--- a/tests/unit/management/conftest.py
+++ b/tests/unit/management/conftest.py
@@ -342,6 +342,7 @@
     "services.management.app.api.v1.hook_metrics",
     "services.management.app.api.v1.fleet",
     "services.management.app.api.v1.proxy_keys",
+    "services.management.app.api.v1.teams",
 ]


@@ -840,7 +841,7 @@


 #: Route modules whose module-level ``db`` the real-database fixture below repoints.
-_REAL_DB_ROUTE_MODULES = ("auth", "users", "organizations", "keys", "proxy_keys")
+_REAL_DB_ROUTE_MODULES = ("auth", "users", "organizations", "keys", "proxy_keys", "teams")


 @pytest.fixture
@@ -869,3 +870,45 @@
     reset_login_throttle()
     reset_auth_rate_limiter()
     reset_token_denylist()
+
+
+#: team_world users -> (team key, team role); ``boss``/``loner`` have no team, ``carl`` is org b's.
+_TEAM_OF = {"olive": "a1", "adam": "a1", "mia": "a1", "vic": "a1", "nat": "a2", "carl": "b1"}
+_TEAM_ROLE_OF = {
+    "olive": "owner", "adam": "admin", "mia": "member", "vic": "viewer", "nat": "member",
+    "carl": "owner",
+}  # fmt: skip
+
+
+@pytest.fixture
+def team_headers(team_world, mint_token):
+    """Factory ``team_headers("mia")`` -> bearer headers for a ``team_world`` user.
+
+    The token carries the same team context + scopes a real login would mint (``boss`` is the
+    org-a admin; ``loner`` a teamless org-a user; ``carl`` org b's team owner).
+    """
+
+    def _make(user: str) -> dict[str, str]:
+        w = team_world
+        org = "b" if user == "carl" else "a"
+        team_key = _TEAM_OF.get(user)
+        token = mint_token(
+            role="admin" if user == "boss" else "user",
+            user_id=w.u[user],
+            org_id=w.org[org],
+            team_id=w.t[team_key] if team_key else None,
+            team_role=_TEAM_ROLE_OF.get(user),
+        )
+        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
+
+    return _make
+
+
+@pytest.fixture
+def redis_spy(monkeypatch) -> MagicMock:
+    """A recording stand-in for management's Valkey client (``extensions.redis_client``)."""
+    import services.management.app.extensions as ext_mod
+
+    client = MagicMock()
+    monkeypatch.setattr(ext_mod, "redis_client", client)
+    return client
```

```python
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
    got = await real_db_client.get(f"/api/v1/teams/{team_world.t['a1']}", headers=hdr)
    assert (created.status_code, listed.status_code, got.status_code) == (404, 404, 404)
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `.venv/bin/python -m pytest tests/unit/management/test_teams_routes.py --no-cov -q -x`
Expected: ERROR at fixture setup `ModuleNotFoundError: No module named 'services.management.app.api.v1.teams'` (the `flask_app` fixture patches every module in `ROUTE_MODULES`, including the one you have not written yet).

- [ ] **Step 3: Implement the module, register it, update the scope-authz pins**

```python
"""WaddleAI Management API v1 - Teams (teams spec §10).

Team CRUD (membership and ownership routes are added in the next task). Authorization:

* Org-layer routes (create, list) use ``team:org_admin`` / authentication + a tenant filter.
* Team-layer routes call :func:`._team_authz.authorize_team`, which re-derives the caller's team
  permissions from the LIVE membership row (never a stale token claim) and enforces tenancy.

Every response goes through an explicit dataclass model (``@validate_response``) -- never a raw
row -- and carries user ids only, no PII. Every mutation that changes who acts in which team
invalidates the proxy auth cache for the affected users (D7).
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from penguin_dal.db import DB
from quart import g, jsonify
from quart_schema import security_scheme, tag, validate_request, validate_response

from shared.auth.rbac import Permission
from shared.auth.team_flags import teams_enabled

from ...extensions import db
from ...services import teams as team_svc
from ...services.auth_cache_invalidation import invalidate_user_auth_cache
from ...services.teams import TeamError
from . import api_v1_bp
from ._pagination import PageRequest
from ._team_authz import authorize_team
from .auth import require_auth, require_scope

_BEARER_AUTH: list[dict[str, list[str]]] = [{"bearerAuth": []}]


def _db() -> DB:
    """Return the process-wide penguin-dal handle, narrowed away from ``None``."""
    if db is None:
        raise RuntimeError("database not initialized")
    return db


@dataclass(slots=True)
class MessageResponse:
    """Generic ``{"message": str}`` envelope."""

    message: str


@dataclass(slots=True)
class PaginationMeta:
    """Bounded-window metadata attached to the list response."""

    page: int
    limit: int
    total: int | None
    pages: int | None


@dataclass(slots=True)
class CreateTeamRequest:
    """Request body for POST /api/v1/teams (tenant comes from the token, never the body)."""

    name: str | None = None


@dataclass(slots=True)
class UpdateTeamRequest:
    """Request body for PUT /api/v1/teams/<team_id>. Partial update."""

    name: str | None = None
    enabled: bool | None = None


@dataclass(slots=True)
class TeamItem:
    """A team as returned by the API -- exactly these fields, never the raw row."""

    id: int
    organization_id: int
    name: str
    enabled: bool
    created_at: str | None


@dataclass(slots=True)
class TeamListResponse:
    """Response body for GET /api/v1/teams."""

    teams: list[TeamItem]
    total: int
    pagination: PaginationMeta


@dataclass(slots=True)
class CreateTeamResponse:
    """Response body for POST /api/v1/teams."""

    id: int
    name: str
    message: str


@dataclass(slots=True)
class TeamDetailResponse:
    """Response body for GET /api/v1/teams/<team_id>."""

    team: TeamItem
    member_count: int
    your_team_role: str | None


def _team_item(team: Any) -> dict[str, Any]:
    """Project a ``teams`` row onto the :class:`TeamItem` field set."""
    return {
        "id": team.id,
        "organization_id": team.organization_id,
        "name": team.name,
        "enabled": team.enabled is not False,
        "created_at": team.created_at.isoformat() if team.created_at else None,
    }


def _error(exc: TeamError) -> tuple[Any, int]:
    """Render a service-layer :class:`TeamError` as a JSON error response."""
    return jsonify({"error": exc.message}), exc.status


async def _teams_off() -> tuple[Any, int] | None:
    """Return a 404 response when the ``waddleai.disable-teams`` kill switch is ON, else None."""
    if await asyncio.to_thread(teams_enabled, str(g.user.get("organization_id"))):
        return None
    return jsonify({"error": "Teams are disabled"}), 404


@api_v1_bp.route("/teams", methods=["POST"])
@tag(["Teams"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.TEAM_ORG_ADMIN)
@validate_response(CreateTeamResponse, 201)
@validate_request(CreateTeamRequest)
async def create_team(data: CreateTeamRequest):
    """Create a team in the caller's own organization (org-admin scope)."""
    if (off := await _teams_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    try:
        team_id = await asyncio.to_thread(team_svc.create_team, _db(), org_id, data.name)
    except TeamError as exc:
        return _error(exc)
    return {"id": team_id, "name": (data.name or "").strip(), "message": "Team created"}, 201


@api_v1_bp.route("/teams", methods=["GET"])
@tag(["Teams"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(TeamListResponse, 200)
async def list_teams():
    """List the caller's tenant's teams: all of them for an org admin, else only their own."""
    if (off := await _teams_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    user_id = g.user.get("user_id")
    is_org_admin = Permission.TEAM_ORG_ADMIN.value in set(g.user.get("scope") or [])
    page = PageRequest.from_request()
    database = _db()

    def _fetch() -> list[Any]:
        query = database.teams.organization_id == org_id
        if not is_org_admin:
            mine = database(database.team_members.user_id == user_id).select()
            query = query & database.teams.id.belongs([int(m.team_id) for m in mine] or [0])
        return list(database(query).select(limitby=page.limitby, orderby=database.teams.id))

    rows = await asyncio.to_thread(_fetch)
    items = [_team_item(r) for r in rows]
    return {"teams": items, "total": len(items), **page.meta()}


@api_v1_bp.route("/teams/<int:team_id>", methods=["GET"])
@tag(["Teams"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(TeamDetailResponse, 200)
async def get_team(team_id: int):
    """Get one team (any member, including Viewer, or the org admin)."""
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_READ)
    if error is not None or access is None:
        return error
    database = _db()
    count = await asyncio.to_thread(
        lambda: database(database.team_members.team_id == team_id).count()
    )
    return {
        "team": _team_item(access.team),
        "member_count": count,
        "your_team_role": access.team_role,
    }


@api_v1_bp.route("/teams/<int:team_id>", methods=["PUT"])
@tag(["Teams"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
@validate_request(UpdateTeamRequest)
async def update_team(team_id: int, data: UpdateTeamRequest):
    """Rename or enable/disable a team (team Admin+ or org admin)."""
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_ADMIN)
    if error is not None or access is None:
        return error
    try:
        stale = await asyncio.to_thread(
            lambda: team_svc.update_team(_db(), access.team, name=data.name, enabled=data.enabled)
        )
    except TeamError as exc:
        return _error(exc)
    await invalidate_user_auth_cache(_db(), stale)
    return {"message": "Team updated"}


@api_v1_bp.route("/teams/<int:team_id>", methods=["DELETE"])
@tag(["Teams"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
async def delete_team(team_id: int):
    """Delete a team (team Owner or org admin); its members' keys become teamless."""
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_DELETE)
    if error is not None or access is None:
        return error
    stale = await asyncio.to_thread(team_svc.delete_team, _db(), access.team)
    await invalidate_user_auth_cache(_db(), stale)
    return {"message": "Team deleted"}
```

```diff
--- a/services/management/app/api/v1/__init__.py
+++ b/services/management/app/api/v1/__init__.py
@@ -34,4 +34,5 @@
     fleet,
     signing_keys,
     proxy_keys,
+    teams,
 )
```

```diff
--- a/tests/unit/management/test_scope_authz.py
+++ b/tests/unit/management/test_scope_authz.py
@@ -117,10 +117,14 @@
     Permission.ROUTING_POLICY_DELETE.value,
     Permission.SECURITY_POLICY_ADMIN.value,
     Permission.MODEL_ACCESS_POLICY_DELETE.value,
+    # Not a require_role migration: the teams subsystem's org-layer scope (POST /teams).
+    # Team-layer routes authorize via ``_team_authz.authorize_team`` on live membership
+    # instead, so they carry no ``_required_scopes`` -- ``test_teams_routes.py`` enumerates them.
+    Permission.TEAM_ORG_ADMIN.value,
 }

-_EXPECTED_ROUTE_COUNT = 113  # audited require_role call sites, both migration waves,
-#   +3 net-new (not a require_role migration):
+_EXPECTED_ROUTE_COUNT = 114  # audited require_role call sites, both migration waves,
+#   +4 net-new (not a require_role migration; +1 teams: POST /teams -> TEAM_ORG_ADMIN):
 #   94 from the original OIDC migration, +16 from branches cut before it landed
 #   (6 fleet, 9 hook_rules, 1 hook_metrics), +3 from the model-access-policy
 #   CRUD blueprint (POST/PUT -> MODEL_ACCESS_POLICY_WRITE, DELETE ->
```

- [ ] **Step 4: Run them**

Run: `.venv/bin/python -m pytest tests/unit/management/test_teams_routes.py tests/unit/management/test_scope_authz.py --no-cov -q`
Expected: all pass (the first run builds the Quart app, ~45 s).

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/teams.py services/management/app/api/v1/__init__.py tests/unit/management/conftest.py tests/unit/management/test_scope_authz.py tests/unit/management/test_teams_routes.py
git commit -m "feat(teams): team CRUD API with live-membership authorization"
```

- [ ] **Phase 6c gate:** Step 4 passes.
