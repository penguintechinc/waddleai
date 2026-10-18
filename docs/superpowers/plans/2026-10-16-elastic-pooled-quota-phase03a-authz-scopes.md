# Elastic Pooled Quota — Phase 3a: Team-Layer Quota Scopes

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec §8, D11.

Adds the two team-layer scopes the team routes (Task 18) authorize with.

---

### Task 13: `team:quota_read` / `team:quota_admin` permissions

**Files:**
- Modify: `shared/auth/permissions.py` (Team scope tier block, after `TEAM_TRANSFER_OWNER`)
- Modify: `shared/auth/team_rbac.py:23-36` (the `_TEAM_*` bundles)
- Modify: `shared/auth/rbac.py` (`Role.ADMIN` team block, after `Permission.TEAM_TRANSFER_OWNER,` ~line 122)
- Modify: `tests/unit/test_team_roles.py:28-52` and `tests/unit/test_rbac_team_resolution.py:111-114` (expected sets)
- Test: `tests/unit/test_team_quota_permissions.py`

**Interfaces:**
- Consumes: existing `Permission`, `TeamRole`, `TEAM_ROLE_PERMISSIONS`, `TEAM_SCOPED_PERMISSIONS`, `ROLE_PERMISSIONS`.
- Produces: `Permission.TEAM_QUOTA_READ = "team:quota_read"` (viewer and up), `Permission.TEAM_QUOTA_ADMIN = "team:quota_admin"` (admin and owner); both are team-LAYER (auto-included in `TEAM_SCOPED_PERMISSIONS` because the value starts with `team:`), and held by `Role.ADMIN` in the org bundle.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_team_quota_permissions.py`:

```python
"""Team-layer quota scopes (spec D11): who holds them and where they are honoured."""

import pytest

from shared.auth.permissions import Permission as P
from shared.auth.rbac import ROLE_PERMISSIONS, Role
from shared.auth.team_rbac import TEAM_ROLE_PERMISSIONS, TEAM_SCOPED_PERMISSIONS, TeamRole


@pytest.mark.parametrize("role", list(TeamRole))
def test_every_team_role_can_read_quota(role):
    """Even a Viewer may see the team's quota (read-only transparency)."""
    assert P.TEAM_QUOTA_READ in TEAM_ROLE_PERMISSIONS[role]


@pytest.mark.parametrize(
    ("role", "can_admin"),
    [(TeamRole.VIEWER, False), (TeamRole.MEMBER, False), (TeamRole.ADMIN, True), (TeamRole.OWNER, True)],
)
def test_only_team_admin_and_owner_manage_quota(role, can_admin):
    """Buffer + reservation edits need Admin or Owner; Member/Viewer never."""
    assert (P.TEAM_QUOTA_ADMIN in TEAM_ROLE_PERMISSIONS[role]) is can_admin


def test_quota_scopes_are_team_layer_and_not_org_layer():
    """They are pinned to the caller's own team by check_permission, like every team:* scope."""
    assert {P.TEAM_QUOTA_READ, P.TEAM_QUOTA_ADMIN} <= TEAM_SCOPED_PERMISSIONS
    assert P.TEAM_ORG_ADMIN not in TEAM_ROLE_PERMISSIONS[TeamRole.OWNER]


def test_org_admin_bundle_carries_both_and_other_org_roles_neither():
    """Org admin acts on any team in its tenant; no other org role holds a team quota scope."""
    assert {P.TEAM_QUOTA_READ, P.TEAM_QUOTA_ADMIN} <= ROLE_PERMISSIONS[Role.ADMIN]
    for role in (Role.RESOURCE_MANAGER, Role.REPORTER, Role.USER):
        assert not {P.TEAM_QUOTA_READ, P.TEAM_QUOTA_ADMIN} & ROLE_PERMISSIONS[role]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_team_quota_permissions.py -v --no-cov`
Expected: FAIL — `AttributeError: TEAM_QUOTA_READ`.

- [ ] **Step 3: Write minimal implementation**

`permissions.py` after `TEAM_TRANSFER_OWNER = "team:transfer_owner"`:

```python
    # Pooled-quota team layer (elastic quota spec D11): read the team's quota (viewer+) and
    # manage its shared buffer + per-user reservations (admin+). Team-LAYER scopes.
    TEAM_QUOTA_READ = "team:quota_read"
    TEAM_QUOTA_ADMIN = "team:quota_admin"
```

`team_rbac.py` bundles become:

```python
_TEAM_VIEWER = {Permission.TEAM_READ, Permission.TEAM_QUOTA_READ}
_TEAM_MEMBER = _TEAM_VIEWER | {Permission.TEAM_USE, Permission.TEAM_KEY_CREATE}
_TEAM_ADMIN = _TEAM_MEMBER | {
    Permission.TEAM_ADMIN,
    Permission.TEAM_MEMBER_MANAGE,
    Permission.TEAM_KEY_ADMIN,
    Permission.TEAM_QUOTA_ADMIN,
}
```

`rbac.py` `Role.ADMIN` set: after `Permission.TEAM_TRANSFER_OWNER,` add `Permission.TEAM_QUOTA_READ,` and `Permission.TEAM_QUOTA_ADMIN,`.

Update the existing expectations: `test_team_roles.py` — viewer `== {P.TEAM_READ, P.TEAM_QUOTA_READ}`; member `== {P.TEAM_READ, P.TEAM_QUOTA_READ, P.TEAM_USE, P.TEAM_KEY_CREATE}`; admin delta `{P.TEAM_ADMIN, P.TEAM_MEMBER_MANAGE, P.TEAM_KEY_ADMIN, P.TEAM_QUOTA_ADMIN}`. `test_rbac_team_resolution.py` owner set gains `P.TEAM_QUOTA_READ, P.TEAM_QUOTA_ADMIN`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_team_quota_permissions.py tests/unit/test_team_roles.py tests/unit/test_rbac_team_resolution.py tests/unit/test_check_permission_team.py tests/unit/test_auth_module_split.py tests/unit/management/test_auth_team_claims.py tests/unit/management/test_team_authz.py tests/unit/management/test_scope_authz.py -v --no-cov`
Expected: PASS. Any other test enumerating the exact minted scope list: add the two new scopes where the team scopes are listed.

- [ ] **Step 5: Commit**

```bash
git add shared/auth/permissions.py shared/auth/team_rbac.py shared/auth/rbac.py tests/unit/test_team_quota_permissions.py tests/unit/test_team_roles.py tests/unit/test_rbac_team_resolution.py
git commit -m "feat(authz): team:quota_read / team:quota_admin team-layer scopes"
git push
```

---
