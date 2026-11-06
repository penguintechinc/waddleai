"""WaddleAI Management API v1 - llama.cpp Deployment Management Endpoints."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests
from quart import g, jsonify, request
from quart_schema import validate_request, validate_response

from shared.auth.rbac import Permission
from shared.fleet.enums import LlamaCppTopology, LocalDeploymentKind, PriceMode
from shared.fleet.llamacpp_rpc import RpcRefused
from shared.fleet.vllm_ray.slug import derive_slug

from ...extensions import db
from ...services import fleet_secrets
from ...services import llamacpp_rpc_service as rpc_service
from ...services.llamacpp_manager import LlamaCppManager
from . import api_v1_bp
from ._pagination import PageRequest
from ._pricing_body import (
    PriceFieldError,
    PricingRefusedError,
    parse_price_fields,
    price_local_deployment,
    pricing_requested,
)
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)

_MODEL_URL_ERROR = (
    "Invalid model_url: must be an https URL with no control characters or shell metacharacters"
)
_MODEL_FILENAME_ERROR = "Invalid model_filename: bare filename only (alphanumeric . - _)"
_TOPOLOGY_ERROR = "topology must be 1 (single node) or 2 (RPC multi-node)"
_RPC_FLAGS_REFUSAL = "llama.cpp RPC requires the GPU Fleet flags"
_RPC_K8S_ONLY = "RPC layouts run on Kubernetes nodes only."
_RPC_GGUF_BYTES = "RPC layout needs the GGUF file size (gguf_bytes); it is not guessed."
_RPC_ONE_NODE = "Fits on one node. Use the single-node topology."
_RPC_MODEL_SOURCE = (
    "RPC deployments need model_url and model_filename: the head downloads the GGUF."
)
_RPC_LAYOUT_KEYS = frozenset({"topology", "rpc_workers", "allow_mixed_class", "gguf_bytes"})
_RPC_LAYOUT_RUNNING = "Stop the deployment before changing its layout."
_RPC_LIVE_STATUSES = frozenset({"deploying", "running", "degraded", "error"})
_RPC_START_FAILED = "llama.cpp RPC deployment failed to start; nothing was kept."
_RPC_DERIVED_KEYS = frozenset({"gpu_count", "node_selector", "k8s_namespace", "n_gpu_layers"})
_RPC_DERIVED_ERROR = (
    "gpu_count, node_selector, k8s_namespace and n_gpu_layers are derived from the RPC layout; "
    "change topology, allow_mixed_class or rpc_workers instead."
)


# ---------------------------------------------------------------------------
# OpenAPI request/response models (audit-2026-09-14-wave2).
#
# Request models make every field Optional with the handler's own defaults so
# quart-schema's automatic validation never pre-empts the handler's own
# presence checks and their exact error messages. Response models list exactly
# the fields each handler returns -- a field omitted here is silently dropped
# from the response by quart-schema, which is precisely the client-breaking
# regression these models exist to prevent.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PageMeta:
    """Pagination metadata block emitted by ``PageRequest.meta``."""

    page: int
    limit: int
    total: int | None
    pages: int | None


@dataclass(slots=True)
class LlamaCppDeployment:
    """A llama.cpp deployment row, exactly as ``_deployment_to_dict`` serialises it."""

    id: int
    name: str
    deployment_type: str | None
    status: str | None
    status_message: str | None
    model_name: str | None
    model_url: str | None
    model_filename: str | None
    n_ctx: int | None
    n_gpu_layers: int | None
    gpu_count: int | None
    endpoint_url: str | None
    k8s_namespace: str | None
    k8s_daemonset_name: str | None
    node_selector: dict[str, Any] | None
    node_affinity: dict[str, Any] | None
    created_at: str | None
    modified_at: str | None
    public_id: str | None = None
    price_mode: int | None = None
    price_input_per_1m: str | None = None
    price_output_per_1m: str | None = None
    price_breakdown: dict[str, Any] | None = None
    comparison_model: str | None = None
    topology: int | None = None
    allow_mixed_class: bool | None = None
    rpc_workers: list[dict[str, Any]] | None = None
    tensor_split: str | None = None


@dataclass(slots=True)
class LlamaCppDeploymentListResponse:
    """Response body for GET /api/v1/llamacpp/deployments."""

    deployments: list[LlamaCppDeployment]
    pagination: PageMeta


@dataclass(slots=True)
class CreateLlamaCppDeploymentRequest:
    """Request body for POST /api/v1/llamacpp/deployments. Every field optional."""

    name: str | None = None
    model_name: str | None = None
    model_url: str | None = None
    model_filename: str | None = None
    deployment_type: str | None = "kubernetes"
    n_ctx: int | None = 4096
    n_gpu_layers: int | None = -1
    gpu_count: int | None = 1
    endpoint_url: str | None = None
    k8s_namespace: str | None = "waddleai"
    node_selector: dict[str, Any] | None = None
    node_affinity: dict[str, Any] | None = None
    price_mode: int | None = None
    price_input_per_1m: str | None = None
    price_output_per_1m: str | None = None
    comparison_model: str | None = None
    topology: int | None = 1
    allow_mixed_class: bool | None = False
    rpc_workers: list[dict[str, Any]] | None = None
    gguf_bytes: int | None = None


@dataclass(slots=True)
class CreateLlamaCppDeploymentResponse:
    """Response body for a successful POST /api/v1/llamacpp/deployments."""

    deployment_id: int
    message: str
    public_id: str | None = None
    price_mode: int | None = None
    price_input_per_1m: str | None = None
    price_output_per_1m: str | None = None
    price_breakdown: dict[str, Any] | None = None
    comparison_model: str | None = None
    api_key_fingerprint: str | None = None
    explanation: str | None = None


# NOTE: the PATCH body is parsed and validated in-handler (not via a
# @validate_request model) so the deployment existence/running checks (404/409)
# fire ahead of any body 400 -- see update_llamacpp_deployment and regression
# 44cc384.


@dataclass(slots=True)
class MessageResponse:
    """Generic ``{"message": str}`` envelope used by several llama.cpp endpoints."""

    message: str


@dataclass(slots=True)
class DeployResponse:
    """Response body for POST /api/v1/llamacpp/deployments/<id>/deploy."""

    message: str
    deployment_id: int


@dataclass(slots=True)
class LlamaCppHealthResponse:
    """Health-check response -- the union of every 200 branch's fields.

    Only ``status`` is always present; the remaining fields are populated per
    branch (``reason`` when no endpoint is set, ``endpoint`` when healthy,
    ``http_status``/``error`` when unhealthy) and default to ``None`` otherwise.
    """

    status: str
    reason: str | None = None
    endpoint: str | None = None
    http_status: int | None = None
    error: str | None = None
    status_message: str | None = None
    workers_up: int | None = None
    workers_total: int | None = None


def _validate_model_url(url: str) -> bool:
    """Validate model_url: https only, no control characters, no shell metacharacters.

    regression: security review 2026-07-26 — Vuln D: command injection prevention
    regression: gh-146 follow-up 2026-08-21 — plaintext http (supply-chain MITM on the
    downloaded model weights) and embedded control characters (header/argument
    injection wherever the URL is interpolated) were both left open.
    """
    if not url:
        return False
    # Reject raw control characters (newline/CR/tab/null) and their percent-encoded
    # forms — both are header/argument injection vectors wherever this URL is
    # interpolated (curl invocation, HTTP client, logs).
    if any(c in url for c in "\r\n\t\x00"):
        return False
    lowered = url.lower()
    if "%0a" in lowered or "%0d" in lowered:
        return False
    # https only — a model downloaded over plaintext http is a supply-chain hole:
    # a network MITM can swap the weights in transit.
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        return False
    if not parsed.netloc:
        return False
    # Reject shell metacharacters
    shell_chars = set(";|&$`()\\\"'<>")
    if any(c in url for c in shell_chars):
        return False
    return True


def _validate_model_filename(filename: str) -> bool:
    """Validate model_filename: bare basename only, alphanumeric/dot/dash/underscore.

    regression: security review 2026-07-26 — Vuln D: path traversal prevention
    regression: gh-146 follow-up 2026-08-21 — a bare ``".."`` (no path separator)
    still resolves to the parent directory and passed the old allowlist regex
    unchanged, since ``.`` is itself an allowed character.
    """
    if not filename:
        return False
    if len(filename) > 255:
        return False
    # Must not contain path separators
    if "/" in filename or "\\" in filename:
        return False
    # A filename made up entirely of dots (".", "..", "...", ...) resolves to the
    # current/parent directory rather than naming a real file — reject regardless
    # of the allowlist regex below, which would otherwise accept it.
    if set(filename) == {"."}:
        return False
    # Allow only alphanumeric, dot, dash, underscore
    if not re.match(r"^[A-Za-z0-9._-]+$", filename):
        return False
    return True


_PRICE_KEYS = frozenset(
    {"price_mode", "price_input_per_1m", "price_output_per_1m", "comparison_model"}
)


def _price_fields(dep: Any) -> dict[str, Any]:
    """The identity and pricing columns of a deployment row as response fields."""

    def _text(value: Any) -> str | None:
        return None if value is None else str(value)

    if dep is None:
        return dict.fromkeys(
            (
                "public_id", "price_mode", "price_input_per_1m", "price_output_per_1m",
                "price_breakdown", "comparison_model",
            )
        )  # fmt: skip
    breakdown = dep.price_breakdown
    return {
        "public_id": _text(dep.public_id),
        "price_mode": None if dep.price_mode is None else int(dep.price_mode),
        "price_input_per_1m": _text(dep.price_input_per_1m),
        "price_output_per_1m": _text(dep.price_output_per_1m),
        "price_breakdown": breakdown if isinstance(breakdown, dict) else None,
        "comparison_model": _text(dep.comparison_model),
    }


def _rpc_fields(dep: Any) -> dict[str, Any]:
    """The multi-node topology columns of a deployment row as response fields."""
    workers = dep.rpc_workers
    split = dep.tensor_split
    return {
        "topology": None if dep.topology is None else int(dep.topology),
        "allow_mixed_class": None if dep.allow_mixed_class is None else bool(dep.allow_mixed_class),
        "rpc_workers": workers if isinstance(workers, list) else None,
        "tensor_split": split if isinstance(split, str) else None,
    }


def _deployment_to_dict(dep: Any) -> dict[str, Any]:
    """Convert a LlamaCppDeployment model to a dict for JSON response."""
    return {
        "id": dep.id,
        "name": dep.name,
        "deployment_type": dep.deployment_type,
        "status": dep.status,
        "status_message": dep.status_message,
        "model_name": dep.model_name,
        "model_url": dep.model_url,
        "model_filename": dep.model_filename,
        "n_ctx": dep.n_ctx,
        "n_gpu_layers": dep.n_gpu_layers,
        "gpu_count": dep.gpu_count,
        "endpoint_url": dep.endpoint_url,
        "k8s_namespace": dep.k8s_namespace,
        "k8s_daemonset_name": dep.k8s_daemonset_name,
        "node_selector": dep.node_selector,
        "node_affinity": dep.node_affinity,
        "created_at": dep.created_at.isoformat() if dep.created_at else None,
        "modified_at": dep.modified_at.isoformat() if dep.modified_at else None,
        **_price_fields(dep),
        **_rpc_fields(dep),
    }


@api_v1_bp.route("/llamacpp/deployments", methods=["GET"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
@validate_response(LlamaCppDeploymentListResponse, 200)
async def list_llamacpp_deployments():
    """List the caller's org's llama.cpp deployments (bounded page).

    Optional filters: ``topology`` (1 single-node, 2 RPC multi-node) and ``status``. Another
    org's deployments are never listed; unowned legacy rows are visible to the system org only
    (the same rule as the id routes' ``_visible_to_org``).
    """
    page = PageRequest.from_request()
    org_id = g.user.get("organization_id")
    if org_id is None:  # fail closed: an org-less caller must not match unowned rows
        return jsonify({"error": "organization required"}), 403
    topology_arg = request.args.get("topology")
    topology_filter: int | None = None
    if topology_arg is not None:
        if topology_arg not in ("1", "2"):
            return jsonify({"error": _TOPOLOGY_ERROR}), 400
        topology_filter = int(topology_arg)
    status_filter = request.args.get("status")

    def _fetch():
        table = db.llamacpp_deployments
        query = table.owner_org_id == org_id
        if _is_system_org(org_id):
            query = query | (table.owner_org_id == None)  # noqa: E711 -- DAL IS NULL idiom
        query = query & (table.id > 0)
        if topology_filter is not None:
            query = query & (table.topology == topology_filter)
        if status_filter:
            query = query & (table.status == status_filter)
        total = db(query).count()
        rows = db(query).select(limitby=page.limitby, orderby=table.id)
        return total, list(rows)

    total, deployments = await asyncio.to_thread(_fetch)
    return {
        "deployments": [_deployment_to_dict(d) for d in deployments],
        **page.meta(total),
    }, 200


@api_v1_bp.route("/llamacpp/deployments", methods=["POST"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
@validate_response(CreateLlamaCppDeploymentResponse, 201)
@validate_request(CreateLlamaCppDeploymentRequest)
async def create_llamacpp_deployment(data: CreateLlamaCppDeploymentRequest):
    """Create a new llama.cpp deployment."""
    name = (data.name or "").strip()
    model_name = (data.model_name or "").strip()

    if not name:
        return jsonify({"error": "name is required"}), 400
    if not model_name:
        return jsonify({"error": "model_name is required"}), 400

    # Vuln D fix: validate model_url and model_filename at API layer
    model_url = (data.model_url or "").strip()
    model_filename = (data.model_filename or "").strip()

    if model_url and not _validate_model_url(model_url):
        return jsonify({"error": _MODEL_URL_ERROR}), 400

    if model_filename and not _validate_model_filename(model_filename):
        return jsonify({"error": _MODEL_FILENAME_ERROR}), 400

    topology = 1 if data.topology is None else data.topology
    if topology not in (int(LlamaCppTopology.SINGLE_NODE), int(LlamaCppTopology.RPC_MULTI_NODE)):
        return jsonify({"error": _TOPOLOGY_ERROR}), 400
    if topology == int(LlamaCppTopology.RPC_MULTI_NODE):
        return await _create_rpc_deployment(
            data,
            name=name,
            model_name=model_name,
            model_url=model_url,
            model_filename=model_filename,
        )

    deployment_type = data.deployment_type or "kubernetes"

    price_body = {
        "price_mode": data.price_mode,
        "price_input_per_1m": data.price_input_per_1m,
        "price_output_per_1m": data.price_output_per_1m,
        "comparison_model": data.comparison_model,
    }
    price_fields = None
    if pricing_requested(price_body):
        try:
            price_fields = parse_price_fields(
                {k: v for k, v in price_body.items() if v is not None},
                default_mode=PriceMode.AUTO_TCO,
            )
        except PriceFieldError as exc:
            return jsonify({"error": str(exc)}), 400

    public_id = str(uuid.uuid4())
    owner_org_id = g.user.get("organization_id")

    def _create():
        mgr = LlamaCppManager(db)
        new_id = db.llamacpp_deployments.insert(
            name=name,
            deployment_type=deployment_type,
            status="pending",
            model_name=model_name,
            model_url=data.model_url,
            model_filename=data.model_filename,
            n_ctx=data.n_ctx if data.n_ctx is not None else 4096,
            n_gpu_layers=data.n_gpu_layers if data.n_gpu_layers is not None else -1,
            gpu_count=data.gpu_count if data.gpu_count is not None else 1,
            endpoint_url=data.endpoint_url,
            k8s_namespace=data.k8s_namespace or "waddleai",
            k8s_daemonset_name=mgr._daemonset_name(name),
            node_selector=data.node_selector,
            node_affinity=data.node_affinity,
            public_id=public_id,
            owner_org_id=owner_org_id,
        )
        db.commit()
        return new_id

    dep_id = await asyncio.to_thread(_create)
    row = await asyncio.to_thread(_llamacpp_row, dep_id)

    if price_fields is not None:
        try:
            price_extra = await price_local_deployment(
                db,
                int(LocalDeploymentKind.LLAMACPP),
                g.user.get("organization_id"),
                row,
                price_fields,
                data.comparison_model,
            )
        except PricingRefusedError as exc:
            await asyncio.to_thread(_discard_llamacpp, dep_id)
            return jsonify({"error": str(exc)}), 422

        await asyncio.to_thread(_llamacpp_update, dep_id, price_extra)

    fingerprint: str | None = None
    if _fleet_keys_enabled(g.user.get("organization_id")):
        try:
            token, link_id = await fleet_secrets.provision_key_and_link(
                db,
                org_id=g.user.get("organization_id"),
                slug=derive_slug(name, public_id),
                link_name=f"llamacpp-{public_id}",
                models=[model_name],
                provider="llamacpp",
            )
        except fleet_secrets.SecretWriteFailed:
            await asyncio.to_thread(_discard_llamacpp, dep_id)
            return jsonify({"error": "secret write failed", "reason": "secret write failed"}), 502
        fingerprint = fleet_secrets.fingerprint(token)
        del link_id

    row = await asyncio.to_thread(_llamacpp_row, dep_id)
    return {
        "deployment_id": dep_id,
        "message": "Deployment created",
        **{k: v for k, v in _price_fields(row).items()},
        "api_key_fingerprint": fingerprint,
    }, 201


def _is_rpc(dep: Any) -> bool:
    """True when the row uses the multi-node RPC topology."""
    return getattr(dep, "topology", None) == int(LlamaCppTopology.RPC_MULTI_NODE)


def _parse_explicit_workers(raw: Any) -> tuple[list[tuple[str, str]] | None, str | None]:
    """Validate a client ``rpc_workers`` list into ``(node_uid, gpu_class)`` pairs, else an error.

    Each entry names ONE GPU (``gpu_count`` must be 1: an rpc-server pod owns a single device);
    together they restrict which GPUs the planner may use.
    """
    if raw is None:
        return None, None
    if not isinstance(raw, list) or not raw:
        return None, "rpc_workers must be a non-empty list of {node_uid, gpu_class, gpu_count: 1}"
    pairs: list[tuple[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None, "rpc_workers entries must be objects"
        node_uid, gpu_class = entry.get("node_uid"), entry.get("gpu_class")
        count = entry.get("gpu_count", 1)
        if not (
            isinstance(node_uid, str) and node_uid and isinstance(gpu_class, str) and gpu_class
        ):
            return None, "each rpc_workers entry needs node_uid and gpu_class"
        if isinstance(count, bool) or count != 1:
            return None, "each rpc_workers entry is one GPU: gpu_count must be 1"
        pairs.append((node_uid, gpu_class))
    return pairs, None


def _valid_gguf_bytes(value: Any) -> bool:
    """True for a positive integer GGUF size (booleans are not sizes)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


async def _plan_rpc(
    org_id: Any, gguf_bytes: int, allow_mixed: bool, allowed: list[tuple[str, str]] | None
) -> tuple[Any, int, dict[str, str]]:
    """Plan a layout for the org (thread-offloaded); ``RpcRefused`` carries admin-facing copy."""
    mgr = LlamaCppManager(db)
    return await asyncio.to_thread(
        rpc_service.plan_for_org,
        db,
        org_id,
        gguf_bytes=gguf_bytes,
        allow_mixed_class=allow_mixed,
        allowed=allowed,
        mutual_auth_enabled=mgr.mutual_auth_enabled,
    )


async def _plan_layout_patch(
    data: dict[str, Any], current: Any, updates: dict[str, Any]
) -> tuple[dict[str, Any], Any]:
    """Columns for a PATCH that changes the topology/layout, or ``(.., error response)``.

    Re-plans from the live inventory when the result is multi-node (the GGUF size must be sent
    again; it is never guessed or stored), and resets the RPC columns when going single-node.
    """
    org_id = g.user.get("organization_id")
    topology = data.get("topology", current.topology or 1)
    if isinstance(topology, bool) or topology not in (
        int(LlamaCppTopology.SINGLE_NODE),
        int(LlamaCppTopology.RPC_MULTI_NODE),
    ):
        return {}, (jsonify({"error": _TOPOLOGY_ERROR}), 400)
    allow_mixed = data.get("allow_mixed_class", bool(current.allow_mixed_class))
    if not isinstance(allow_mixed, bool):
        return {}, (jsonify({"error": "allow_mixed_class must be true or false"}), 400)
    if topology == int(LlamaCppTopology.SINGLE_NODE):
        return {**rpc_service.single_node_columns(), "allow_mixed_class": False}, None
    if not _fleet_keys_enabled(org_id):
        return {}, (jsonify({"error": _RPC_FLAGS_REFUSAL}), 403)
    if current.deployment_type != "kubernetes":
        return {}, (jsonify({"error": _RPC_K8S_ONLY}), 422)
    if not (updates.get("model_url") or current.model_url) or not (
        updates.get("model_filename") or current.model_filename
    ):
        return {}, (jsonify({"error": _RPC_MODEL_SOURCE}), 422)
    if not _valid_gguf_bytes(data.get("gguf_bytes")):
        return {}, (jsonify({"error": _RPC_GGUF_BYTES}), 422)
    allowed, allowed_error = _parse_explicit_workers(data.get("rpc_workers"))
    if allowed_error:
        return {}, (jsonify({"error": allowed_error}), 422)
    base = rpc_service.rpc_base_name(current.name, current.public_id)
    if await asyncio.to_thread(rpc_service.base_name_taken, db, base, int(current.id)):
        return {}, (jsonify({"error": "That deployment name is already in use."}), 409)
    try:
        layout, _backend_id, _hostnames = await _plan_rpc(
            org_id, int(data["gguf_bytes"]), allow_mixed, allowed
        )
    except RpcRefused as exc:
        return {}, (jsonify({"error": str(exc)}), 422)
    if layout.nodes_used == 1:
        return {}, (jsonify({"error": _RPC_ONE_NODE}), 422)
    return {
        **rpc_service.layout_columns(layout),
        "allow_mixed_class": allow_mixed,
        "k8s_namespace": fleet_secrets.namespace(),
        "k8s_daemonset_name": base,
    }, None


async def _create_rpc_deployment(
    data: CreateLlamaCppDeploymentRequest,
    *,
    name: str,
    model_name: str,
    model_url: str,
    model_filename: str,
):
    """Create, price, key and deploy a multi-node llama.cpp deployment (topology 2).

    Everything that can be refused is refused before the row is inserted; the row is only kept
    when the Kubernetes objects were created. The org comes from the JWT, never the body, and the
    inventory read is filtered to that org.
    """
    org_id = g.user.get("organization_id")
    if not _fleet_keys_enabled(org_id):
        return jsonify({"error": _RPC_FLAGS_REFUSAL}), 403
    if (data.deployment_type or "kubernetes") != "kubernetes":
        return jsonify({"error": _RPC_K8S_ONLY}), 422
    if not model_url or not model_filename:
        return jsonify({"error": _RPC_MODEL_SOURCE}), 422
    if not _valid_gguf_bytes(data.gguf_bytes):
        return jsonify({"error": _RPC_GGUF_BYTES}), 422
    namespace = fleet_secrets.namespace()
    if data.k8s_namespace not in (None, "waddleai", namespace):
        return jsonify({"error": f"RPC deployments run in the fleet namespace '{namespace}'."}), 422
    allowed, allowed_error = _parse_explicit_workers(data.rpc_workers)
    if allowed_error:
        return jsonify({"error": allowed_error}), 422
    mgr = LlamaCppManager(db)
    public_id = str(uuid.uuid4())
    base = rpc_service.rpc_base_name(name, public_id)
    if await asyncio.to_thread(rpc_service.base_name_taken, db, base):
        return jsonify({"error": "That deployment name is already in use; retry."}), 409
    try:
        price_fields = parse_price_fields(
            {
                k: v
                for k, v in {
                    "price_mode": data.price_mode,
                    "price_input_per_1m": data.price_input_per_1m,
                    "price_output_per_1m": data.price_output_per_1m,
                }.items()
                if v is not None
            },
            default_mode=PriceMode.AUTO_TCO,
        )
    except PriceFieldError as exc:
        return jsonify({"error": str(exc)}), 400

    gguf_bytes = int(data.gguf_bytes or 0)
    try:
        layout, _backend_id, hostnames = await _plan_rpc(
            org_id, gguf_bytes, bool(data.allow_mixed_class), allowed
        )
    except RpcRefused as exc:
        return jsonify({"error": str(exc)}), 422
    if layout.nodes_used == 1:
        return jsonify({"error": _RPC_ONE_NODE}), 422
    needed = {layout.head_node_uid, *(w.node_uid for w in layout.workers)}
    if needed - set(hostnames):
        return jsonify({"error": "A planned node left the GPU inventory; retry the request."}), 409
    try:
        image = rpc_service.rpc_image_for_vendor(layout.vendor)
    except rpc_service.RpcImageNotConfigured as exc:
        logger.error("llamacpp rpc image not configured: %s", exc)
        return jsonify({"error": str(exc)}), 500

    def _create() -> int:
        return _database().llamacpp_deployments.insert(
            name=name,
            deployment_type="kubernetes",
            status="pending",
            model_name=model_name,
            model_url=data.model_url,
            model_filename=data.model_filename,
            n_ctx=data.n_ctx if data.n_ctx is not None else 4096,
            endpoint_url=None,
            k8s_namespace=namespace,
            k8s_daemonset_name=base,
            node_affinity=data.node_affinity,
            allow_mixed_class=bool(data.allow_mixed_class),
            public_id=public_id,
            owner_org_id=org_id,
            **rpc_service.layout_columns(layout),
        )

    dep_id = await asyncio.to_thread(lambda: _commit_after(_create))
    row = await asyncio.to_thread(_llamacpp_row, dep_id)
    slug = derive_slug(name, public_id)

    try:
        price_extra = await price_local_deployment(
            db,
            int(LocalDeploymentKind.LLAMACPP),
            org_id,
            row,
            price_fields,
            data.comparison_model,
        )
    except PricingRefusedError as exc:
        await asyncio.to_thread(_discard_llamacpp, dep_id)
        return jsonify({"error": str(exc)}), 422
    await asyncio.to_thread(_llamacpp_update, dep_id, price_extra)

    try:
        token, _link_id = await fleet_secrets.provision_key_and_link(
            db,
            org_id=org_id,
            slug=slug,
            link_name=f"llamacpp-{public_id}",
            models=[model_name],
            provider="llamacpp",
        )
    except fleet_secrets.SecretWriteFailed:
        await asyncio.to_thread(_discard_llamacpp, dep_id)
        return jsonify({"error": "secret write failed", "reason": "secret write failed"}), 502

    row = await asyncio.to_thread(_llamacpp_row, dep_id)
    try:
        await asyncio.to_thread(mgr.deploy_rpc, row, layout, hostnames, image)
        await asyncio.to_thread(_commit_after, lambda: None)
    except Exception:
        logger.error("llamacpp rpc deploy failed for deployment %s", public_id, exc_info=True)
        await _rollback_rpc_create(dep_id, public_id, slug)
        return jsonify({"error": _RPC_START_FAILED}), 503

    row = await asyncio.to_thread(_llamacpp_row, dep_id)
    logger.info(
        "llamacpp.rpc.created deployment=%s nodes=%s gpus=%s",
        public_id,
        layout.nodes_used,
        len(layout.workers) + layout.head_gpus,
    )
    return {
        "deployment_id": dep_id,
        "message": "Deployment created",
        **_price_fields(row),
        "api_key_fingerprint": fleet_secrets.fingerprint(token),
        "explanation": layout.explanation,
    }, 201


def _commit_after(fn: Any) -> Any:
    """Run ``fn`` then commit the DAL handle (a no-op under auto-commit); returns ``fn``'s value."""
    value = fn()
    _database().commit()
    return value


async def _rollback_rpc_create(dep_id: int, public_id: str, slug: str) -> None:
    """Undo a create whose Kubernetes deploy failed: key, link, rate row and deployment row."""
    try:
        await fleet_secrets.remove_key(slug)
    except fleet_secrets.SecretWriteFailed:
        logger.error("rollback could not remove the deployment key for %s", public_id)
    await asyncio.to_thread(_drop_link_and_rate, public_id)
    await asyncio.to_thread(_discard_llamacpp, dep_id)


def _drop_link_and_rate(public_id: Any) -> None:
    """Delete a deployment's key link and its per-deployment rate row (idempotent)."""
    database = _database()
    database(database.connection_links.name == f"llamacpp-{public_id}").delete()
    database(
        (database.token_conversion_rates.provider == "llamacpp")
        & (database.token_conversion_rates.model == f"deployment/{public_id}")
    ).delete()
    database.commit()


def _fleet_keys_enabled(org_id: Any) -> bool:
    """Per-deployment key minting is part of the vLLM-on-Ray fleet: both flags must be on."""
    from .fleet import _vllm_ray_enabled

    return _vllm_ray_enabled(org_id)


def _database() -> Any:
    """The process-wide penguin-dal handle (narrowed away from ``None``)."""
    if db is None:
        raise RuntimeError("database not initialized")
    return db


def _llamacpp_row(deployment_id: int) -> Any:
    """Fetch one ``llamacpp_deployments`` row (None when absent)."""
    database = _database()
    return database(database.llamacpp_deployments.id == deployment_id).select().first()


def _llamacpp_row_by_public_id(public_id: str) -> Any:
    """Fetch one ``llamacpp_deployments`` row by its public UUID (None when absent)."""
    database = _database()
    return database(database.llamacpp_deployments.public_id == public_id).select().first()


def _owner_org_id(dep: Any) -> int | None:
    """Owning org of a deployment: ``owner_org_id`` column, else backend org, else key link.

    The column (migration 031) is authoritative; the backend/link fallbacks cover rows written
    before it. Returns None for unowned legacy rows (visible to the system org only).
    """
    owner = getattr(dep, "owner_org_id", None)
    if owner is not None:
        return int(owner)
    database = _database()
    backend_id = getattr(dep, "fleet_backend_id", None)
    if backend_id is not None:
        backend = database(database.fleet_backends.id == backend_id).select().first()
        if backend is not None:
            return int(backend.org_id)
    link = database(database.connection_links.name == f"llamacpp-{dep.public_id}").select().first()
    if link is not None and link.owner_org_id is not None:
        return int(link.owner_org_id)
    return None


def _is_system_org(org_id: Any) -> bool:
    """True when ``org_id`` is the reserved system org (``WADDLEAI_SYSTEM_ORG_ID``).

    Unset or malformed configuration means nobody qualifies (fail closed).
    """
    configured = os.environ.get("WADDLEAI_SYSTEM_ORG_ID", "").strip()
    return configured.isdigit() and org_id == int(configured)


def _visible_to_org(dep: Any, org_id: Any) -> bool:
    """True when ``org_id`` may act on ``dep``; another org's deployment is invisible.

    Fails closed: an unowned legacy row is visible only to the system org.
    """
    owner = _owner_org_id(dep)
    if owner is None:
        return _is_system_org(org_id)
    return owner == org_id


def _llamacpp_update(deployment_id: int, values: dict[str, Any]) -> None:
    """Persist column values on one ``llamacpp_deployments`` row."""
    database = _database()
    database(database.llamacpp_deployments.id == deployment_id).update(**values)
    database.commit()


def _llamacpp_link(link_name: str) -> Any:
    """Fetch one ``connection_links`` row by name (None when absent)."""
    database = _database()
    return database(database.connection_links.name == link_name).select().first()


def _store_link_key(link_id: int, token: str) -> None:
    """Re-encrypt a rotated token into its connection link and commit."""
    from shared.database.models import update_connection_link_api_key

    database = _database()
    update_connection_link_api_key(database, link_id, token)
    database.commit()


def _discard_llamacpp(deployment_id: int) -> None:
    """Remove a just-created deployment whose pricing or key provisioning failed."""
    database = _database()
    row = database(database.llamacpp_deployments.id == deployment_id).select().first()
    if row is not None and row.public_id is not None:
        database(
            (database.token_conversion_rates.provider == "llamacpp")
            & (database.token_conversion_rates.model == f"deployment/{row.public_id}")
        ).delete()
    database(database.llamacpp_deployments.id == deployment_id).delete()
    database.commit()


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>", methods=["GET"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
@validate_response(LlamaCppDeployment, 200)
async def get_llamacpp_deployment(deployment_id):
    """Get a specific llama.cpp deployment."""
    dep = await asyncio.to_thread(
        lambda: db(db.llamacpp_deployments.id == deployment_id).select().first()
    )
    if not dep or not await asyncio.to_thread(_visible_to_org, dep, g.user.get("organization_id")):
        return jsonify({"error": "Deployment not found"}), 404
    return _deployment_to_dict(dep), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>", methods=["PATCH"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
@validate_response(MessageResponse, 200)
async def update_llamacpp_deployment(deployment_id):
    """Update a llama.cpp deployment (can only update stopped deployments).

    The existence/running checks run BEFORE the body is parsed, so a missing
    deployment returns 404 (and a running one 409) even when the body is
    non-JSON/absent. This is a deliberate ordering contract
    (tests/contract/test_management_mutations.py, regression 44cc384):
    @validate_request is intentionally NOT used here because it would parse the
    body ahead of the handler and 400 first. The body is parsed and its
    security-sensitive fields validated in-handler after those checks.
    """
    peek = await request.get_json(silent=True)
    price_only = (
        isinstance(peek, dict) and bool(peek) and all(k in _PRICE_KEYS for k in peek)
    )  # a price change never needs the deployment stopped: it affects new requests only

    def _check():
        dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
        if not dep or not _visible_to_org(dep, g.user.get("organization_id")):
            return "not_found"
        live = dep.status == "running" or (_is_rpc(dep) and dep.status in _RPC_LIVE_STATUSES)
        if live and not price_only:
            return "running"
        return "ok"

    check_result = await asyncio.to_thread(_check)

    if check_result == "not_found":
        return jsonify({"error": "Deployment not found"}), 404
    if check_result == "running":
        if isinstance(peek, dict) and _RPC_LAYOUT_KEYS & peek.keys():
            return jsonify({"error": _RPC_LAYOUT_RUNNING}), 409
        return jsonify({"error": "Stop the deployment before modifying it"}), 409

    data = (await request.get_json(silent=True)) or {}
    allowed = {
        "model_name",
        "model_url",
        "model_filename",
        "n_ctx",
        "n_gpu_layers",
        "gpu_count",
        "k8s_namespace",
        "node_selector",
        "node_affinity",
    }
    updates: dict[str, Any] = {k: v for k, v in data.items() if k in allowed}

    current = None
    layout_changed = bool(_RPC_LAYOUT_KEYS & data.keys())
    if layout_changed or pricing_requested(data) or _RPC_DERIVED_KEYS & data.keys():
        current = await asyncio.to_thread(
            lambda: db(db.llamacpp_deployments.id == deployment_id).select().first()
        )
    if current is not None and _is_rpc(current) and _RPC_DERIVED_KEYS & data.keys():
        return jsonify({"error": _RPC_DERIVED_ERROR}), 422
    if layout_changed and current is not None:
        layout_updates, refusal = await _plan_layout_patch(data, current, updates)
        if refusal is not None:
            return refusal
        updates.update(layout_updates)

    if current is not None and (
        pricing_requested(data) or (layout_changed and int(current.price_mode or 1) == 1)
    ):
        view: Any = {k: current[k] for k in current.keys()} | updates if layout_changed else current
        try:
            fields = parse_price_fields(
                {k: v for k, v in data.items() if k in _PRICE_KEYS and v is not None},
                default_mode=PriceMode(int(current.price_mode or 1)),
            )
            updates.update(
                await price_local_deployment(
                    db,
                    int(LocalDeploymentKind.LLAMACPP),
                    g.user.get("organization_id"),
                    view,
                    fields,
                    data.get("comparison_model") or current.comparison_model,
                )
            )
        except PriceFieldError as exc:
            return jsonify({"error": str(exc)}), 400
        except PricingRefusedError as exc:
            return jsonify({"error": str(exc)}), 422

    # Vuln D fix: validate model_url and model_filename in PATCH too
    if "model_url" in updates:
        model_url = str(updates["model_url"]).strip()
        if model_url and not _validate_model_url(model_url):
            return jsonify({"error": _MODEL_URL_ERROR}), 400

    if "model_filename" in updates:
        model_filename = str(updates["model_filename"]).strip()
        if model_filename and not _validate_model_filename(model_filename):
            return jsonify({"error": _MODEL_FILENAME_ERROR}), 400

    def _update():
        if updates:
            db(db.llamacpp_deployments.id == deployment_id).update(**updates)
            db.commit()
        return "ok"

    await asyncio.to_thread(_update)

    return {"message": "Deployment updated"}, 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
@validate_response(MessageResponse, 200)
async def delete_llamacpp_deployment(deployment_id):
    """Delete a llama.cpp deployment."""
    force = request.args.get("force", "").lower() == "true"

    def _prepare():
        dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
        if not dep or not _visible_to_org(dep, g.user.get("organization_id")):
            return "not_found", None

        # an RPC deployment owns cluster objects in every non-stopped state, not only running
        live = dep.status == "running" or (_is_rpc(dep) and dep.status in _RPC_LIVE_STATUSES)
        if live and not force:
            return "running", None

        if live and force and dep.deployment_type == "kubernetes":
            mgr = LlamaCppManager(db)
            try:
                mgr.remove_daemonset(dep, force=True)
            except Exception as e:
                logger.warning(f"Error during forced removal of {dep.name}: {e}")
        return "ok", dep

    result, dep = await asyncio.to_thread(_prepare)

    if result == "not_found":
        return jsonify({"error": "Deployment not found"}), 404
    if result == "running":
        return jsonify({"error": "Deployment is running. Use ?force=true to delete it."}), 409

    fleet_managed = _is_rpc(dep) or _fleet_keys_enabled(g.user.get("organization_id"))
    if fleet_managed:
        try:  # the bearer token must not outlive its deployment (AM18)
            await fleet_secrets.remove_key(derive_slug(dep.name, dep.public_id))
        except fleet_secrets.SecretWriteFailed:
            return jsonify({"error": "secret write failed", "reason": "secret write failed"}), 502

    def _finish():
        if fleet_managed:  # only fleet-managed rows have a key link and a virtual-price rate row
            _drop_link_and_rate(dep.public_id)
        db(db.llamacpp_deployments.id == deployment_id).delete()
        db.commit()

    await asyncio.to_thread(_finish)
    return {"message": "Deployment deleted"}, 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/deploy", methods=["POST"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
@validate_response(DeployResponse, 200)
async def deploy_llamacpp(deployment_id):
    """Deploy a llama.cpp deployment (create DaemonSet or register remote endpoint)."""
    existing = await asyncio.to_thread(_llamacpp_row, deployment_id)
    if (
        existing is not None
        and _is_rpc(existing)
        and await asyncio.to_thread(_visible_to_org, existing, g.user.get("organization_id"))
    ):
        return await _deploy_rpc_row(existing)

    def _deploy():
        dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
        if not dep or not _visible_to_org(dep, g.user.get("organization_id")):
            return "not_found", None

        mgr = LlamaCppManager(db)
        try:
            if dep.deployment_type == "kubernetes":
                mgr.deploy_daemonset(dep)
            else:
                mgr.register_remote(dep)
        except Exception as e:
            return "error", str(e)

        return "ok", None

    status, error = await asyncio.to_thread(_deploy)

    if status == "not_found":
        return jsonify({"error": "Deployment not found"}), 404
    if status == "error":
        return jsonify({"error": error}), 503

    return {"message": "Deployment initiated", "deployment_id": deployment_id}, 200


async def _rotate_key_for_redeploy(dep: Any) -> str | None:
    """Mint a fresh bearer token for a redeploy (AM11): Secret first, then the connection link.

    Returns an error message, or ``None`` on success. A failed link update restores the previous
    Secret value so the Secret and the link never disagree.
    """
    slug = derive_slug(dep.name, dep.public_id)
    link = await asyncio.to_thread(_llamacpp_link, f"llamacpp-{dep.public_id}")
    if link is None:
        try:
            await fleet_secrets.provision_key_and_link(
                db,
                org_id=_owner_org_id(dep),
                slug=slug,
                link_name=f"llamacpp-{dep.public_id}",
                models=[dep.model_name],
                provider="llamacpp",
            )
        except fleet_secrets.SecretWriteFailed:
            return "secret write failed"
        return None
    token = fleet_secrets.mint_token()
    try:
        previous = await fleet_secrets.read_deployment_key(slug)
        await fleet_secrets.write_deployment_key(slug, token)
    except fleet_secrets.SecretWriteFailed:
        return "secret write failed"
    try:
        await asyncio.to_thread(_store_link_key, link.id, token)
    except Exception:
        logger.error("redeploy key rotation: link update failed for %s", dep.public_id)
        try:
            await fleet_secrets.restore_deployment_key(slug, previous)
        except fleet_secrets.SecretWriteFailed:
            logger.error("redeploy key rotation: restore failed for %s", dep.public_id)
        return "link update failed; previous token restored"
    return None


async def _deploy_rpc_row(dep: Any):
    """(Re)deploy an RPC deployment from its stored layout, with a freshly rotated token."""
    org_id = g.user.get("organization_id")
    if not _fleet_keys_enabled(org_id):
        return jsonify({"error": _RPC_FLAGS_REFUSAL}), 403
    if dep.status in _RPC_LIVE_STATUSES:
        return jsonify({"error": "Already deployed. Remove it before deploying again."}), 409
    try:
        layout, hostnames = await asyncio.to_thread(
            rpc_service.layout_from_row, db, dep, _owner_org_id(dep)
        )
        image = rpc_service.rpc_image_for_vendor(layout.vendor)
    except rpc_service.RpcPlacementUnavailable as exc:
        return jsonify({"error": str(exc)}), 409
    except rpc_service.RpcImageNotConfigured as exc:
        logger.error("llamacpp rpc image not configured: %s", exc)
        return jsonify({"error": str(exc)}), 500
    if (rotation_error := await _rotate_key_for_redeploy(dep)) is not None:
        return jsonify({"error": rotation_error, "reason": rotation_error}), 502
    mgr = LlamaCppManager(db)
    try:
        await asyncio.to_thread(mgr.deploy_rpc, dep, layout, hostnames, image)
        await asyncio.to_thread(_commit_after, lambda: None)
    except RpcRefused as exc:
        return jsonify({"error": str(exc)}), 422
    except Exception:
        logger.error("llamacpp rpc redeploy failed for %s", dep.public_id, exc_info=True)
        return jsonify({"error": _RPC_START_FAILED}), 503
    return {"message": "Deployment initiated", "deployment_id": dep.id}, 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/remove", methods=["POST"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
@validate_response(MessageResponse, 200)
async def remove_llamacpp(deployment_id):
    """Remove a running llama.cpp deployment."""

    def _remove():
        dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
        if not dep or not _visible_to_org(dep, g.user.get("organization_id")):
            return "not_found", None

        mgr = LlamaCppManager(db)
        try:
            if dep.deployment_type == "kubernetes":
                mgr.remove_daemonset(dep, force=True)
            else:
                db(db.llamacpp_deployments.id == deployment_id).update(status="stopped")
                db.commit()
        except Exception as e:
            return "error", str(e)

        return "ok", None

    status, error = await asyncio.to_thread(_remove)

    if status == "not_found":
        return jsonify({"error": "Deployment not found"}), 404
    if status == "error":
        return jsonify({"error": error}), 503

    return {"message": "Deployment removed"}, 200


def _rpc_health_fields(dep: Any) -> dict[str, Any]:
    """Worker counts for an RPC row: live from Kubernetes, ``None`` when it cannot be read."""
    fields: dict[str, Any] = {
        "status_message": dep.status_message,
        "workers_up": None,
        "workers_total": len(dep.rpc_workers) if isinstance(dep.rpc_workers, list) else None,
    }
    try:
        seen = LlamaCppManager(db).rpc_observe(dep)
    except Exception:
        logger.warning("could not observe RPC workers for %s", dep.public_id, exc_info=True)
        return fields
    fields["workers_up"], fields["workers_total"] = seen.workers_up, seen.workers_total
    return fields


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/health", methods=["GET"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
@validate_response(LlamaCppHealthResponse, 200)
async def check_llamacpp_health(deployment_id):
    """Check the health status of a llama.cpp deployment."""

    def _check():
        dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
        if not dep or not _visible_to_org(dep, g.user.get("organization_id")):
            return "not_found", None

        extra = _rpc_health_fields(dep) if _is_rpc(dep) else {}
        if not dep.endpoint_url:
            return "no_endpoint", extra

        try:
            resp = requests.get(f"{dep.endpoint_url}/health", timeout=10)
            if resp.status_code == 200:
                return "healthy", (dep.endpoint_url, extra)
            return "unhealthy_status", (resp.status_code, extra)
        except Exception as e:
            return "unhealthy_error", (str(e), extra)

    status, payload = await asyncio.to_thread(_check)

    if status == "not_found":
        return jsonify({"error": "Deployment not found"}), 404
    if status == "no_endpoint":
        return {"status": "unknown", "reason": "endpoint_url not set", **payload}, 200
    value, extra = payload
    if status == "healthy":
        return {"status": "healthy", "endpoint": value, **extra}, 200
    if status == "unhealthy_status":
        return {"status": "unhealthy", "http_status": value, **extra}, 200
    return {"status": "unhealthy", "error": value, **extra}, 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/export/k8s", methods=["GET"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
async def export_llamacpp_k8s(deployment_id):
    """Export Kubernetes manifest for a llama.cpp deployment."""

    def _export():
        dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
        if not dep or not _visible_to_org(dep, g.user.get("organization_id")):
            return None, None

        mgr = LlamaCppManager(db)
        if _is_rpc(dep):
            return dep, _export_rpc_manifests(mgr, dep)
        manifest = mgr.export_k8s_manifest(dep)
        return dep, manifest

    try:
        dep, manifest = await asyncio.to_thread(_export)
    except (
        rpc_service.RpcPlacementUnavailable,
        rpc_service.RpcImageNotConfigured,
        RpcRefused,
    ) as exc:
        return jsonify({"error": str(exc)}), 409

    if not dep:
        return jsonify({"error": "Deployment not found"}), 404

    return manifest, 200, {"Content-Type": "application/x-yaml"}


def _export_rpc_manifests(mgr: LlamaCppManager, dep: Any) -> str:
    """YAML of an RPC deployment's objects, rebuilt from its stored layout and live inventory."""
    import yaml

    layout, hostnames = rpc_service.layout_from_row(db, dep, _owner_org_id(dep))
    image = rpc_service.rpc_image_for_vendor(layout.vendor)
    docs = mgr.export_rpc_manifests(dep, layout, hostnames, image, int(dep.n_ctx or 4096))
    return yaml.dump_all(docs, default_flow_style=False)


@dataclass(slots=True)
class RotateLlamaCppTokenResponse:
    """Response body for POST /api/v1/fleet/llamacpp/deployments/<uuid>/rotate-token."""

    public_id: str
    api_key_fingerprint: str


@api_v1_bp.route("/fleet/llamacpp/deployments/<uuid:public_id>/rotate-token", methods=["POST"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(RotateLlamaCppTokenResponse, 200)
async def rotate_llamacpp_token(public_id: uuid.UUID):
    """Rotate a llama.cpp deployment's bearer token: Secret key, then head restart (AM18).

    llama-server loads ``--api-key-file`` once at startup, so unlike vLLM the head pod must be
    rolled (workers untouched). A failed Secret write leaves the old token in place; a failed
    restart is reported as "rotated, restart pending" (Secret and link already moved).
    """
    if not _fleet_keys_enabled(g.user.get("organization_id")):
        return jsonify({"status": "error", "error": "not_found"}), 404

    from ...services import fleet_secrets as secrets_svc

    dep = await asyncio.to_thread(_llamacpp_row_by_public_id, str(public_id))
    if dep is None or not await asyncio.to_thread(
        _visible_to_org, dep, g.user.get("organization_id")
    ):
        return jsonify({"error": "Deployment not found"}), 404
    slug = derive_slug(dep.name, public_id)
    link = await asyncio.to_thread(_llamacpp_link, f"llamacpp-{public_id}")
    token = secrets_svc.mint_token()
    try:
        previous = await secrets_svc.read_deployment_key(slug)
        await secrets_svc.write_deployment_key(slug, token)
    except secrets_svc.SecretWriteFailed:
        return jsonify({"error": "secret write failed", "reason": "secret write failed"}), 502
    if link is not None:
        # Keep the link in step with the Secret immediately; a restart failure below must never
        # leave the proxy/canary holding a stale token.
        try:
            await asyncio.to_thread(_store_link_key, link.id, token)
        except Exception:
            logger.error(
                "fleet.deployment.token_rotation_failed deployment=%s slug=%s: link update failed",
                public_id,
                slug,
            )
            try:
                await secrets_svc.restore_deployment_key(slug, previous)
            except secrets_svc.SecretWriteFailed:
                logger.error(
                    "fleet.deployment.token_rotation_restore_failed deployment=%s slug=%s",
                    public_id,
                    slug,
                )
                return (
                    jsonify(
                        {
                            "error": "token rotation failed",
                            "reason": "link update failed; restore failed, rotate again",
                        }
                    ),
                    502,
                )
            return (
                jsonify(
                    {
                        "error": "token rotation failed",
                        "reason": "link update failed; previous token restored",
                    }
                ),
                502,
            )
    try:
        # RPC: the head is a Deployment (workers are never touched); single-node keeps its target
        await secrets_svc.bump_restart(
            "Deployment" if _is_rpc(dep) else "StatefulSet", dep.k8s_daemonset_name or dep.name
        )
    except secrets_svc.SecretWriteFailed:
        logger.error(
            "fleet.deployment.token_rotation_partial deployment=%s slug=%s: restart pending",
            public_id,
            slug,
        )
        return (
            jsonify(
                {
                    "error": "rotated, restart pending, retry",
                    "reason": "restart pending",
                }
            ),
            502,
        )
    fingerprint = secrets_svc.fingerprint(token)
    logger.info(
        "fleet.deployment.token_rotated deployment=%s fingerprint_new=%s", public_id, fingerprint
    )
    return {"public_id": str(public_id), "api_key_fingerprint": fingerprint}, 200
