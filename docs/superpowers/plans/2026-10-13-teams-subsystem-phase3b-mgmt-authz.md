# Teams Subsystem — Phase 3b: Management-Plane Team Authorization

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §3 D5/D9, §8 ("team enforcement must be added to both planes"), §11.
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Phases 1-2, 3a (the `team_members` table and the team role bundles).

---

<!-- TASK 9 -->
## Task 9: `authorize_team` -- live-membership team authorization for routes

**Files:**
- Create: `services/management/app/api/v1/_team_authz.py`
- Modify: `tests/conftest.py` (add the `team_world` fixture)
- Test: `tests/unit/management/test_team_authz.py` (create)

**Interfaces:**
- Consumes: `expand_team_permissions`, `Permission`, `teams_enabled`.
- Produces: `TeamAccess(team, is_org_admin: bool, team_role: str | None)` and `async authorize_team(database, team_id: int, permission: Permission) -> tuple[TeamAccess | None, ErrorResponse | None]` -- exactly one slot is set; the error is a `(response, status)` pair a handler returns verbatim. Order: kill switch (404) -> team exists (404) -> same tenant (404, never leaking another tenant's team ids) -> `team:org_admin` in the token scope (allowed) -> **live** `team_members` row (403 if absent, if the team is disabled, or if the live role lacks the permission). A NULL `teams.enabled` counts as enabled.
- Why not `require_scope`: it only reads the token's scope claim, which can be up to one token-TTL stale. Team permissions must bite at once on demotion/removal, so this re-derives them from the DB using the same `expand_team_permissions` the token mint uses. (`require_scope(Permission.TEAM_ORG_ADMIN)` is still used for the org-layer create route in Phase 6.)
- Produces fixture `team_world` (used by almost every later test): `TeamWorld(db, org, u, t)` with `org["a"|"b"]`, `t["a1"|"a2"|"b1"]`, and users `u[...]`: org a -- `boss` (org admin, no team), `olive` (owner of a1), `adam` (admin of a1), `mia` (member of a1), `vic` (viewer of a1), `nat` (member of a2), `loner` (no team); org b -- `carl` (owner of b1).

- [ ] **Step 1: Write the fixture and the failing test**

```diff
--- a/tests/conftest.py
+++ b/tests/conftest.py
@@ -1,11 +1,13 @@
 """Pytest configuration and shared fixtures for WaddleAI tests."""

+import dataclasses
 import importlib
 import os
 import shutil
 import sys
 import tempfile
 import types
+from typing import Any
 from unittest.mock import MagicMock, Mock

 import pytest
@@ -284,3 +286,56 @@
     Base.metadata.create_all(engine)
     engine.dispose()
     return get_db(url)
+
+
+_PLACEHOLDER_HASH = "x"  # noqa: S105 -- placeholder, never a loginable hash
+
+
+@dataclasses.dataclass
+class TeamWorld:
+    """Seeded teams fixture data: ``db`` plus name->id maps for orgs, users and teams."""
+
+    db: Any
+    org: dict[str, int]
+    u: dict[str, int]
+    t: dict[str, int]
+
+
+@pytest.fixture
+def team_world(real_schema_db) -> TeamWorld:
+    """Two orgs, three teams and eight users covering every team role, on a real schema.
+
+    org a: boss(admin, no team) | team a1: olive=owner adam=admin mia=member vic=viewer |
+    team a2: nat=member | loner(user, no team);   org b: team b1: carl=owner.
+    """
+    db = real_schema_db
+    org = {k: int(db.organizations.insert(name=k)) for k in ("a", "b")}
+    seed = [  # (username, org role, org, team key, team role)
+        ("boss", "admin", "a", None, None),
+        ("olive", "user", "a", "a1", "owner"),
+        ("adam", "user", "a", "a1", "admin"),
+        ("mia", "user", "a", "a1", "member"),
+        ("vic", "user", "a", "a1", "viewer"),
+        ("nat", "user", "a", "a2", "member"),
+        ("loner", "user", "a", None, None),
+        ("carl", "user", "b", "b1", "owner"),
+    ]
+    teams = {"a1": ("a", "Alpha"), "a2": ("a", "Beta"), "b1": ("b", "Gamma")}
+    t = {k: int(db.teams.insert(organization_id=org[o], name=n)) for k, (o, n) in teams.items()}
+    u: dict[str, int] = {}
+    for name, role, o, team_key, team_role in seed:
+        u[name] = int(
+            db.users.insert(
+                username=name,
+                email=f"{name}@example.com",
+                password_hash=_PLACEHOLDER_HASH,
+                role=role,
+                organization_id=org[o],
+            )  # fmt: skip
+        )
+        if team_key:
+            db.team_members.insert(
+                team_id=t[team_key], user_id=u[name], organization_id=org[o], team_role=team_role
+            )
+    db.commit()
+    return TeamWorld(db=db, org=org, u=u, t=t)
```

```python
"""Management-plane team authorization (teams spec D9): ``authorize_team``.

The management plane re-derives team permissions from the LIVE ``team_members`` row on every
call -- never from the (up to token-TTL stale) scope claim -- so a demotion or removal takes
effect immediately. Runs against ``team_world`` (real schema, no mocks).
"""

import pytest
from quart import g

from services.management.app.api.v1._team_authz import authorize_team
from shared.auth.rbac import ROLE_PERMISSIONS, Permission, Role

P = Permission


def _as(world, name: str, org: str = "a", role: str = "user", scope=None) -> dict:
    """Build the ``g.user`` dict a JWT for ``name`` would yield (scope = org bundle by default)."""
    bundle = {p.value for p in ROLE_PERMISSIONS[Role(role)]}
    return {
        "user_id": world.u[name],
        "organization_id": world.org[org],
        "role": role,
        "scope": sorted(scope if scope is not None else bundle),
    }


async def _authorize(app, world, who: dict, team_key: str, perm: Permission):
    """Run ``authorize_team`` inside a request context carrying ``who`` as ``g.user``."""
    async with app.test_request_context("/"):
        g.user = who
        return await authorize_team(world.db, world.t[team_key], perm)


async def _status(app, world, who, team_key, perm) -> int | None:
    """Return the HTTP status of a refusal, or None when access was granted."""
    access, err = await _authorize(app, world, who, team_key, perm)
    if err is None:
        assert access is not None
        return None
    return int(err[1])


async def test_org_admin_may_act_on_any_team_in_their_tenant(flask_app, team_world) -> None:
    """``team:org_admin`` is the org-layer scope: boss passes without being a member."""
    who = _as(team_world, "boss", role="admin")
    assert await _status(flask_app, team_world, who, "a2", P.TEAM_DELETE) is None


async def test_org_admin_cannot_reach_another_tenants_team(flask_app, team_world) -> None:
    """Tenant isolation is a hard boundary: a team in org b is a 404 to org a's admin."""
    who = _as(team_world, "boss", role="admin")
    assert await _status(flask_app, team_world, who, "b1", P.TEAM_READ) == 404


async def test_member_reads_own_team_but_cannot_administer_it(flask_app, team_world) -> None:
    """Mia (member of a1): TEAM_READ ok, TEAM_ADMIN refused with 403."""
    who = _as(team_world, "mia")
    assert await _status(flask_app, team_world, who, "a1", P.TEAM_READ) is None
    assert await _status(flask_app, team_world, who, "a1", P.TEAM_ADMIN) == 403


async def test_team_admin_manages_members_but_cannot_delete(flask_app, team_world) -> None:
    """Adam (team admin): TEAM_MEMBER_MANAGE ok; TEAM_DELETE is Owner-only."""
    who = _as(team_world, "adam")
    assert await _status(flask_app, team_world, who, "a1", P.TEAM_MEMBER_MANAGE) is None
    assert await _status(flask_app, team_world, who, "a1", P.TEAM_DELETE) == 403


async def test_cross_team_access_is_refused(flask_app, team_world) -> None:
    """Nat (member of a2) and loner (no team) get 403 on a1, even for the weakest permission."""
    for name in ("nat", "loner"):
        who = _as(team_world, name)
        assert await _status(flask_app, team_world, who, "a1", P.TEAM_READ) == 403, name


async def test_unknown_team_is_404(flask_app, team_world) -> None:
    """A team id that does not exist is a 404, not a 403 (and not a crash)."""
    async with flask_app.test_request_context("/"):
        g.user = _as(team_world, "boss", role="admin")
        _access, err = await authorize_team(team_world.db, 99999, P.TEAM_READ)
    assert err is not None
    assert err[1] == 404


async def test_live_role_beats_a_stale_scope_claim(flask_app, team_world) -> None:
    """Vic is a viewer in the DB; a token still carrying team:admin must not grant it."""
    stale = _as(team_world, "vic", scope={P.TEAM_ADMIN.value, P.TEAM_READ.value})
    assert await _status(flask_app, team_world, stale, "a1", P.TEAM_ADMIN) == 403


async def test_removed_member_with_a_valid_token_is_refused(flask_app, team_world) -> None:
    """Membership change is effective at once: delete mia's row, her old token loses access."""
    db = team_world.db
    db(db.team_members.user_id == team_world.u["mia"]).delete()
    db.commit()
    who = _as(team_world, "mia", scope={P.TEAM_READ.value, P.TEAM_USE.value})
    assert await _status(flask_app, team_world, who, "a1", P.TEAM_READ) == 403


async def test_disabled_team_is_refused_to_members_but_not_the_org_admin(
    flask_app, team_world
) -> None:
    """A disabled team locks out its own members; the org admin can still manage/re-enable it."""
    db = team_world.db
    db(db.teams.id == team_world.t["a1"]).update(enabled=False)
    db.commit()
    assert await _status(flask_app, team_world, _as(team_world, "olive"), "a1", P.TEAM_READ) == 403
    boss = _as(team_world, "boss", role="admin")
    assert await _status(flask_app, team_world, boss, "a1", P.TEAM_ADMIN) is None


@pytest.mark.parametrize("name", ["boss", "olive"])
async def test_kill_switch_makes_every_team_route_404(
    flask_app, team_world, monkeypatch, name
) -> None:
    """``waddleai.disable-teams`` ON: the team API is off for everyone, admins included."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", "1")
    role = "admin" if name == "boss" else "user"
    assert (
        await _status(flask_app, team_world, _as(team_world, name, role=role), "a1", P.TEAM_READ)
        == 404
    )
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/management/test_team_authz.py --no-cov -q -x`
Expected: collection error `ModuleNotFoundError: No module named 'services.management.app.api.v1._team_authz'`.

- [ ] **Step 3: Implement**

```python
"""Team-scoped authorization for management routes (teams spec D5/D9).

``require_scope`` only checks the (up to token-TTL stale) scope claim, which is right for
org-layer scopes but wrong for team permissions: a demotion or removal must bite at once.
:func:`authorize_team` therefore re-derives the caller's team permissions from the LIVE
``team_members`` row on every call -- using the same ``expand_team_permissions`` bundle the
token mint uses -- and enforces tenancy before anything else.
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from penguin_dal.db import DB
from quart import g, jsonify

from shared.auth.rbac import Permission, expand_team_permissions
from shared.auth.team_flags import teams_enabled

#: ``(response, status)`` pair a handler returns verbatim on refusal.
ErrorResponse = tuple[Any, int]


@dataclass(slots=True)
class TeamAccess:
    """A granted team access: the team row, and how the caller earned it."""

    team: Any
    is_org_admin: bool
    team_role: str | None


def _refuse(message: str, status: int, **extra: object) -> ErrorResponse:
    """Build a JSON error ``(response, status)`` pair."""
    return jsonify({"error": message, **extra}), status


async def authorize_team(
    database: DB, team_id: int, permission: Permission
) -> tuple[TeamAccess | None, ErrorResponse | None]:
    """Authorize ``g.user`` for ``permission`` on ``team_id``; exactly one tuple slot is set.

    A NULL ``enabled`` (a row written without the column's Python-side default) counts as
    enabled; only an explicit ``False`` disables. Order matters: kill switch (404) -> team
    exists (404) -> same tenant (404, never leaking another tenant's team ids) -> org admin
    via ``team:org_admin`` (allowed) -> live membership (403 if absent, or if the team is
    disabled, or if the live role lacks ``permission``).
    """
    user = g.user
    org_id = user.get("organization_id")

    if not await asyncio.to_thread(teams_enabled, str(org_id)):
        return None, _refuse("Teams are disabled", 404)

    team = await asyncio.to_thread(lambda: database(database.teams.id == team_id).select().first())
    if team is None or team.organization_id != org_id:
        return None, _refuse("Team not found", 404)

    if Permission.TEAM_ORG_ADMIN.value in set(user.get("scope") or []):
        return TeamAccess(team=team, is_org_admin=True, team_role=None), None

    member = await asyncio.to_thread(
        lambda: (
            database(
                (database.team_members.team_id == team_id)
                & (database.team_members.user_id == user.get("user_id"))
            )
            .select()
            .first()
        )
    )
    if member is None or team.enabled is False:
        return None, _refuse("Insufficient permissions", 403, required_scope=permission.value)
    if permission not in expand_team_permissions(member.team_role):
        return None, _refuse("Insufficient permissions", 403, required_scope=permission.value)
    return TeamAccess(team=team, is_org_admin=False, team_role=member.team_role), None
```

- [ ] **Step 4: Run it**

Run: `.venv/bin/python -m pytest tests/unit/management/test_team_authz.py --no-cov -q`
Expected: `11 passed` (the first run builds the Quart app and takes ~45 s).

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/_team_authz.py tests/conftest.py tests/unit/management/test_team_authz.py
git commit -m "feat(teams): authorize_team re-derives team permissions from live membership"
```

- [ ] **Phase 3b gate:** `.venv/bin/python -m pytest tests/unit/management/test_team_authz.py tests/unit/test_check_permission_team.py --no-cov -q` -- all pass.
