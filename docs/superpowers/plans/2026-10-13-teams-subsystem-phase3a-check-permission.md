# Teams Subsystem — Phase 3a: `check_permission` Team Branch (Proxy Plane)

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §3 D5/D9, §5, §11.
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Phase 2 (`Permission.TEAM_*`, `TeamRole`, `expand_team_permissions`).

---

<!-- TASK 8 -->
## Task 8: `UserContext` team fields and the `check_permission` Team branch

**Files:**
- Modify: `shared/auth/rbac.py` (`UserContext`, `RBACManager.check_permission`)
- Test: `tests/unit/test_check_permission_team.py` (create)

**Interfaces:**
- Produces: `UserContext.team_id: int | None = None` and `UserContext.team_role: str | None = None` (defaults keep every pre-teams construction site valid); `RBACManager.check_permission(user_context, permission, resource_org_id=None, resource_user_id=None, resource_team_id: int | None = None) -> bool`.
- Semantics (spec D5): effective permissions are `user ∪ org-role ∪ team-role`, each confined to its layer. A **team-layer** permission (`permission in TEAM_SCOPED_PERMISSIONS`) is honoured only if (a) `resource_team_id` is given and equals `user_context.team_id` -- no team named means **deny** for non-admins (fail closed), and (b) the caller's `team_role` actually grants it: the scope set may be stale or forged, so `expand_team_permissions(user_context.team_role)` is authoritative. Org-layer checks are unchanged and run afterwards, so a team role never lets a `USER` cross the org boundary. `Role.ADMIN` keeps its existing early return (the org admin).
- Review Focus coverage in this task's tests: cross-team access attempt; team scopes with no acting team; a scope claim exceeding the team role; a team Owner on a plain USER role gaining nothing org-layer.

- [ ] **Step 1: Write the failing test**

```python
"""``RBACManager.check_permission`` Team branch (teams spec D5/D9): team ∪ org ∪ user.

A team-layer permission is honoured only against the caller's OWN team and only if their team
role actually grants it; org-layer checks are unchanged and a team role never widens them
(narrower restricts, never expands -- ``security.md`` cascade).
"""

from typing import Any

import pytest

from shared.auth.rbac import (
    ROLE_PERMISSIONS,
    Permission,
    RBACManager,
    Role,
    UserContext,
    expand_team_permissions,
)

P = Permission
OWN_TEAM, OTHER_TEAM = 10, 11


def _ctx(
    role: Role = Role.USER,
    team_role: str | None = "member",
    team_id: int | None = OWN_TEAM,
    org: int = 1,
    extra: set | None = None,
) -> UserContext:
    """Build a UserContext as ``build_user_context`` would: org bundle ∪ team bundle."""
    perms = set(ROLE_PERMISSIONS[role]) | expand_team_permissions(team_role) | (extra or set())
    return UserContext(
        user_id=5, username="u", role=role, organization_id=org, managed_orgs=[],
        permissions=perms, team_id=team_id, team_role=team_role,
    )  # fmt: skip


@pytest.fixture
def rbac() -> RBACManager:
    """check_permission is pure -- no DB handle needed."""
    return RBACManager(db=None)


def test_user_context_defaults_to_teamless() -> None:
    """New fields default to None so every pre-teams construction site keeps working."""
    uc = UserContext(1, "u", Role.USER, 1, [], set())
    assert (uc.team_id, uc.team_role) == (None, None)


def test_member_may_use_their_own_team(rbac) -> None:
    """Own team + a permission their role grants -> allowed."""
    assert rbac.check_permission(_ctx(), P.TEAM_USE, resource_team_id=OWN_TEAM)


def test_member_is_denied_on_another_team(rbac) -> None:
    """Cross-team attempt: same permission, different team -> denied."""
    assert not rbac.check_permission(_ctx(), P.TEAM_USE, resource_team_id=OTHER_TEAM)


def test_member_is_denied_permissions_their_role_lacks(rbac) -> None:
    """Own team but the role does not grant it (member cannot administer) -> denied."""
    assert not rbac.check_permission(_ctx(), P.TEAM_ADMIN, resource_team_id=OWN_TEAM)


def test_viewer_reads_but_cannot_spend(rbac) -> None:
    """Viewer: read-only on own team, no use/key creation."""
    viewer = _ctx(team_role="viewer")
    assert rbac.check_permission(viewer, P.TEAM_READ, resource_team_id=OWN_TEAM)
    assert not rbac.check_permission(viewer, P.TEAM_USE, resource_team_id=OWN_TEAM)
    assert not rbac.check_permission(viewer, P.TEAM_KEY_CREATE, resource_team_id=OWN_TEAM)


def test_team_permission_without_a_target_team_fails_closed(rbac) -> None:
    """A team-layer check that names no team is denied for non-admins (no implicit scope)."""
    assert not rbac.check_permission(_ctx(), P.TEAM_USE)


def test_teamless_caller_holding_forged_team_scopes_is_denied(rbac) -> None:
    """Team scopes in the permission set with no acting team (forged/stale claim) -> denied."""
    forged = _ctx(team_role=None, team_id=None, extra={P.TEAM_ADMIN, P.TEAM_USE})
    assert not rbac.check_permission(forged, P.TEAM_ADMIN, resource_team_id=OWN_TEAM)


def test_scope_claim_exceeding_the_team_role_is_denied(rbac) -> None:
    """Token claims TEAM_DELETE but the resolved team role is viewer -> role is authoritative."""
    over = _ctx(team_role="viewer", extra={P.TEAM_DELETE})
    assert not rbac.check_permission(over, P.TEAM_DELETE, resource_team_id=OWN_TEAM)


def test_permissions_from_a_jwt_arrive_as_strings(rbac) -> None:
    """Claims-built contexts carry scope strings; the Team branch accepts them."""
    uc = _ctx()
    scopes: set[Any] = {p.value for p in uc.permissions}
    uc.permissions = scopes
    assert rbac.check_permission(uc, P.TEAM_USE, resource_team_id=OWN_TEAM)
    assert rbac.check_permission(uc, "team:use", resource_team_id=OWN_TEAM)
    assert not rbac.check_permission(uc, "team:use", resource_team_id=OTHER_TEAM)


def test_team_owner_does_not_gain_org_layer_access(rbac) -> None:
    """Narrower restricts: an Owner on a plain USER org role still lacks org-layer permissions."""
    owner = _ctx(role=Role.USER, team_role="owner")
    assert rbac.check_permission(owner, P.TEAM_DELETE, resource_team_id=OWN_TEAM)
    assert not rbac.check_permission(owner, P.ORG_UPDATE)
    assert not rbac.check_permission(owner, P.APIKEY_DELETE)
    assert not rbac.check_permission(owner, P.TEAM_ORG_ADMIN)


def test_org_layer_checks_still_apply_after_the_team_branch(rbac) -> None:
    """A team grant never lets a USER cross the org boundary on a combined check."""
    assert not rbac.check_permission(
        _ctx(), P.TEAM_USE, resource_org_id=2, resource_team_id=OWN_TEAM
    )
    assert rbac.check_permission(_ctx(), P.TEAM_USE, resource_org_id=1, resource_team_id=OWN_TEAM)


def test_org_admin_may_act_on_any_team(rbac) -> None:
    """The org admin bundle (team:org_admin + team:*) passes regardless of acting team."""
    admin = _ctx(role=Role.ADMIN, team_role=None, team_id=None)
    assert rbac.check_permission(admin, P.TEAM_DELETE, resource_team_id=OTHER_TEAM)


def test_unknown_permission_string_is_denied(rbac) -> None:
    """A made-up permission string is not in the set and is denied (no enum lookup crash)."""
    assert not rbac.check_permission(_ctx(), "team:nonsense", resource_team_id=OWN_TEAM)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_check_permission_team.py --no-cov -q -x`
Expected: FAIL with `TypeError: UserContext.__init__() got an unexpected keyword argument 'team_id'`.

- [ ] **Step 3: Implement**

```diff
--- a/shared/auth/rbac.py
+++ b/shared/auth/rbac.py
@@ -180,6 +180,9 @@
     managed_orgs: list[int]
     permissions: set[Permission]
     api_key_id: int | None = None
+    #: Acting team (teams spec D3) and the caller's role in it; ``None`` = teamless.
+    team_id: int | None = None
+    team_role: str | None = None


 # Role-based permission mapping
@@ -571,8 +574,16 @@
         permission: Permission,
         resource_org_id: int | None = None,
         resource_user_id: int | None = None,
+        resource_team_id: int | None = None,
     ) -> bool:
-        """Check if user has permission for specific resource."""
+        """Check if user has permission for specific resource.
+
+        Effective permissions are ``user ∪ org-role ∪ team-role``, each confined to its own
+        layer (teams spec D5): a team-layer permission is honoured only against the caller's
+        own team (``resource_team_id`` must equal ``user_context.team_id``) and only if the
+        caller's resolved ``team_role`` grants it, so a stale or forged scope claim can never
+        exceed the role. Org-layer checks are unchanged -- a team role never widens them.
+        """
         # Check base permission — permissions may be stored as strings (e.g. from JWT)
         perm_value = permission.value if isinstance(permission, Permission) else permission
         if (
@@ -584,6 +595,17 @@
         # Admin has access to everything
         if user_context.role == Role.ADMIN:
             return True
+
+        # Team branch: team-layer permissions are pinned to the caller's own team and role.
+        try:
+            perm_enum: Permission | None = Permission(perm_value)
+        except ValueError:
+            perm_enum = None
+        if perm_enum in TEAM_SCOPED_PERMISSIONS:
+            if resource_team_id is None or user_context.team_id != resource_team_id:
+                return False
+            if perm_enum not in expand_team_permissions(user_context.team_role):
+                return False

         # Resource-specific checks
         if resource_org_id is not None:
```

- [ ] **Step 4: Run it and the neighbouring RBAC suites**

Run: `.venv/bin/python -m pytest tests/unit/test_check_permission_team.py tests/unit/test_rbac.py tests/unit/test_rbac_additional.py --no-cov -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add shared/auth/rbac.py tests/unit/test_check_permission_team.py
git commit -m "feat(teams): check_permission Team branch pins team scopes to the caller's own team"
```

- [ ] **Phase 3a gate:** the three suites in Step 4 pass.
