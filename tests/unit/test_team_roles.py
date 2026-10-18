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
    assert TEAM_ROLE_PERMISSIONS[TeamRole.VIEWER] == {P.TEAM_READ, P.TEAM_QUOTA_READ}


def test_member_can_use_the_team_and_make_own_keys() -> None:
    """Member: viewer + consume the team + create own team-bound keys."""
    assert TEAM_ROLE_PERMISSIONS[TeamRole.MEMBER] == {
        P.TEAM_READ, P.TEAM_QUOTA_READ, P.TEAM_USE, P.TEAM_KEY_CREATE,
    }  # fmt: skip


def test_admin_adds_membership_settings_and_key_issuance() -> None:
    """Admin: member + manage members, settings, and keys for others."""
    assert TEAM_ROLE_PERMISSIONS[TeamRole.ADMIN] == TEAM_ROLE_PERMISSIONS[TeamRole.MEMBER] | {
        P.TEAM_ADMIN, P.TEAM_MEMBER_MANAGE, P.TEAM_KEY_ADMIN, P.TEAM_QUOTA_ADMIN,
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
def test_unknown_or_malformed_team_role_expands_to_nothing(bad: object) -> None:
    """Fail closed: an unrecognized role string/type grants no team permissions."""
    assert expand_team_permissions(bad) == set()
