# Teams Subsystem — Phase 2: Team Roles as Scope Bundles and the Kill Switch

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §3 D4/D5, §5.
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** none from Phase 1 (pure `shared/auth` code), but land Phase 1 first so the branch builds in order.

---

<!-- TASK 6 -->
## Task 6: Team roles Owner/Admin/Member/Viewer as scope bundles

**Files:**
- Modify: `shared/auth/rbac.py`
- Test: `tests/unit/test_team_roles.py` (create)

**Interfaces:**
- Produces: nine `Permission` members -- the org-layer `TEAM_ORG_ADMIN = "team:org_admin"` and the team-layer `TEAM_READ`, `TEAM_USE`, `TEAM_KEY_CREATE`, `TEAM_KEY_ADMIN`, `TEAM_MEMBER_MANAGE`, `TEAM_ADMIN`, `TEAM_DELETE`, `TEAM_TRANSFER_OWNER` (`team:read` ... `team:transfer_owner`); `TeamRole` enum (`OWNER`/`ADMIN`/`MEMBER`/`VIEWER`, wire values `owner`/`admin`/`member`/`viewer`); `TEAM_ROLE_PERMISSIONS: dict[TeamRole, set[Permission]]` (cumulative owner > admin > member > viewer); `TEAM_SCOPED_PERMISSIONS: frozenset[Permission]` (every team-layer permission); `expand_team_permissions(team_role: object) -> set[Permission]` (fresh set; **fails closed** to `set()` for `None`, non-strings, wrong case or whitespace).
- The `Role.ADMIN` org bundle gains all nine (it is the org admin: it may act on any team in its tenant; tenancy is enforced in the route layer). No other org role gains any.
- Design: the org-layer scope `team:org_admin` is deliberately NOT in any team bundle, so a team Owner can never reach org-layer actions -- "narrower restricts, never expands" (`security.md` cascade).

- [ ] **Step 1: Write the failing test**

```python
"""Team roles as OIDC scope bundles (teams spec §5, D4/D5).

Mirrors how ``ROLE_PERMISSIONS`` expands org roles: a team role expands to team-scoped
permissions, cumulative owner > admin > member > viewer, and the org-layer scope
(``team:org_admin``) is never reachable through a team role -- narrower restricts, never expands.
"""

import pytest

from shared.auth.rbac import (
    ROLE_PERMISSIONS,
    TEAM_ROLE_PERMISSIONS,
    TEAM_SCOPED_PERMISSIONS,
    Permission,
    Role,
    TeamRole,
    expand_team_permissions,
)

P = Permission


def test_team_roles_are_owner_admin_member_viewer() -> None:
    """D4: exactly four team roles, with the wire values the DB stores."""
    assert {r.value for r in TeamRole} == {"owner", "admin", "member", "viewer"}


def test_viewer_is_read_only() -> None:
    """Viewer: view team usage/config, no spend, no mutation."""
    assert TEAM_ROLE_PERMISSIONS[TeamRole.VIEWER] == {P.TEAM_READ}


def test_member_can_use_the_team_and_make_own_keys() -> None:
    """Member: viewer + consume the team + create own team-bound keys."""
    assert TEAM_ROLE_PERMISSIONS[TeamRole.MEMBER] == {
        P.TEAM_READ, P.TEAM_USE, P.TEAM_KEY_CREATE,
    }  # fmt: skip


def test_admin_adds_membership_settings_and_key_issuance() -> None:
    """Admin: member + manage members, settings, and keys for others."""
    assert TEAM_ROLE_PERMISSIONS[TeamRole.ADMIN] == TEAM_ROLE_PERMISSIONS[TeamRole.MEMBER] | {
        P.TEAM_ADMIN, P.TEAM_MEMBER_MANAGE, P.TEAM_KEY_ADMIN,
    }  # fmt: skip


def test_owner_adds_delete_and_transfer() -> None:
    """Owner: admin + delete the team + transfer ownership."""
    assert TEAM_ROLE_PERMISSIONS[TeamRole.OWNER] == TEAM_ROLE_PERMISSIONS[TeamRole.ADMIN] | {
        P.TEAM_DELETE, P.TEAM_TRANSFER_OWNER,
    }  # fmt: skip


def test_bundles_are_strictly_cumulative() -> None:
    """Each role strictly contains the one below it (no lateral or inverted grants)."""
    order = [TeamRole.VIEWER, TeamRole.MEMBER, TeamRole.ADMIN, TeamRole.OWNER]
    for lower, higher in zip(order, order[1:], strict=False):
        assert TEAM_ROLE_PERMISSIONS[lower] < TEAM_ROLE_PERMISSIONS[higher]


def test_no_team_role_reaches_the_org_layer_scope() -> None:
    """``team:org_admin`` is org-layer only -- not even Owner may hold it via a team role."""
    for perms in TEAM_ROLE_PERMISSIONS.values():
        assert P.TEAM_ORG_ADMIN not in perms


def test_team_scoped_permissions_are_every_team_perm_except_org_admin() -> None:
    """The team-layer set is what ``check_permission`` pins to the caller's own team."""
    all_team = {p for p in Permission if p.value.startswith("team:")}
    assert TEAM_SCOPED_PERMISSIONS == frozenset(all_team - {P.TEAM_ORG_ADMIN})


def test_org_admin_holds_every_team_permission_others_hold_none() -> None:
    """Only the org admin bundle carries team perms; the other org roles carry none."""
    team_perms = {p for p in Permission if p.value.startswith("team:")}
    assert team_perms <= ROLE_PERMISSIONS[Role.ADMIN]
    for role in (Role.RESOURCE_MANAGER, Role.REPORTER, Role.USER):
        assert not (team_perms & ROLE_PERMISSIONS[role]), role


@pytest.mark.parametrize("role", list(TeamRole))
def test_expand_matches_the_bundle_and_returns_a_fresh_set(role: TeamRole) -> None:
    """Expansion by wire string equals the bundle, and mutating it never corrupts the table."""
    before = set(TEAM_ROLE_PERMISSIONS[role])
    expanded = expand_team_permissions(role.value)
    assert expanded == before
    expanded.add(P.TEAM_ORG_ADMIN)
    assert TEAM_ROLE_PERMISSIONS[role] == before


@pytest.mark.parametrize("bad", [None, "", "OWNER", "superuser", "owner ", 7])
def test_unknown_or_malformed_team_role_expands_to_nothing(bad) -> None:
    """Fail closed: an unrecognized role string/type grants no team permissions."""
    assert expand_team_permissions(bad) == set()
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_team_roles.py --no-cov -q`
Expected: collection error `ImportError: cannot import name 'TEAM_ROLE_PERMISSIONS' from 'shared.auth.rbac'`.

- [ ] **Step 3: Implement**

```diff
--- a/shared/auth/rbac.py
+++ b/shared/auth/rbac.py
@@ -141,6 +141,32 @@
     MODEL_ACCESS_POLICY_ADMIN = "model_access_policy:admin"
     CACHE_CONFIG_ADMIN = "cache_config:admin"
     ROUTING_DECISION_READ = "routing_decision:read"
+
+    # ------------------------------------------------------------------
+    # Team scope tier (teams subsystem spec §5). TEAM_ORG_ADMIN is the ORG-layer
+    # scope ("act on any team in my tenant"): held by Role.ADMIN only, never by a
+    # team role. Every other team:* permission is TEAM-layer -- granted by a team
+    # role bundle (TEAM_ROLE_PERMISSIONS) and only honoured against the caller's
+    # own team (see RBACManager.check_permission's Team branch).
+    # ------------------------------------------------------------------
+    TEAM_ORG_ADMIN = "team:org_admin"
+    TEAM_READ = "team:read"
+    TEAM_USE = "team:use"
+    TEAM_KEY_CREATE = "team:key_create"
+    TEAM_KEY_ADMIN = "team:key_admin"
+    TEAM_MEMBER_MANAGE = "team:member_manage"
+    TEAM_ADMIN = "team:admin"
+    TEAM_DELETE = "team:delete"
+    TEAM_TRANSFER_OWNER = "team:transfer_owner"
+
+
+class TeamRole(Enum):
+    """Per-team roles (``team_members.team_role``); expanded to scopes like org roles are."""
+
+    OWNER = "owner"
+    ADMIN = "admin"
+    MEMBER = "member"
+    VIEWER = "viewer"


 @dataclass
@@ -227,6 +253,17 @@
         Permission.MODEL_ACCESS_POLICY_ADMIN,
         Permission.CACHE_CONFIG_ADMIN,
         Permission.ROUTING_DECISION_READ,
+        # Team scope tier: the org admin holds the org-layer scope plus every team-layer
+        # scope (it may act on any team in its tenant -- the route layer enforces tenancy).
+        Permission.TEAM_ORG_ADMIN,
+        Permission.TEAM_READ,
+        Permission.TEAM_USE,
+        Permission.TEAM_KEY_CREATE,
+        Permission.TEAM_KEY_ADMIN,
+        Permission.TEAM_MEMBER_MANAGE,
+        Permission.TEAM_ADMIN,
+        Permission.TEAM_DELETE,
+        Permission.TEAM_TRANSFER_OWNER,
     },
     Role.RESOURCE_MANAGER: {
         Permission.SYSTEM_HEALTH,
@@ -279,6 +316,46 @@
 }


+_TEAM_VIEWER = {Permission.TEAM_READ}
+_TEAM_MEMBER = _TEAM_VIEWER | {Permission.TEAM_USE, Permission.TEAM_KEY_CREATE}
+_TEAM_ADMIN = _TEAM_MEMBER | {
+    Permission.TEAM_ADMIN,
+    Permission.TEAM_MEMBER_MANAGE,
+    Permission.TEAM_KEY_ADMIN,
+}
+_TEAM_OWNER = _TEAM_ADMIN | {Permission.TEAM_DELETE, Permission.TEAM_TRANSFER_OWNER}
+
+#: Team role -> the team-layer permissions it expands to (cumulative: owner > admin > member
+#: > viewer). Expanded at UserContext build time, exactly as ``ROLE_PERMISSIONS`` is for org roles.
+TEAM_ROLE_PERMISSIONS: dict[TeamRole, set[Permission]] = {
+    TeamRole.VIEWER: _TEAM_VIEWER,
+    TeamRole.MEMBER: _TEAM_MEMBER,
+    TeamRole.ADMIN: _TEAM_ADMIN,
+    TeamRole.OWNER: _TEAM_OWNER,
+}
+
+#: Every team-LAYER permission: honoured only against the caller's own team. ``TEAM_ORG_ADMIN``
+#: is deliberately absent -- it is an org-layer scope no team role can grant.
+TEAM_SCOPED_PERMISSIONS: frozenset[Permission] = frozenset(
+    p for p in Permission if p.value.startswith("team:") and p is not Permission.TEAM_ORG_ADMIN
+)
+
+
+def expand_team_permissions(team_role: object) -> set[Permission]:
+    """Expand a ``team_members.team_role`` string to a fresh set of team-layer permissions.
+
+    Fails closed: ``None``, a non-string, or any value that is not exactly a ``TeamRole`` wire
+    value (case-sensitive, no whitespace tolerance) expands to nothing, so a corrupt row or a
+    forged claim can never widen access. Always returns a new set so callers may mutate it.
+    """
+    if not isinstance(team_role, str):
+        return set()
+    try:
+        return set(TEAM_ROLE_PERMISSIONS[TeamRole(team_role)])
+    except (ValueError, KeyError):
+        return set()
+
+
 #: Valkey key prefix for the proxy's per-request api-key auth-lookup cache
 #: (``proxy/apps/proxy_server/auth_cache.py``). Defined here -- a module
 #: already shared by the proxy and management services -- rather than in
```

- [ ] **Step 4: Run it and the scope-authz guards**

Run: `.venv/bin/python -m pytest tests/unit/test_team_roles.py tests/unit/management/test_scope_authz.py tests/unit/test_rbac_additional.py --no-cov -q`
Expected: all pass (`test_scope_authz.py` proves no other role's bundle changed).

- [ ] **Step 5: Commit**

```bash
git add shared/auth/rbac.py tests/unit/test_team_roles.py
git commit -m "feat(teams): team roles expand to team:* scope bundles"
```

---

<!-- TASK 7 -->
## Task 7: `waddleai.disable-teams` kill switch

**Files:**
- Create: `shared/auth/team_flags.py`
- Test: `tests/unit/test_team_flags.py` (create)

**Interfaces:**
- Produces: `DISABLE_TEAMS_FLAG = "waddleai.disable-teams"` and `teams_enabled(distinct_id: str = "server") -> bool`. Follows the repo's opt-out convention (`waddleai.disable-auth-cache`, `waddleai.disable-cache-singleflight`, ...): unseen/OFF = teams ON; ON = legacy teamless behaviour. Resolved through `shared.utils.feature_flags.is_feature_enabled`, so the env override is `WADDLEAI_FLAG_DISABLE_TEAMS`. Any flag-layer failure fails **open** to teams ON (never strand a deployment) and logs a WARNING.
- It lives in its own module with no `rbac` import so `shared/auth/rbac.py` (Task 10) and the management service can both use it without an import cycle.
- Naming confirmed against the codebase: every kill switch is `waddleai.disable-<mechanism>`; the env name is derived by `feature_flags._env_var_name`.

- [ ] **Step 1: Write the failing test**

```python
"""``waddleai.disable-teams`` opt-out kill switch (the core-mechanism pattern).

Unseen/OFF = teams ON; flag ON = legacy teamless behaviour. Resolved through the shared
feature-flag layer so ``WADDLEAI_FLAG_DISABLE_TEAMS`` is the env override, exactly like every
other ``waddleai.disable-*`` switch.
"""

from unittest.mock import patch

import pytest

from shared.auth.team_flags import DISABLE_TEAMS_FLAG, teams_enabled
from shared.utils import feature_flags


@pytest.fixture(autouse=True)
def _clean_flags(monkeypatch):
    """Start every test with no env override and a clean flag cache."""
    monkeypatch.delenv("WADDLEAI_FLAG_DISABLE_TEAMS", raising=False)
    feature_flags.reset_for_testing()
    yield
    feature_flags.reset_for_testing()


def test_flag_key_follows_the_waddleai_disable_convention() -> None:
    """The kill switch is named ``waddleai.disable-teams`` (opt-out pattern)."""
    assert DISABLE_TEAMS_FLAG == "waddleai.disable-teams"


def test_teams_are_on_when_the_flag_is_unseen() -> None:
    """Unseen/OFF flag -> mechanism ON (the default for every deployment)."""
    assert teams_enabled() is True


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_env_override_turns_teams_off(monkeypatch, value) -> None:
    """``WADDLEAI_FLAG_DISABLE_TEAMS`` truthy -> kill switch ON -> teams disabled."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", value)
    assert teams_enabled() is False


def test_env_override_false_keeps_teams_on(monkeypatch) -> None:
    """An explicit falsy override leaves the mechanism on."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", "0")
    assert teams_enabled() is True


def test_flag_layer_failure_fails_open_to_teams_on() -> None:
    """If flag resolution itself blows up, teams stay ON (never strand a deployment) and it logs."""
    with patch(
        "shared.auth.team_flags.is_feature_enabled", side_effect=RuntimeError("flag store down")
    ):
        assert teams_enabled() is True
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_team_flags.py --no-cov -q`
Expected: collection error `ModuleNotFoundError: No module named 'shared.auth.team_flags'`.

- [ ] **Step 3: Implement**

```python
"""Kill switch for the teams subsystem (opt-out pattern, ``critical-rules.md``).

``waddleai.disable-teams``: unseen/OFF (the default) leaves teams ON; ON reverts every
team-aware code path to the pre-teams, teamless behaviour -- no team permissions expanded, no
acting team resolved, the team API refusing. Kept in its own tiny module (no ``rbac`` import)
so ``shared.auth.rbac`` and the management service can both use it without an import cycle.
"""

import logging

from shared.utils.feature_flags import is_feature_enabled

logger = logging.getLogger(__name__)

#: Opt-out kill-switch flag key; env override is ``WADDLEAI_FLAG_DISABLE_TEAMS``.
DISABLE_TEAMS_FLAG = "waddleai.disable-teams"


def teams_enabled(distinct_id: str = "server") -> bool:
    """Return True unless the ``waddleai.disable-teams`` kill switch is ON.

    ``distinct_id`` lets PostHog target the switch at one org (pass ``str(org_id)``). Any
    failure resolving the flag fails open to teams ON -- a flag-store outage must never strand
    a deployment on the legacy path or break auth -- and is logged at WARNING.
    """
    try:
        return not is_feature_enabled(DISABLE_TEAMS_FLAG, distinct_id=distinct_id, default=False)
    except Exception:  # noqa: BLE001 -- flag layer is documented never-raises; belt and braces
        logger.warning("teams: %s lookup failed; defaulting to teams ON", DISABLE_TEAMS_FLAG)
        return True
```

- [ ] **Step 4: Run it**

Run: `.venv/bin/python -m pytest tests/unit/test_team_flags.py --no-cov -q`
Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add shared/auth/team_flags.py tests/unit/test_team_flags.py
git commit -m "feat(teams): waddleai.disable-teams opt-out kill switch"
```

- [ ] **Phase 2 gate:** `.venv/bin/python -m pytest tests/unit/test_team_roles.py tests/unit/test_team_flags.py tests/unit/management/test_scope_authz.py --no-cov -q` -- all pass.
