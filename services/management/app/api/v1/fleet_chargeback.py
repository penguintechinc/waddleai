"""WaddleAI Management API v1 - fleet savings and chargeback reports (Plan C8, AM10/AM22).

``GET /api/v1/fleet/savings`` and ``GET /api/v1/fleet/chargeback`` report, per organization,
what locally hosted models (vLLM, Ollama, llama.cpp) cost at their snapshotted virtual price and
what that usage would have cost on each deployment's commercial comparison model. The
``/fleet/vllm/*`` paths are kind=vLLM aliases. Org-scoped from the JWT, ``fleet:admin`` scope,
behind the ``waddleai.fleet_v2`` flag. Money is returned as exact decimal strings.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from penguin_dal.db import DB
from quart import g, jsonify, request
from quart_schema import validate_response

from shared.auth.rbac import Permission
from shared.fleet.enums import LocalDeploymentKind
from shared.fleet.vllm_ray.rollup import (
    ChargebackResult,
    SavingsResult,
    chargeback,
    savings,
)
from shared.utils.feature_flags import is_feature_enabled

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)

FLEET_V2_FLAG = "waddleai.fleet_v2"
_PERIODS = {"7d": 7, "30d": 30}
_DEFAULT_PERIOD = "30d"
_KINDS = frozenset(int(k) for k in LocalDeploymentKind)


def _db() -> DB:
    """Return the process-wide penguin-dal handle, narrowed away from ``None``."""
    if db is None:
        raise RuntimeError("database not initialized")
    return db


@dataclass(slots=True)
class DeploymentSavingsDTO:
    """One deployment's savings line."""

    kind: int
    deployment_id: int
    comparison_model: str | None
    tokens_input: int
    tokens_output: int
    virtual_cost: str
    commercial_equivalent_cost: str | None
    savings: str | None


@dataclass(slots=True)
class SavingsData:
    """Savings report payload (decimal strings)."""

    since: str
    tokens_input: int
    tokens_output: int
    virtual_cost: str
    commercial_equivalent_cost: str
    savings: str
    by_deployment: list[DeploymentSavingsDTO]
    rows_examined: int
    rows_missing_price: int
    missing_commercial_models: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ReportMeta:
    """Envelope ``meta`` for a report: the period and generation time."""

    period: str
    timestamp: str


@dataclass(slots=True)
class SavingsResponse:
    """Response body for GET /api/v1/fleet/savings."""

    status: str
    data: SavingsData
    meta: ReportMeta


@dataclass(slots=True)
class ChargebackRowDTO:
    """One (team, deployment) chargeback line."""

    team_id: int | None
    local_deployment_kind: int
    local_deployment_id: int
    tokens_input: int
    tokens_output: int
    request_count: int
    virtual_cost: str


@dataclass(slots=True)
class ChargebackData:
    """Chargeback report payload (decimal strings)."""

    since: str
    rows: list[ChargebackRowDTO]
    total_virtual_cost: str
    rows_examined: int
    rows_missing_price: int


@dataclass(slots=True)
class ChargebackResponse:
    """Response body for GET /api/v1/fleet/chargeback."""

    status: str
    data: ChargebackData
    meta: ReportMeta


def _now() -> str:
    """UTC timestamp string used in every envelope ``meta``."""
    return datetime.utcnow().isoformat() + "Z"


def _error(detail: str, status: int = 400) -> tuple[Any, int]:
    """Error envelope."""
    return jsonify({"status": "error", "error": detail}), status


def _int_arg(name: str) -> int | None:
    """Optional positive-int query arg; raises ``ValueError`` when present but invalid."""
    raw = request.args.get(name)
    if raw is None or raw == "":
        return None
    if not raw.isdigit() or int(raw) <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(raw)


def _params(forced_kind: int | None) -> tuple[str, date, int | None, int | None, int | None]:
    """Validated ``(period, since, kind, team_id, deployment_id)`` from the query string."""
    period = request.args.get("since", _DEFAULT_PERIOD)
    if period not in _PERIODS:
        raise ValueError(f"since must be one of {sorted(_PERIODS)}")
    kind = _int_arg("kind") if forced_kind is None else forced_kind
    if kind is not None and kind not in _KINDS:
        raise ValueError(f"kind must be one of {sorted(_KINDS)}")
    team_id = _int_arg("team_id")
    deployment_id = _int_arg("deployment_id")
    if deployment_id is not None and kind is None:
        raise ValueError("deployment_id requires kind")
    since = date.today() - timedelta(days=_PERIODS[period])
    return period, since, kind, team_id, deployment_id


def _savings_payload(result: SavingsResult, since: date) -> dict[str, Any]:
    """Serialize a SavingsResult with exact decimal strings."""
    return {
        "since": since.isoformat(),
        "tokens_input": result.tokens_input,
        "tokens_output": result.tokens_output,
        "virtual_cost": str(result.virtual_cost),
        "commercial_equivalent_cost": str(result.commercial_equivalent_cost),
        "savings": str(result.savings),
        "by_deployment": [
            {
                "kind": d.kind,
                "deployment_id": d.deployment_id,
                "comparison_model": d.comparison_model,
                "tokens_input": d.tokens_input,
                "tokens_output": d.tokens_output,
                "virtual_cost": str(d.virtual_cost),
                "commercial_equivalent_cost": (
                    None
                    if d.commercial_equivalent_cost is None
                    else str(d.commercial_equivalent_cost)
                ),
                "savings": None if d.savings is None else str(d.savings),
            }
            for d in result.by_deployment
        ],
        "rows_examined": result.rows_examined,
        "rows_missing_price": result.rows_missing_price,
        "missing_commercial_models": result.missing_commercial_models,
    }


def _chargeback_payload(result: ChargebackResult, since: date) -> dict[str, Any]:
    """Serialize a ChargebackResult with exact decimal strings."""
    return {
        "since": since.isoformat(),
        "rows": [
            {
                "team_id": r.team_id,
                "local_deployment_kind": r.local_deployment_kind,
                "local_deployment_id": r.local_deployment_id,
                "tokens_input": r.tokens_input,
                "tokens_output": r.tokens_output,
                "request_count": r.request_count,
                "virtual_cost": str(r.virtual_cost),
            }
            for r in result.rows
        ],
        "total_virtual_cost": str(result.total_virtual_cost),
        "rows_examined": result.rows_examined,
        "rows_missing_price": result.rows_missing_price,
    }


async def _savings_response(forced_kind: int | None) -> tuple[Any, int]:
    """Shared body of the savings route and its vLLM alias."""
    org_id = g.user.get("organization_id")
    if not is_feature_enabled(FLEET_V2_FLAG, distinct_id=str(org_id), default=False):
        return jsonify({"status": "error", "error": "not_found"}), 404
    try:
        period, since, kind, _team, _dep = _params(forced_kind)
    except ValueError as exc:
        return _error(str(exc))
    result = await asyncio.to_thread(lambda: savings(_db(), org_id, since, kind))
    return {
        "status": "success",
        "data": _savings_payload(result, since),
        "meta": {"period": period, "timestamp": _now()},
    }, 200


async def _chargeback_response(forced_kind: int | None) -> tuple[Any, int]:
    """Shared body of the chargeback route and its vLLM alias."""
    org_id = g.user.get("organization_id")
    if not is_feature_enabled(FLEET_V2_FLAG, distinct_id=str(org_id), default=False):
        return jsonify({"status": "error", "error": "not_found"}), 404
    try:
        period, since, kind, team_id, deployment_id = _params(forced_kind)
    except ValueError as exc:
        return _error(str(exc))
    result = await asyncio.to_thread(
        lambda: chargeback(_db(), org_id, since, team_id, deployment_id, kind)
    )
    return {
        "status": "success",
        "data": _chargeback_payload(result, since),
        "meta": {"period": period, "timestamp": _now()},
    }, 200


@api_v1_bp.route("/fleet/savings", methods=["GET"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(SavingsResponse, 200)
async def fleet_savings():
    """Savings of the local fleet (all kinds, or ``?kind=``) versus commercial comparison."""
    return await _savings_response(None)


@api_v1_bp.route("/fleet/chargeback", methods=["GET"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(ChargebackResponse, 200)
async def fleet_chargeback():
    """Virtual-price chargeback per team and deployment (all kinds, or ``?kind=``)."""
    return await _chargeback_response(None)


@api_v1_bp.route("/fleet/vllm/savings", methods=["GET"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(SavingsResponse, 200)
async def fleet_vllm_savings():
    """Alias of ``/fleet/savings`` restricted to vLLM deployments."""
    return await _savings_response(int(LocalDeploymentKind.VLLM))


@api_v1_bp.route("/fleet/vllm/chargeback", methods=["GET"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(ChargebackResponse, 200)
async def fleet_vllm_chargeback():
    """Alias of ``/fleet/chargeback`` restricted to vLLM deployments."""
    return await _chargeback_response(int(LocalDeploymentKind.VLLM))
