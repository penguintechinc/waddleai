# Teams Subsystem — Phase 7c: Seating New Users and Requiring a Team on Key Create

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §9, §7 ("Key creation must select a team the creating principal is authorized for"), §3 D2/D3.
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Phase 7b (`seat_new_user`, `ensure_default_team`).

---

<!-- TASK n2 -->
## Task 29: New users are seated in their organization's Default team

**Files:**
- Modify: `services/management/app/api/v1/users.py` (`create_user`)
- Modify: `tests/unit/management/conftest.py` (guard stub for `users.seat_new_user`)
- Test: `tests/unit/management/test_team_seating_users.py` (create)

**Interfaces:**
- Consumes: `seat_new_user(database, user_id, org_id, org_role)`.
- `POST /api/v1/users` seats the user right after the insert, inside the same worker thread, in the **target** org's Default team (an org admin creating a user in another org seats them there; the team is created on demand): `member`, or team `admin` when the user's org role holds `org:admin_update`. A request rejected before the insert (e.g. the 409 duplicate username) seats nobody. There is deliberately no `team_id` parameter: an admin moves a user afterwards with the membership API (one team per org, D2).
- Review Focus: two new users share one Default team (no duplicate team).

- [ ] **Step 1: Write the failing test and the guard stub**

```diff
--- a/tests/unit/management/conftest.py
+++ b/tests/unit/management/conftest.py
@@ -836,6 +836,7 @@
     monkeypatch.setattr(
         "services.management.app.api.v1.organizations.ensure_default_team", lambda *a, **kw: 1
     )
+    monkeypatch.setattr("services.management.app.api.v1.users.seat_new_user", lambda *a, **kw: 1)
     monkeypatch.setattr(
         "services.management.app.services.teams.ensure_default_team", lambda *a, **kw: 1
     )
```

```python
"""New users are seated in their organization's Default team (024 seats existing ones only).

``POST /api/v1/users`` against a real database (``team_world`` + ``real_db_client``).
"""

import secrets

import pytest

from services.management.app.services.teams import DEFAULT_TEAM_NAME

_NEW_PASSWORD = secrets.token_urlsafe(16)  # random per run; the route only needs a non-empty one


def _membership(db, user_id: int):
    """Return the user's single ``team_members`` row, or ``None``."""
    return db(db.team_members.user_id == user_id).select().first()


async def _create_user(client, headers, **over) -> dict:
    """POST /users with sane defaults and return the JSON body (asserting 201)."""
    payload = {"username": "nina", "email": "nina@example.com", "password": _NEW_PASSWORD}
    payload.update(over)
    resp = await client.post("/api/v1/users", json=payload, headers=headers)
    assert resp.status_code == 201, await resp.get_data()
    return await resp.get_json()


@pytest.mark.parametrize(("role", "team_role"), [("user", "member"), ("admin", "admin")])
async def test_new_user_joins_the_default_team(
    team_world, real_db_client, team_headers, role, team_role
) -> None:
    """A user created in org a joins a Default team there: member, or admin for an org admin."""
    w = team_world
    body = await _create_user(real_db_client, team_headers("boss"), role=role)
    row = _membership(w.db, body["id"])
    team = w.db.teams[row.team_id]
    assert (team.name, team.organization_id) == (DEFAULT_TEAM_NAME, w.org["a"])
    assert (row.organization_id, row.team_role) == (w.org["a"], team_role)


async def test_user_created_in_another_org_joins_that_orgs_default_team(
    team_world, real_db_client, team_headers
) -> None:
    """The org admin creating a user in org b seats them in b's Default team (created on demand)."""
    w = team_world
    body = await _create_user(
        real_db_client, team_headers("boss"), organization_id=w.org["b"], username="bob2",
        email="bob2@example.com",
    )  # fmt: skip
    team = w.db.teams[_membership(w.db, body["id"]).team_id]
    assert (team.name, team.organization_id) == (DEFAULT_TEAM_NAME, w.org["b"])


async def test_two_new_users_share_one_default_team(
    team_world, real_db_client, team_headers
) -> None:
    """Seating is idempotent on the team: the second user reuses the first user's Default team."""
    w = team_world
    one = await _create_user(real_db_client, team_headers("boss"))
    two = await _create_user(
        real_db_client, team_headers("boss"), username="omar", email="omar@example.com"
    )
    assert _membership(w.db, one["id"]).team_id == _membership(w.db, two["id"]).team_id
    named = w.db((w.db.teams.organization_id == w.org["a"]) & (w.db.teams.name == "Default"))
    assert named.count() == 1


async def test_a_rejected_create_seats_nobody(team_world, real_db_client, team_headers) -> None:
    """A 409 (duplicate username) must not leave a stray membership behind."""
    w = team_world
    before = w.db(w.db.team_members.id > 0).count()
    resp = await real_db_client.post(
        "/api/v1/users",
        json={"username": "mia", "email": "other@example.com", "password": _NEW_PASSWORD},
        headers=team_headers("boss"),
    )
    assert resp.status_code == 409
    assert w.db(w.db.team_members.id > 0).count() == before
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/management/test_team_seating_users.py --no-cov -q -x`
Expected: FAIL `AttributeError: 'NoneType' object has no attribute 'team_id'` (the new user has no membership).

- [ ] **Step 3: Implement**

```diff
--- a/services/management/app/api/v1/users.py
+++ b/services/management/app/api/v1/users.py
@@ -13,6 +13,7 @@

 from ... import dsar
 from ...extensions import db
+from ...services.teams import seat_new_user
 from . import api_v1_bp
 from ._pagination import PageRequest
 from .auth import require_auth, require_scope
@@ -393,6 +394,9 @@
             created_at=datetime.utcnow(),
         )
         database.commit()
+        # Seat the user in their org's Default team (migration 024 only seats users that exist
+        # at upgrade time); admin for an org admin, else member.
+        seat_new_user(database, new_user_id, target_org_id, role)
         return new_user_id

     user_id = await asyncio.to_thread(_insert)
```

- [ ] **Step 4: Run it and the legacy user route suite**

Run: `.venv/bin/python -m pytest tests/unit/management/test_team_seating_users.py tests/unit/management/test_user_routes.py --no-cov -q`
Expected: all pass (`47 passed` for the two files together).

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/users.py tests/unit/management/conftest.py tests/unit/management/test_team_seating_users.py
git commit -m "feat(teams): new users join their organization's Default team"
```

---

<!-- TASK n3 -->
## Task 30: Key creation requires the owner to be in a team

**Files:**
- Modify: `services/management/app/services/key_minting.py` (`resolve_key_team`)
- Modify: `tests/unit/management/test_key_team_resolution.py`, `tests/unit/management/test_create_key_teams.py`, `tests/unit/management/test_teams_e2e.py`

**Interfaces:**
- Behaviour change: with teams enabled, `resolve_key_team` with no team requested now **raises `KeyTeamError(409)`** when the key owner is in no team, instead of returning `None`. Combined with Tasks 28-29 (every org and user is seated) this makes the invariant "every key acts in a team": a user removed from their team must be re-added before they can mint keys. An *explicit* `team_id` was already validated (exists in the tenant, enabled, caller authorized, owner a member). With the kill switch ON nothing changes: default `None`, explicit team -> 404.
- Existing teamless keys are unaffected: they keep authenticating (teamless) and are adopted into a team when their owner joins one (Task 22). The E2E lifecycle test now builds such a key with `mint_proxy_key(team_id=None)` because the API can no longer create one.
- Review Focus: a teamless owner cannot get a key (409, nothing minted) -- `test_user_in_no_team_cannot_create_a_key`.

- [ ] **Step 1: Update the tests (they now expect the 409)**

```diff
--- a/tests/unit/management/test_key_team_resolution.py
+++ b/tests/unit/management/test_key_team_resolution.py
@@ -27,9 +27,15 @@


 def test_default_is_the_target_users_own_team(team_world) -> None:
-    """No team requested: bind the key to the owner's single team, or None if teamless."""
+    """No team requested: bind the key to the owner's single team."""
     assert _resolve(team_world, caller="mia") == team_world.t["a1"]
-    assert _resolve(team_world, caller="loner") is None
+
+
+def test_default_requires_the_owner_to_be_in_a_team(team_world) -> None:
+    """An owner in no team cannot get a key: 409 (every key acts in a team)."""
+    with pytest.raises(KeyTeamError) as exc:
+        _resolve(team_world, caller="loner")
+    assert exc.value.status == 409


 def test_default_for_another_user_uses_that_users_team(team_world) -> None:
```

```diff
--- a/tests/unit/management/test_create_key_teams.py
+++ b/tests/unit/management/test_create_key_teams.py
@@ -39,17 +39,14 @@
     assert (ctx.team_id, ctx.team_role) == (team_world.t["a1"], "member")


-async def test_teamless_user_gets_a_teamless_but_working_key(
-    team_world, real_db_client, mint_token
-) -> None:
-    """Loner has no team: the key is created teamless and still authenticates."""
+async def test_user_in_no_team_cannot_create_a_key(team_world, real_db_client, mint_token) -> None:
+    """Loner is in no team: key creation is refused (409) and nothing is minted."""
+    before = team_world.db(team_world.db.api_keys.id > 0).count()
     resp = await real_db_client.post(
         "/api/v1/keys", json={"name": "ci"}, headers=_hdr(mint_token, team_world, "loner")
     )
-    assert resp.status_code == 201
-    body = await resp.get_json()
-    assert body["team_id"] is None
-    assert RBACManager(team_world.db).authenticate_api_key(body["api_key"]).team_id is None
+    assert resp.status_code == 409
+    assert team_world.db(team_world.db.api_keys.id > 0).count() == before


 @pytest.mark.parametrize(
```

```diff
--- a/tests/unit/management/test_teams_e2e.py
+++ b/tests/unit/management/test_teams_e2e.py
@@ -8,6 +8,7 @@
 import pytest

 from proxy.apps.proxy_server.auth_cache import ApiKeyAuthCache, ApiKeyAuthenticator
+from services.management.app.services.key_minting import mint_proxy_key
 from shared.auth.rbac import Permission, RBACManager

 P = Permission
@@ -30,7 +31,7 @@
 async def test_lifecycle_membership_changes_reach_the_proxy(
     team_world, real_db_client, team_headers, proxy
 ) -> None:
-    """Org admin builds a team; the member's mgmt-created key acts in it at the proxy.
+    """Org admin builds a team; a member's keys act in it at the proxy.

     # regression: gh-303 (key works at the proxy) + D7 (every membership change is visible there
     once management invalidates): added -> promoted -> removed, same key throughout.
@@ -42,8 +43,18 @@
     assert created.status_code == 201
     ops = (await created.get_json())["id"]

-    # loner has no team yet: their management-created key is teamless but works at the proxy.
-    first = await _create_key(real_db_client, loner)
+    # loner is in no team, so the API refuses to mint them a key (every key acts in a team).
+    refused = await real_db_client.post("/api/v1/keys", json={"name": "e2e"}, headers=loner)
+    assert refused.status_code == 409
+
+    # A pre-existing teamless key (e.g. minted before they left their team) still works at the
+    # proxy, teamless -- and is adopted when they join a team.
+    legacy = mint_proxy_key(
+        w.db, user_id=w.u["loner"], org_id=w.org["a"], name="old", owner_role="user",
+        team_id=None, expires_at=None, allowed_models=None, allowed_providers=None,
+        budget_limit_daily=None, budget_limit_monthly=None, tpm_limit=None, rpm_limit=None,
+    )  # fmt: skip
+    first = {"api_key": legacy.api_key}
     ctx = await proxy.authenticate(first["api_key"])
     assert (ctx.user_id, ctx.team_id) == (w.u["loner"], None)
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `.venv/bin/python -m pytest tests/unit/management/test_key_team_resolution.py tests/unit/management/test_create_key_teams.py --no-cov -q`
Expected: FAIL `Failed: DID NOT RAISE <class '...KeyTeamError'>` and `assert 201 == 409`.

- [ ] **Step 3: Implement**

```diff
--- a/services/management/app/services/key_minting.py
+++ b/services/management/app/services/key_minting.py
@@ -79,7 +79,9 @@
 ) -> int | None:
     """Pick the acting team for a key being created for ``target_user_id`` in ``org_id``.

-    No team requested: the key owner's own team in the tenant (``None`` if teamless). A team
+    No team requested: the key owner's own team in the tenant -- **required**: with teams
+    enabled an owner in no team is a 409, so every new key acts in a team (users are seated
+    on creation; an owner removed from their team must be re-added first). A team
     requested: it must exist in the tenant and be enabled; the CALLER must be allowed to issue
     into it (org admin via ``team:org_admin``; otherwise a live member holding ``team:key_create``
     for their own key or ``team:key_admin`` for someone else's); and the key OWNER must be a
@@ -88,7 +90,7 @@

     Raises:
         KeyTeamError: 404 unknown/foreign team or teams disabled, 403 caller not authorized,
-            409 team disabled, 400 key owner not a member.
+            409 team disabled or the key owner is in no team, 400 key owner not a member.

     """
     if not teams_enabled(str(org_id)):
@@ -98,6 +100,11 @@

     if requested_team_id is None:
         team_id, _role = lookup_user_team(database, target_user_id, org_id)
+        if team_id is None:
+            raise KeyTeamError(
+                "Key owner is not in a team in this organization; add them to a team first",
+                409,
+            )
         return team_id

     team = database(database.teams.id == requested_team_id).select().first()
```

- [ ] **Step 4: Run them**

Run: `.venv/bin/python -m pytest tests/unit/management/test_key_team_resolution.py tests/unit/management/test_create_key_teams.py tests/unit/management/test_teams_e2e.py tests/unit/management/test_key_routes.py --no-cov -q`
Expected: `83 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/services/key_minting.py tests/unit/management/test_key_team_resolution.py tests/unit/management/test_create_key_teams.py tests/unit/management/test_teams_e2e.py
git commit -m "feat(teams): key creation requires the owner to be in a team"
```

- [ ] **Phase 7c gate:** Step 4 of both tasks passes.
