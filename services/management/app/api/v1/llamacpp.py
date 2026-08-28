"""WaddleAI Management API v1 - llama.cpp Deployment Management Endpoints."""

import asyncio
import logging
import re
from urllib.parse import urlsplit

import requests
from quart import jsonify, request

from shared.auth.rbac import Permission

from ...extensions import db
from ...services.llamacpp_manager import LlamaCppManager
from . import api_v1_bp
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)

_MODEL_URL_ERROR = (
    "Invalid model_url: must be an https URL with no control characters or shell metacharacters"
)


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


def _deployment_to_dict(dep) -> dict:
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
async def list_llamacpp_deployments():
    """List all llama.cpp deployments."""
    deployments = await asyncio.to_thread(lambda: db(db.llamacpp_deployments.id > 0).select())
    return jsonify({"deployments": [_deployment_to_dict(d) for d in deployments]}), 200


@api_v1_bp.route("/llamacpp/deployments", methods=["POST"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
async def create_llamacpp_deployment():
    """Create a new llama.cpp deployment."""
    data = (await request.get_json()) or {}
    name = data.get("name", "").strip()
    model_name = data.get("model_name", "").strip()

    if not name:
        return jsonify({"error": "name is required"}), 400
    if not model_name:
        return jsonify({"error": "model_name is required"}), 400

    # Vuln D fix: validate model_url and model_filename at API layer
    model_url = data.get("model_url", "").strip()
    model_filename = data.get("model_filename", "").strip()

    if model_url and not _validate_model_url(model_url):
        return jsonify({"error": _MODEL_URL_ERROR}), 400

    if model_filename and not _validate_model_filename(model_filename):
        return jsonify(
            {"error": "Invalid model_filename: bare filename only (alphanumeric . - _)"}
        ), 400

    deployment_type = data.get("deployment_type", "kubernetes")

    def _create():
        mgr = LlamaCppManager(db)
        new_id = db.llamacpp_deployments.insert(
            name=name,
            deployment_type=deployment_type,
            status="pending",
            model_name=model_name,
            model_url=data.get("model_url"),
            model_filename=data.get("model_filename"),
            n_ctx=data.get("n_ctx", 4096),
            n_gpu_layers=data.get("n_gpu_layers", -1),
            gpu_count=data.get("gpu_count", 1),
            endpoint_url=data.get("endpoint_url"),
            k8s_namespace=data.get("k8s_namespace", "waddleai"),
            k8s_daemonset_name=mgr._daemonset_name(name),
            node_selector=data.get("node_selector"),
            node_affinity=data.get("node_affinity"),
        )
        db.commit()
        return new_id

    dep_id = await asyncio.to_thread(_create)
    return jsonify({"deployment_id": dep_id, "message": "Deployment created"}), 201


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>", methods=["GET"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
async def get_llamacpp_deployment(deployment_id):
    """Get a specific llama.cpp deployment."""
    dep = await asyncio.to_thread(
        lambda: db(db.llamacpp_deployments.id == deployment_id).select().first()
    )
    if not dep:
        return jsonify({"error": "Deployment not found"}), 404
    return jsonify(_deployment_to_dict(dep)), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>", methods=["PATCH"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
async def update_llamacpp_deployment(deployment_id):
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
    updates = {k: v for k, v in data.items() if k in allowed}

    # Vuln D fix: validate model_url and model_filename in PATCH too
    if "model_url" in updates:
        model_url = str(updates["model_url"]).strip()
        if model_url and not _validate_model_url(model_url):
            return jsonify({"error": _MODEL_URL_ERROR}), 400

    if "model_filename" in updates:
        model_filename = str(updates["model_filename"]).strip()
        if model_filename and not _validate_model_filename(model_filename):
            return jsonify(
                {"error": "Invalid model_filename: bare filename only (alphanumeric . - _)"}
            ), 400

    def _update():
        if updates:
            db(db.llamacpp_deployments.id == deployment_id).update(**updates)
            db.commit()
        return "ok"

    await asyncio.to_thread(_update)

    return jsonify({"message": "Deployment updated"}), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
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

    return jsonify({"message": "Deployment deleted"}), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/deploy", methods=["POST"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
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

    return jsonify({"message": "Deployment initiated", "deployment_id": deployment_id}), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/remove", methods=["POST"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
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

    return jsonify({"message": "Deployment removed"}), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/health", methods=["GET"])
@require_auth
@require_scope(Permission.LLAMACPP_ADMIN)
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
        return jsonify({"status": "unknown", "reason": "endpoint_url not set"}), 200
    if status == "healthy":
        return jsonify({"status": "healthy", "endpoint": payload}), 200
    if status == "unhealthy_status":
        return jsonify({"status": "unhealthy", "http_status": payload}), 200
    return jsonify({"status": "unhealthy", "error": payload}), 200


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
