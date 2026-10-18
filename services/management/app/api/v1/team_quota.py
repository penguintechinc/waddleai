"""WaddleAI Management API v1 - Pooled quota, team layer (pooled-quota spec §8, D11).

View a team's quota, set its shared buffer, and manage per-user reservations. Authorization
re-derives the caller's team permissions from the LIVE membership row via ``authorize_team``
(a demotion bites immediately); the org admin passes via ``team:org_admin``. Behind the
``waddleai.pooled_quota`` flag. Responses carry user ids and credits only -- no PII.
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from quart import jsonify
from quart_schema import security_scheme, tag, validate_request, validate_response

from shared.auth.rbac import Permission

from ...services import quota_pools as qp
from ...services.quota_invalidation import invalidate_quota_config
from ...services.quota_pools import QuotaError
from . import api_v1_bp
from ._pagination import PageRequest
from ._team_authz import authorize_team
from .auth import require_auth
from .quota_pools import (
    _BEARER_AUTH,
    MessageResponse,
    PaginationMeta,
    _db,
    _error,
    _quota_off,
    _record_write,
)


@dataclass(slots=True)
class SetBufferRequest:
    """Request body for PUT /teams/<id>/pooled-quota/buffer."""

    shared_buffer_credits: int | None = None


@dataclass(slots=True)
class SetReservationRequest:
    """Request body for PUT /teams/<id>/pooled-quota/reservations/<user_id>."""

    guaranteed_credits: int | None = None
    daily_subcap: int | None = None


@dataclass(slots=True)
class ReservationItem:
    """A member's reservation: the user id (never a name or email) and credits."""

    user_id: int
    guaranteed_credits: int
    daily_subcap: int | None


@dataclass(slots=True)
class TeamQuotaResponse:
    """A team's quota as returned by the API -- exactly these fields, never the raw rows."""

    team_id: int
    configured: bool
    pool_credits: int
    shared_buffer_credits: int
    reserved_credits: int
    unallocated_credits: int
    reservations: list[ReservationItem]
    total: int
    pagination: PaginationMeta


@api_v1_bp.route("/teams/<int:team_id>/pooled-quota", methods=["GET"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(TeamQuotaResponse, 200)
async def get_team_quota(team_id: int):
    """Show a team's pool, buffer, reserved/unallocated credits and per-user reservations."""
    if (off := await _quota_off()) is not None:
        return off
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_QUOTA_READ)
    if error is not None or access is None:
        return error
    page = PageRequest.from_request()
    database = _db()

    def _load() -> dict[str, Any]:
        pool = database(database.quota_team_pool.team_id == team_id).select().first()
        rows = list(
            database(database.quota_user_reservation.team_id == team_id).select(
                orderby=database.quota_user_reservation.id
            )
        )
        items: list[dict[str, Any]] = [
            {"user_id": int(r.user_id), "guaranteed_credits": int(r.guaranteed_credits or 0),
             "daily_subcap": None if r.daily_subcap is None else int(r.daily_subcap)}
            for r in rows
        ]  # fmt: skip
        reserved = sum(i["guaranteed_credits"] for i in items)
        pool_credits = int(pool.pool_credits or 0) if pool else 0
        buffer = int(pool.shared_buffer_credits or 0) if pool else 0
        start, stop = page.limitby
        return {
            "team_id": team_id, "configured": pool is not None, "pool_credits": pool_credits,
            "shared_buffer_credits": buffer, "reserved_credits": reserved,
            "unallocated_credits": max(0, pool_credits - reserved - buffer),
            "reservations": items[start:stop], "total": len(items), **page.meta(len(items)),
        }  # fmt: skip

    return await asyncio.to_thread(_load)


@api_v1_bp.route("/teams/<int:team_id>/pooled-quota/buffer", methods=["PUT"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
@validate_request(SetBufferRequest)
async def put_team_buffer(team_id: int, data: SetBufferRequest):
    """Set the team's lendable shared buffer (team Admin+ or org admin)."""
    if (off := await _quota_off()) is not None:
        return off
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_QUOTA_ADMIN)
    if error is not None or access is None:
        return error
    buffer_credits = data.shared_buffer_credits
    if buffer_credits is None:
        return jsonify({"error": "shared_buffer_credits is required"}), 400
    try:
        await asyncio.to_thread(lambda: qp.set_team_buffer(_db(), access.team, buffer_credits))
    except QuotaError as exc:
        return _error(exc)
    await invalidate_quota_config(team_id=team_id)
    _record_write("team buffer", "updated", team_id=team_id)
    return {"message": "Team buffer updated"}


@api_v1_bp.route("/teams/<int:team_id>/pooled-quota/reservations/<int:user_id>", methods=["PUT"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
@validate_request(SetReservationRequest)
async def put_team_reservation(team_id: int, user_id: int, data: SetReservationRequest):
    """Upsert a member's guaranteed credits and optional daily sub-cap (team Admin+)."""
    if (off := await _quota_off()) is not None:
        return off
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_QUOTA_ADMIN)
    if error is not None or access is None:
        return error
    guaranteed = data.guaranteed_credits
    if guaranteed is None:
        return jsonify({"error": "guaranteed_credits is required"}), 400
    try:
        await asyncio.to_thread(
            lambda: qp.set_user_reservation(
                _db(), access.team, user_id, guaranteed, data.daily_subcap
            )
        )
    except QuotaError as exc:
        _record_write("reservation_set", f"rejected_{exc.status}", team_id=team_id, user_id=user_id)
        return _error(exc)
    await invalidate_quota_config(team_id=team_id)
    _record_write("reservation_set", "updated", team_id=team_id, user_id=user_id)
    return {"message": "Reservation updated"}


@api_v1_bp.route("/teams/<int:team_id>/pooled-quota/reservations/<int:user_id>", methods=["DELETE"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
async def delete_team_reservation(team_id: int, user_id: int):
    """Delete a member's reservation (team Admin+); 404 when none exists."""
    if (off := await _quota_off()) is not None:
        return off
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_QUOTA_ADMIN)
    if error is not None or access is None:
        return error
    deleted = await asyncio.to_thread(qp.delete_user_reservation, _db(), access.team, user_id)
    if not deleted:
        return jsonify({"error": "Reservation not found"}), 404
    await invalidate_quota_config(team_id=team_id)
    _record_write("reservation_delete", "deleted", team_id=team_id, user_id=user_id)
    return {"message": "Reservation deleted"}
