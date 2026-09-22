"""WaddleAI Management API v1 - llama.cpp Deployment Management Endpoints."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests
from quart import jsonify, request
from quart_schema import validate_request, validate_response

from shared.auth.rbac import Permission

from ...extensions import db
from ...services.llamacpp_manager import LlamaCppManager
from . import api_v1_bp
from ._pagination import PageRequest
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


@dataclass(slots=True)
class CreateLlamaCppDeploymentResponse:
    """Response body for a successful POST /api/v1/llamacpp/deployments."""

    deployment_id: int
    message: str


@dataclass(slots=True)
class UpdateLlamaCppDeploymentRequest:
    """Request body for PATCH /api/v1/llamacpp/deployments/<id>. Every field a partial."""

    model_name: str | None = None
    model_url: str | None = None
    model_filename: str | None = None
    n_ctx: int | None = None
    n_gpu_layers: int | None = None
    gpu_count: int | None = None
    k8s_namespace: str | None = None
    node_selector: dict[str, Any] | None = None
    node_affinity: dict[str, Any] | None = None


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
        )
        db.commit()
        return new_id

    dep_id = await asyncio.to_thread(_create)
    return {"deployment_id": dep_id, "message": "Deployment created"}, 201


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
@validate_request(UpdateLlamaCppDeploymentRequest)
async def update_llamacpp_deployment(deployment_id, data: UpdateLlamaCppDeploymentRequest):
    """Update a llama.cpp deployment (can only update stopped deployments)."""

    def _check():
        dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
        if not dep:
            return "not_found"
        if dep.status == "running":
            return "running"
        return "ok"

    check_result = await asyncio.to_thread(_check)

    if check_result == "not_found":
        return jsonify({"error": "Deployment not found"}), 404
    if check_result == "running":
        return jsonify({"error": "Stop the deployment before modifying it"}), 409

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
    updates: dict[str, Any] = {
        name: value for name in allowed if (value := getattr(data, name)) is not None
    }

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
