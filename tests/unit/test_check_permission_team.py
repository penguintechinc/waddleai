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
