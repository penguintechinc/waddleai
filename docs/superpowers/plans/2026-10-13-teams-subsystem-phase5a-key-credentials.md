# Teams Subsystem — Phase 5a: Shared Credential Minting and Key-Team Policy

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §7 ("Key-create path fix"), §8 (issue #303), D3/D10.
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Phases 1-4 (`api_keys.team_id`, `lookup_user_team`, `teams_enabled`, `team_world`).

**The bug (issue #303):** management `POST /keys` mints `wa-{secret}` into `virtual_keys`; the proxy only authenticates `wa-{key_id}-{secret}` rows in `api_keys` (`parse_wa_key_id` + `fetch_key_and_user`). The only bridge today is the bootstrap admin key, which writes the same plaintext into both tables. Phases 5a-5d make every management-created key work at the proxy, bound to a team.

---

<!-- TASK 15 -->
## Task 16: One place that mints `wa-{key_id}-{secret}`; `create_api_key` takes a team

**Files:**
- Modify: `shared/auth/rbac.py`
- Test: `tests/unit/test_rbac_create_api_key_team.py` (create)

**Interfaces:**
- Produces: `mint_wa_credential() -> tuple[str, str]` (`(key_id, api_key)`: 16 hex chars -- never contains `-`, so `parse_wa_key_id` recovers it -- plus a 43-char urlsafe secret that MAY contain `-`); `access_level_for_role(role: str) -> str` (`admin` -> `admin_api`, `resource_manager` -> `management_api`, else `proxy_api`); `RBACManager.create_api_key(user_context, name, permissions=None, expires_days=None, team_id=None) -> (api_key, row_id)` now uses both and writes `team_id`.
- Gotcha fixed here: `create_api_key` now inserts `enabled=True` explicitly. Auth filters `enabled == True` and a SQLAlchemy-built schema has no server-side default, so omitting it minted keys that could never authenticate.

- [ ] **Step 1: Write the failing test**

```python
"""Credential minting shared by both planes + team-bound ``create_api_key`` (teams spec §7, D10).

``mint_wa_credential`` is the single place a ``wa-{key_id}-{secret}`` credential is born, so the
management endpoint and ``RBACManager.create_api_key`` cannot drift from what the proxy parses.
"""

import re

import pytest

from shared.auth.rbac import (
    ROLE_PERMISSIONS,
    RBACManager,
    Role,
    UserContext,
    access_level_for_role,
    mint_wa_credential,
    parse_wa_key_id,
)

_FORMAT = re.compile(r"^wa-[0-9a-f]{16}-[A-Za-z0-9_-]{43}$")


def test_minted_credential_has_the_proxy_parsable_shape() -> None:
    """``wa-{16 hex key_id}-{43-char urlsafe secret}``; the proxy parser recovers the key_id."""
    for _ in range(50):
        key_id, api_key = mint_wa_credential()
        assert _FORMAT.match(api_key), api_key
        assert parse_wa_key_id(api_key) == key_id
        assert "-" not in key_id


def test_minted_credentials_are_unique() -> None:
    """No collisions across a batch (key_id is the unique api_keys column)."""
    ids = {mint_wa_credential()[0] for _ in range(200)}
    assert len(ids) == 200


@pytest.mark.parametrize(
    ("role", "level"),
    [("admin", "admin_api"), ("resource_manager", "management_api"),
     ("reporter", "proxy_api"), ("user", "proxy_api"), ("bogus", "proxy_api")],
)  # fmt: skip
def test_access_level_for_role(role, level) -> None:
    """Admin -> admin_api, resource_manager -> management_api, everything else proxy_api."""
    assert access_level_for_role(role) == level


def test_create_api_key_binds_the_team_and_authenticates_with_it(team_world) -> None:
    """A key created for mia with team_id=a1 authenticates at the proxy as a1/member."""
    w = team_world
    owner = UserContext(
        user_id=w.u["mia"], username="mia", role=Role.USER, organization_id=w.org["a"],
        managed_orgs=[], permissions=ROLE_PERMISSIONS[Role.USER],
    )  # fmt: skip
    rbac = RBACManager(w.db)
    api_key, row_id = rbac.create_api_key(owner, name="ci", team_id=w.t["a1"])
    w.db.commit()

    assert w.db.api_keys[row_id].team_id == w.t["a1"]
    ctx = rbac.authenticate_api_key(api_key)
    assert (ctx.team_id, ctx.team_role) == (w.t["a1"], "member")


def test_create_api_key_without_team_stays_teamless(team_world) -> None:
    """Default behaviour unchanged: no team_id -> NULL column -> teamless context."""
    w = team_world
    owner = UserContext(
        user_id=w.u["mia"], username="mia", role=Role.USER, organization_id=w.org["a"],
        managed_orgs=[], permissions=ROLE_PERMISSIONS[Role.USER],
    )  # fmt: skip
    rbac = RBACManager(w.db)
    api_key, row_id = rbac.create_api_key(owner, name="ci")
    w.db.commit()
    assert w.db.api_keys[row_id].team_id is None
    assert rbac.authenticate_api_key(api_key).team_id is None
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_rbac_create_api_key_team.py --no-cov -q -x`
Expected: collection error `ImportError: cannot import name 'access_level_for_role' from 'shared.auth.rbac'`.

- [ ] **Step 3: Implement**

```diff
--- a/shared/auth/rbac.py
+++ b/shared/auth/rbac.py
@@ -4,6 +4,7 @@
 """

 import functools
+import secrets
 from dataclasses import dataclass
 from datetime import datetime, timedelta
 from enum import Enum
@@ -434,6 +435,27 @@
     return parts[1]


+def mint_wa_credential() -> tuple[str, str]:
+    """Mint a fresh ``wa-{key_id}-{secret}`` credential; return ``(key_id, api_key)``.
+
+    The single place the proxy-authenticated credential format is produced: ``key_id`` is 16 hex
+    chars (never contains ``-``) so :func:`parse_wa_key_id` recovers it from the credential
+    alone, and the secret is 43 urlsafe chars (it may contain ``-``). Used by both
+    :meth:`RBACManager.create_api_key` and the management key-create endpoint.
+    """
+    key_id = secrets.token_hex(8)
+    return key_id, f"wa-{key_id}-{secrets.token_urlsafe(32)}"
+
+
+def access_level_for_role(role: str) -> str:
+    """Map an org role string to the ``api_keys.api_access_level`` a key it owns is issued at."""
+    if role == Role.ADMIN.value:
+        return "admin_api"
+    if role == Role.RESOURCE_MANAGER.value:
+        return "management_api"
+    return "proxy_api"
+
+
 class AuthenticationError(Exception):
     """Authentication failed."""

@@ -718,14 +740,11 @@
         name: str,
         permissions: dict[str, bool] | None = None,
         expires_days: int | None = None,
+        team_id: int | None = None,
     ) -> tuple[str, str]:
-        """Create new API key for user."""
-        import secrets
-
+        """Create new API key for user, optionally bound to an acting team (teams spec D3)."""
         # Generate API key
-        key_id = secrets.token_hex(8)
-        secret = secrets.token_urlsafe(32)
-        api_key = f"wa-{key_id}-{secret}"
+        key_id, api_key = mint_wa_credential()

         # Hash the API key
         key_hash = bcrypt.hash(api_key)
@@ -735,11 +754,7 @@
             expires_at = datetime.utcnow() + timedelta(days=expires_days)

         # Determine access level based on user role
-        access_level = "proxy_api"
-        if user_context.role in [Role.ADMIN, Role.RESOURCE_MANAGER]:
-            access_level = "management_api"
-        if user_context.role == Role.ADMIN:
-            access_level = "admin_api"
+        access_level = access_level_for_role(user_context.role.value)

         # Insert into database
         key_record_id = self.db.api_keys.insert(
@@ -751,6 +766,10 @@
             permissions=permissions or {},
             api_access_level=access_level,
             expires_at=expires_at,
+            team_id=team_id,
+            # Explicit: auth filters ``enabled == True`` and a SQLAlchemy-built schema has no
+            # server-side default, so omitting it mints a key that can never authenticate.
+            enabled=True,
         )

         return api_key, key_record_id
```

- [ ] **Step 4: Run it and the RBAC suites**

Run: `.venv/bin/python -m pytest tests/unit/test_rbac_create_api_key_team.py tests/unit/test_rbac.py tests/unit/test_rbac_additional.py --no-cov -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add shared/auth/rbac.py tests/unit/test_rbac_create_api_key_team.py
git commit -m "feat(teams): single wa-{key_id}-{secret} mint; create_api_key binds a team"
```

---

<!-- TASK 17 -->
## Task 17: `resolve_key_team` -- which team may this caller bind a new key to?

**Files:**
- Create: `services/management/app/services/key_minting.py`
- Test: `tests/unit/management/test_key_team_resolution.py` (create)

**Interfaces:**
- Consumes: `lookup_user_team`, `expand_team_permissions`, `teams_enabled`, `Permission.TEAM_KEY_CREATE/TEAM_KEY_ADMIN/TEAM_ORG_ADMIN`.
- Produces: `KeyTeamError(message, status)` and `resolve_key_team(database, *, org_id, target_user_id, requested_team_id, caller_user_id, caller_scopes) -> int | None`. No team requested -> the key owner's own team (or `None`). A team requested -> it must exist **in the tenant** (404 otherwise, no existence leak) and be enabled (409); the CALLER needs `team:org_admin`, or a live membership holding `team:key_create` (own key) / `team:key_admin` (someone else's) -> else 403; the key OWNER must be a member (400). **Authorization is checked before the owner's membership** so an unauthorized caller cannot probe who belongs to a team. Kill switch ON: default `None`, an explicit team is a 404.
- Review Focus: cross-team access attempt (a member of a2 naming a1 is a 403, not a 400).

- [ ] **Step 1: Write the failing test**

```python
"""Which team does a newly created key act in? (teams spec §7).

The key must be bound to a team the creating principal is authorized for. Real database via
``team_world``.
"""

import pytest

from services.management.app.services.key_minting import KeyTeamError, resolve_key_team
from shared.auth.rbac import ROLE_PERMISSIONS, Permission, Role

_USER_SCOPES = {p.value for p in ROLE_PERMISSIONS[Role.USER]}
_ADMIN_SCOPES = {p.value for p in ROLE_PERMISSIONS[Role.ADMIN]}


def _resolve(w, *, caller: str, target: str | None = None, team: str | None = None, org="a"):
    """Call ``resolve_key_team`` as ``caller`` creating a key for ``target`` (default: self)."""
    scopes = _ADMIN_SCOPES if caller == "boss" else _USER_SCOPES
    return resolve_key_team(
        w.db,
        org_id=w.org[org],
        target_user_id=w.u[target or caller],
        requested_team_id=w.t[team] if team else None,
        caller_user_id=w.u[caller],
        caller_scopes=scopes,
    )


def test_default_is_the_target_users_own_team(team_world) -> None:
    """No team requested: bind the key to the owner's single team, or None if teamless."""
    assert _resolve(team_world, caller="mia") == team_world.t["a1"]
    assert _resolve(team_world, caller="loner") is None


def test_default_for_another_user_uses_that_users_team(team_world) -> None:
    """An org admin minting for mia without naming a team binds mia's team, not the admin's."""
    assert _resolve(team_world, caller="boss", target="mia") == team_world.t["a1"]


def test_member_may_bind_their_own_key_to_their_team(team_world) -> None:
    """Explicit team = own team: Member holds team:key_create."""
    assert _resolve(team_world, caller="mia", team="a1") == team_world.t["a1"]


def test_viewer_cannot_explicitly_bind_a_key(team_world) -> None:
    """Viewer has no team:key_create -> 403 (no spend)."""
    with pytest.raises(KeyTeamError) as exc:
        _resolve(team_world, caller="vic", team="a1")
    assert exc.value.status == 403


def test_member_cannot_issue_for_a_teammate(team_world) -> None:
    """Issuing for ANOTHER user needs team:key_admin (Admin+) -> member gets 403."""
    with pytest.raises(KeyTeamError) as exc:
        _resolve(team_world, caller="mia", target="vic", team="a1")
    assert exc.value.status == 403


def test_team_admin_may_issue_for_a_teammate(team_world) -> None:
    """Adam (team admin) binds a key for vic to a1."""
    assert _resolve(team_world, caller="adam", target="vic", team="a1") == team_world.t["a1"]


def test_cross_team_request_is_forbidden(team_world) -> None:
    """Nat (member of a2) asking for a1 is refused -- authorization precedes membership checks."""
    with pytest.raises(KeyTeamError) as exc:
        _resolve(team_world, caller="nat", team="a1")
    assert exc.value.status == 403


def test_org_admin_may_bind_any_member_but_not_a_non_member(team_world) -> None:
    """Boss may name a1 for mia; for loner (not in a1) the key would be teamless -> 400."""
    assert _resolve(team_world, caller="boss", target="mia", team="a1") == team_world.t["a1"]
    with pytest.raises(KeyTeamError) as exc:
        _resolve(team_world, caller="boss", target="loner", team="a1")
    assert exc.value.status == 400


def test_team_in_another_tenant_or_missing_is_404(team_world) -> None:
    """A team id outside the caller's tenant (or nonexistent) is a 404 -- no existence leak."""
    w = team_world
    with pytest.raises(KeyTeamError) as exc:
        resolve_key_team(
            w.db, org_id=w.org["a"], target_user_id=w.u["mia"], requested_team_id=w.t["b1"],
            caller_user_id=w.u["boss"], caller_scopes=_ADMIN_SCOPES,
        )  # fmt: skip
    assert exc.value.status == 404
    with pytest.raises(KeyTeamError) as exc2:
        resolve_key_team(
            w.db, org_id=w.org["a"], target_user_id=w.u["mia"], requested_team_id=99999,
            caller_user_id=w.u["boss"], caller_scopes=_ADMIN_SCOPES,
        )  # fmt: skip
    assert exc2.value.status == 404


def test_disabled_team_cannot_receive_new_keys(team_world) -> None:
    """A disabled team is refused with 409 even for the org admin."""
    db = team_world.db
    db(db.teams.id == team_world.t["a1"]).update(enabled=False)
    db.commit()
    with pytest.raises(KeyTeamError) as exc:
        _resolve(team_world, caller="boss", target="mia", team="a1")
    assert exc.value.status == 409


def test_kill_switch_ignores_default_and_rejects_explicit(team_world, monkeypatch) -> None:
    """``waddleai.disable-teams`` ON: default is teamless; naming a team is a 404."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", "1")
    assert _resolve(team_world, caller="mia") is None
    with pytest.raises(KeyTeamError) as exc:
        _resolve(team_world, caller="mia", team="a1")
    assert exc.value.status == 404


def test_permission_constants_used_are_the_team_key_pair() -> None:
    """Guard: the two permissions this policy keys on exist with their spec names."""
    assert Permission.TEAM_KEY_CREATE.value == "team:key_create"
    assert Permission.TEAM_KEY_ADMIN.value == "team:key_admin"
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/management/test_key_team_resolution.py --no-cov -q -x`
Expected: collection error `ModuleNotFoundError: No module named 'services.management.app.services.key_minting'`.

- [ ] **Step 3: Implement**

```python
"""Proxy-key issuance for the management plane (teams spec §7, D3/D10; closes issue #303).

The management ``POST /keys`` endpoint historically minted ``wa-{secret}`` into ``virtual_keys``
only -- a credential the proxy can never authenticate. This module is the one place that decides
which team a new key acts in and mints the proxy-parsable ``wa-{key_id}-{secret}`` credential.
"""

import logging
from collections.abc import Collection

from penguin_dal.db import DB

from shared.auth.rbac import (
    Permission,
    expand_team_permissions,
    lookup_user_team,
)
from shared.auth.team_flags import teams_enabled

logger = logging.getLogger(__name__)


class KeyTeamError(Exception):
    """A requested key team is invalid or the caller may not use it; carries the HTTP status."""

    def __init__(self, message: str, status: int) -> None:
        """Record the user-facing message and the HTTP status to answer with."""
        super().__init__(message)
        self.message = message
        self.status = status


def _membership(database: DB, team_id: int, user_id: int):
    """Return the ``team_members`` row for (team, user), or ``None``."""
    return (
        database(
            (database.team_members.team_id == team_id) & (database.team_members.user_id == user_id)
        )
        .select()
        .first()
    )


def resolve_key_team(
    database: DB,
    *,
    org_id: int,
    target_user_id: int,
    requested_team_id: int | None,
    caller_user_id: int,
    caller_scopes: Collection[str],
) -> int | None:
    """Pick the acting team for a key being created for ``target_user_id`` in ``org_id``.

    No team requested: the key owner's own team in the tenant (``None`` if teamless). A team
    requested: it must exist in the tenant and be enabled; the CALLER must be allowed to issue
    into it (org admin via ``team:org_admin``; otherwise a live member holding ``team:key_create``
    for their own key or ``team:key_admin`` for someone else's); and the key OWNER must be a
    member, since the proxy would otherwise drop the team. Authorization is checked before the
    owner's membership so a non-member cannot probe who belongs to a team.

    Raises:
        KeyTeamError: 404 unknown/foreign team or teams disabled, 403 caller not authorized,
            409 team disabled, 400 key owner not a member.

    """
    if not teams_enabled(str(org_id)):
        if requested_team_id is not None:
            raise KeyTeamError("Teams are disabled", 404)
        return None

    if requested_team_id is None:
        team_id, _role = lookup_user_team(database, target_user_id, org_id)
        return team_id

    team = database(database.teams.id == requested_team_id).select().first()
    if team is None or team.organization_id != org_id:
        raise KeyTeamError("Team not found", 404)
    if team.enabled is False:
        raise KeyTeamError("Team is disabled", 409)

    if Permission.TEAM_ORG_ADMIN.value not in caller_scopes:
        caller = _membership(database, requested_team_id, caller_user_id)
        needed = (
            Permission.TEAM_KEY_CREATE
            if target_user_id == caller_user_id
            else Permission.TEAM_KEY_ADMIN
        )
        if caller is None or needed not in expand_team_permissions(caller.team_role):
            raise KeyTeamError("Insufficient permissions to issue keys for this team", 403)

    if _membership(database, requested_team_id, target_user_id) is None:
        raise KeyTeamError("Key owner is not a member of that team", 400)
    return int(requested_team_id)
```

- [ ] **Step 4: Run it**

Run: `.venv/bin/python -m pytest tests/unit/management/test_key_team_resolution.py --no-cov -q`
Expected: `12 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/services/key_minting.py tests/unit/management/test_key_team_resolution.py
git commit -m "feat(teams): resolve_key_team decides which team a new key may act in"
```

- [ ] **Phase 5a gate:** both new test files pass.
