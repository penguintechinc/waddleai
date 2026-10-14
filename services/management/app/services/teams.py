"""Team and membership business rules (teams spec §5, §10; D2/D3/D7).

Pure database logic with no HTTP: the routes in ``api/v1/teams.py`` authorize first, then call
these. Every mutator that changes who acts in which team returns the user ids whose proxy
auth-cache entries it staled, so the caller can invalidate them (D7).

Decisions encoded here (each has a test in ``test_team_service.py``):

* D2 -- one team per (user, tenant): checked here for a clear 409 and enforced again by the
  ``UNIQUE(user_id, organization_id)`` constraint, which also closes a concurrent-add race.
* D3 -- joining a team adopts the user's unbound (NULL-team) keys; leaving or deleting unbinds.
* A team that has an owner always keeps one; only an owner (or org admin) may grant, hold, edit
  or remove ``owner`` -- a team Admin cannot escalate.
"""

from datetime import datetime
from typing import Any

from penguin_dal.db import DB
from sqlalchemy.exc import IntegrityError

from shared.auth.rbac import TeamRole

_ROLES = frozenset(r.value for r in TeamRole)
_MAX_NAME = 255


class TeamError(Exception):
    """A team rule violation; carries the HTTP status the route should answer with."""

    def __init__(self, message: str, status: int) -> None:
        """Record the user-facing message and the HTTP status to answer with."""
        super().__init__(message)
        self.message = message
        self.status = status


def _clean_name(name: object) -> str:
    """Trim a team name and enforce 1-255 characters."""
    cleaned = name.strip() if isinstance(name, str) else ""
    if not cleaned or len(cleaned) > _MAX_NAME:
        raise TeamError(f"name must be 1-{_MAX_NAME} characters", 400)
    return cleaned


def _managed_orgs(user: Any) -> list[int]:
    """Normalize ``users.managed_orgs`` (JSON list, CSV string or None) to ``list[int]``."""
    raw = user.managed_orgs
    if not raw:
        return []
    if isinstance(raw, str):
        return [int(x) for x in raw.replace("|", ",").split(",") if x.strip().isdigit()]
    return [int(x) for x in raw]


def get_member(database: DB, team_id: int, user_id: int) -> Any:
    """Return the ``team_members`` row for (team, user), or ``None``."""
    return (
        database(
            (database.team_members.team_id == team_id) & (database.team_members.user_id == user_id)
        )
        .select()
        .first()
    )


def member_user_ids(database: DB, team_id: int) -> list[int]:
    """Return the user ids of every member of ``team_id``."""
    rows = database(database.team_members.team_id == team_id).select()
    return [int(r.user_id) for r in rows]


def _name_taken(database: DB, org_id: int, name: str, exclude_id: int | None = None) -> bool:
    """True if another team in ``org_id`` already uses ``name``."""
    query = (database.teams.organization_id == org_id) & (database.teams.name == name)
    if exclude_id is not None:
        query = query & (database.teams.id != exclude_id)
    return bool(database(query).count())


def create_team(database: DB, org_id: int, name: object) -> int:
    """Create an enabled team in ``org_id`` and return its id (409 on a duplicate name)."""
    cleaned = _clean_name(name)
    if _name_taken(database, org_id, cleaned):
        raise TeamError("A team with that name already exists in this organization", 409)
    try:
        team_id = database.teams.insert(
            organization_id=org_id, name=cleaned, enabled=True, created_at=datetime.utcnow()
        )
    except IntegrityError as exc:
        raise TeamError("A team with that name already exists in this organization", 409) from exc
    database.commit()
    return int(team_id)


def update_team(database: DB, team: Any, *, name: object | None, enabled: bool | None) -> list[int]:
    """Rename and/or enable/disable ``team``; return members staled by an enabled change."""
    fields: dict[str, Any] = {}
    if name is not None:
        cleaned = _clean_name(name)
        if cleaned != team.name and _name_taken(database, team.organization_id, cleaned, team.id):
            raise TeamError("A team with that name already exists in this organization", 409)
        fields["name"] = cleaned
    was_enabled = team.enabled is not False
    if enabled is not None and enabled != was_enabled:
        fields["enabled"] = enabled
    if fields:
        database(database.teams.id == team.id).update(**fields)
        database.commit()
    return member_user_ids(database, team.id) if "enabled" in fields else []


def delete_team(database: DB, team: Any) -> list[int]:
    """Delete ``team``: unbind its keys, drop memberships; return the members it staled."""
    stale = member_user_ids(database, team.id)
    database(database.api_keys.team_id == team.id).update(team_id=None)
    database(database.team_members.team_id == team.id).delete()
    database(database.teams.id == team.id).delete()
    database.commit()
    return stale


def add_member(
    database: DB, team: Any, user_id: int, team_role: object, *, can_manage_owner: bool = False
) -> list[int]:
    """Add ``user_id`` to ``team`` (D2: 409 if they already have a team in this tenant).

    Granting ``owner`` needs an owner-capable actor (``can_manage_owner``), like role edits.
    """
    if not isinstance(team_role, str) or team_role not in _ROLES:
        raise TeamError(f"team_role must be one of {sorted(_ROLES)}", 400)
    if team_role == "owner" and not can_manage_owner:
        raise TeamError("Only a team owner or organization admin may grant owner", 403)
    user = database(database.users.id == user_id).select().first()
    if user is None:
        raise TeamError("User not found", 404)
    if user.enabled is False:
        raise TeamError("User is disabled", 400)
    if user.organization_id != team.organization_id and team.organization_id not in _managed_orgs(
        user
    ):
        raise TeamError("User does not belong to this team's organization", 400)

    clash = (
        database(
            (database.team_members.user_id == user_id)
            & (database.team_members.organization_id == team.organization_id)
        )
        .select()
        .first()
    )
    if clash is not None:
        raise TeamError(
            f"User is already a member of team {clash.team_id} in this organization; "
            "remove them from it first (one team per user per organization)",
            409,
        )
    try:
        database.team_members.insert(
            team_id=team.id,
            user_id=user_id,
            organization_id=team.organization_id,
            team_role=team_role,
            created_at=datetime.utcnow(),
        )
    except IntegrityError as exc:  # lost a concurrent-add race to the UNIQUE constraint
        raise TeamError("User is already a member of a team in this organization", 409) from exc
    # D3: keys the user already owns in this tenant with no acting team now act in this one.
    database(
        (database.api_keys.user_id == user_id)
        & (database.api_keys.organization_id == team.organization_id)
        & (database.api_keys.team_id == None)  # noqa: E711 -- SQL IS NULL, not a Python compare
    ).update(team_id=team.id)
    database.commit()
    return [user_id]
