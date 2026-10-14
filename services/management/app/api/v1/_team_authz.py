"""Team-scoped authorization for management routes (teams spec D5/D9).

``require_scope`` only checks the (up to token-TTL stale) scope claim, which is right for
org-layer scopes but wrong for team permissions: a demotion or removal must bite at once.
:func:`authorize_team` therefore re-derives the caller's team permissions from the LIVE
``team_members`` row on every call -- using the same ``expand_team_permissions`` bundle the
token mint uses -- and enforces tenancy before anything else.
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from penguin_dal.db import DB
from quart import g, jsonify

from shared.auth.rbac import Permission, expand_team_permissions
from shared.auth.team_flags import teams_enabled

#: ``(response, status)`` pair a handler returns verbatim on refusal.
ErrorResponse = tuple[Any, int]


@dataclass(slots=True)
class TeamAccess:
    """A granted team access: the team row, and how the caller earned it."""

    team: Any
    is_org_admin: bool
    team_role: str | None


def _refuse(message: str, status: int, **extra: object) -> ErrorResponse:
    """Build a JSON error ``(response, status)`` pair."""
    return jsonify({"error": message, **extra}), status


async def authorize_team(
    database: DB, team_id: int, permission: Permission
) -> tuple[TeamAccess | None, ErrorResponse | None]:
    """Authorize ``g.user`` for ``permission`` on ``team_id``; exactly one tuple slot is set.

    A NULL ``enabled`` (a row written without the column's Python-side default) counts as
    enabled; only an explicit ``False`` disables. Order matters: kill switch (404) -> team
    exists (404) -> same tenant (404, never leaking another tenant's team ids) -> org admin
    via ``team:org_admin`` (allowed) -> live membership (403 if absent, or if the team is
    disabled, or if the live role lacks ``permission``).
    """
    user = g.user
    org_id = user.get("organization_id")

    if not await asyncio.to_thread(teams_enabled, str(org_id)):
        return None, _refuse("Teams are disabled", 404)

    team = await asyncio.to_thread(lambda: database(database.teams.id == team_id).select().first())
    if team is None or team.organization_id != org_id:
        return None, _refuse("Team not found", 404)

    if Permission.TEAM_ORG_ADMIN.value in set(user.get("scope") or []):
        return TeamAccess(team=team, is_org_admin=True, team_role=None), None

    member = await asyncio.to_thread(
        lambda: (
            database(
                (database.team_members.team_id == team_id)
                & (database.team_members.user_id == user.get("user_id"))
            )
            .select()
            .first()
        )
    )
    if member is None or team.enabled is False:
        return None, _refuse("Insufficient permissions", 403, required_scope=permission.value)
    if permission not in expand_team_permissions(member.team_role):
        return None, _refuse("Insufficient permissions", 403, required_scope=permission.value)
    return TeamAccess(team=team, is_org_admin=False, team_role=member.team_role), None
