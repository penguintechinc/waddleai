"""WaddleAI Management API v1 - Teams (teams spec §10).

Team CRUD (membership and ownership routes are added in the next task). Authorization:

* Org-layer routes (create, list) use ``team:org_admin`` / authentication + a tenant filter.
* Team-layer routes call :func:`._team_authz.authorize_team`, which re-derives the caller's team
  permissions from the LIVE membership row (never a stale token claim) and enforces tenancy.

Every response goes through an explicit dataclass model (``@validate_response``) -- never a raw
row -- and carries user ids only, no PII. Every mutation that changes who acts in which team
invalidates the proxy auth cache for the affected users (D7).
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from penguin_dal.db import DB
from quart import g, jsonify
from quart_schema import security_scheme, tag, validate_request, validate_response

from shared.auth.rbac import Permission
from shared.auth.team_flags import teams_enabled

from ...extensions import db
from ...services import teams as team_svc
from ...services.auth_cache_invalidation import invalidate_user_auth_cache
from ...services.teams import TeamError
from . import api_v1_bp
from ._pagination import PageRequest
from ._team_authz import authorize_team
from .auth import require_auth, require_scope

_BEARER_AUTH: list[dict[str, list[str]]] = [{"bearerAuth": []}]


def _db() -> DB:
    """Return the process-wide penguin-dal handle, narrowed away from ``None``."""
    if db is None:
        raise RuntimeError("database not initialized")
    return db


@dataclass(slots=True)
class MessageResponse:
    """Generic ``{"message": str}`` envelope."""

    message: str


@dataclass(slots=True)
class PaginationMeta:
    """Bounded-window metadata attached to every list response."""

    page: int
    limit: int
    total: int | None
    pages: int | None


@dataclass(slots=True)
class CreateTeamRequest:
    """Request body for POST /api/v1/teams (tenant comes from the token, never the body)."""

    name: str | None = None


@dataclass(slots=True)
class UpdateTeamRequest:
    """Request body for PUT /api/v1/teams/<team_id>. Partial update."""

    name: str | None = None
    enabled: bool | None = None


@dataclass(slots=True)
class TeamItem:
    """A team as returned by the API -- exactly these fields, never the raw row."""

    id: int
    organization_id: int
    name: str
    enabled: bool
    created_at: str | None


@dataclass(slots=True)
class TeamListResponse:
    """Response body for GET /api/v1/teams."""

    teams: list[TeamItem]
    total: int
    pagination: PaginationMeta


@dataclass(slots=True)
class CreateTeamResponse:
    """Response body for POST /api/v1/teams."""

    id: int
    name: str
    message: str


@dataclass(slots=True)
class TeamDetailResponse:
    """Response body for GET /api/v1/teams/<team_id>."""

    team: TeamItem
    member_count: int
    your_team_role: str | None


def _team_item(team: Any) -> dict[str, Any]:
    """Project a ``teams`` row onto the :class:`TeamItem` field set."""
    return {
        "id": team.id,
        "organization_id": team.organization_id,
        "name": team.name,
        "enabled": team.enabled is not False,
        "created_at": team.created_at.isoformat() if team.created_at else None,
    }


def _error(exc: TeamError) -> tuple[Any, int]:
    """Render a service-layer :class:`TeamError` as a JSON error response."""
    return jsonify({"error": exc.message}), exc.status


async def _teams_off() -> tuple[Any, int] | None:
    """Return a 404 response when the ``waddleai.disable-teams`` kill switch is ON, else None."""
    if await asyncio.to_thread(teams_enabled, str(g.user.get("organization_id"))):
        return None
    return jsonify({"error": "Teams are disabled"}), 404


@api_v1_bp.route("/teams", methods=["POST"])
@tag(["Teams"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.TEAM_ORG_ADMIN)
@validate_response(CreateTeamResponse, 201)
@validate_request(CreateTeamRequest)
async def create_team(data: CreateTeamRequest):
    """Create a team in the caller's own organization (org-admin scope)."""
    if (off := await _teams_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    try:
        team_id = await asyncio.to_thread(team_svc.create_team, _db(), org_id, data.name)
    except TeamError as exc:
        return _error(exc)
    return {"id": team_id, "name": (data.name or "").strip(), "message": "Team created"}, 201


@api_v1_bp.route("/teams", methods=["GET"])
@tag(["Teams"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(TeamListResponse, 200)
async def list_teams():
    """List the caller's tenant's teams: all of them for an org admin, else only their own."""
    if (off := await _teams_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    user_id = g.user.get("user_id")
    is_org_admin = Permission.TEAM_ORG_ADMIN.value in set(g.user.get("scope") or [])
    page = PageRequest.from_request()
    database = _db()

    def _fetch() -> list[Any]:
        query = database.teams.organization_id == org_id
        if not is_org_admin:
            mine = database(database.team_members.user_id == user_id).select()
            query = query & database.teams.id.belongs([int(m.team_id) for m in mine] or [0])
        return list(database(query).select(limitby=page.limitby, orderby=database.teams.id))

    rows = await asyncio.to_thread(_fetch)
    items = [_team_item(r) for r in rows]
    return {"teams": items, "total": len(items), **page.meta()}


@api_v1_bp.route("/teams/<int:team_id>", methods=["GET"])
@tag(["Teams"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(TeamDetailResponse, 200)
async def get_team(team_id: int):
    """Get one team (any member, including Viewer, or the org admin)."""
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_READ)
    if error is not None or access is None:
        return error
    database = _db()
    count = await asyncio.to_thread(
        lambda: database(database.team_members.team_id == team_id).count()
    )
    return {
        "team": _team_item(access.team),
        "member_count": count,
        "your_team_role": access.team_role,
    }


@api_v1_bp.route("/teams/<int:team_id>", methods=["PUT"])
@tag(["Teams"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
@validate_request(UpdateTeamRequest)
async def update_team(team_id: int, data: UpdateTeamRequest):
    """Rename or enable/disable a team (team Admin+ or org admin)."""
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_ADMIN)
    if error is not None or access is None:
        return error
    try:
        stale = await asyncio.to_thread(
            lambda: team_svc.update_team(_db(), access.team, name=data.name, enabled=data.enabled)
        )
    except TeamError as exc:
        return _error(exc)
    await invalidate_user_auth_cache(_db(), stale)
    return {"message": "Team updated"}


@api_v1_bp.route("/teams/<int:team_id>", methods=["DELETE"])
@tag(["Teams"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
async def delete_team(team_id: int):
    """Delete a team (team Owner or org admin); its members' keys become teamless."""
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_DELETE)
    if error is not None or access is None:
        return error
    stale = await asyncio.to_thread(team_svc.delete_team, _db(), access.team)
    await invalidate_user_auth_cache(_db(), stale)
    return {"message": "Team deleted"}
