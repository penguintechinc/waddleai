# Teams Subsystem — Phase 7b: Seating New Organizations (Default Team on Create and on First Boot)

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §9 (migration seats only *existing* data), §3 D2/D3, §7 (key `team_id` must resolve).
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Phases 1-7a (team service, `team_world`, `real_db_client`).

Migration 024 creates a Default team per org that exists at upgrade time. Anything created afterwards has no team, so its users' keys could not bind one. Tasks 28-30 close that: new orgs get a Default team, new users are seated in it, and key creation requires the owner to be in a team. Seating is **independent of the kill switch** (it is data, not behaviour): creating the rows while teams are disabled means re-enabling later does not strand anyone.

---

<!-- TASK n1 -->
## Task 28: New organizations get a Default team (org-create route and the first-boot bootstrap)

**Files:**
- Modify: `services/management/app/services/teams.py` (three helpers)
- Modify: `services/management/app/api/v1/organizations.py` (`create_organization`)
- Modify: `services/management/app/extensions.py` (`init_default_data`)
- Modify: `tests/unit/management/conftest.py` (extend the autouse mock-DB guard)
- Test: `tests/unit/management/test_team_seating_orgs.py` (create)

**Interfaces:**
- Produces in `services/teams.py`: `DEFAULT_TEAM_NAME = "Default"`; `ensure_default_team(database, org_id) -> int` (idempotent; a concurrent creator that wins the `UNIQUE(organization_id, name)` race is handled by re-reading); `default_team_role(org_role: str) -> str` (`admin` if the org role holds `org:admin_update`, else `member`, never `owner` -- the same rule migration 024 applies; unknown roles are members); `seat_new_user(database, user_id, org_id, org_role) -> int` (ensures the team and inserts the membership directly -- a brand-new user has no membership or keys, so `add_member`'s validation/adoption is unnecessary; used in Task 29).
- `POST /api/v1/organizations` creates the org's Default team in the same worker thread right after the insert (no cross-table transaction exists in penguin-dal; if the team insert fails the request is a 500 and `ensure_default_team` is idempotent, so a retry or the next user-create repairs it).
- First boot (`init_default_data`): the Default team is ensured for the "default" org, the bootstrap admin is seated as team **admin**, and the admin master key (minted straight into `api_keys`, never through `POST /keys`) is bound with `team_id=` -- otherwise the one key every fresh install relies on would act teamless forever. A second boot adds nothing.
- The legacy MagicMock-DB tests (`test_organization_routes.py`, `test_security_bootstrap_secrets.py`) feed canned rows to every query, so the autouse guard now also stubs `ensure_default_team`/`seat_new_user` for tests that do not request `team_world`/`real_schema_db`.

- [ ] **Step 1: Write the failing test and extend the guard**

```diff
--- a/tests/unit/management/conftest.py
+++ b/tests/unit/management/conftest.py
@@ -827,10 +827,19 @@

     Those tests wire ONE canned row to every ``db(...).select().first()`` call, so the new
     live team lookup in ``auth._acting_team`` would read that row as a ``team_members`` row.
-    Tests that opt in to a real database (the ``team_world`` fixture) keep the real lookup.
-    """
-    if "team_world" in request.fixturenames:
+    Tests that opt in to a real database (``team_world`` / ``real_schema_db``) keep the real
+    behaviour. The same canned rows would also confuse the Default-team seeding, so the
+    org-create route and the bootstrap seed are stubbed out here too.
+    """
+    if {"team_world", "real_schema_db"} & set(request.fixturenames):
         return
+    monkeypatch.setattr(
+        "services.management.app.api.v1.organizations.ensure_default_team", lambda *a, **kw: 1
+    )
+    monkeypatch.setattr(
+        "services.management.app.services.teams.ensure_default_team", lambda *a, **kw: 1
+    )
+    monkeypatch.setattr("services.management.app.services.teams.seat_new_user", lambda *a, **kw: 1)
     monkeypatch.setattr(
         "services.management.app.api.v1.auth._acting_team",
         AsyncMock(return_value=(None, None)),
```

```python
"""New organizations get a Default team (migration 024 only seats data that exists at upgrade).

Covers the service helpers, ``POST /api/v1/organizations`` and the first-boot bootstrap
(``init_default_data``) against a real database (``team_world`` / ``real_schema_db``).
"""

import pytest

from services.management.app.extensions import init_default_data
from services.management.app.services.teams import (
    DEFAULT_TEAM_NAME,
    default_team_role,
    ensure_default_team,
    seat_new_user,
)

_PLACEHOLDER_HASH = "x"  # noqa: S105 -- placeholder, never a loginable hash


def _team_rows(db, org_id: int) -> list:
    """Return every team row of ``org_id``."""
    return list(db(db.teams.organization_id == org_id).select())


def test_ensure_default_team_creates_it_once(team_world) -> None:
    """The first call creates ``Default`` (enabled); a second call returns the same id."""
    w = team_world
    first = ensure_default_team(w.db, w.org["a"])
    second = ensure_default_team(w.db, w.org["a"])
    assert first == second
    named = [t for t in _team_rows(w.db, w.org["a"]) if t.name == DEFAULT_TEAM_NAME]
    assert len(named) == 1
    assert named[0].enabled is True


def test_ensure_default_team_reuses_an_existing_default(team_world) -> None:
    """A Default team created by migration 024 (or an admin) is reused, never duplicated."""
    w = team_world
    existing = w.db.teams.insert(organization_id=w.org["b"], name=DEFAULT_TEAM_NAME, enabled=True)
    w.db.commit()
    assert ensure_default_team(w.db, w.org["b"]) == existing


@pytest.mark.parametrize(
    ("org_role", "team_role"),
    [("admin", "admin"), ("resource_manager", "member"), ("reporter", "member"), ("user", "member"),
     ("bogus", "member")],
)  # fmt: skip
def test_default_team_role_follows_the_org_admin_scope(org_role, team_role) -> None:
    """Same rule as migration 024: org-admin-scope holders become team admin, never owner."""
    assert default_team_role(org_role) == team_role


def test_seat_new_user_puts_them_in_the_orgs_default_team(team_world) -> None:
    """A user created after 024 lands in Default as member (admin for an org admin)."""
    w = team_world
    new_user = w.db.users.insert(
        username="newbie", email="newbie@example.com", password_hash=_PLACEHOLDER_HASH, role="user",
        organization_id=w.org["a"], enabled=True,
    )  # fmt: skip
    w.db.commit()
    team_id = seat_new_user(w.db, new_user, w.org["a"], "user")
    row = w.db(w.db.team_members.user_id == new_user).select().first()
    assert (row.team_id, row.team_role, row.organization_id) == (team_id, "member", w.org["a"])


async def test_creating_an_org_creates_its_default_team(
    team_world, real_db_client, team_headers
) -> None:
    """POST /organizations: the new org has exactly one enabled Default team."""
    resp = await real_db_client.post(
        "/api/v1/organizations", json={"name": "newco"}, headers=team_headers("boss")
    )
    assert resp.status_code == 201
    org_id = (await resp.get_json())["id"]
    teams = _team_rows(team_world.db, org_id)
    assert [(t.name, t.enabled) for t in teams] == [(DEFAULT_TEAM_NAME, True)]


def test_bootstrap_seats_the_admin_and_binds_the_master_key(real_schema_db) -> None:
    """First boot: Default org gets a Default team, the admin is its team admin, the key is bound.

    The bootstrap admin key is minted straight into ``api_keys`` (it never goes through
    ``POST /keys``), so it must be bound to the team here or it would act teamless forever.
    """
    db = real_schema_db
    init_default_data(db, config={"ADMIN_INITIAL_PASSWORD": "bootstrap-pw-123"})

    org = db(db.organizations.name == "default").select().first()
    admin = db(db.users.username == "admin").select().first()
    team = db(db.teams.organization_id == org.id).select().first()
    member = db(db.team_members.user_id == admin.id).select().first()
    key = db(db.api_keys.user_id == admin.id).select().first()
    assert team.name == DEFAULT_TEAM_NAME
    assert (member.team_id, member.team_role) == (team.id, "admin")
    assert key.team_id == team.id


def test_bootstrap_is_idempotent_for_teams(real_schema_db) -> None:
    """A second boot creates no second Default team and no second membership."""
    db = real_schema_db
    config = {"ADMIN_INITIAL_PASSWORD": "bootstrap-pw-123"}
    init_default_data(db, config=config)
    init_default_data(db, config=config)
    assert db(db.teams.id > 0).count() == 1
    assert db(db.team_members.id > 0).count() == 1
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/management/test_team_seating_orgs.py --no-cov -q -x`
Expected: collection error `ImportError: cannot import name 'DEFAULT_TEAM_NAME' from 'services.management.app.services.teams'`.

- [ ] **Step 3: Implement**

```diff
--- a/services/management/app/services/teams.py
+++ b/services/management/app/services/teams.py
@@ -19,10 +19,14 @@
 from penguin_dal.db import DB
 from sqlalchemy.exc import IntegrityError

-from shared.auth.rbac import TeamRole
+from shared.auth.rbac import ROLE_PERMISSIONS, Permission, Role, TeamRole

 _ROLES = frozenset(r.value for r in TeamRole)
 _MAX_NAME = 255
+
+#: Name of the team every organization gets (migration 024 seeds it for existing orgs;
+#: the org-create route and the bootstrap seed it for new ones).
+DEFAULT_TEAM_NAME = "Default"


 class TeamError(Exception):
@@ -126,6 +130,56 @@
     database(database.teams.id == team.id).delete()
     database.commit()
     return stale
+
+
+def ensure_default_team(database: DB, org_id: int) -> int:
+    """Return the id of ``org_id``'s Default team, creating it if absent (idempotent).
+
+    Used for organizations created after migration 024. A concurrent creator that wins the
+    ``UNIQUE(organization_id, name)`` race is handled by re-reading instead of failing.
+    """
+    query = (database.teams.organization_id == org_id) & (database.teams.name == DEFAULT_TEAM_NAME)
+    existing = database(query).select().first()
+    if existing is not None:
+        return int(existing.id)
+    try:
+        return create_team(database, org_id, DEFAULT_TEAM_NAME)
+    except TeamError:
+        raced = database(query).select().first()
+        if raced is None:
+            raise
+        return int(raced.id)
+
+
+def default_team_role(org_role: str) -> str:
+    """Team role a newly seated user gets: ``admin`` for org-admin-scope holders, else ``member``.
+
+    The same rule migration 024 applies to existing users; never ``owner`` (an org admin
+    designates one explicitly). An unrecognized org role is a plain member.
+    """
+    try:
+        role = Role(org_role)
+    except ValueError:
+        return "member"
+    return "admin" if Permission.ORG_ADMIN_UPDATE in ROLE_PERMISSIONS[role] else "member"
+
+
+def seat_new_user(database: DB, user_id: int, org_id: int, org_role: str) -> int:
+    """Seat a just-created user in their organization's Default team; return the team id.
+
+    A brand-new user has no membership and no keys, so this skips ``add_member``'s
+    validation and adoption steps and inserts the membership directly.
+    """
+    team_id = ensure_default_team(database, org_id)
+    database.team_members.insert(
+        team_id=team_id,
+        user_id=user_id,
+        organization_id=org_id,
+        team_role=default_team_role(org_role),
+        created_at=datetime.utcnow(),
+    )
+    database.commit()
+    return team_id


 def add_member(
```

```diff
--- a/services/management/app/api/v1/organizations.py
+++ b/services/management/app/api/v1/organizations.py
@@ -12,6 +12,7 @@

 from ...extensions import db
 from ...services.cilium_policy import CiliumPolicyReconciler
+from ...services.teams import ensure_default_team
 from . import api_v1_bp
 from ._pagination import PageRequest
 from .auth import require_auth, require_scope
@@ -315,6 +316,9 @@
             created_at=datetime.utcnow(),
         )
         database.commit()
+        # Every organization gets a Default team (teams spec: migration 024 only seats
+        # data that exists at upgrade time; new orgs need theirs created here).
+        ensure_default_team(database, new_org_id)
         return new_org_id

     org_id = await asyncio.to_thread(_insert)
```

```diff
--- a/services/management/app/extensions.py
+++ b/services/management/app/extensions.py
@@ -252,6 +252,11 @@
             raise RuntimeError("default organization vanished between select and first()")
         org_id = existing_org.id

+    # Every organization has a Default team; the admin and the master key join it below.
+    from .services.teams import ensure_default_team, seat_new_user
+
+    default_team_id = ensure_default_team(db, org_id)
+
     # Create admin user if doesn't exist
     if not db(db.users.username == "admin").select():
         admin_id = db.users.insert(
@@ -265,6 +270,8 @@
             enabled=True,
             created_at=datetime.utcnow(),
         )
+
+        seat_new_user(db, admin_id, org_id, "admin")

         # Create the admin master key. audit-2026-09-23 M3: the O(1) API-key
         # lookups now resolve a key from the value itself, so the value MUST
@@ -306,6 +313,7 @@
             name="Admin Master Key",
             api_access_level="admin_api",
             permissions={"*": True},
+            team_id=default_team_id,
         )

         logger.info("Created admin user, API key, and virtual key")
```

- [ ] **Step 4: Run it and the legacy suites it could disturb**

Run: `.venv/bin/python -m pytest tests/unit/management/test_team_seating_orgs.py tests/unit/management/test_security_bootstrap_secrets.py tests/unit/management/test_organization_routes.py --no-cov -q`
Expected: all pass (`11 passed` in the new file).

- [ ] **Step 5: Commit**

```bash
git add services/management/app/services/teams.py services/management/app/api/v1/organizations.py services/management/app/extensions.py tests/unit/management/conftest.py tests/unit/management/test_team_seating_orgs.py
git commit -m "feat(teams): new organizations and the bootstrap org get a Default team"
```

- [ ] **Phase 7b gate:** Step 4 passes.
