"""WaddleAI Management API v1 - Ollama Deployment Management Endpoints."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import yaml
from quart import Response, current_app, jsonify, request
from quart_schema import validate_request, validate_response

from shared.auth.rbac import Permission

from ...extensions import db
from . import api_v1_bp
from ._pagination import PageRequest
from .auth import require_auth, require_scope

_DEFAULT_GPU_CONFIG: dict[str, Any] = {"count": 0, "driver": "nvidia"}
_DEFAULT_RESOURCE_LIMITS: dict[str, Any] = {"cpu": "4", "memory": "8G"}


# ---------------------------------------------------------------------------
# OpenAPI request/response models (audit-2026-09-14-wave2).
#
# Request models make every field Optional with the handler's own defaults so
# quart-schema never pre-empts the handler's presence checks and their exact
# error messages. Response models list exactly the fields each handler returns
# -- a field omitted here is silently dropped from the response, the
# client-breaking regression these models exist to prevent.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PageMeta:
    """Pagination metadata block emitted by ``PageRequest.meta``."""

    page: int
    limit: int
    total: int | None
    pages: int | None


@dataclass(slots=True)
class OllamaDeploymentSummary:
    """One row of the deployment-list response."""

    id: int
    name: str
    endpoint_url: str | None
    deployment_type: str | None
    status: str | None
    health_status: str | None
    model_count: int
    auto_start: bool | None
    last_health_check: str | None
    created_at: str | None


@dataclass(slots=True)
class OllamaDeploymentListResponse:
    """Response body for GET /api/v1/ollama/deployments."""

    deployments: list[OllamaDeploymentSummary]
    total: int
    pagination: PageMeta


@dataclass(slots=True)
class OllamaModelSummary:
    """A model row embedded in the single-deployment detail response."""

    id: int
    model_name: str
    model_tag: str | None
    status: str | None
    size_bytes: int | None


@dataclass(slots=True)
class OllamaDeploymentDetail:
    """Response body for GET /api/v1/ollama/deployments/<id>."""

    id: int
    name: str
    endpoint_url: str | None
    deployment_type: str | None
    docker_compose_config: dict[str, Any] | None
    gpu_config: dict[str, Any] | None
    resource_limits: dict[str, Any] | None
    status: str | None
    health_status: str | None
    auto_start: bool | None
    last_health_check: str | None
    created_at: str | None
    models: list[OllamaModelSummary]


@dataclass(slots=True)
class CreateOllamaDeploymentRequest:
    """Request body for POST /api/v1/ollama/deployments. Every field optional."""

    name: str | None = None
    endpoint_url: str | None = None
    deployment_type: str | None = "external"
    gpu_config: dict[str, Any] | None = field(default=None)
    resource_limits: dict[str, Any] | None = field(default=None)
    auto_start: bool | None = True


@dataclass(slots=True)
class CreateOllamaDeploymentResponse:
    """Response body for a successful POST /api/v1/ollama/deployments."""

    id: int
    name: str
    deployment_type: str | None
    message: str


@dataclass(slots=True)
class UpdateOllamaDeploymentRequest:
    """Request body for PUT /api/v1/ollama/deployments/<id>. Every field a partial."""

    name: str | None = None
    endpoint_url: str | None = None
    gpu_config: dict[str, Any] | None = field(default=None)
    resource_limits: dict[str, Any] | None = field(default=None)
    auto_start: bool | None = None


@dataclass(slots=True)
class MessageResponse:
    """Generic ``{"message": str}`` envelope."""

    message: str


@dataclass(slots=True)
class OllamaActionResponse:
    """Response body for start/stop/restart lifecycle actions."""

    deployment_id: int
    status: str
    message: str


@dataclass(slots=True)
class OllamaHealthResponse:
    """Response body for GET /api/v1/ollama/deployments/<id>/health."""

    deployment_id: int
    endpoint_url: str | None
    health_status: str
    healthy: bool
    checked_at: str
    error: str | None


@dataclass(slots=True)
class OllamaLogsResponse:
    """Response body for GET /api/v1/ollama/deployments/<id>/logs."""

    deployment_id: int
    lines: int
    logs: str


@dataclass(slots=True)
class PullOllamaModelRequest:
    """Request body for POST /api/v1/ollama/deployments/<id>/models/pull."""

    model: str | None = None
    tag: str | None = "latest"


@dataclass(slots=True)
class PullOllamaModelResponse:
    """Response body for a successful model pull."""

    deployment_id: int
    model: str
    status: str
    completed: bool
    message: str


@dataclass(slots=True)
class RemoveOllamaModelResponse:
    """Response body for DELETE .../models/<model_name>."""

    deployment_id: int
    model: str
    message: str


@api_v1_bp.route("/ollama/deployments", methods=["GET"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
@validate_response(OllamaDeploymentListResponse, 200)
async def list_ollama_deployments():
    """List all Ollama deployments (bounded page)."""
    if not current_app.config.get("ENABLE_OLLAMA_MANAGEMENT", True):
        return jsonify({"error": "Ollama management is disabled"}), 403

    page = PageRequest.from_request()

    def _fetch():
        query = db.ollama_deployments.id > 0
        total = db(query).count()
        deployments = db(query).select(limitby=page.limitby, orderby=db.ollama_deployments.id)
        return total, [(d, db(db.ollama_models.deployment_id == d.id).count()) for d in deployments]

    total, deployment_rows = await asyncio.to_thread(_fetch)

    result = []
    for deployment, model_count in deployment_rows:
        result.append(
            {
                "id": deployment.id,
                "name": deployment.name,
                "endpoint_url": deployment.endpoint_url,
                "deployment_type": deployment.deployment_type,
                "status": deployment.status,
                "health_status": deployment.health_status,
                "model_count": model_count,
                "auto_start": deployment.auto_start,
                "last_health_check": deployment.last_health_check.isoformat()
                if deployment.last_health_check
                else None,
                "created_at": deployment.created_at.isoformat() if deployment.created_at else None,
            }
        )

    return {"deployments": result, "total": len(result), **page.meta(total)}, 200


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>", methods=["GET"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
@validate_response(OllamaDeploymentDetail, 200)
async def get_ollama_deployment(deployment_id):
    """Get Ollama deployment details."""

    def _fetch():
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return None, None
        models = db(db.ollama_models.deployment_id == deployment_id).select()
        return deployment, models

    deployment, models = await asyncio.to_thread(_fetch)

    if not deployment:
        return jsonify({"error": "Deployment not found"}), 404

    return {
        "id": deployment.id,
        "name": deployment.name,
        "endpoint_url": deployment.endpoint_url,
        "deployment_type": deployment.deployment_type,
        "docker_compose_config": deployment.docker_compose_config,
        "gpu_config": deployment.gpu_config,
        "resource_limits": deployment.resource_limits,
        "status": deployment.status,
        "health_status": deployment.health_status,
        "auto_start": deployment.auto_start,
        "last_health_check": deployment.last_health_check.isoformat()
        if deployment.last_health_check
        else None,
        "created_at": deployment.created_at.isoformat() if deployment.created_at else None,
        "models": [
            {
                "id": m.id,
                "model_name": m.model_name,
                "model_tag": m.model_tag,
                "status": m.status,
                "size_bytes": m.size_bytes,
            }
            for m in models
        ],
    }, 200


@api_v1_bp.route("/ollama/deployments", methods=["POST"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
@validate_response(CreateOllamaDeploymentResponse, 201)
@validate_request(CreateOllamaDeploymentRequest)
async def create_ollama_deployment(data: CreateOllamaDeploymentRequest):
    """Create a new Ollama deployment."""
    if data.name is None:
        return jsonify({"error": "name is required"}), 400
    if data.endpoint_url is None:
        return jsonify({"error": "endpoint_url is required"}), 400

    deployment_type = data.deployment_type or "external"
    gpu_config = data.gpu_config if data.gpu_config is not None else _DEFAULT_GPU_CONFIG.copy()
    resource_limits = data.resource_limits
    if resource_limits is None:
        resource_limits = _DEFAULT_RESOURCE_LIMITS.copy()
    auto_start = data.auto_start if data.auto_start is not None else True

    # Generate docker-compose config if type is docker
    docker_compose_config = None
    if deployment_type == "docker":
        docker_compose_config = generate_docker_compose_config(
            name=data.name, gpu_config=gpu_config, resource_limits=resource_limits
        )

    def _create():
        existing = db(db.ollama_deployments.name == data.name).select().first()
        if existing:
            return "name_conflict", None

        new_id = db.ollama_deployments.insert(
            name=data.name,
            endpoint_url=data.endpoint_url,
            deployment_type=deployment_type,
            docker_compose_config=docker_compose_config,
            gpu_config=gpu_config,
            resource_limits=resource_limits,
            status="unknown",
            auto_start=auto_start,
            created_at=datetime.utcnow(),
        )
        db.commit()
        return "ok", new_id

    result, deployment_id = await asyncio.to_thread(_create)

    if result == "name_conflict":
        return jsonify({"error": "Deployment name already exists"}), 409

    return {
        "id": deployment_id,
        "name": data.name,
        "deployment_type": deployment_type,
        "message": "Deployment created successfully",
    }, 201


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
@validate_response(MessageResponse, 200)
@validate_request(UpdateOllamaDeploymentRequest)
async def update_ollama_deployment(deployment_id, data: UpdateOllamaDeploymentRequest):
    """Update Ollama deployment.

    An empty body (or one carrying no recognised field) is a valid no-op
    partial update -- the typed request model erases the pre-audit
    raw-dict-truthiness ``if not data`` 400, matching keys.py.
    """

    def _update():
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()

        if not deployment:
            return "not_found"

        update_fields: dict[str, Any] = {}

        if data.name is not None:
            existing = (
                db(
                    (db.ollama_deployments.name == data.name)
                    & (db.ollama_deployments.id != deployment_id)
                )
                .select()
                .first()
            )
            if existing:
                return "name_conflict"
            update_fields["name"] = data.name

        if data.endpoint_url is not None:
            update_fields["endpoint_url"] = data.endpoint_url

        if data.gpu_config is not None:
            update_fields["gpu_config"] = data.gpu_config

        if data.resource_limits is not None:
            update_fields["resource_limits"] = data.resource_limits

        if data.auto_start is not None:
            update_fields["auto_start"] = data.auto_start

        if update_fields:
            db(db.ollama_deployments.id == deployment_id).update(**update_fields)

            # Regenerate docker-compose if needed
            if "gpu_config" in update_fields or "resource_limits" in update_fields:
                updated = db(db.ollama_deployments.id == deployment_id).select().first()
                if updated.deployment_type == "docker":
                    docker_compose_config = generate_docker_compose_config(
                        name=updated.name,
                        gpu_config=updated.gpu_config,
                        resource_limits=updated.resource_limits,
                    )
                    db(db.ollama_deployments.id == deployment_id).update(
                        docker_compose_config=docker_compose_config
                    )

            db.commit()

        return "ok"

    result = await asyncio.to_thread(_update)

    if result == "not_found":
        return jsonify({"error": "Deployment not found"}), 404
    if result == "name_conflict":
        return jsonify({"error": "Deployment name already exists"}), 409

    return {"message": "Deployment updated successfully"}, 200


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
@validate_response(MessageResponse, 200)
async def delete_ollama_deployment(deployment_id):
    """Delete Ollama deployment."""

    def _delete():
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()

        if not deployment:
            return "not_found"

        # Delete associated models
        db(db.ollama_models.deployment_id == deployment_id).delete()

        # Delete deployment
        db(db.ollama_deployments.id == deployment_id).delete()
        db.commit()

        return "ok"

    result = await asyncio.to_thread(_delete)

    if result == "not_found":
        return jsonify({"error": "Deployment not found"}), 404

    return {"message": "Deployment deleted successfully"}, 200


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/start", methods=["POST"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
@validate_response(OllamaActionResponse, 200)
async def start_ollama_deployment(deployment_id):
    """Start Ollama deployment (orchestrated mode only)."""
    mode = current_app.config.get("OLLAMA_MANAGEMENT_MODE", "both")
    if mode == "manual":
        return jsonify(
            {"error": "Orchestrated mode is disabled. Use docker-compose export instead."}
        ), 400

    def _start():
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()

        if not deployment:
            return "not_found"

        if deployment.deployment_type not in ["docker"]:
            return "invalid_type"

        # TODO: Implement Docker API integration
        # For now, return a mock response
        db(db.ollama_deployments.id == deployment_id).update(status="running")
        db.commit()

        return "ok"

    result = await asyncio.to_thread(_start)

    if result == "not_found":
        return jsonify({"error": "Deployment not found"}), 404
    if result == "invalid_type":
        return jsonify({"error": "Only docker deployments can be started via API"}), 400

    return {
        "deployment_id": deployment_id,
        "status": "running",
        "message": "Deployment started successfully",
    }, 200


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/stop", methods=["POST"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
@validate_response(OllamaActionResponse, 200)
async def stop_ollama_deployment(deployment_id):
    """Stop Ollama deployment (orchestrated mode only)."""
    mode = current_app.config.get("OLLAMA_MANAGEMENT_MODE", "both")
    if mode == "manual":
        return jsonify({"error": "Orchestrated mode is disabled"}), 400

    def _stop():
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()

        if not deployment:
            return "not_found"

        # TODO: Implement Docker API integration
        db(db.ollama_deployments.id == deployment_id).update(status="stopped")
        db.commit()

        return "ok"

    result = await asyncio.to_thread(_stop)

    if result == "not_found":
        return jsonify({"error": "Deployment not found"}), 404

    return {
        "deployment_id": deployment_id,
        "status": "stopped",
        "message": "Deployment stopped successfully",
    }, 200


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/restart", methods=["POST"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
@validate_response(OllamaActionResponse, 200)
async def restart_ollama_deployment(deployment_id):
    """Restart Ollama deployment (orchestrated mode only)."""
    mode = current_app.config.get("OLLAMA_MANAGEMENT_MODE", "both")
    if mode == "manual":
        return jsonify({"error": "Orchestrated mode is disabled"}), 400

    def _restart():
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()

        if not deployment:
            return "not_found"

        # TODO: Implement Docker API integration
        db(db.ollama_deployments.id == deployment_id).update(status="running")
        db.commit()

        return "ok"

    result = await asyncio.to_thread(_restart)

    if result == "not_found":
        return jsonify({"error": "Deployment not found"}), 404

    return {
        "deployment_id": deployment_id,
        "status": "running",
        "message": "Deployment restarted successfully",
    }, 200


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/health", methods=["GET"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
@validate_response(OllamaHealthResponse, 200)
async def check_ollama_health(deployment_id):
    """Health check for Ollama deployment."""
    from ...services.ollama_manager import OllamaDeploymentManager

    def _check():
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return None, None

        manager = OllamaDeploymentManager(db)
        result = manager.health_check(deployment_id)
        return deployment, result

    deployment, result = await asyncio.to_thread(_check)

    if not deployment:
        return jsonify({"error": "Deployment not found"}), 404

    return {
        "deployment_id": deployment_id,
        "endpoint_url": deployment.endpoint_url,
        "health_status": result.get("status", "unknown"),
        "healthy": result.get("healthy", False),
        "checked_at": result.get("checked_at", datetime.utcnow().isoformat()),
        "error": result.get("error"),
    }, 200


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/logs", methods=["GET"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
@validate_response(OllamaLogsResponse, 200)
async def get_ollama_logs(deployment_id):
    """Get Ollama deployment logs (orchestrated mode only)."""
    mode = current_app.config.get("OLLAMA_MANAGEMENT_MODE", "both")
    if mode == "manual":
        return jsonify({"error": "Orchestrated mode is disabled"}), 400

    lines = request.args.get("lines", 100, type=int)

    deployment = await asyncio.to_thread(
        lambda: db(db.ollama_deployments.id == deployment_id).select().first()
    )

    if not deployment:
        return jsonify({"error": "Deployment not found"}), 404

    # TODO: Implement Docker API to get logs
    return {
        "deployment_id": deployment_id,
        "lines": lines,
        "logs": "Log retrieval not yet implemented",
    }, 200


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/models/pull", methods=["POST"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
@validate_response(PullOllamaModelResponse, 200)
@validate_request(PullOllamaModelRequest)
async def pull_ollama_model(deployment_id, data: PullOllamaModelRequest):
    """Pull a model to Ollama deployment."""
    from ...services.ollama_manager import OllamaDeploymentManager

    def _check_deployment():
        return db(db.ollama_deployments.id == deployment_id).select().first()

    deployment = await asyncio.to_thread(_check_deployment)
    if not deployment:
        return jsonify({"error": "Deployment not found"}), 404

    if data.model is None:
        return jsonify({"error": "model is required"}), 400

    model_name = data.model
    model_tag = data.tag or "latest"
    full_model = f"{model_name}:{model_tag}" if model_tag != "latest" else model_name

    def _pull():
        manager = OllamaDeploymentManager(db)
        return manager.pull_model(deployment_id, full_model)

    result = await asyncio.to_thread(_pull)

    if result.error:
        return jsonify({"error": result.error, "model": full_model, "status": result.status}), 500

    return {
        "deployment_id": deployment_id,
        "model": full_model,
        "status": result.status,
        "completed": result.completed,
        "message": "Model pulled successfully" if result.completed else "Model pull initiated",
    }, 200


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/models/<model_name>", methods=["DELETE"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
@validate_response(RemoveOllamaModelResponse, 200)
async def remove_ollama_model(deployment_id, model_name):
    """Remove a model from Ollama deployment."""

    def _remove():
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()

        if not deployment:
            return "deployment_not_found"

        model = (
            db(
                (db.ollama_models.deployment_id == deployment_id)
                & (db.ollama_models.model_name == model_name)
            )
            .select()
            .first()
        )

        if not model:
            return "model_not_found"

        # TODO: Implement actual Ollama API call to remove model
        db(db.ollama_models.id == model.id).update(status="removed")
        db.commit()

        return "ok"

    result = await asyncio.to_thread(_remove)

    if result == "deployment_not_found":
        return jsonify({"error": "Deployment not found"}), 404
    if result == "model_not_found":
        return jsonify({"error": "Model not found"}), 404

    return {
        "deployment_id": deployment_id,
        "model": model_name,
        "message": "Model removed successfully",
    }, 200


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/docker-compose", methods=["GET"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
async def export_docker_compose(deployment_id):
    """Export docker-compose.yml for Ollama deployment."""
    deployment = await asyncio.to_thread(
        lambda: db(db.ollama_deployments.id == deployment_id).select().first()
    )

    if not deployment:
        return jsonify({"error": "Deployment not found"}), 404

    if deployment.docker_compose_config:
        compose_yaml = yaml.dump(deployment.docker_compose_config, default_flow_style=False)
    else:
        compose_config = generate_docker_compose_config(
            name=deployment.name,
            gpu_config=deployment.gpu_config or {},
            resource_limits=deployment.resource_limits or {},
        )
        compose_yaml = yaml.dump(compose_config, default_flow_style=False)

    return Response(
        compose_yaml,
        mimetype="text/yaml",
        headers={
            "Content-Disposition": f"attachment; filename=ollama-{deployment.name}-compose.yml"
        },
    )


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/k8s-manifest", methods=["GET"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
async def export_k8s_manifest(deployment_id):
    """Export Kubernetes manifest for Ollama deployment.

    Returns DaemonSet + shared RWX PVC for kubernetes-daemonset type,
    or single-replica Deployment for kubernetes type.
    """
    from ...services.ollama_manager import OllamaDeploymentManager

    def _generate():
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return None, None, None

        if deployment.deployment_type == "kubernetes-daemonset":
            manager = OllamaDeploymentManager(db)
            manifest_yaml = manager.generate_daemonset_manifest(deployment_id)
            filename = f"ollama-{deployment.name}-daemonset.yml"
        else:
            manifest = generate_k8s_manifest(
                name=deployment.name,
                gpu_config=deployment.gpu_config or {},
                resource_limits=deployment.resource_limits or {},
            )
            manifest_yaml = yaml.dump_all(manifest, default_flow_style=False)
            filename = f"ollama-{deployment.name}-k8s.yml"

        return deployment, manifest_yaml, filename

    deployment, manifest_yaml, filename = await asyncio.to_thread(_generate)

    if not deployment:
        return jsonify({"error": "Deployment not found"}), 404

    return Response(
        manifest_yaml,
        mimetype="text/yaml",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def generate_docker_compose_config(name: str, gpu_config: dict, resource_limits: dict) -> dict:
    """Generate docker-compose configuration for Ollama."""
    gpu_count = gpu_config.get("count", 0)
    port = 11434  # Default Ollama port

    config: dict[str, Any] = {
        "version": "3.8",
        "services": {
            f"ollama-{name}": {
                "image": "ollama/ollama:latest",
                "container_name": f"waddleai-ollama-{name}",
                "ports": [f"{port}:11434"],
                "volumes": [f"ollama-{name}-data:/root/.ollama"],
                "environment": ["OLLAMA_HOST=0.0.0.0"],
                "restart": "unless-stopped",
            }
        },
        "volumes": {f"ollama-{name}-data": {}},
    }

    # Add GPU configuration
    if gpu_count > 0:
        config["services"][f"ollama-{name}"]["deploy"] = {
            "resources": {
                "reservations": {
                    "devices": [
                        {
                            "driver": gpu_config.get("driver", "nvidia"),
                            "count": gpu_count,
                            "capabilities": ["gpu"],
                        }
                    ]
                }
            }
        }

    return config


def generate_k8s_manifest(name: str, gpu_config: dict, resource_limits: dict) -> list:
    """Generate Kubernetes manifests for Ollama."""
    gpu_count = gpu_config.get("count", 0)

    deployment: dict[str, Any] = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": f"ollama-{name}", "namespace": "waddleai"},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": f"ollama-{name}"}},
            "template": {
                "metadata": {"labels": {"app": f"ollama-{name}"}},
                "spec": {
                    "containers": [
                        {
                            "name": "ollama",
                            "image": "ollama/ollama:latest",
                            "ports": [{"containerPort": 11434}],
                            "volumeMounts": [{"name": "ollama-data", "mountPath": "/root/.ollama"}],
                            "resources": {
                                "limits": {
                                    "cpu": resource_limits.get("cpu", "4"),
                                    "memory": resource_limits.get("memory", "8Gi"),
                                }
                            },
                        }
                    ],
                    "volumes": [
                        {
                            "name": "ollama-data",
                            "persistentVolumeClaim": {"claimName": f"ollama-{name}-pvc"},
                        }
                    ],
                },
            },
        },
    }

    # Add GPU resources
    if gpu_count > 0:
        deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"][
            "nvidia.com/gpu"
        ] = gpu_count

    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": f"ollama-{name}", "namespace": "waddleai"},
        "spec": {
            "selector": {"app": f"ollama-{name}"},
            "ports": [{"port": 11434, "targetPort": 11434}],
        },
    }

    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": f"ollama-{name}-pvc", "namespace": "waddleai"},
        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "50Gi"}}},
    }

    return [deployment, service, pvc]


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/metallb-service", methods=["GET"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
async def export_metallb_service(deployment_id):
    """Export MetalLB-compatible LoadBalancer Service for Ollama deployment.

    Returns a single LoadBalancer Service with model annotations.
    """
    from ...services.ollama_manager import OllamaDeploymentManager

    def _generate():
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return None, None

        manager = OllamaDeploymentManager(db)
        service_yaml = manager.generate_metallb_service(deployment_id)
        return deployment, service_yaml

    deployment, service_yaml = await asyncio.to_thread(_generate)

    if not deployment:
        return jsonify({"error": "Deployment not found"}), 404

    if not service_yaml:
        return jsonify({"error": "No models assigned to deployment"}), 400

    return Response(
        service_yaml,
        mimetype="text/yaml",
        headers={
            "Content-Disposition": f"attachment; filename=ollama-{deployment.name}-metallb.yml"
        },
    )


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/metallb-model-services", methods=["GET"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
async def export_metallb_model_services(deployment_id):
    """Export individual MetalLB Services for each model on deployment.

    This creates separate LoadBalancer IPs for each model, enabling
    direct model-to-IP routing:
    - llama3.2 → 192.168.1.100:11434
    - mistral → 192.168.1.101:11434
    """
    from ...services.ollama_manager import OllamaDeploymentManager

    def _generate():
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return None, None

        manager = OllamaDeploymentManager(db)
        services_yaml = manager.generate_model_specific_metallb_services(deployment_id)
        return deployment, services_yaml

    deployment, services_yaml = await asyncio.to_thread(_generate)

    if not deployment:
        return jsonify({"error": "Deployment not found"}), 404

    if not services_yaml:
        return jsonify({"error": "No models assigned to deployment"}), 400

    filename = f"ollama-{deployment.name}-models-metallb.yml"
    return Response(
        services_yaml,
        mimetype="text/yaml",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@api_v1_bp.route("/ollama/export/metallb-all", methods=["GET"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
async def export_all_metallb_services():
    """Export MetalLB configuration for all Ollama deployments.

    Returns complete YAML with model-specific LoadBalancer Services
    for all active deployments.
    """
    from ...services.ollama_manager import OllamaDeploymentManager

    def _export():
        manager = OllamaDeploymentManager(db)
        return manager.export_metallb_config()

    config_yaml = await asyncio.to_thread(_export)

    if not config_yaml:
        return jsonify({"error": "No active Ollama deployments with models"}), 404

    return Response(
        config_yaml,
        mimetype="text/yaml",
        headers={"Content-Disposition": "attachment; filename=ollama-metallb-all.yml"},
    )
