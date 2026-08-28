"""WaddleAI Management API v1 - Ollama Deployment Management Endpoints."""

import asyncio
from datetime import datetime

import yaml
from quart import Response, current_app, jsonify, request

from shared.auth.rbac import Permission

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth, require_scope


@api_v1_bp.route("/ollama/deployments", methods=["GET"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
async def list_ollama_deployments():
    """List all Ollama deployments."""
    if not current_app.config.get("ENABLE_OLLAMA_MANAGEMENT", True):
        return jsonify({"error": "Ollama management is disabled"}), 403

    def _fetch():
        deployments = db(db.ollama_deployments.id > 0).select()
        return [(d, db(db.ollama_models.deployment_id == d.id).count()) for d in deployments]

    deployment_rows = await asyncio.to_thread(_fetch)

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

    return jsonify({"deployments": result, "total": len(result)})


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>", methods=["GET"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
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

    return jsonify(
        {
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
        }
    )


@api_v1_bp.route("/ollama/deployments", methods=["POST"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
async def create_ollama_deployment():
    """Create a new Ollama deployment."""
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    required_fields = ["name", "endpoint_url"]
    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"{field} is required"}), 400

    deployment_type = data.get("deployment_type", "external")
    gpu_config = data.get("gpu_config", {"count": 0, "driver": "nvidia"})
    resource_limits = data.get("resource_limits", {"cpu": "4", "memory": "8G"})

    # Generate docker-compose config if type is docker
    docker_compose_config = None
    if deployment_type == "docker":
        docker_compose_config = generate_docker_compose_config(
            name=data["name"], gpu_config=gpu_config, resource_limits=resource_limits
        )

    def _create():
        existing = db(db.ollama_deployments.name == data["name"]).select().first()
        if existing:
            return "name_conflict", None

        new_id = db.ollama_deployments.insert(
            name=data["name"],
            endpoint_url=data["endpoint_url"],
            deployment_type=deployment_type,
            docker_compose_config=docker_compose_config,
            gpu_config=gpu_config,
            resource_limits=resource_limits,
            status="unknown",
            auto_start=data.get("auto_start", True),
            created_at=datetime.utcnow(),
        )
        db.commit()
        return "ok", new_id

    result, deployment_id = await asyncio.to_thread(_create)

    if result == "name_conflict":
        return jsonify({"error": "Deployment name already exists"}), 409

    return (
        jsonify(
            {
                "id": deployment_id,
                "name": data["name"],
                "deployment_type": deployment_type,
                "message": "Deployment created successfully",
            }
        ),
        201,
    )


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
async def update_ollama_deployment(deployment_id):
    """Update Ollama deployment."""
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    def _update():
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()

        if not deployment:
            return "not_found"

        update_fields = {}

        if "name" in data:
            existing = (
                db(
                    (db.ollama_deployments.name == data["name"])
                    & (db.ollama_deployments.id != deployment_id)
                )
                .select()
                .first()
            )
            if existing:
                return "name_conflict"
            update_fields["name"] = data["name"]

        if "endpoint_url" in data:
            update_fields["endpoint_url"] = data["endpoint_url"]

        if "gpu_config" in data:
            update_fields["gpu_config"] = data["gpu_config"]

        if "resource_limits" in data:
            update_fields["resource_limits"] = data["resource_limits"]

        if "auto_start" in data:
            update_fields["auto_start"] = data["auto_start"]

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

    return jsonify({"message": "Deployment updated successfully"})


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
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

    return jsonify({"message": "Deployment deleted successfully"})


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/start", methods=["POST"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
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

    return jsonify(
        {
            "deployment_id": deployment_id,
            "status": "running",
            "message": "Deployment started successfully",
        }
    )


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/stop", methods=["POST"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
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

    return jsonify(
        {
            "deployment_id": deployment_id,
            "status": "stopped",
            "message": "Deployment stopped successfully",
        }
    )


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/restart", methods=["POST"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
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

    return jsonify(
        {
            "deployment_id": deployment_id,
            "status": "running",
            "message": "Deployment restarted successfully",
        }
    )


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/health", methods=["GET"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
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

    return jsonify(
        {
            "deployment_id": deployment_id,
            "endpoint_url": deployment.endpoint_url,
            "health_status": result.get("status", "unknown"),
            "healthy": result.get("healthy", False),
            "checked_at": result.get("checked_at", datetime.utcnow().isoformat()),
            "error": result.get("error"),
        }
    )


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/logs", methods=["GET"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
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
    return jsonify(
        {
            "deployment_id": deployment_id,
            "lines": lines,
            "logs": "Log retrieval not yet implemented",
        }
    )


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/models/pull", methods=["POST"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
async def pull_ollama_model(deployment_id):
    """Pull a model to Ollama deployment."""
    from ...services.ollama_manager import OllamaDeploymentManager

    def _check_deployment():
        return db(db.ollama_deployments.id == deployment_id).select().first()

    deployment = await asyncio.to_thread(_check_deployment)
    if not deployment:
        return jsonify({"error": "Deployment not found"}), 404

    data = await request.get_json()
    if not data or "model" not in data:
        return jsonify({"error": "model is required"}), 400

    model_name = data["model"]
    model_tag = data.get("tag", "latest")
    full_model = f"{model_name}:{model_tag}" if model_tag != "latest" else model_name

    def _pull():
        manager = OllamaDeploymentManager(db)
        return manager.pull_model(deployment_id, full_model)

    result = await asyncio.to_thread(_pull)

    if result.error:
        return jsonify({"error": result.error, "model": full_model, "status": result.status}), 500

    return jsonify(
        {
            "deployment_id": deployment_id,
            "model": full_model,
            "status": result.status,
            "completed": result.completed,
            "message": "Model pulled successfully" if result.completed else "Model pull initiated",
        }
    )


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/models/<model_name>", methods=["DELETE"])
@require_auth
@require_scope(Permission.OLLAMA_ADMIN)
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

    return jsonify(
        {
            "deployment_id": deployment_id,
            "model": model_name,
            "message": "Model removed successfully",
        }
    )


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

    config = {
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

    deployment = {
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
