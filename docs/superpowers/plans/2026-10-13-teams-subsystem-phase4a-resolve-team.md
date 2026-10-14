# Teams Subsystem — Phase 4a: Resolving the Acting Team into `UserContext`

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §3 D3/D5, §7 ("UserContext plumbing"), §11.
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Phases 1-3 (`team_members`, `TeamRole` bundles, `UserContext.team_*`, the `team_world` fixture, `teams_enabled`).

---

<!-- TASK 10 -->
## Task 10: Resolve `(team_id, team_role)` for keys and logins; build `UserContext` with team scopes

**Files:**
- Modify: `shared/auth/rbac.py` (`RBACManager`, two module functions)
- Modify: `tests/conftest.py` (`team_world` rows need `enabled=True`: key auth filters on it and a `create_all` schema has no server default)
- Modify: `tests/unit/test_rbac_additional.py` (one mock key row needs `team_id = None`)
- Test: `tests/unit/test_rbac_team_resolution.py` (create)

**Interfaces:**
- Consumes: `UserContext.team_*`, `expand_team_permissions`, `teams_enabled`, fixture `team_world`.
- Produces: `lookup_key_team(database, user_id, organization_id, key_team_id) -> tuple[int | None, str | None]` (a key acts in the team it names -- D3; NULL means teamless) and `lookup_user_team(database, user_id, organization_id)` (login/JWT: the user's one team in the tenant, D2); both read the **live** `team_members`/`teams` rows and return `(None, None)` -- teamless, never an error -- when the kill switch is on, the membership is absent in **this tenant** (a key row naming another tenant's team finds nothing), the user left the team, or the team is explicitly disabled. `RBACManager.resolve_key_team(key_record, user)`; `RBACManager.build_user_context(..., team_id=None, team_role=None)` and `_build_user_context(user, team_id, team_role)` now return `permissions = org bundle | expand_team_permissions(team_role)` as a **fresh** set (the shared `ROLE_PERMISSIONS` sets are never mutated); `authenticate_api_key` resolves the team.
- Review Focus coverage: a key whose owner was removed, a key naming a team the owner is not in, a disabled team, and the kill switch.

- [ ] **Step 1: Write the failing test**

First fix the fixture rows and the one mock row that the new key lookup would otherwise read as a team:

```diff
--- a/tests/conftest.py
+++ b/tests/conftest.py
@@ -309,7 +309,7 @@
     team a2: nat=member | loner(user, no team);   org b: team b1: carl=owner.
     """
     db = real_schema_db
-    org = {k: int(db.organizations.insert(name=k)) for k in ("a", "b")}
+    org = {k: int(db.organizations.insert(name=k, enabled=True)) for k in ("a", "b")}
     seed = [  # (username, org role, org, team key, team role)
         ("boss", "admin", "a", None, None),
         ("olive", "user", "a", "a1", "owner"),
@@ -321,7 +321,10 @@
         ("carl", "user", "b", "b1", "owner"),
     ]
     teams = {"a1": ("a", "Alpha"), "a2": ("a", "Beta"), "b1": ("b", "Gamma")}
-    t = {k: int(db.teams.insert(organization_id=org[o], name=n)) for k, (o, n) in teams.items()}
+    t = {
+        k: int(db.teams.insert(organization_id=org[o], name=n, enabled=True))
+        for k, (o, n) in teams.items()
+    }
     u: dict[str, int] = {}
     for name, role, o, team_key, team_role in seed:
         u[name] = int(
@@ -330,8 +333,9 @@
                 email=f"{name}@example.com",
                 password_hash=_PLACEHOLDER_HASH,
                 role=role,
+                enabled=True,
                 organization_id=org[o],
-            )  # fmt: skip
+            )
         )
         if team_key:
             db.team_members.insert(
```

```diff
--- a/tests/unit/test_rbac_additional.py
+++ b/tests/unit/test_rbac_additional.py
@@ -113,6 +113,7 @@
         mock_key_record.user_id = 5
         mock_key_record.key_hash = hashed_key
         mock_key_record.enabled = True
+        mock_key_record.team_id = None  # teamless key: no team_members lookup is made

         # 1) key_id lookup: db(...).select().first() -> the one candidate row
         mock_key_lookup = MagicMock()
```

```python
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
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_rbac_team_resolution.py --no-cov -q -x`
Expected: collection error `ImportError: cannot import name 'lookup_key_team' from 'shared.auth.rbac'`.

- [ ] **Step 3: Implement**

```diff
--- a/shared/auth/rbac.py
+++ b/shared/auth/rbac.py
@@ -9,6 +9,8 @@
 from enum import Enum

 from passlib.hash import bcrypt
+
+from shared.auth.team_flags import teams_enabled


 class Role(Enum):
@@ -359,6 +361,49 @@
         return set()


+def _live_team_membership(
+    database, user_id: int, organization_id: int, team_id: int | None
+) -> tuple[int | None, str | None]:
+    """Resolve ``(team_id, team_role)`` from the live ``team_members``/``teams`` rows.
+
+    ``team_id`` ``None`` means "the user's one team in this tenant" (D2 guarantees at most one);
+    an int pins the lookup to that team. Returns ``(None, None)`` -- teamless, never an error --
+    when the kill switch is on, no matching membership exists in this tenant, or the team is
+    explicitly disabled. The tenant filter stops a key row naming another tenant's team.
+    """
+    if not teams_enabled(str(organization_id)):
+        return None, None
+    members = database.team_members
+    query = (members.user_id == user_id) & (members.organization_id == organization_id)
+    if team_id is not None:
+        query = query & (members.team_id == team_id)
+    member = database(query).select().first()
+    if member is None:
+        return None, None
+    team = database(database.teams.id == member.team_id).select().first()
+    if team is None or team.enabled is False:
+        return None, None
+    return int(member.team_id), str(member.team_role)
+
+
+def lookup_key_team(
+    database, user_id: int, organization_id: int, key_team_id: int | None
+) -> tuple[int | None, str | None]:
+    """Acting team for a request authenticated by an API key (spec D3).
+
+    The key names its team (``api_keys.team_id``); NULL means the key acts teamless. The role
+    is looked up live so a removal or demotion is honoured even though the key row is unchanged.
+    """
+    if key_team_id is None:
+        return None, None
+    return _live_team_membership(database, user_id, organization_id, key_team_id)
+
+
+def lookup_user_team(database, user_id: int, organization_id: int) -> tuple[int | None, str | None]:
+    """Acting team for a login/JWT with no key: the user's single team in their tenant, if any."""
+    return _live_team_membership(database, user_id, organization_id, None)
+
+
 #: Valkey key prefix for the proxy's per-request api-key auth-lookup cache
 #: (``proxy/apps/proxy_server/auth_cache.py``). Defined here -- a module
 #: already shared by the proxy and management services -- rather than in
@@ -467,7 +512,8 @@
         if not user or not user.enabled:
             raise AuthenticationError("API key user is disabled")

-        context = self._build_user_context(user)
+        team_id, team_role = self.resolve_key_team(key_record, user)
+        context = self._build_user_context(user, team_id=team_id, team_role=team_role)
         context.api_key_id = key_record.id
         return context

@@ -526,13 +572,16 @@
         organization_id: int,
         managed_orgs: list[int] | None,
         api_key_id: int | None = None,
+        team_id: int | None = None,
+        team_role: str | None = None,
     ) -> UserContext:
         """Build a :class:`UserContext` from already-resolved fields -- no DB access.

         Delegates to :meth:`_build_user_context`'s role->permissions expansion
         and ``managed_orgs`` normalization via a lightweight stand-in object,
         so a cache-populated context (``auth_cache.py``) can never drift from
-        the DB-built one.
+        the DB-built one. ``team_id``/``team_role`` are the already-resolved acting
+        team; the team role is expanded into team-layer scopes exactly like the org role.
         """
         from types import SimpleNamespace

@@ -543,14 +592,22 @@
             username=username,
             organization_id=organization_id,
         )
-        context = self._build_user_context(user_like)
+        context = self._build_user_context(user_like, team_id=team_id, team_role=team_role)
         context.api_key_id = api_key_id
         return context

-    def _build_user_context(self, user) -> UserContext:
-        """Build user context from database record."""
+    def resolve_key_team(self, key_record, user) -> tuple[int | None, str | None]:
+        """Acting ``(team_id, team_role)`` for an authenticated key row and its owning user."""
+        return lookup_key_team(
+            self.db, user.id, user.organization_id, getattr(key_record, "team_id", None)
+        )
+
+    def _build_user_context(
+        self, user, team_id: int | None = None, team_role: str | None = None
+    ) -> UserContext:
+        """Build user context from database record (org bundle ∪ acting-team bundle)."""
         role = Role(user.role)
-        permissions = ROLE_PERMISSIONS.get(role, set())
+        permissions = set(ROLE_PERMISSIONS.get(role, set())) | expand_team_permissions(team_role)

         managed_orgs = []
         if user.managed_orgs:
@@ -566,6 +623,8 @@
             organization_id=user.organization_id,
             managed_orgs=managed_orgs,
             permissions=permissions,
+            team_id=team_id,
+            team_role=team_role,
         )

     def check_permission(
```

- [ ] **Step 4: Run it and the RBAC/gRPC suites that build contexts**

Run: `.venv/bin/python -m pytest tests/unit/test_rbac_team_resolution.py tests/unit/test_rbac.py tests/unit/test_rbac_additional.py tests/unit/test_rbac_auth_cache_support.py tests/unit/test_rbac_service_account.py tests/unit/test_grpc_auth_interceptor.py --no-cov -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add shared/auth/rbac.py tests/conftest.py tests/unit/test_rbac_additional.py tests/unit/test_rbac_team_resolution.py
git commit -m "feat(teams): resolve the acting team into UserContext with team-role scopes"
```

- [ ] **Phase 4a gate:** Step 4's command passes.
