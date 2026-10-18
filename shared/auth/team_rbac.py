"""Team roles as scope bundles and acting-team resolution (teams spec §5, D2/D3/D5).

Split out of ``rbac.py``; ``shared.auth.rbac`` re-exports every public name here, so existing
imports are unaffected. Depends only on ``shared.auth.permissions`` and the kill switch --
never on ``rbac``.
"""

from enum import Enum

from shared.auth.permissions import Permission
from shared.auth.team_flags import teams_enabled


class TeamRole(Enum):
    """Per-team roles (``team_members.team_role``); expanded to scopes like org roles are."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


_TEAM_VIEWER = {Permission.TEAM_READ, Permission.TEAM_QUOTA_READ}
_TEAM_MEMBER = _TEAM_VIEWER | {Permission.TEAM_USE, Permission.TEAM_KEY_CREATE}
_TEAM_ADMIN = _TEAM_MEMBER | {
    Permission.TEAM_ADMIN,
    Permission.TEAM_MEMBER_MANAGE,
    Permission.TEAM_KEY_ADMIN,
    Permission.TEAM_QUOTA_ADMIN,
}
_TEAM_OWNER = _TEAM_ADMIN | {Permission.TEAM_DELETE, Permission.TEAM_TRANSFER_OWNER}

#: Team role -> the team-layer permissions it expands to (cumulative: owner > admin > member
#: > viewer). Expanded at UserContext build time, exactly as ``ROLE_PERMISSIONS`` is for org roles.
TEAM_ROLE_PERMISSIONS: dict[TeamRole, set[Permission]] = {
    TeamRole.VIEWER: _TEAM_VIEWER,
    TeamRole.MEMBER: _TEAM_MEMBER,
    TeamRole.ADMIN: _TEAM_ADMIN,
    TeamRole.OWNER: _TEAM_OWNER,
}

#: Every team-LAYER permission: honoured only against the caller's own team. ``TEAM_ORG_ADMIN``
#: is deliberately absent -- it is an org-layer scope no team role can grant.
TEAM_SCOPED_PERMISSIONS: frozenset[Permission] = frozenset(
    p for p in Permission if p.value.startswith("team:") and p is not Permission.TEAM_ORG_ADMIN
)


def expand_team_permissions(team_role: object) -> set[Permission]:
    """Expand a ``team_members.team_role`` string to a fresh set of team-layer permissions.

    Fails closed: ``None``, a non-string, or any value that is not exactly a ``TeamRole`` wire
    value (case-sensitive, no whitespace tolerance) expands to nothing, so a corrupt row or a
    forged claim can never widen access. Always returns a new set so callers may mutate it.
    """
    if not isinstance(team_role, str):
        return set()
    try:
        return set(TEAM_ROLE_PERMISSIONS[TeamRole(team_role)])
    except (ValueError, KeyError):
        return set()


def _live_team_membership(
    database, user_id: int, organization_id: int, team_id: int | None
) -> tuple[int | None, str | None]:
    """Resolve ``(team_id, team_role)`` from the live ``team_members``/``teams`` rows.

    ``team_id`` ``None`` means "the user's one team in this tenant" (D2 guarantees at most one);
    an int pins the lookup to that team. Returns ``(None, None)`` -- teamless, never an error --
    when the kill switch is on, no matching membership exists in this tenant, or the team is
    explicitly disabled. The tenant filter stops a key row naming another tenant's team.
    """
    if not teams_enabled(str(organization_id)):
        return None, None
    members = database.team_members
    query = (members.user_id == user_id) & (members.organization_id == organization_id)
    if team_id is not None:
        query = query & (members.team_id == team_id)
    member = database(query).select().first()
    if member is None:
        return None, None
    team = database(database.teams.id == member.team_id).select().first()
    if team is None or team.enabled is False:
        return None, None
    return int(member.team_id), str(member.team_role)


def lookup_key_team(
    database, user_id: int, organization_id: int, key_team_id: int | None
) -> tuple[int | None, str | None]:
    """Acting team for a request authenticated by an API key (spec D3).

    The key names its team (``api_keys.team_id``); NULL means the key acts teamless. The role
    is looked up live so a removal or demotion is honoured even though the key row is unchanged.
    """
    if key_team_id is None:
        return None, None
    return _live_team_membership(database, user_id, organization_id, key_team_id)


def lookup_user_team(database, user_id: int, organization_id: int) -> tuple[int | None, str | None]:
    """Acting team for a login/JWT with no key: the user's single team in their tenant, if any."""
    return _live_team_membership(database, user_id, organization_id, None)


def team_permission_allowed(
    permission: object,
    *,
    team_id: int | None,
    team_role: str | None,
    resource_team_id: int | None,
) -> bool:
    """Apply the Team branch of ``check_permission`` (teams spec D5/D9).

    Returns ``True`` for anything that is not a team-layer permission (the org-layer checks
    decide those). A team-layer permission is allowed only if ``resource_team_id`` names the
    caller's own acting team -- no team named means deny -- AND the caller's resolved
    ``team_role`` grants it: a stale or forged scope claim can never exceed the role.
    """
    value = permission.value if isinstance(permission, Permission) else permission
    try:
        perm = Permission(value)
    except ValueError:
        return True
    if perm not in TEAM_SCOPED_PERMISSIONS:
        return True
    if resource_team_id is None or team_id != resource_team_id:
        return False
    return perm in expand_team_permissions(team_role)
