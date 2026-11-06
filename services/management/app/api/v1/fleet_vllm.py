"""WaddleAI Management API v1 - vLLM-on-Ray GPU fleet (Plan C, spec 4.5-4.7, 5.2, 6.3).

Flag-gated (``waddleai.fleet_v2`` AND ``waddleai.fleet-vllm-ray``, default OFF -> 404) and
``fleet:admin``-scoped: GPU inventory, model search, fit-check, TCO price suggestion, deployment
CRUD with per-deployment bearer tokens, events, token rotation and weights teardown. Every
response is an explicit dataclass DTO under ``@validate_response`` (never a raw row), org scope
comes from the validated token only, and refusal text from Plan B passes through verbatim.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from quart import g, jsonify, request
from quart_schema import validate_response

from shared.auth.rbac import Permission
from shared.database.models import update_connection_link_api_key
from shared.fleet.base import BackendType
from shared.fleet.caps import CapEnforcer, CapExceededError
from shared.fleet.enums import (
    DeploymentStatus,
    GpuVendor,
    InventoryState,
    LocalDeploymentKind,
    PriceMode,
    Quantization,
)
from shared.fleet.placement import is_denied_origin
from shared.fleet.registry import build_backend
from shared.fleet.vllm_pricing import (
    NO_PARAMS_B,
    comparison_rates,
    price_deployment,
    resolve_params_b,
    tco_price,
)
from shared.fleet.vllm_ray.fit import QUANT_MATRIX, FitResult, Layout, build_pools, fit_check
from shared.fleet.vllm_ray.gpu_price_reference import GPU_PRICE_REFERENCE
from shared.fleet.vllm_ray.model_size import parse_params_b
from shared.fleet.vllm_ray.slug import derive_slug
from shared.fleet.vllm_ray.tco import TcoResult
from shared.observability.vllm_metrics import record_placement
from shared.routing.prefer_local import validate_prefer_local
from shared.security.credential_encryption import decrypt_credential
from shared.utils.feature_flags import is_feature_enabled

from ...extensions import db
from ...services import fleet_secrets, model_store
from ...services.vllm_reconciler import offer_blocked, sync_deployment_link, unlink_deployment
from . import api_v1_bp
from ._pagination import PageRequest
from ._pricing_body import PriceFieldError, parse_price_fields
from .auth import require_auth, require_scope
from .fleet import (
    FLEET_V2_FLAG,
    FLEET_VLLM_RAY_FLAG,
    ActionMeta,
    ListMeta,
    TimestampMeta,
    _get_license_client,
    _get_org_scoped_backend,
    _now,
    _validation_error,
    _vllm_ray_enabled,
)

logger = logging.getLogger(__name__)

S = DeploymentStatus
_NOT_FOUND = ({"status": "error", "error": "not_found"}, 404)
_NAME_MAX = 255
_REF_MAX = 512
_DTYPES = ("bfloat16", "float16", "float32")
_FIT_FALLBACK = "The fit check did not explain why. Contact your administrator."
_DENIED_TEXT = "This model's publisher is on the blocked-origin list, so it cannot be deployed."
_KINDS = {int(k) for k in LocalDeploymentKind}


def _db() -> Any:
    """Return the process-wide penguin-dal handle, narrowed away from ``None``."""
    if db is None:
        raise RuntimeError("database not initialized")
    return db


def _gate(org_id: Any) -> tuple[Any, int] | None:
    """404 envelope unless both fleet flags are on for the org; None means proceed."""
    if not _vllm_ray_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404
    return None


def _err(message: str, code: int, **extra: Any) -> tuple[Any, int]:
    """Error envelope with optional extra keys."""
    return jsonify({"status": "error", "error": message, **extra}), code


# --------------------------------------------------------------------------------------
# DTOs
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class FlagsData:
    """The two flags that gate the vLLM surface."""

    fleet_v2: bool
    fleet_vllm_ray: bool


@dataclass(slots=True)
class FlagsResponse:
    """GET /fleet/vllm/flags."""

    status: str
    data: FlagsData
    meta: TimestampMeta


@dataclass(slots=True)
class GpuInventoryItem:
    """One discovered GPU (never a raw row)."""

    fleet_backend_id: int
    node_name: str
    gpu_index: int
    vendor: int
    gpu_class: str | None
    product_name: str
    vram_total_mb: int
    vram_used_mb: int
    schedulable: bool
    state: int
    last_seen: str | None


@dataclass(slots=True)
class GpuInventoryResponse:
    """GET /fleet/gpu-inventory."""

    status: str
    data: list[GpuInventoryItem]
    meta: ListMeta


@dataclass(slots=True)
class ModelItem:
    """A selectable registry model."""

    name: str
    public_id: str
    params_b: float | None
    origin: str
    min_vram_gb: int | None


@dataclass(slots=True)
class ModelListResponse:
    """GET /fleet/vllm/models."""

    status: str
    data: list[ModelItem]
    meta: TimestampMeta


@dataclass(slots=True)
class LayoutItem:
    """One candidate placement from the fit check."""

    vendor: int
    gpu_class: str
    replicas: int
    tensor_parallel: int
    pipeline_parallel: int
    strategy: int
    nodes_used: int
    vram_needed_mb: int
    vram_available_mb: int
    recommended: bool
    explanation: str


@dataclass(slots=True)
class FitCheckData:
    """Fit-check outcome plus the quantizations the candidate pools allow."""

    fits: bool
    layouts: list[LayoutItem]
    refusal: str | None
    allowed_quantizations: list[int]


@dataclass(slots=True)
class FitCheckResponse:
    """POST /fleet/vllm/fit-check."""

    status: str
    data: FitCheckData
    meta: TimestampMeta


@dataclass(slots=True)
class PriceSuggestionData:
    """TCO price suggestion (the ``TcoResult`` fields)."""

    hourly_cost_usd: float
    gpu_hourly_usd: float
    node_hourly_usd: float
    node_base_usd: float
    tokens_per_s_used: float
    source: str
    price_input_per_1m: float
    price_output_per_1m: float
    breakdown: dict[str, Any]


@dataclass(slots=True)
class PriceSuggestionResponse:
    """GET /fleet/price-suggestion."""

    status: str
    data: PriceSuggestionData
    meta: TimestampMeta


@dataclass(slots=True)
class EventItem:
    """A deployment timeline event."""

    ts: str
    state: int
    message: str


@dataclass(slots=True)
class EventListResponse:
    """GET /fleet/vllm/deployments/<id>/events."""

    status: str
    data: list[EventItem]
    meta: ListMeta


@dataclass(slots=True)
class DeploymentItem:
    """A vLLM deployment as returned by the API (no numeric PK, no token)."""

    public_id: str
    name: str
    slug: str
    model_ref: str
    served_model_name: str
    fleet_backend_id: int
    vendor: int
    gpu_class: str
    allow_mixed_class: bool
    quantization: int
    dtype: str
    tensor_parallel: int
    pipeline_parallel: int
    replicas_min: int
    replicas_max: int
    max_model_len: int
    status: int
    status_detail: str | None
    endpoint_url: str | None
    expose_via_proxy: bool
    prefer_local: dict[str, Any] | None
    comparison_model: str
    price_mode: int
    price_input_per_1m: str
    price_output_per_1m: str
    price_breakdown: dict[str, Any] | None
    observed_tokens_per_s: float | None
    api_key_fingerprint: str | None
    created_at: str | None
    updated_at: str | None


@dataclass(slots=True)
class DeploymentDetail(DeploymentItem):
    """A deployment plus its most recent timeline events."""

    events: list[EventItem] = field(default_factory=list)


@dataclass(slots=True)
class DeploymentListResponse:
    """GET /fleet/vllm/deployments."""

    status: str
    data: list[DeploymentItem]
    meta: ListMeta


@dataclass(slots=True)
class DeploymentResponse:
    """GET one deployment (with timeline)."""

    status: str
    data: DeploymentDetail
    meta: TimestampMeta


@dataclass(slots=True)
class DeploymentActionResponse:
    """POST create / PATCH update."""

    status: str
    data: DeploymentItem
    meta: ActionMeta


@dataclass(slots=True)
class DeletedData:
    """The ``data`` of a delete response."""

    deleted: bool


@dataclass(slots=True)
class DeleteDeploymentResponse:
    """DELETE /fleet/vllm/deployments/<id>."""

    status: str
    data: DeletedData
    meta: ActionMeta


@dataclass(slots=True)
class RotateTokenData:
    """Result of a token rotation: the new fingerprint only, never the token."""

    public_id: str
    api_key_fingerprint: str


@dataclass(slots=True)
class RotateTokenResponse:
    """POST /fleet/vllm/deployments/<id>/rotate-token."""

    status: str
    data: RotateTokenData
    meta: ActionMeta


@dataclass(slots=True)
class WeightsDeletedData:
    """The ``data`` of a weights delete response."""

    deleted: bool
    model_ref: str


@dataclass(slots=True)
class WeightsDeletedResponse:
    """DELETE /fleet/models/<model_ref>/weights."""

    status: str
    data: WeightsDeletedData
    meta: ActionMeta


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _window(page: PageRequest) -> tuple[int, int]:
    """penguin-dal ``limitby`` is ``(offset, limit)``; ``PageRequest`` gives ``(start, stop)``."""
    return (page.limitby[0], page.limit)


def _iso(value: datetime | None) -> str | None:
    """ISO timestamp or None."""
    return value.isoformat() if value else None


def _fingerprint_for(database: Any, dep: Any) -> str | None:
    """Fingerprint of the deployment's current bearer token (decrypted link key), if any."""
    if not dep.connection_link_id:
        return None
    link = database(database.connection_links.id == dep.connection_link_id).select().first()
    if link is None or not link.api_key:
        return None
    try:
        return fleet_secrets.fingerprint(decrypt_credential(link.api_key))
    except ValueError:
        return None


def _to_item(database: Any, dep: Any) -> DeploymentItem:
    """Serialize a deployment row to its DTO."""
    return DeploymentItem(
        public_id=str(dep.public_id),
        name=dep.name,
        slug=dep.slug,
        model_ref=dep.model_ref,
        served_model_name=dep.served_model_name,
        fleet_backend_id=int(dep.fleet_backend_id),
        vendor=int(dep.vendor),
        gpu_class=dep.gpu_class,
        allow_mixed_class=bool(dep.allow_mixed_class),
        quantization=int(dep.quantization),
        dtype=dep.dtype,
        tensor_parallel=int(dep.tensor_parallel),
        pipeline_parallel=int(dep.pipeline_parallel),
        replicas_min=int(dep.replicas_min),
        replicas_max=int(dep.replicas_max),
        max_model_len=int(dep.max_model_len),
        status=int(dep.status),
        status_detail=dep.status_detail,
        endpoint_url=dep.endpoint_url,
        expose_via_proxy=bool(dep.expose_via_proxy),
        prefer_local=dep.prefer_local,
        comparison_model=dep.comparison_model,
        price_mode=int(dep.price_mode),
        price_input_per_1m=str(dep.price_input_per_1m),
        price_output_per_1m=str(dep.price_output_per_1m),
        price_breakdown=dep.price_breakdown,
        observed_tokens_per_s=(
            None if dep.observed_tokens_per_s is None else float(dep.observed_tokens_per_s)
        ),
        api_key_fingerprint=_fingerprint_for(database, dep),
        created_at=_iso(dep.created_at),
        updated_at=_iso(dep.updated_at),
    )


def _events(database: Any, dep_id: int, limit: int, start: int = 0) -> list[EventItem]:
    """Newest-first events of a deployment."""
    table = database.vllm_deployment_events
    rows = database(table.deployment_id == dep_id).select(
        orderby=[~table.ts, ~table.id], limitby=(start, limit)
    )
    return [EventItem(_iso(r.ts) or "", int(r.state), r.message) for r in rows]


def _get_deployment(database: Any, public_id: uuid.UUID, org_id: int) -> tuple[Any, str]:
    """Deployment by public id plus outcome: ``ok`` or ``not_found``.

    Another org's deployment is ``not_found`` (never 403) so ids are no existence oracle.
    """
    row = database(database.vllm_deployments.public_id == str(public_id)).select().first()
    if row is None or row.org_id != org_id:
        return None, "not_found"
    return row, "ok"


def _deployment_lookup_error(outcome: str) -> tuple[Any, int]:
    """404 envelope for a failed deployment lookup (missing or another org's)."""
    del outcome
    return jsonify({"status": "error", "error": "deployment not found"}), 404


def _inventory_records(
    database: Any, org_id: int, backend_id: int | None, vendor: int | None, gpu_class: str | None
) -> list[Any]:
    """This org's WARM GPU rows (optionally one backend / vendor / class)."""
    table = database.gpu_inventory
    query = (table.org_id == org_id) & (table.state == int(InventoryState.WARM))
    if backend_id is not None:
        query = query & (table.fleet_backend_id == backend_id)
    if vendor is not None:
        query = query & (table.vendor == vendor)
    if gpu_class:
        query = query & (table.gpu_class == gpu_class)
    return list(database(query).select(orderby=[table.node_name, table.gpu_index]))


def _kv_headroom(backend_row: Any) -> int:
    """KV-cache headroom percentage from the backend config (default 20)."""
    value = (backend_row.config or {}).get("kv_headroom_pct", 20)
    return value if isinstance(value, int) and 0 <= value < 100 else 20


def _int(body: dict[str, Any], key: str, *, minimum: int = 1) -> int | None:
    """Integer body field >= ``minimum``; raises ``ValueError`` on a bad type/range."""
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{key} must be an integer >= {minimum}")
    return value


def _vendor(value: Any) -> int:
    """Validate a vendor int against ``GpuVendor``."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value not in {int(v) for v in GpuVendor}
    ):
        raise ValueError(f"vendor must be one of {[int(v) for v in GpuVendor]}")
    return value


def _layout_dto(layout: Layout) -> LayoutItem:
    """Layout -> DTO."""
    return LayoutItem(
        layout.vendor,
        layout.gpu_class,
        layout.replicas,
        layout.tensor_parallel,
        layout.pipeline_parallel,
        layout.strategy,
        layout.nodes_used,
        layout.vram_needed_mb,
        layout.vram_available_mb,
        layout.recommended,
        layout.explanation,
    )


def _allowed_quantizations(vendors: set[int]) -> list[int]:
    """Sorted quantization codes allowed on every given vendor's pools (union)."""
    allowed: set[int] = set()
    for vendor in vendors:
        allowed |= {int(q) for q in QUANT_MATRIX.get(vendor, set())}
    return sorted(allowed)


def _quantization_error(vendor: int, quantization: int) -> str | None:
    """Refusal text when ``quantization`` is not available on ``vendor``."""
    allowed = {int(q) for q in QUANT_MATRIX.get(vendor, set())}
    if quantization in allowed:
        return None
    names = ", ".join(f"{Quantization(q).name} ({int(q)})" for q in sorted(allowed))
    return (
        f"Quantization {quantization} is not available on {GpuVendor(vendor).name} GPUs. "
        f"Allowed options: {names}."
    )


def _pick_layout(fit: FitResult, tp: int, pp: int, gpu_class: str) -> Layout | None:
    """The fit layout matching the requested class and parallelism, if any."""
    for layout in fit.layouts:
        if (
            layout.gpu_class == gpu_class
            and layout.tensor_parallel == tp
            and layout.pipeline_parallel == pp
        ):
            return layout
    return None


# --------------------------------------------------------------------------------------
# Read-only endpoints
# --------------------------------------------------------------------------------------


@api_v1_bp.route("/fleet/vllm/flags", methods=["GET"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(FlagsResponse, 200)
async def vllm_flags() -> Any:
    """Report each gating flag independently (always 200 for a fleet admin)."""
    org_id = g.user.get("organization_id")
    return {
        "status": "success",
        "data": {
            "fleet_v2": is_feature_enabled(FLEET_V2_FLAG, distinct_id=str(org_id), default=False),
            "fleet_vllm_ray": is_feature_enabled(
                FLEET_VLLM_RAY_FLAG, distinct_id=str(org_id), default=False
            ),
        },
        "meta": {"timestamp": _now()},
    }, 200


@api_v1_bp.route("/fleet/gpu-inventory", methods=["GET"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(GpuInventoryResponse, 200)
async def list_gpu_inventory() -> Any:
    """List this org's GPUs; filters: vendor, gpu_class, node, schedulable."""
    org_id = g.user.get("organization_id")
    if (blocked := _gate(org_id)) is not None:
        return blocked
    args = request.args
    table = _db().gpu_inventory
    query = table.org_id == org_id
    if "vendor" in args:
        try:
            query = query & (table.vendor == _vendor(int(args["vendor"])))
        except ValueError:
            return _validation_error(f"vendor must be one of {[int(v) for v in GpuVendor]}")
    if args.get("gpu_class"):
        query = query & (table.gpu_class == args["gpu_class"])
    if args.get("node"):
        query = query & (table.node_name == args["node"])
    if "schedulable" in args:
        flag = args["schedulable"].lower()
        if flag not in ("true", "false"):
            return _validation_error("schedulable must be true or false")
        query = query & (table.schedulable == (flag == "true"))
    page = PageRequest.from_request()

    def _fetch() -> tuple[int, list[Any]]:
        database = _db()
        rows = database(query).select(
            orderby=[table.node_name, table.gpu_index], limitby=_window(page)
        )
        return database(query).count(), list(rows)

    total, rows = await asyncio.to_thread(_fetch)
    return {
        "status": "success",
        "data": [
            {
                "fleet_backend_id": int(r.fleet_backend_id),
                "node_name": r.node_name,
                "gpu_index": int(r.gpu_index),
                "vendor": int(r.vendor),
                "gpu_class": r.gpu_class,
                "product_name": r.product_name,
                "vram_total_mb": int(r.vram_total_mb),
                "vram_used_mb": int(r.vram_used_mb or 0),
                "schedulable": bool(r.schedulable),
                "state": int(r.state),
                "last_seen": _iso(r.last_seen),
            }
            for r in rows
        ],
        "meta": {"total": total, "page": page.page, "limit": page.limit, "timestamp": _now()},
    }, 200


@api_v1_bp.route("/fleet/vllm/models", methods=["GET"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(ModelListResponse, 200)
async def search_vllm_models() -> Any:
    """Search the model registry by name (``q``), bounded by ``limit`` 1-100 (default 25)."""
    org_id = g.user.get("organization_id")
    if (blocked := _gate(org_id)) is not None:
        return blocked
    raw_limit = request.args.get("limit", "25")
    try:
        limit = int(raw_limit)
    except ValueError:
        return _validation_error("limit must be an integer between 1 and 100")
    if not 1 <= limit <= 100:
        return _validation_error("limit must be an integer between 1 and 100")
    q = request.args.get("q", "").strip()

    def _fetch() -> list[Any]:
        database = _db()
        table = database.model_registry
        query = table.name.ilike(f"%{q}%") if q else table.id > 0
        return list(database(query).select(orderby=table.name, limitby=(0, limit)))

    rows = await asyncio.to_thread(_fetch)
    return {
        "status": "success",
        "data": [
            {
                "name": r.name,
                "public_id": str(r.public_id),
                "params_b": None if r.params_b is None else float(r.params_b),
                "origin": r.origin,
                "min_vram_gb": r.min_vram,
            }
            for r in rows
        ],
        "meta": {"timestamp": _now()},
    }, 200


@api_v1_bp.route("/fleet/vllm/fit-check", methods=["POST"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(FitCheckResponse, 200)
async def vllm_fit_check() -> Any:
    """Check whether a model fits this org's WARM GPU pools (no side effects)."""
    org_id = g.user.get("organization_id")
    if (blocked := _gate(org_id)) is not None:
        return blocked
    body = await request.get_json(silent=True)
    if not isinstance(body, dict):
        return _validation_error("request body must be a JSON object")
    try:
        quantization = _int(body, "quantization", minimum=0) or 0
        max_model_len = _int(body, "max_model_len") or 4096
        vendor = _vendor(body["vendor"]) if body.get("vendor") is not None else None
        params_b = body.get("params_b")
        if params_b is not None and (
            isinstance(params_b, bool)
            or not isinstance(params_b, (int, float))
            or not math.isfinite(params_b)
            or params_b <= 0
        ):
            raise ValueError("params_b must be a number greater than zero")
    except ValueError as exc:
        return _validation_error(str(exc))
    dtype = body.get("dtype", "bfloat16")
    if dtype not in _DTYPES:
        return _validation_error(f"dtype must be one of {list(_DTYPES)}")
    model_ref = body.get("model_ref")
    if params_b is None:
        if not model_ref:
            return _validation_error("model_ref or params_b is required")
        params_b = parse_params_b(str(model_ref))
        if params_b is None:
            return _err(NO_PARAMS_B, 422)

    def _run() -> tuple[FitResult, set[int]]:
        records = _inventory_records(_db(), org_id, None, vendor, body.get("gpu_class"))
        pools = build_pools(records)
        result = fit_check(
            float(params_b),
            quantization,
            dtype,
            max_model_len,
            pools,
            bool(body.get("allow_mixed_class", False)),
        )
        return result, {p.vendor for p in pools}

    result, vendors = await asyncio.to_thread(_run)
    return {
        "status": "success",
        "data": {
            "fits": result.fits,
            "layouts": [_layout_dto(layout) for layout in result.layouts],
            "refusal": result.refusal,
            "allowed_quantizations": _allowed_quantizations(vendors),
        },
        "meta": {"timestamp": _now()},
    }, 200


def _suggest_vllm(args: Any, org_id: int) -> tuple[TcoResult | None, tuple[Any, int] | None]:
    """Price suggestion for kind=1 (fit-checked layout)."""
    try:
        vendor = _vendor(int(args["vendor"]))
        tp, pp, replicas = int(args["tp"]), int(args["pp"]), int(args["replicas"])
    except (KeyError, ValueError):
        return None, _validation_error("vendor, tp, pp and replicas are required integers")
    if min(tp, pp, replicas) < 1:
        return None, _validation_error("tp, pp and replicas must be at least 1")
    gpu_class = args.get("gpu_class")
    if not gpu_class:
        return None, _validation_error("gpu_class is required")
    params_b = _params_from_args(args)
    if params_b is None:
        return None, _err(NO_PARAMS_B, 422)
    database = _db()
    backend_row = (
        database(
            (database.fleet_backends.org_id == org_id)
            & (database.fleet_backends.type == "vllm_ray")
        )
        .select(orderby=database.fleet_backends.id)
        .first()
    )
    if backend_row is None:
        return None, _err("No vLLM Ray backend is configured for this organization.", 422)
    pools = build_pools(_inventory_records(database, org_id, backend_row.id, vendor, gpu_class))
    fit = fit_check(params_b, 0, "bfloat16", 4096, pools, False, _kv_headroom(backend_row))
    if not fit.fits:
        return None, _err(fit.refusal or _FIT_FALLBACK, 422)
    layout = _pick_layout(fit, tp, pp, gpu_class)
    if layout is None:
        return None, _err(
            "The requested layout does not fit; recommended: "
            + next((x.explanation for x in fit.layouts if x.recommended), ""),
            422,
        )
    layout.replicas = replicas
    try:
        return tco_price(backend_row.config or {}, layout, params_b, None), None
    except ValueError as exc:
        return None, _err(str(exc), 422)


def _params_from_args(args: Any) -> float | None:
    """``params_b`` query value, else parsed from ``model_ref``; None when unknowable."""
    raw = args.get("params_b")
    if raw is not None:
        try:
            value = float(raw)
        except ValueError:
            return None
        return value if math.isfinite(value) and value > 0 else None
    return parse_params_b(args["model_ref"]) if args.get("model_ref") else None


def _suggest_other(args: Any, org_id: int) -> tuple[TcoResult | None, tuple[Any, int] | None]:
    """Price suggestion for Ollama / llama.cpp: a single-node layout of ``count`` GPUs."""
    gpu_class = args.get("gpu_class")
    if not gpu_class or gpu_class not in GPU_PRICE_REFERENCE:
        return None, _err("gpu_class must be a class from the GPU price reference.", 422)
    try:
        count = int(args.get("count", "1"))
    except ValueError:
        return None, _validation_error("count must be an integer >= 1")
    if count < 1:
        return None, _validation_error("count must be an integer >= 1")
    params_b = _params_from_args(args)
    if params_b is None:
        return None, _err(NO_PARAMS_B, 422)
    database = _db()
    backend_row = (
        database(
            (database.fleet_backends.org_id == org_id)
            & (database.fleet_backends.type == "vllm_ray")
        )
        .select(orderby=database.fleet_backends.id)
        .first()
    )
    if backend_row is None:
        return None, _err(
            "No vLLM Ray backend is configured; pricing inputs come from its config.", 422
        )
    ref = GPU_PRICE_REFERENCE[gpu_class]
    layout = Layout(int(ref.vendor), gpu_class, 1, count, 1, 1, 1, 0, 0, True, "")
    try:
        return tco_price(backend_row.config or {}, layout, params_b, None), None
    except ValueError as exc:
        return None, _err(str(exc), 422)


@api_v1_bp.route("/fleet/price-suggestion", methods=["GET"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(PriceSuggestionResponse, 200)
async def price_suggestion() -> Any:
    """TCO price suggestion for a local deployment kind (``kind`` 1 vLLM, 2 Ollama, 3 llama.cpp)."""
    return await _price_suggestion(alias=False)


@api_v1_bp.route("/fleet/vllm/price-suggestion", methods=["GET"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(PriceSuggestionResponse, 200)
async def vllm_price_suggestion() -> Any:
    """Alias of ``/fleet/price-suggestion`` pinned to ``kind=1`` (vLLM)."""
    return await _price_suggestion(alias=True)


async def _price_suggestion(*, alias: bool) -> Any:
    """Shared body of the two price-suggestion routes."""
    org_id = g.user.get("organization_id")
    if (blocked := _gate(org_id)) is not None:
        return blocked
    args = request.args
    try:
        kind = 1 if alias else int(args.get("kind", "1"))
    except ValueError:
        return _validation_error("kind must be 1, 2 or 3")
    if kind not in _KINDS:
        return _validation_error("kind must be 1, 2 or 3")
    suggest = _suggest_vllm if kind == int(LocalDeploymentKind.VLLM) else _suggest_other
    result, error = await asyncio.to_thread(suggest, args, org_id)
    if error is not None:
        return error
    if result is None:
        return _err("pricing is unavailable for this layout", 422)
    return {
        "status": "success",
        "data": {
            "hourly_cost_usd": result.hourly_cost_usd,
            "gpu_hourly_usd": result.gpu_hourly_usd,
            "node_hourly_usd": result.node_hourly_usd,
            "node_base_usd": result.node_base_usd,
            "tokens_per_s_used": result.tokens_per_s_used,
            "source": result.source,
            "price_input_per_1m": result.price_input_per_1m,
            "price_output_per_1m": result.price_output_per_1m,
            "breakdown": result.breakdown,
        },
        "meta": {"timestamp": _now()},
    }, 200


# --------------------------------------------------------------------------------------
# Deployments
# --------------------------------------------------------------------------------------


@api_v1_bp.route("/fleet/vllm/deployments", methods=["GET"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(DeploymentListResponse, 200)
async def list_vllm_deployments() -> Any:
    """List this org's deployments; filters: status, vendor, model (substring)."""
    org_id = g.user.get("organization_id")
    if (blocked := _gate(org_id)) is not None:
        return blocked
    args = request.args
    table = _db().vllm_deployments
    query = table.org_id == org_id
    try:
        if "status" in args:
            status = int(args["status"])
            if status not in {int(s) for s in S}:
                raise ValueError
            query = query & (table.status == status)
        if "vendor" in args:
            query = query & (table.vendor == _vendor(int(args["vendor"])))
    except ValueError:
        return _validation_error("status and vendor must be valid integer codes")
    if args.get("model"):
        query = query & table.model_ref.ilike(f"%{args['model']}%")
    page = PageRequest.from_request()

    def _fetch() -> tuple[int, list[DeploymentItem]]:
        database = _db()
        rows = database(query).select(orderby=table.id, limitby=_window(page))
        return database(query).count(), [_to_item(database, r) for r in rows]

    total, items = await asyncio.to_thread(_fetch)
    return {
        "status": "success",
        "data": items,
        "meta": {"total": total, "page": page.page, "limit": page.limit, "timestamp": _now()},
    }, 200


@api_v1_bp.route("/fleet/vllm/deployments/<uuid:public_id>", methods=["GET"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(DeploymentResponse, 200)
async def get_vllm_deployment(public_id: uuid.UUID) -> Any:
    """One deployment with its recent timeline (404 for unknown and for another org's)."""
    org_id = g.user.get("organization_id")
    if (blocked := _gate(org_id)) is not None:
        return blocked

    def _fetch() -> tuple[str, DeploymentItem | None, list[EventItem]]:
        database = _db()
        row, outcome = _get_deployment(database, public_id, org_id)
        if row is None:
            return outcome, None, []
        return outcome, _to_item(database, row), _events(database, row.id, 50)

    outcome, item, events = await asyncio.to_thread(_fetch)
    if item is None:
        return _deployment_lookup_error(outcome)
    return {
        "status": "success",
        "data": {**asdict(item), "events": events},
        "meta": {"timestamp": _now()},
    }, 200


@api_v1_bp.route("/fleet/vllm/deployments/<uuid:public_id>/events", methods=["GET"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(EventListResponse, 200)
async def list_vllm_deployment_events(public_id: uuid.UUID) -> Any:
    """Newest-first, paginated timeline of a deployment."""
    org_id = g.user.get("organization_id")
    if (blocked := _gate(org_id)) is not None:
        return blocked
    page = PageRequest.from_request()

    def _fetch() -> tuple[str, int, list[EventItem]]:
        database = _db()
        row, outcome = _get_deployment(database, public_id, org_id)
        if row is None:
            return outcome, 0, []
        table = database.vllm_deployment_events
        total = database(table.deployment_id == row.id).count()
        return outcome, total, _events(database, row.id, page.limit, page.limitby[0])

    outcome, total, events = await asyncio.to_thread(_fetch)
    if outcome != "ok":
        return _deployment_lookup_error(outcome)
    return {
        "status": "success",
        "data": events,
        "meta": {"total": total, "page": page.page, "limit": page.limit, "timestamp": _now()},
    }, 200


@dataclass(slots=True)
class _CreateSpec:
    """A validated create request."""

    name: str
    model_ref: str
    served_model_name: str
    backend_id: int
    vendor: int
    gpu_class: str
    allow_mixed_class: bool
    quantization: int
    dtype: str
    tp: int
    pp: int
    replicas_min: int
    replicas_max: int
    max_model_len: int
    price_mode: PriceMode
    price_in: Decimal | None
    price_out: Decimal | None
    comparison_model: str
    params_b: float | None
    expose_via_proxy: bool
    prefer_local: dict[str, Any] | None


def _parse_create(body: dict[str, Any]) -> _CreateSpec:
    """Validate a create body; raises ``ValueError`` / ``PriceFieldError`` (caller-safe text)."""
    name = str(body.get("name", "")).strip()
    if not name or len(name) > _NAME_MAX:
        raise ValueError("name is required and must be 1-255 characters")
    model_ref = str(body.get("model_ref", "")).strip()
    if not model_ref or len(model_ref) > _REF_MAX:
        raise ValueError("model_ref is required and must be 1-512 characters")
    try:
        model_store.safe_model_ref(model_ref)
    except ValueError:
        raise ValueError("model_ref must be a plain HuggingFace repo id or registry name") from None
    served = str(body.get("served_model_name") or model_ref.rsplit("/", 1)[-1]).strip()
    if not served or len(served) > _NAME_MAX:
        raise ValueError("served_model_name must be 1-255 characters")
    backend_id = body.get("fleet_backend_id")
    if isinstance(backend_id, bool) or not isinstance(backend_id, int):
        raise ValueError("fleet_backend_id is required (integer)")
    vendor = _vendor(body.get("vendor"))
    gpu_class = str(body.get("gpu_class", "")).strip()
    if not gpu_class:
        raise ValueError("gpu_class is required")
    quantization = _int(body, "quantization", minimum=0) or 0
    if quantization not in {int(q) for q in Quantization}:
        raise ValueError(f"quantization must be one of {[int(q) for q in Quantization]}")
    dtype = body.get("dtype", "bfloat16")
    if dtype not in _DTYPES:
        raise ValueError(f"dtype must be one of {list(_DTYPES)}")
    tp = _int(body, "tensor_parallel") or 1
    pp = _int(body, "pipeline_parallel") or 1
    rmin = _int(body, "replicas_min", minimum=0)
    rmin = 1 if rmin is None else rmin
    rmax = _int(body, "replicas_max")
    rmax = max(rmin, 1) if rmax is None else rmax
    if rmax < rmin:
        raise ValueError("replicas_max must be >= replicas_min")
    max_len = _int(body, "max_model_len") or 4096
    price = parse_price_fields(body, default_mode=PriceMode.AUTO_TCO)
    comparison = str(body.get("comparison_model", "")).strip()
    if not comparison or len(comparison) > _NAME_MAX:
        raise ValueError("comparison_model is required (provider/model, for savings)")
    params_b = body.get("params_b")
    if params_b is not None and (
        isinstance(params_b, bool)
        or not isinstance(params_b, (int, float))
        or not math.isfinite(params_b)
        or params_b <= 0
    ):
        raise ValueError("params_b must be a number greater than zero")
    expose = body.get("expose_via_proxy", True)
    if not isinstance(expose, bool):
        raise ValueError("expose_via_proxy must be true or false")
    prefer = body.get("prefer_local")
    if prefer is not None and not (
        isinstance(prefer, dict)
        and isinstance(prefer.get("fallback_model"), str)
        and isinstance(prefer.get("match", {}), dict)
    ):
        raise ValueError("prefer_local must be {fallback_model: str, match: object}")
    if prefer is not None:
        validate_prefer_local(prefer.get("match", {}), prefer["fallback_model"])
    return _CreateSpec(
        name, model_ref, served, backend_id, vendor, gpu_class,
        bool(body.get("allow_mixed_class", False)), quantization, dtype, tp, pp, rmin, rmax,
        max_len, price.mode, price.price_input_per_1m, price.price_output_per_1m, comparison,
        None if params_b is None else float(params_b), expose, prefer,
    )  # fmt: skip


def _model_origin(database: Any, model_ref: str) -> str:
    """Origin of the model: its registry row, else the HuggingFace repo owner segment."""
    entry = database(database.model_registry.name == model_ref).select().first()
    if entry is not None and entry.origin:
        return str(entry.origin)
    return model_ref.split("/", 1)[0] if "/" in model_ref else ""


async def _create_refusal(
    database: Any, org_id: int, spec: _CreateSpec, backend_row: Any
) -> tuple[tuple[Any, int] | None, float | None, Layout | None]:
    """Run the ordered admission checks; returns (error, params_b, layout)."""
    origin = await asyncio.to_thread(_model_origin, database, spec.model_ref)
    if is_denied_origin(origin) or is_denied_origin(spec.model_ref.split("/", 1)[0]):
        record_placement("denied_origin")
        return _err(_DENIED_TEXT, 403), None, None
    problem = _quantization_error(spec.vendor, spec.quantization)
    if problem:
        record_placement("denied_quantization")
        return _err(problem, 422), None, None
    rates = await asyncio.to_thread(comparison_rates, database, spec.comparison_model)
    if rates is None:
        return (
            _err(
                f"No commercial rate is configured for '{spec.comparison_model}'; "
                "pick a comparison model that has a rate.",
                422,
            ),
            None,
            None,
        )
    try:
        await CapEnforcer(database, _get_license_client(), org_id).enforce_model_cap(
            spec.served_model_name
        )
    except CapExceededError as exc:
        record_placement("denied_cap")
        return _err(f"{exc} Upgrade your license to deploy more models.", 403), None, None
    params_b = spec.params_b or parse_params_b(spec.model_ref)
    if params_b is None:
        params_b = await asyncio.to_thread(
            lambda: resolve_params_b(
                database, SimpleNamespace(model_ref=spec.model_ref, price_breakdown=None)
            )
        )
    if params_b is None:
        return _err(NO_PARAMS_B, 422), None, None
    pools = build_pools(
        await asyncio.to_thread(
            _inventory_records, database, org_id, backend_row.id, spec.vendor, spec.gpu_class
        )
    )
    fit = fit_check(
        params_b,
        spec.quantization,
        spec.dtype,
        spec.max_model_len,
        pools,
        spec.allow_mixed_class,
        _kv_headroom(backend_row),
    )
    if not fit.fits:
        record_placement("no_fit")
        return _err(fit.refusal or _FIT_FALLBACK, 422), None, None
    layout = _pick_layout(fit, spec.tp, spec.pp, spec.gpu_class)
    if layout is None:
        record_placement("no_fit")
        recommended = next((x.explanation for x in fit.layouts if x.recommended), "")
        return _err(f"The requested layout does not fit. {recommended}".strip(), 422), None, None
    record_placement("placed")
    return None, params_b, layout


@api_v1_bp.route("/fleet/vllm/deployments", methods=["POST"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(DeploymentActionResponse, 201)
async def create_vllm_deployment() -> Any:
    """Create a deployment: validate, admit, price, mint its token, pull weights, deploy."""
    org_id = g.user.get("organization_id")
    if (blocked := _gate(org_id)) is not None:
        return blocked
    body = await request.get_json(silent=True)
    if not isinstance(body, dict):
        return _validation_error("request body must be a JSON object")
    try:
        spec = _parse_create(body)
    except (ValueError, PriceFieldError) as exc:
        return _validation_error(str(exc))
    database = _db()
    backend_row, outcome = await asyncio.to_thread(_get_org_scoped_backend, spec.backend_id, org_id)
    if outcome == "not_found":
        return _err("fleet backend not found", 404)
    if backend_row.type != BackendType.VLLM_RAY.value:
        return _validation_error("fleet_backend_id must reference a vllm_ray backend")
    refusal, params_b, layout = await _create_refusal(database, org_id, spec, backend_row)
    if refusal is not None:
        return refusal
    if params_b is None or layout is None:
        return _err(NO_PARAMS_B, 422)

    if await asyncio.to_thread(offer_blocked, database, org_id, spec.served_model_name):
        # Generic on purpose: never say whose offer holds the name.
        return _err(
            "A model offer with this served_model_name already exists; "
            "choose a different served_model_name.",
            409,
        )

    if await asyncio.to_thread(_name_taken, database, org_id, spec.name):
        return _err(f"a deployment named '{spec.name}' already exists for this org", 409)

    public_id = uuid.uuid4()
    slug = derive_slug(spec.name, public_id)
    config = backend_row.config or {}
    zero = Decimal(0)
    row = SimpleNamespace(
        public_id=public_id,
        price_mode=int(spec.price_mode),
        price_input_per_1m=spec.price_in or zero,
        price_output_per_1m=spec.price_out or zero,
        tensor_parallel=spec.tp,
        pipeline_parallel=spec.pp,
        replicas_min=spec.replicas_min,
        gpu_class=spec.gpu_class,
        vendor=spec.vendor,
        model_ref=spec.model_ref,
    )
    try:
        priced = await price_deployment(database, row, config, None, params_b)
    except ValueError as exc:
        return _err(str(exc), 422)

    now = datetime.utcnow()

    def _insert() -> tuple[str, Any]:
        existing = (
            database(
                (database.vllm_deployments.org_id == org_id)
                & (database.vllm_deployments.name == spec.name)
            )
            .select()
            .first()
        )
        if existing is not None:
            return "name_conflict", None
        new_id = database.vllm_deployments.insert(
            public_id=str(public_id),
            org_id=org_id,
            fleet_backend_id=backend_row.id,
            name=spec.name,
            slug=slug,
            model_ref=spec.model_ref,
            served_model_name=spec.served_model_name,
            vendor=spec.vendor,
            gpu_class=spec.gpu_class,
            allow_mixed_class=spec.allow_mixed_class,
            quantization=spec.quantization,
            dtype=spec.dtype,
            tensor_parallel=spec.tp,
            pipeline_parallel=spec.pp,
            replicas_min=spec.replicas_min,
            replicas_max=spec.replicas_max,
            max_model_len=spec.max_model_len,
            status=int(S.PENDING),
            serve_app_name=f"d-{slug}"[:128],
            expose_via_proxy=spec.expose_via_proxy,
            prefer_local=spec.prefer_local,
            comparison_model=spec.comparison_model,
            price_mode=int(spec.price_mode),
            price_breakdown=priced.price_breakdown,
            price_input_per_1m=priced.price_input_per_1m,
            price_output_per_1m=priced.price_output_per_1m,
            created_at=now,
            updated_at=now,
        )
        database.commit()
        return "ok", database(database.vllm_deployments.id == new_id).select().first()

    try:
        status, dep = await asyncio.to_thread(_insert)
    except Exception:
        # price_deployment already upserted the rate row: never leave it orphaned
        await asyncio.to_thread(_discard_rate, database, public_id)
        raise
    if status == "name_conflict":
        await asyncio.to_thread(_discard_rate, database, public_id)
        return _err(f"a deployment named '{spec.name}' already exists for this org", 409)

    try:
        token, link_id = await fleet_secrets.provision_key_and_link(
            database,
            org_id=org_id,
            slug=slug,
            link_name=f"vllm-{org_id}-{slug}",
            models=[spec.served_model_name],
        )
    except fleet_secrets.SecretWriteFailed:
        await asyncio.to_thread(_discard, database, dep)
        return _err("secret write failed", 502, reason="secret write failed")
    del token

    def _attach() -> None:
        database(database.vllm_deployments.id == dep.id).update(connection_link_id=link_id)
        database.commit()

    await asyncio.to_thread(_attach)

    try:
        await model_store.ensure_weights(
            database, org_id, spec.model_ref, dep.id, int(LocalDeploymentKind.VLLM), slug=slug
        )
    except Exception as exc:
        logger.error("model pull could not start for deployment %s: %s", dep.id, type(exc).__name__)
        await _fail(database, dep, "could not start the model pull")
        return _err("could not start the model pull", 502)

    def _reload() -> Any:
        return database(database.vllm_deployments.id == dep.id).select().first()

    dep = await asyncio.to_thread(_reload)
    if int(dep.status) == int(S.PENDING):
        backend: Any = build_backend(database, backend_row)
        try:
            await backend.deploy(dep)
        except RuntimeError as exc:
            # Plan B raises plain-language RuntimeErrors (already recorded on the row)
            logger.warning("vllm deploy failed for %s", dep.id)
            await _fail(database, dep, str(exc) or "deploy failed")
            return _err(str(exc) or "deploy failed", 502)
        dep = await asyncio.to_thread(_reload)
    item = await asyncio.to_thread(_to_item, database, dep)
    return {
        "status": "success",
        "data": item,
        "meta": {"action": "created", "timestamp": _now()},
    }, 201


def _name_taken(database: Any, org_id: int, name: str) -> bool:
    """True when this org already has a deployment called ``name`` (checked before pricing)."""
    table = database.vllm_deployments
    return database((table.org_id == org_id) & (table.name == name)).select().first() is not None


def _discard_rate(database: Any, public_id: uuid.UUID) -> None:
    """Remove the per-deployment rate row of a deployment that was never inserted."""
    database(
        (database.token_conversion_rates.provider == "vllm")
        & (database.token_conversion_rates.model == f"deployment/{public_id}")
    ).delete()
    database.commit()


def _discard(database: Any, dep: Any) -> None:
    """Remove a just-inserted deployment (and its rate row) after a failed create step."""
    database(database.vllm_deployments.id == dep.id).delete()
    database(
        (database.token_conversion_rates.provider == "vllm")
        & (database.token_conversion_rates.model == f"deployment/{dep.public_id}")
    ).delete()
    database.commit()


async def _fail(database: Any, dep: Any, message: str) -> None:
    """Mark a deployment FAILED with a reason."""

    def _mark() -> None:
        database(database.vllm_deployments.id == dep.id).update(
            status=int(S.FAILED), status_detail=message, updated_at=datetime.utcnow()
        )
        database.commit()

    await asyncio.to_thread(_mark)


@api_v1_bp.route("/fleet/vllm/deployments/<uuid:public_id>", methods=["PATCH"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(DeploymentActionResponse, 200)
async def update_vllm_deployment(public_id: uuid.UUID) -> Any:
    """Scale, stop/resume, or re-price a deployment (a price change affects new requests only)."""
    org_id = g.user.get("organization_id")
    if (blocked := _gate(org_id)) is not None:
        return blocked
    database = _db()
    dep, outcome = await asyncio.to_thread(_get_deployment, database, public_id, org_id)
    if dep is None:
        return _deployment_lookup_error(outcome)
    body = await request.get_json(silent=True)
    if not isinstance(body, dict):
        return _validation_error("request body must be a JSON object")
    try:
        rmin = _int(body, "replicas_min", minimum=0)
        rmax = _int(body, "replicas_max")
        pricing = (
            parse_price_fields(body, default_mode=PriceMode(int(dep.price_mode)))
            if any(k in body for k in ("price_mode", "price_input_per_1m", "price_output_per_1m"))
            else None
        )
    except (ValueError, PriceFieldError) as exc:
        return _validation_error(str(exc))
    action = body.get("action")
    if action not in (None, "stop", "resume"):
        return _validation_error("action must be 'stop' or 'resume'")
    new_min = int(dep.replicas_min) if rmin is None else rmin
    new_max = int(dep.replicas_max) if rmax is None else rmax
    if (rmin is not None or rmax is not None) and new_max < new_min:
        return _validation_error("replicas_max must be >= replicas_min")
    status = int(dep.status)
    if action == "stop" and status == int(S.STOPPED):
        return _err("deployment is already stopped", 409)
    if action == "resume" and status not in (int(S.STOPPED), int(S.FAILED)):
        return _err("only a stopped or failed deployment can be resumed", 409)

    backend_row = await asyncio.to_thread(
        lambda: database(database.fleet_backends.id == dep.fleet_backend_id).select().first()
    )
    if backend_row is None:
        return _err("the deployment's fleet backend no longer exists", 409)
    backend: Any = build_backend(database, backend_row)
    try:
        if rmin is not None or rmax is not None:
            await backend.scale(dep, new_min, new_max)
        if action == "stop":
            await backend.stop(dep)
        elif action == "resume":
            await backend.deploy(dep)
    except (RuntimeError, ValueError) as exc:
        return _err(str(exc), 502)
    if pricing is not None:
        err = await _apply_price_change(database, dep, backend_row, pricing)
        if err is not None:
            return err
    dep = await asyncio.to_thread(
        lambda: database(database.vllm_deployments.id == dep.id).select().first()
    )
    await sync_deployment_link(database, dep)
    item = await asyncio.to_thread(_to_item, database, dep)
    return {
        "status": "success",
        "data": item,
        "meta": {"action": "updated", "timestamp": _now()},
    }, 200


async def _apply_price_change(
    database: Any, dep: Any, backend_row: Any, pricing: Any
) -> tuple[Any, int] | None:
    """Re-price a deployment; the row is only written after the price validated.

    The row is reloaded first: ``backend.scale`` may just have changed ``replicas_min`` and the
    AUTO price must be computed for the committed layout, not the pre-scale one.
    """
    dep = await asyncio.to_thread(
        lambda: database(database.vllm_deployments.id == dep.id).select().first()
    )
    row = SimpleNamespace(
        public_id=dep.public_id,
        price_mode=int(pricing.mode),
        price_input_per_1m=pricing.price_input_per_1m or dep.price_input_per_1m,
        price_output_per_1m=pricing.price_output_per_1m or dep.price_output_per_1m,
        tensor_parallel=dep.tensor_parallel,
        pipeline_parallel=dep.pipeline_parallel,
        replicas_min=dep.replicas_min,
        gpu_class=dep.gpu_class,
        vendor=dep.vendor,
        model_ref=dep.model_ref,
        price_breakdown=dep.price_breakdown,
    )
    params_b = await asyncio.to_thread(resolve_params_b, database, row)
    observed = None if dep.observed_tokens_per_s is None else float(dep.observed_tokens_per_s)
    try:
        priced = await price_deployment(database, row, backend_row.config or {}, observed, params_b)
    except ValueError as exc:
        return _err(str(exc), 422)

    def _persist() -> None:
        database(database.vllm_deployments.id == dep.id).update(
            price_mode=int(pricing.mode),
            price_input_per_1m=priced.price_input_per_1m,
            price_output_per_1m=priced.price_output_per_1m,
            price_breakdown=priced.price_breakdown,
            updated_at=datetime.utcnow(),
        )
        database.commit()

    await asyncio.to_thread(_persist)
    return None


@api_v1_bp.route("/fleet/vllm/deployments/<uuid:public_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(DeleteDeploymentResponse, 200)
async def delete_vllm_deployment(public_id: uuid.UUID) -> Any:
    """Delete: backend.delete -> remove its token key -> unlink the proxy offer -> drop the row."""
    org_id = g.user.get("organization_id")
    if (blocked := _gate(org_id)) is not None:
        return blocked
    database = _db()
    dep, outcome = await asyncio.to_thread(_get_deployment, database, public_id, org_id)
    if dep is None:
        return _deployment_lookup_error(outcome)
    backend_row = await asyncio.to_thread(
        lambda: database(database.fleet_backends.id == dep.fleet_backend_id).select().first()
    )
    if backend_row is not None:
        backend: Any = build_backend(database, backend_row)
        try:
            await backend.delete(dep)
        except RuntimeError as exc:
            return _err(str(exc), 502)
    try:
        await fleet_secrets.remove_key(dep.slug)
        await fleet_secrets.remove_hf_token_secret(dep.id)  # a leftover pull-token Secret
    except fleet_secrets.SecretWriteFailed:
        return _err("secret write failed", 502, reason="secret write failed")
    await unlink_deployment(database, dep)

    def _drop() -> None:
        database(database.vllm_deployment_events.deployment_id == dep.id).delete()
        database(
            (database.token_conversion_rates.provider == "vllm")
            & (database.token_conversion_rates.model == f"deployment/{dep.public_id}")
        ).delete()
        database(database.vllm_deployments.id == dep.id).delete()
        database.commit()

    await asyncio.to_thread(_drop)
    return {
        "status": "success",
        "data": {"deleted": True},
        "meta": {"action": "deleted", "timestamp": _now()},
    }, 200


def _audit_rotation(database: Any, dep: Any, old: str | None, new: str) -> None:
    """Record who rotated a deployment token (fingerprints only, never a value)."""
    database.audit_log.insert(
        method="POST",
        path=f"fleet.deployment.token_rotated/{dep.slug}",
        resource_id=f"{old}->{new}",
        status_code=200,
        user_id=g.user.get("user_id"),
        organization_id=g.user.get("organization_id"),
        created_at=datetime.utcnow(),
    )
    database.commit()


@api_v1_bp.route("/fleet/vllm/deployments/<uuid:public_id>/rotate-token", methods=["POST"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(RotateTokenResponse, 200)
async def rotate_vllm_token(public_id: uuid.UUID) -> Any:
    """Mint a new bearer token; no Serve redeploy (the middleware re-reads the Secret file).

    The old token stays valid until the kubelet syncs the Secret (document: up to ~2 minutes).
    A failed Secret write leaves the old token in place (never half-rotated).
    """
    org_id = g.user.get("organization_id")
    if (blocked := _gate(org_id)) is not None:
        return blocked
    database = _db()
    dep, outcome = await asyncio.to_thread(_get_deployment, database, public_id, org_id)
    if dep is None:
        return _deployment_lookup_error(outcome)
    if not dep.connection_link_id:
        # Fail loudly BEFORE writing anything: a Secret rotated without its link would leave the
        # readiness canary and the proxy holding a token the engine no longer accepts.
        logger.error(
            "fleet.deployment.token_rotation_refused deployment=%s slug=%s: no connection link",
            dep.public_id,
            dep.slug,
        )
        return _err(
            "deployment has no connection link; token cannot be rotated safely",
            409,
            reason="no connection link",
        )
    old = await asyncio.to_thread(_fingerprint_for, database, dep)
    token = fleet_secrets.mint_token()
    try:
        previous = await fleet_secrets.read_deployment_key(dep.slug)
        await fleet_secrets.write_deployment_key(dep.slug, token)
    except fleet_secrets.SecretWriteFailed:
        return _err("secret write failed", 502, reason="secret write failed")

    def _store_link() -> None:
        update_connection_link_api_key(database, dep.connection_link_id, token)
        database.commit()

    try:
        await asyncio.to_thread(_store_link)
    except Exception:
        # never half-rotated: put the previous Secret value back (the token is never logged)
        logger.error(
            "fleet.deployment.token_rotation_failed deployment=%s slug=%s: link update failed",
            dep.public_id,
            dep.slug,
        )
        try:
            await fleet_secrets.restore_deployment_key(dep.slug, previous)
        except fleet_secrets.SecretWriteFailed:
            logger.error(
                "fleet.deployment.token_rotation_restore_failed deployment=%s slug=%s: "
                "Secret and link may disagree; rotate again",
                dep.public_id,
                dep.slug,
            )
            return _err(
                "token rotation failed",
                502,
                reason="link update failed; restore failed, rotate again",
            )
        return _err(
            "token rotation failed", 502, reason="link update failed; previous token restored"
        )
    fingerprint = fleet_secrets.fingerprint(token)
    await asyncio.to_thread(_audit_rotation, database, dep, old, fingerprint)
    logger.info(
        "fleet.deployment.token_rotated deployment=%s actor=%s fingerprint_old=%s "
        "fingerprint_new=%s",
        dep.public_id,
        g.user.get("user_id"),
        old,
        fingerprint,
    )
    return {
        "status": "success",
        "data": {"public_id": str(dep.public_id), "api_key_fingerprint": fingerprint},
        "meta": {"action": "token_rotated", "timestamp": _now()},
    }, 200


@api_v1_bp.route("/fleet/models/<path:model_ref>/weights", methods=["DELETE"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(WeightsDeletedResponse, 200)
async def delete_model_weights(model_ref: str) -> Any:
    """Delete a model's stored weights; 409 while any deployment still references them."""
    org_id = g.user.get("organization_id")
    if (blocked := _gate(org_id)) is not None:
        return blocked
    try:
        model_store.safe_model_ref(model_ref)
    except ValueError:
        return _validation_error("invalid model reference")
    try:
        await model_store.delete_weights(_db(), org_id, model_ref)
    except model_store.WeightsInUse as exc:
        return _err(
            "weights in use",
            409,
            reason="weights in use",
            deployments=[{"kind": kind, "public_id": pid} for kind, pid in exc.deployments],
        )
    return {
        "status": "success",
        "data": {"deleted": True, "model_ref": model_ref},
        "meta": {"action": "deleted", "timestamp": _now()},
    }, 200
