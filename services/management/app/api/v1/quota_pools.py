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
import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from penguin_dal.db import DB
from quart import g, jsonify, request
from quart_schema import security_scheme, tag, validate_request, validate_response

from shared.auth.rbac import Permission
from shared.quota.cycle import cycle_bounds, cycle_index, cycle_started, utc_today
from shared.quota.flags import pooled_quota_enabled
from shared.quota.ledger import QuotaLedger
from shared.utils.conversion_rate_store import upsert_conversion_rate

from ... import extensions as _ext
from ...extensions import db
from ...services import quota_pools as qp
from ...services.quota_invalidation import invalidate_quota_config
from ...services.quota_pools import QuotaError
from . import api_v1_bp
from ._pagination import PageRequest
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


@dataclass(slots=True)
class CreateOverrideRequest:
    """Request body for POST /pooled-quota/overrides (tenant comes from the token)."""

    user_id: int | None = None
    model: str | None = None
    provider: str | None = None
    cap_credits: int | None = None


@dataclass(slots=True)
class OverrideItem:
    """An override as returned by the API -- ids, dimensions and the cap only."""

    id: int
    user_id: int | None
    model: str | None
    provider: str | None
    cap_credits: int


@dataclass(slots=True)
class OverrideListResponse:
    """Response body for GET /pooled-quota/overrides."""

    overrides: list[OverrideItem]
    total: int
    pagination: PaginationMeta


@dataclass(slots=True)
class CreateOverrideResponse:
    """Response body for POST /pooled-quota/overrides."""

    id: int
    message: str


def _override_item(row: Any) -> dict[str, Any]:
    """Project a ``quota_override`` row onto :class:`OverrideItem`."""
    return {
        "id": int(row.id),
        "user_id": None if row.user_id is None else int(row.user_id),
        "model": row.model or None,
        "provider": row.provider or None,
        "cap_credits": int(row.cap_credits),
    }


@api_v1_bp.route("/pooled-quota/overrides", methods=["GET"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ORG_UPDATE)
@validate_response(OverrideListResponse, 200)
async def list_override_caps():
    """List the caller's org override caps (bounded window)."""
    if (off := await _quota_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    page = PageRequest.from_request()
    rows = await asyncio.to_thread(qp.list_overrides, _db(), org_id)
    start, stop = page.limitby
    return {
        "overrides": [_override_item(r) for r in rows[start:stop]],
        "total": len(rows),
        **page.meta(len(rows)),
    }


@api_v1_bp.route("/pooled-quota/overrides", methods=["POST"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ORG_UPDATE)
@validate_response(CreateOverrideResponse, 201)
@validate_request(CreateOverrideRequest)
async def create_override_cap(data: CreateOverrideRequest):
    """Create a hard cap on a user and/or model and/or provider (further-restrict only)."""
    if (off := await _quota_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    cap = data.cap_credits
    if cap is None:
        _record_write("override_create", "rejected_400", org_id=org_id)
        return jsonify({"error": "cap_credits is required"}), 400
    try:
        override_id = await asyncio.to_thread(
            lambda: qp.create_override(
                _db(), org_id, user_id=data.user_id, model=data.model,
                provider=data.provider, cap_credits=cap,
            )
        )  # fmt: skip
    except QuotaError as exc:
        _record_write("override_create", f"rejected_{exc.status}", org_id=org_id)
        return _error(exc)
    await invalidate_quota_config(org_id=org_id)
    _record_write("override_create", "created", org_id=org_id, override_id=override_id)
    return {"id": override_id, "message": "Override created"}, 201


@api_v1_bp.route("/pooled-quota/overrides/<int:override_id>", methods=["DELETE"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ORG_UPDATE)
@validate_response(MessageResponse, 200)
async def delete_override_cap(override_id: int):
    """Delete an override of the caller's own org (another tenant's id is a 404)."""
    if (off := await _quota_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    deleted = await asyncio.to_thread(qp.delete_override, _db(), org_id, override_id)
    if not deleted:
        _record_write("override_delete", "rejected_404", org_id=org_id)
        return jsonify({"error": "Override not found"}), 404
    await invalidate_quota_config(org_id=org_id)
    _record_write("override_delete", "deleted", org_id=org_id, override_id=override_id)
    return {"message": "Override deleted"}


_MAX_RATE = 1e9


@dataclass(slots=True)
class SetRateRequest:
    """Request body for PUT /pooled-quota/conversion-rates (upsert by provider + model)."""

    provider: str | None = None
    model: str | None = None
    input_rate: float | None = None
    output_rate: float | None = None
    base_cost_per_waddleai_token: float = 0.001
    enabled: bool = True


@dataclass(slots=True)
class RateItem:
    """A conversion rate as returned by the API (rates are divisors of LLM tokens)."""

    id: int
    provider: str
    model: str
    input_rate: float
    output_rate: float
    base_cost_per_waddleai_token: float
    enabled: bool


@dataclass(slots=True)
class RateListResponse:
    """Response body for GET /pooled-quota/conversion-rates."""

    rates: list[RateItem]
    total: int
    pagination: PaginationMeta


@dataclass(slots=True)
class RateResponse:
    """Response body for PUT /pooled-quota/conversion-rates."""

    id: int
    message: str


def _clean_name(value: str | None, name: str, limit: int) -> str:
    """Trim a required name and enforce 1..limit characters."""
    cleaned = value.strip() if isinstance(value, str) else ""
    if not cleaned or len(cleaned) > limit:
        raise QuotaError(f"{name} must be 1-{limit} characters", 400)
    return cleaned


def _divisor(value: float | None, name: str) -> float:
    """A conversion divisor must be finite and in (0, 1e9]."""
    if (
        value is None
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not 0 < value <= _MAX_RATE
    ):
        raise QuotaError(f"{name} must be a number greater than 0 and at most {_MAX_RATE:g}", 400)
    return float(value)


@api_v1_bp.route("/pooled-quota/conversion-rates", methods=["GET"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ADMIN)
@validate_response(RateListResponse, 200)
async def list_conversion_rates():
    """List the global credit conversion rates (bounded window, admin only)."""
    if (off := await _quota_off()) is not None:
        return off
    page = PageRequest.from_request()
    database = _db()

    def _load() -> dict[str, Any]:
        t = database.token_conversion_rates
        rows = list(database(t.id > 0).select(orderby=t.id, limitby=page.limitby))
        total = int(database(t.id > 0).count())
        items = [
            {"id": int(r.id), "provider": r.provider, "model": r.model,
             "input_rate": float(r.input_rate), "output_rate": float(r.output_rate),
             "base_cost_per_waddleai_token": float(r.base_cost_per_waddleai_token or 0.0),
             "enabled": r.enabled is not False}
            for r in rows
        ]  # fmt: skip
        return {"rates": items, "total": total, **page.meta(total)}

    return await asyncio.to_thread(_load)


@api_v1_bp.route("/pooled-quota/conversion-rates", methods=["PUT"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ADMIN)
@validate_response(RateResponse, 200)
@validate_request(SetRateRequest)
async def put_conversion_rate(data: SetRateRequest):
    """Create or update the credit conversion rate for one provider:model (admin only)."""
    if (off := await _quota_off()) is not None:
        return off
    try:
        provider = _clean_name(data.provider, "provider", 100)
        model = _clean_name(data.model, "model", 255)
        input_rate = _divisor(data.input_rate, "input_rate")
        output_rate = _divisor(data.output_rate, "output_rate")
        cost = data.base_cost_per_waddleai_token
        if not math.isfinite(cost) or cost < 0:
            raise QuotaError("base_cost_per_waddleai_token must be a finite number >= 0", 400)
    except QuotaError as exc:
        _record_write("rate_set", f"rejected_{exc.status}")
        return _error(exc)
    rate_id = await asyncio.to_thread(
        lambda: upsert_conversion_rate(
            _db(), provider=provider, model=model, input_rate=input_rate, output_rate=output_rate,
            base_cost_per_waddleai_token=cost, enabled=data.enabled,
        )
    )  # fmt: skip
    _record_write("rate_set", "updated", rate_id=rate_id)
    return {"id": rate_id, "message": "Conversion rate saved"}


@dataclass(slots=True)
class UsageEntry:
    """Credits consumed by one scope in one cycle (ids and credits only)."""

    scope_type: str
    scope_id: int
    credits_used: int


@dataclass(slots=True)
class UsageResponse:
    """Response body for GET /pooled-quota/usage."""

    org_id: int
    cycle: int | None
    entries: list[UsageEntry]
    total: int
    pagination: PaginationMeta


@dataclass(slots=True)
class RebuildRequest:
    """Request body for POST /pooled-quota/rebuild; ``cycle`` defaults to the current one."""

    cycle: int | None = None


@dataclass(slots=True)
class RebuildResponse:
    """Response body for POST /pooled-quota/rebuild."""

    cycle: int
    counters_restored: int


def current_cycle_for(database: DB, org_id: int) -> int | None:
    """The org's current cycle index, or ``None`` when it has no started anchor."""
    return _org_pool_view(org_id, qp.get_org_pool_row(database, org_id), 0)["current_cycle"]


@api_v1_bp.route("/pooled-quota/usage", methods=["GET"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ORG_UPDATE)
@validate_response(UsageResponse, 200)
async def get_usage():
    """Per-scope credit usage for a cycle (default current), from the durable ledger."""
    if (off := await _quota_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    database = _db()
    requested = request.args.get("cycle", type=int)
    page = PageRequest.from_request()

    def _load() -> dict[str, Any]:
        cycle = requested if requested is not None else current_cycle_for(database, org_id)
        rows = QuotaLedger(database).usage(org_id, cycle) if cycle is not None else []
        start, stop = page.limitby
        entries = [
            {"scope_type": e.scope_type, "scope_id": e.scope_id, "credits_used": e.credits}
            for e in rows[start:stop]
        ]
        return {"org_id": org_id, "cycle": cycle, "entries": entries, "total": len(rows),
                **page.meta(len(rows))}  # fmt: skip

    return await asyncio.to_thread(_load)


@api_v1_bp.route("/pooled-quota/rebuild", methods=["POST"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ORG_UPDATE)
@validate_response(RebuildResponse, 200)
@validate_request(RebuildRequest)
async def rebuild_counters(data: RebuildRequest):
    """Restore missing Valkey counters from the ledger; never overwrites live ones."""
    if (off := await _quota_off()) is not None:
        return off
    client = getattr(_ext, "redis_client", None)
    if client is None:
        return jsonify({"error": "Cache is unavailable"}), 503
    org_id = g.user.get("organization_id")
    database = _db()
    cycle = (
        data.cycle
        if data.cycle is not None
        else await asyncio.to_thread(current_cycle_for, database, org_id)
    )
    if cycle is None:
        return jsonify({"error": "No active cycle; configure the org pool anchor first"}), 409
    restored = await asyncio.to_thread(
        QuotaLedger(database).rebuild_counters, client, org_id, cycle
    )
    _record_write("counter_rebuild", "restored", org_id=org_id, cycle=cycle, restored=restored)
    return {"cycle": cycle, "counters_restored": restored}
