"""WaddleAI Management API v1 - Pooled quota, org layer (pooled-quota spec §8, D11).

Org pool + cycle anchor, team pool allocation, override caps, conversion rates, usage and
rebuild. Authorization: ``quota:org_update`` for org config, ``quota:admin`` for the global
rate table, and -- for every team-scoped route -- :func:`authorize_team` against the SPECIFIC
path team (live membership, tenancy 404). Tenant comes from the token, never the body;
cross-tenant ids are 404. Every route is behind the ``waddleai.pooled_quota`` flag (404 when
OFF). Responses are dataclass DTOs carrying ids and credits only -- no PII. Every config write
drops the proxy's cached config blob (after the DB write) and emits a log, counter and span.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from penguin_dal.db import DB
from quart import g, jsonify
from quart_schema import security_scheme, tag, validate_request, validate_response

from shared.auth.rbac import Permission
from shared.quota.cycle import cycle_bounds, cycle_index, cycle_started, utc_today
from shared.quota.flags import pooled_quota_enabled

from ...extensions import db
from ...services import quota_pools as qp
from ...services.quota_invalidation import invalidate_quota_config
from ...services.quota_pools import QuotaError
from . import api_v1_bp
from ._team_authz import authorize_team
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)

_BEARER_AUTH: list[dict[str, list[str]]] = [{"bearerAuth": []}]
_config_writes: Any = None


def _record_write(kind: str, outcome: str, **ids: int) -> None:
    """Emit the config-write telemetry: INFO log (ids only), OTel counter, and a span event."""
    global _config_writes
    logger.info(
        "quota: %s %s", kind, outcome, extra={"quota_kind": kind, "quota_outcome": outcome, **ids}
    )
    try:
        from opentelemetry import trace

        from shared.observability.metrics import get_meter

        if _config_writes is None:
            _config_writes = get_meter().create_counter(
                "quota_config_writes_total",
                unit="1",
                description="Pooled-quota config writes by kind and outcome",
            )
        _config_writes.add(1, {"kind": kind, "outcome": outcome})
        trace.get_current_span().add_event("quota.config_write", {"kind": kind, "outcome": outcome})
    except Exception as exc:  # noqa: BLE001 -- a dead exporter never fails a request
        logger.debug("quota: telemetry emit failed: %s", exc)


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
class SetOrgPoolRequest:
    """Request body for PUT /pooled-quota/org-pool; ``None`` leaves a field unchanged."""

    cycle_anchor_date: str | None = None
    pool_credits: int | None = None
    enabled: bool | None = None


@dataclass(slots=True)
class SetTeamPoolRequest:
    """Request body for PUT /pooled-quota/teams/<id>/pool."""

    pool_credits: int | None = None


@dataclass(slots=True)
class OrgPoolResponse:
    """The org pool as returned by the API -- exactly these fields, never the raw row."""

    org_id: int
    enabled: bool
    cycle_anchor_date: str | None
    pool_credits: int
    allocated_to_teams: int
    current_cycle: int | None
    cycle_start: str | None
    cycle_end: str | None


async def _quota_off() -> tuple[Any, int] | None:
    """Return a 404 response when ``waddleai.pooled_quota`` is OFF for the caller's org."""
    if await asyncio.to_thread(pooled_quota_enabled, str(g.user.get("organization_id"))):
        return None
    return jsonify({"error": "Pooled quota is disabled"}), 404


def _error(exc: QuotaError) -> tuple[Any, int]:
    """Render a service-layer :class:`QuotaError` as a JSON error response."""
    return jsonify({"error": exc.message}), exc.status


def _parse_anchor(raw: str | None) -> date | None:
    """Parse an ISO ``YYYY-MM-DD`` anchor; ``None`` stays ``None``; garbage is a 400."""
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise QuotaError("cycle_anchor_date must be an ISO date (YYYY-MM-DD)", 400) from exc


def _org_pool_view(org_id: int, row: Any, allocated: int) -> dict[str, Any]:
    """Project a ``quota_org_pool`` row (or its absence) onto :class:`OrgPoolResponse`."""
    anchor = row.cycle_anchor_date if row is not None else None
    if isinstance(anchor, str):
        anchor = date.fromisoformat(anchor[:10])
    cycle = start = end = None
    if anchor is not None and cycle_started(anchor, utc_today()):
        cycle = cycle_index(anchor, utc_today())
        bounds = cycle_bounds(anchor, cycle)
        start, end = bounds[0].isoformat(), bounds[1].isoformat()
    return {
        "org_id": org_id,
        "enabled": bool(row.enabled) if row is not None else False,
        "cycle_anchor_date": anchor.isoformat() if anchor else None,
        "pool_credits": int(row.pool_credits or 0) if row is not None else 0,
        "allocated_to_teams": allocated,
        "current_cycle": cycle,
        "cycle_start": start,
        "cycle_end": end,
    }


@api_v1_bp.route("/pooled-quota/org-pool", methods=["GET"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ORG_UPDATE)
@validate_response(OrgPoolResponse, 200)
async def get_org_pool():
    """Return the caller's org pool, anchor, and current cycle (disabled zeros when unset)."""
    if (off := await _quota_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    database = _db()

    def _load() -> dict[str, Any]:
        return _org_pool_view(
            org_id, qp.get_org_pool_row(database, org_id), qp.allocated_to_teams(database, org_id)
        )

    return await asyncio.to_thread(_load)


@api_v1_bp.route("/pooled-quota/org-pool", methods=["PUT"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ORG_UPDATE)
@validate_response(MessageResponse, 200)
@validate_request(SetOrgPoolRequest)
async def put_org_pool(data: SetOrgPoolRequest):
    """Set the org pool size, cycle anchor and enabled switch (any subset)."""
    if (off := await _quota_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    try:
        anchor = _parse_anchor(data.cycle_anchor_date)
        await asyncio.to_thread(
            lambda: qp.set_org_pool(
                _db(), org_id, cycle_anchor_date=anchor,
                pool_credits=data.pool_credits, enabled=data.enabled,
            )
        )  # fmt: skip
    except QuotaError as exc:
        _record_write("org pool", f"rejected_{exc.status}", org_id=org_id)
        return _error(exc)
    await invalidate_quota_config(org_id=org_id)
    _record_write("org pool", "updated", org_id=org_id)
    return {"message": "Org pool updated"}


@api_v1_bp.route("/pooled-quota/teams/<int:team_id>/pool", methods=["PUT"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ORG_UPDATE)
@validate_response(MessageResponse, 200)
@validate_request(SetTeamPoolRequest)
async def put_team_pool(team_id: int, data: SetTeamPoolRequest):
    """Allocate ``pool_credits`` of the org pool to a team in the caller's own tenant.

    Org-layer scope AND ``authorize_team(team:quota_admin)`` on the path team, so tenancy (404)
    and live team authorization hold even for a caller whose scope claim is stale.
    """
    if (off := await _quota_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    database = _db()
    access, refusal = await authorize_team(database, team_id, Permission.TEAM_QUOTA_ADMIN)
    if refusal is not None or access is None:
        return refusal
    credits = data.pool_credits
    if credits is None:
        return jsonify({"error": "pool_credits is required"}), 400
    try:
        await asyncio.to_thread(lambda: qp.set_team_pool(database, org_id, access.team, credits))
    except QuotaError as exc:
        _record_write("team pool", f"rejected_{exc.status}", org_id=org_id, team_id=team_id)
        return _error(exc)
    await invalidate_quota_config(team_id=team_id)
    _record_write("team pool", "updated", org_id=org_id, team_id=team_id)
    return {"message": "Team pool updated"}
