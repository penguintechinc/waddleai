"""WaddleAI Management API v1 - Teams (teams spec §10).

Team CRUD plus membership and ownership. Authorization:

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

from shared.auth.rbac import Permission, expand_team_permissions
from shared.auth.team_flags import teams_enabled

from ...extensions import db
from ...services import teams as team_svc
from ...services.auth_cache_invalidation import invalidate_user_auth_cache
from ...services.teams import TeamError
from . import api_v1_bp
from ._pagination import PageRequest
from ._team_authz import TeamAccess, authorize_team
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
class AddMemberRequest:
    """Request body for POST /api/v1/teams/<team_id>/members."""

    user_id: int | None = None
    team_role: str | None = "member"


@dataclass(slots=True)
class SetMemberRoleRequest:
    """Request body for PUT /api/v1/teams/<team_id>/members/<user_id>."""

    team_role: str | None = None


@dataclass(slots=True)
class TransferOwnershipRequest:
    """Request body for POST /api/v1/teams/<team_id>/transfer-ownership."""

    new_owner_user_id: int | None = None


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
class MemberItem:
    """A membership: the user id (never a name or email) and their team role."""

    user_id: int
    team_role: str
    created_at: str | None


@dataclass(slots=True)
class TeamDetailResponse:
    """Response body for GET /api/v1/teams/<team_id>."""

    team: TeamItem
    member_count: int
    your_team_role: str | None


@dataclass(slots=True)
class MemberListResponse:
    """Response body for GET /api/v1/teams/<team_id>/members."""

    team_id: int
    members: list[MemberItem]
    total: int


def _team_item(team: Any) -> dict[str, Any]:
    """Project a ``teams`` row onto the :class:`TeamItem` field set."""
    return {
        "id": team.id,
        "organization_id": team.organization_id,
        "name": team.name,
        "enabled": team.enabled is not False,
        "created_at": team.created_at.isoformat() if team.created_at else None,
    }


def _can_manage_owner(access: TeamAccess) -> bool:
    """True for the org admin or a live team Owner (``team:transfer_owner``)."""
    return access.is_org_admin or Permission.TEAM_TRANSFER_OWNER in expand_team_permissions(
        access.team_role
    )


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

    def _fetch() -> tuple[list[Any], int]:
        query = database.teams.organization_id == org_id
        if not is_org_admin:
            mine = database(database.team_members.user_id == user_id).select()
            query = query & database.teams.id.belongs([int(m.team_id) for m in mine] or [0])
        page_rows = list(database(query).select(limitby=page.limitby, orderby=database.teams.id))
        return page_rows, int(database(query).count())

    rows, total = await asyncio.to_thread(_fetch)
    items = [_team_item(r) for r in rows]
    return {"teams": items, "total": total, **page.meta(total)}


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


@api_v1_bp.route("/teams/<int:team_id>/members", methods=["GET"])
@tag(["Teams"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MemberListResponse, 200)
async def list_members(team_id: int):
    """List a team's members (any member or the org admin): user ids and roles only."""
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_READ)
    if error is not None or access is None:
        return error
    database = _db()
    page = PageRequest.from_request()

    def _fetch() -> tuple[list[Any], int]:
        query = database.team_members.team_id == team_id
        page_rows = list(
            database(query).select(limitby=page.limitby, orderby=database.team_members.id)
        )
        return page_rows, int(database(query).count())

    rows, total = await asyncio.to_thread(_fetch)
    members = [
        {
            "user_id": r.user_id,
            "team_role": r.team_role,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    return {"team_id": team_id, "members": members, "total": total}


@api_v1_bp.route("/teams/<int:team_id>/members", methods=["POST"])
@tag(["Teams"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 201)
@validate_request(AddMemberRequest)
async def add_team_member(team_id: int, data: AddMemberRequest):
    """Add a user to the team (team Admin+ or org admin); 409 if they already have a team here."""
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_MEMBER_MANAGE)
    if error is not None or access is None:
        return error
    if data.user_id is None:
        return jsonify({"error": "user_id is required"}), 400
    new_member_id: int = data.user_id
    try:
        stale = await asyncio.to_thread(
            lambda: team_svc.add_member(
                _db(),
                access.team,
                new_member_id,
                data.team_role,
                can_manage_owner=_can_manage_owner(access),
            )  # fmt: skip
        )
    except TeamError as exc:
        return _error(exc)
    await invalidate_user_auth_cache(_db(), stale)
    return {"message": "Member added"}, 201


@api_v1_bp.route("/teams/<int:team_id>/members/<int:user_id>", methods=["PUT"])
@tag(["Teams"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
@validate_request(SetMemberRoleRequest)
async def set_member_role(team_id: int, user_id: int, data: SetMemberRoleRequest):
    """Change a member's team role (team Admin+ or org admin; owner changes need an owner)."""
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_MEMBER_MANAGE)
    if error is not None or access is None:
        return error
    try:
        stale = await asyncio.to_thread(
            lambda: team_svc.set_member_role(
                _db(),
                access.team,
                user_id,
                data.team_role,
                can_manage_owner=_can_manage_owner(access),
            )  # fmt: skip
        )
    except TeamError as exc:
        return _error(exc)
    await invalidate_user_auth_cache(_db(), stale)
    return {"message": "Member role updated"}


@api_v1_bp.route("/teams/<int:team_id>/members/<int:user_id>", methods=["DELETE"])
@tag(["Teams"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
async def remove_team_member(team_id: int, user_id: int):
    """Remove a member (team Admin+ or org admin; removing an owner needs an owner)."""
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_MEMBER_MANAGE)
    if error is not None or access is None:
        return error
    try:
        stale = await asyncio.to_thread(
            lambda: team_svc.remove_member(
                _db(), access.team, user_id, can_manage_owner=_can_manage_owner(access)
            )
        )
    except TeamError as exc:
        return _error(exc)
    await invalidate_user_auth_cache(_db(), stale)
    return {"message": "Member removed"}


@api_v1_bp.route("/teams/<int:team_id>/transfer-ownership", methods=["POST"])
@tag(["Teams"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
@validate_request(TransferOwnershipRequest)
async def transfer_team_ownership(team_id: int, data: TransferOwnershipRequest):
    """Make an existing member the sole owner (team Owner or org admin)."""
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_TRANSFER_OWNER)
    if error is not None or access is None:
        return error
    if data.new_owner_user_id is None:
        return jsonify({"error": "new_owner_user_id is required"}), 400
    try:
        stale = await asyncio.to_thread(
            team_svc.transfer_ownership, _db(), access.team, data.new_owner_user_id
        )
    except TeamError as exc:
        return _error(exc)
    await invalidate_user_auth_cache(_db(), stale)
    return {"message": "Ownership transferred"}
