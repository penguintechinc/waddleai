"""WaddleAI Management API v1 - llama.cpp Deployment Management Endpoints."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests
from quart import g, jsonify, request
from quart_schema import validate_request, validate_response

from shared.auth.rbac import Permission
from shared.fleet.enums import LocalDeploymentKind, PriceMode
from shared.fleet.vllm_ray.slug import derive_slug

from ...extensions import db
from ...services import fleet_secrets
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
    }


@api_v1_bp.route("/llamacpp/deployments", methods=["GET"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
@validate_response(LlamaCppDeploymentListResponse, 200)
async def list_llamacpp_deployments():
    """List all llama.cpp deployments (bounded page)."""
    page = PageRequest.from_request()

    def _fetch():
        query = db.llamacpp_deployments.id > 0
        total = db(query).count()
        rows = db(query).select(limitby=page.limitby, orderby=db.llamacpp_deployments.id)
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
    if not dep:
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
        if not dep:
            return "not_found"
        if dep.status == "running" and not price_only:
            return "running"
        return "ok"

    check_result = await asyncio.to_thread(_check)

    if check_result == "not_found":
        return jsonify({"error": "Deployment not found"}), 404
    if check_result == "running":
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

    if pricing_requested(data):
        current = await asyncio.to_thread(
            lambda: db(db.llamacpp_deployments.id == deployment_id).select().first()
        )
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
                    current,
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

    def _delete():
        dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
        if not dep:
            return "not_found"

        if dep.status == "running" and not force:
            return "running"

        if dep.status == "running" and force and dep.deployment_type == "kubernetes":
            mgr = LlamaCppManager(db)
            try:
                mgr.remove_daemonset(dep, force=True)
            except Exception as e:
                logger.warning(f"Error during forced removal of {dep.name}: {e}")

        db(db.llamacpp_deployments.id == deployment_id).delete()
        db.commit()
        return "ok"

    result = await asyncio.to_thread(_delete)

    if result == "not_found":
        return jsonify({"error": "Deployment not found"}), 404
    if result == "running":
        return jsonify({"error": "Deployment is running. Use ?force=true to delete it."}), 409

    return {"message": "Deployment deleted"}, 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/deploy", methods=["POST"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
@validate_response(DeployResponse, 200)
async def deploy_llamacpp(deployment_id):
    """Deploy a llama.cpp deployment (create DaemonSet or register remote endpoint)."""

    def _deploy():
        dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
        if not dep:
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


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/remove", methods=["POST"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
@validate_response(MessageResponse, 200)
async def remove_llamacpp(deployment_id):
    """Remove a running llama.cpp deployment."""

    def _remove():
        dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
        if not dep:
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


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/health", methods=["GET"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
@validate_response(LlamaCppHealthResponse, 200)
async def check_llamacpp_health(deployment_id):
    """Check the health status of a llama.cpp deployment."""

    def _check():
        dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
        if not dep:
            return "not_found", None

        if not dep.endpoint_url:
            return "no_endpoint", None

        try:
            resp = requests.get(f"{dep.endpoint_url}/health", timeout=10)
            if resp.status_code == 200:
                return "healthy", dep.endpoint_url
            return "unhealthy_status", resp.status_code
        except Exception as e:
            return "unhealthy_error", str(e)

    status, payload = await asyncio.to_thread(_check)

    if status == "not_found":
        return jsonify({"error": "Deployment not found"}), 404
    if status == "no_endpoint":
        return {"status": "unknown", "reason": "endpoint_url not set"}, 200
    if status == "healthy":
        return {"status": "healthy", "endpoint": payload}, 200
    if status == "unhealthy_status":
        return {"status": "unhealthy", "http_status": payload}, 200
    return {"status": "unhealthy", "error": payload}, 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/export/k8s", methods=["GET"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
async def export_llamacpp_k8s(deployment_id):
    """Export Kubernetes manifest for a llama.cpp deployment."""

    def _export():
        dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
        if not dep:
            return None, None

        mgr = LlamaCppManager(db)
        manifest = mgr.export_k8s_manifest(dep)
        return dep, manifest

    dep, manifest = await asyncio.to_thread(_export)

    if not dep:
        return jsonify({"error": "Deployment not found"}), 404

    return manifest, 200, {"Content-Type": "application/x-yaml"}


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
    if dep is None:
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
        await secrets_svc.bump_restart("StatefulSet", dep.k8s_daemonset_name or dep.name)
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
