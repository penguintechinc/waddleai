"""Team-layer quota scopes (spec D11): who holds them and where they are honoured."""

import pytest

from shared.auth.permissions import Permission
from shared.auth.rbac import ROLE_PERMISSIONS, Role
from shared.auth.team_rbac import TEAM_ROLE_PERMISSIONS, TEAM_SCOPED_PERMISSIONS, TeamRole


@pytest.mark.parametrize("role", list(TeamRole))
def test_every_team_role_can_read_quota(role):
    """Even a Viewer may see the team's quota (read-only transparency)."""
    assert Permission.TEAM_QUOTA_READ in TEAM_ROLE_PERMISSIONS[role]


@pytest.mark.parametrize(
    ("role", "can_admin"),
    [
        (TeamRole.VIEWER, False),
        (TeamRole.MEMBER, False),
        (TeamRole.ADMIN, True),
        (TeamRole.OWNER, True),
    ],
)
def test_only_team_admin_and_owner_manage_quota(role, can_admin):
    """Buffer + reservation edits need Admin or Owner; Member/Viewer never."""
    assert (Permission.TEAM_QUOTA_ADMIN in TEAM_ROLE_PERMISSIONS[role]) is can_admin


def test_quota_scopes_are_team_layer_and_not_org_layer():
    """They are pinned to the caller's own team by check_permission, like every team:* scope."""
    assert {Permission.TEAM_QUOTA_READ, Permission.TEAM_QUOTA_ADMIN} <= TEAM_SCOPED_PERMISSIONS
    assert Permission.TEAM_ORG_ADMIN not in TEAM_ROLE_PERMISSIONS[TeamRole.OWNER]


def test_org_admin_bundle_carries_both_and_other_org_roles_neither():
    """Org admin acts on any team in its tenant; no other org role holds a team quota scope."""
    assert {Permission.TEAM_QUOTA_READ, Permission.TEAM_QUOTA_ADMIN} <= ROLE_PERMISSIONS[Role.ADMIN]
    for role in (Role.RESOURCE_MANAGER, Role.REPORTER, Role.USER):
        assert (
            not {Permission.TEAM_QUOTA_READ, Permission.TEAM_QUOTA_ADMIN} & ROLE_PERMISSIONS[role]
        )
