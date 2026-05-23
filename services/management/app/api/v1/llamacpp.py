"""
WaddleAI Management API v1 - llama.cpp Deployment Management Endpoints
"""

import logging

import requests
from flask import jsonify, request

from . import api_v1_bp
from ...extensions import db
from ...services.llamacpp_manager import LlamaCppManager
from .auth import require_auth, require_role

logger = logging.getLogger(__name__)


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
@require_role("admin")
def list_llamacpp_deployments():
    """List all llama.cpp deployments."""
    deployments = db(db.llamacpp_deployments.id > 0).select()
    return jsonify({"deployments": [_deployment_to_dict(d) for d in deployments]}), 200


@api_v1_bp.route("/llamacpp/deployments", methods=["POST"])
@require_auth
@require_role("admin")
def create_llamacpp_deployment():
    """Create a new llama.cpp deployment."""
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    model_name = data.get("model_name", "").strip()

    if not name:
        return jsonify({"error": "name is required"}), 400
    if not model_name:
        return jsonify({"error": "model_name is required"}), 400

    deployment_type = data.get("deployment_type", "kubernetes")
    mgr = LlamaCppManager(db)

    dep_id = db.llamacpp_deployments.insert(
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
    return jsonify({"deployment_id": dep_id, "message": "Deployment created"}), 201


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>", methods=["GET"])
@require_auth
@require_role("admin")
def get_llamacpp_deployment(deployment_id):
    """Get a specific llama.cpp deployment."""
    dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
    if not dep:
        return jsonify({"error": "Deployment not found"}), 404
    return jsonify(_deployment_to_dict(dep)), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>", methods=["PATCH"])
@require_auth
@require_role("admin")
def update_llamacpp_deployment(deployment_id):
    """Update a llama.cpp deployment (can only update stopped deployments)."""
    dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
    if not dep:
        return jsonify({"error": "Deployment not found"}), 404
    if dep.status == "running":
        return jsonify({"error": "Stop the deployment before modifying it"}), 409

    data = request.get_json() or {}
    allowed = {"model_name", "model_url", "model_filename", "n_ctx", "n_gpu_layers",
               "gpu_count", "k8s_namespace", "node_selector", "node_affinity"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if updates:
        db(db.llamacpp_deployments.id == deployment_id).update(**updates)
    return jsonify({"message": "Deployment updated"}), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>", methods=["DELETE"])
@require_auth
@require_role("admin")
def delete_llamacpp_deployment(deployment_id):
    """Delete a llama.cpp deployment."""
    dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
    if not dep:
        return jsonify({"error": "Deployment not found"}), 404

    force = request.args.get("force", "").lower() == "true"
    if dep.status == "running" and not force:
        return jsonify({"error": "Deployment is running. Use ?force=true to delete it."}), 409

    if dep.status == "running" and force and dep.deployment_type == "kubernetes":
        mgr = LlamaCppManager(db)
        try:
            mgr.remove_daemonset(dep, force=True)
        except Exception as e:
            logger.warning(f"Error during forced removal of {dep.name}: {e}")

    db(db.llamacpp_deployments.id == deployment_id).delete()
    return jsonify({"message": "Deployment deleted"}), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/deploy", methods=["POST"])
@require_auth
@require_role("admin")
def deploy_llamacpp(deployment_id):
    """Deploy a llama.cpp deployment (create DaemonSet or register remote endpoint)."""
    dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
    if not dep:
        return jsonify({"error": "Deployment not found"}), 404

    mgr = LlamaCppManager(db)
    try:
        if dep.deployment_type == "kubernetes":
            mgr.deploy_daemonset(dep)
        else:
            mgr.register_remote(dep)
    except Exception as e:
        return jsonify({"error": str(e)}), 503

    return jsonify({"message": "Deployment initiated", "deployment_id": deployment_id}), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/remove", methods=["POST"])
@require_auth
@require_role("admin")
def remove_llamacpp(deployment_id):
    """Remove a running llama.cpp deployment."""
    dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
    if not dep:
        return jsonify({"error": "Deployment not found"}), 404

    mgr = LlamaCppManager(db)
    try:
        if dep.deployment_type == "kubernetes":
            mgr.remove_daemonset(dep, force=True)
        else:
            db(db.llamacpp_deployments.id == deployment_id).update(status="stopped")
    except Exception as e:
        return jsonify({"error": str(e)}), 503

    return jsonify({"message": "Deployment removed"}), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/health", methods=["GET"])
@require_auth
@require_role("admin")
def check_llamacpp_health(deployment_id):
    """Check the health status of a llama.cpp deployment."""
    dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
    if not dep:
        return jsonify({"error": "Deployment not found"}), 404
    if not dep.endpoint_url:
        return jsonify({"status": "unknown", "reason": "endpoint_url not set"}), 200

    try:
        resp = requests.get(f"{dep.endpoint_url}/health", timeout=10)
        if resp.status_code == 200:
            return jsonify({"status": "healthy", "endpoint": dep.endpoint_url}), 200
        return jsonify({"status": "unhealthy", "http_status": resp.status_code}), 200
    except Exception as e:
        return jsonify({"status": "unhealthy", "error": str(e)}), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/export/k8s", methods=["GET"])
@require_auth
@require_role("admin")
def export_llamacpp_k8s(deployment_id):
    """Export Kubernetes manifest for a llama.cpp deployment."""
    dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
    if not dep:
        return jsonify({"error": "Deployment not found"}), 404

    mgr = LlamaCppManager(db)
    manifest = mgr.export_k8s_manifest(dep)
    return manifest, 200, {"Content-Type": "application/x-yaml"}
