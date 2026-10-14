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
