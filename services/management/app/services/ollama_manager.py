"""
WaddleAI Ollama Deployment Manager

Manages Ollama deployments in two modes:
- Manual: Generate docker-compose/k8s manifests for user deployment
- Orchestrated: Directly manage containers via Docker API
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx
import yaml

from shared.fleet.base import (
    BackendType,
    Endpoint,
    FleetHealth,
    InferenceFleetBackend,
    ManagementScope,
    ModelPlacement,
    NodeInfo,
    ProvisionSpec,
)
from shared.fleet.registry import register

logger = logging.getLogger(__name__)


class DeploymentMode(str, Enum):
    """Ollama deployment management mode"""

    MANUAL = "manual"  # Generate configs only
    ORCHESTRATED = "orchestrated"  # Manage containers directly
    BOTH = "both"  # Support both modes


class DeploymentStatus(str, Enum):
    """Ollama deployment status"""

    PENDING = "pending"
    RUNNING = "running"
    STOPPED = "stopped"
    PULLING = "pulling"
    ERROR = "error"
    UNKNOWN = "unknown"


class HealthStatus(str, Enum):
    """Ollama health status"""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class OllamaDeploymentConfig:
    """Configuration for an Ollama deployment"""

    name: str
    endpoint_url: str = "http://localhost:11434"
    deployment_type: str = "docker"  # docker, kubernetes, kubernetes-daemonset, external
    port: int = 11434
    gpu_count: int = 0
    gpu_ids: List[str] = field(default_factory=list)
    cpu_limit: str = "4"
    memory_limit: str = "8g"
    auto_start: bool = True
    environment: Dict[str, str] = field(default_factory=dict)
    volumes: Dict[str, str] = field(default_factory=dict)
    # Kubernetes DaemonSet fields
    node_selector: Dict[str, str] = field(default_factory=lambda: {"gpu": "true"})
    tolerations: List[Dict] = field(default_factory=lambda: [
        {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
    ])
    shared_storage_size: str = "200Gi"
    pvc_access_mode: str = "ReadWriteMany"
    storage_class: str = ""
    namespace: str = "waddleai"
    # Pool mode (Deployment+nodeSelector, see generate_pool_manifest) vs the
    # default DaemonSet mode — mixed-GPU clusters use pool mode to pin a
    # fixed replica count to a specific node class instead of one pod/node.
    pool_mode: bool = False
    replicas: int = 1


@dataclass(slots=True)
class OllamaModel:
    """Ollama model information"""

    name: str
    size: int = 0
    digest: str = ""
    modified_at: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PullStatus:
    """Model pull operation status"""

    model: str
    status: str
    progress: float = 0.0
    completed: bool = False
    error: Optional[str] = None


@register(BackendType.OLLAMA)
class OllamaDeploymentManager(InferenceFleetBackend):
    """
    Manages Ollama deployments in manual or orchestrated mode.

    Manual mode: Generates docker-compose.yml and Kubernetes manifests
    Orchestrated mode: Directly manages containers via Docker API

    Implements ``InferenceFleetBackend`` (spec §10.1) by wrapping the
    existing sync PyDAL/Docker/HTTP methods below in ``asyncio.to_thread`` —
    a restructure, not a rewrite; every pre-existing method keeps its exact
    signature and behavior for the legacy ``api/v1/ollama.py`` routes.
    """

    type = BackendType.OLLAMA
    management_scope = ManagementScope.FULL_LIFECYCLE

    def __init__(
        self,
        db,
        mode: DeploymentMode = DeploymentMode.BOTH,
        docker_host: str = "unix:///var/run/docker.sock",
        *,
        config: dict[str, Any] | None = None,
        credentials: str | None = None,
    ):
        self.db = db
        self.mode = mode
        self.docker_host = docker_host
        self._docker_client = None
        # registry.build_backend construction contract (§10.1 Task 3). Ollama
        # has no cloud credentials of its own; `credentials` is accepted for
        # interface uniformity and currently unused.
        self.config = config or {}
        self.credentials = credentials

    @property
    def docker_client(self):
        """Lazy-load Docker client"""
        if self._docker_client is None and self.mode != DeploymentMode.MANUAL:
            try:
                import docker

                self._docker_client = docker.from_env()
            except Exception as e:
                logger.warning(f"Failed to initialize Docker client: {e}")
        return self._docker_client

    # Deployment CRUD Operations

    def create_deployment(self, config: OllamaDeploymentConfig) -> Dict[str, Any]:
        """Create a new Ollama deployment"""
        db = self.db

        # Check for duplicate name
        existing = db(db.ollama_deployments.name == config.name).select().first()
        if existing:
            return {"success": False, "error": "Deployment with this name already exists"}

        # Create deployment record
        deployment_id = db.ollama_deployments.insert(
            name=config.name,
            endpoint_url=config.endpoint_url,
            deployment_type=config.deployment_type,
            docker_compose_config=self._generate_docker_config(config),
            gpu_config={"gpu_count": config.gpu_count, "gpu_ids": config.gpu_ids},
            resource_limits={"cpu_limit": config.cpu_limit, "memory_limit": config.memory_limit},
            status="pending",
            health_status="unknown",
            auto_start=config.auto_start,
            pool_mode=config.pool_mode,
            created_at=datetime.utcnow(),
        )
        db.commit()

        logger.info(f"Created Ollama deployment: {config.name}")
        return {
            "success": True,
            "deployment_id": deployment_id,
            "name": config.name,
            "message": "Deployment created successfully",
        }

    def update_deployment(self, deployment_id: int, config: OllamaDeploymentConfig) -> Dict[str, Any]:
        """Update an existing Ollama deployment"""
        db = self.db

        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return {"success": False, "error": "Deployment not found"}

        db(db.ollama_deployments.id == deployment_id).update(
            name=config.name,
            endpoint_url=config.endpoint_url,
            deployment_type=config.deployment_type,
            docker_compose_config=self._generate_docker_config(config),
            gpu_config={"gpu_count": config.gpu_count, "gpu_ids": config.gpu_ids},
            resource_limits={"cpu_limit": config.cpu_limit, "memory_limit": config.memory_limit},
            auto_start=config.auto_start,
            pool_mode=config.pool_mode,
        )
        db.commit()

        logger.info(f"Updated Ollama deployment: {config.name}")
        return {"success": True, "deployment_id": deployment_id, "message": "Deployment updated successfully"}

    def delete_deployment(self, deployment_id: int) -> Dict[str, Any]:
        """Delete an Ollama deployment"""
        db = self.db

        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return {"success": False, "error": "Deployment not found"}

        # Stop if running (orchestrated mode)
        if self.mode != DeploymentMode.MANUAL and deployment.status == "running":
            self.stop_deployment(deployment_id)

        # Delete associated models
        db(db.ollama_models.deployment_id == deployment_id).delete()

        # Delete deployment
        db(db.ollama_deployments.id == deployment_id).delete()
        db.commit()

        logger.info(f"Deleted Ollama deployment: {deployment.name}")
        return {"success": True, "message": "Deployment deleted successfully"}

    # Config Generation (Manual Mode)

    def _generate_docker_config(self, config: OllamaDeploymentConfig) -> Dict[str, Any]:
        """Generate Docker configuration for deployment"""
        service_config = {
            "image": "ollama/ollama:latest",
            "container_name": f"waddleai-ollama-{config.name}",
            "ports": [f"{config.port}:11434"],
            "environment": {"OLLAMA_HOST": "0.0.0.0", **config.environment},
            "volumes": [f"ollama-{config.name}-data:/root/.ollama", *[f"{k}:{v}" for k, v in config.volumes.items()]],
            "restart": "unless-stopped",
        }

        # Add GPU support if configured
        if config.gpu_count > 0:
            service_config["deploy"] = {
                "resources": {
                    "reservations": {
                        "devices": [{"driver": "nvidia", "count": config.gpu_count, "capabilities": ["gpu"]}]
                    }
                }
            }

        # Add resource limits
        if config.cpu_limit or config.memory_limit:
            if "deploy" not in service_config:
                service_config["deploy"] = {"resources": {}}
            service_config["deploy"]["resources"]["limits"] = {}
            if config.cpu_limit:
                service_config["deploy"]["resources"]["limits"]["cpus"] = config.cpu_limit
            if config.memory_limit:
                service_config["deploy"]["resources"]["limits"]["memory"] = config.memory_limit

        return service_config

    def generate_docker_compose(self, deployment_id: int) -> str:
        """Generate docker-compose.yml for deployment"""
        db = self.db

        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return ""

        compose = {
            "version": "3.8",
            "services": {f"ollama-{deployment.name}": deployment.docker_compose_config},
            "volumes": {f"ollama-{deployment.name}-data": {}},
        }

        return yaml.dump(compose, default_flow_style=False, sort_keys=False)

    def generate_k8s_manifest(self, deployment_id: int) -> str:
        """Generate Kubernetes manifest for deployment"""
        db = self.db

        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return ""

        gpu_config = deployment.gpu_config or {}
        resource_limits = deployment.resource_limits or {}
        gpu_count = gpu_config.get("gpu_count", 0)

        # Parse port from endpoint URL
        from urllib.parse import urlparse

        parsed = urlparse(deployment.endpoint_url)
        _ = parsed.port or 11434

        # Deployment manifest
        k8s_deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": f"ollama-{deployment.name}",
                "namespace": "waddleai",
                "labels": {"app": f"ollama-{deployment.name}", "managed-by": "waddleai"},
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": f"ollama-{deployment.name}"}},
                "template": {
                    "metadata": {"labels": {"app": f"ollama-{deployment.name}"}},
                    "spec": {
                        "containers": [
                            {
                                "name": "ollama",
                                "image": "ollama/ollama:latest",
                                "ports": [{"containerPort": 11434}],
                                "env": [{"name": "OLLAMA_HOST", "value": "0.0.0.0"}],
                                "volumeMounts": [{"name": "ollama-data", "mountPath": "/root/.ollama"}],
                                "resources": {"limits": {}},
                            }
                        ],
                        "volumes": [
                            {
                                "name": "ollama-data",
                                "persistentVolumeClaim": {"claimName": f"ollama-{deployment.name}-pvc"},
                            }
                        ],
                    },
                },
            },
        }

        # Add resource limits
        limits = k8s_deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
        if resource_limits.get("cpu_limit"):
            limits["cpu"] = resource_limits["cpu_limit"]
        if resource_limits.get("memory_limit"):
            limits["memory"] = resource_limits["memory_limit"]
        if gpu_count > 0:
            limits["nvidia.com/gpu"] = str(gpu_count)

        # Service manifest
        k8s_service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": f"ollama-{deployment.name}", "namespace": "waddleai"},
            "spec": {"selector": {"app": f"ollama-{deployment.name}"}, "ports": [{"port": 11434, "targetPort": 11434}]},
        }

        # PVC manifest
        k8s_pvc = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": f"ollama-{deployment.name}-pvc", "namespace": "waddleai"},
            "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "50Gi"}}},
        }

        # Combine all manifests
        manifests = [k8s_pvc, k8s_deployment, k8s_service]
        return "---\n".join(yaml.dump(m, default_flow_style=False) for m in manifests)

    def generate_metallb_service(self, deployment_id: int) -> str:
        """
        Generate MetalLB-compatible Kubernetes Service manifest with model labels.

        Creates a LoadBalancer Service with model annotations for intelligent routing.
        """
        db = self.db

        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return ""

        # Get all models on this deployment
        models = db(db.ollama_models.deployment_id == deployment_id).select()
        model_names = [m.model_name for m in models]

        # MetalLB Service with model labels
        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": f"ollama-{deployment.name}-lb",
                "namespace": "waddleai",
                "labels": {"app": f"ollama-{deployment.name}", "managed-by": "waddleai", "service-type": "ollama"},
                "annotations": {
                    # MetalLB configuration
                    "metallb.universe.tf/allow-shared-ip": f"ollama-{deployment.name}",
                    # Model routing information
                    "waddleai.io/models": ",".join(model_names),
                    "waddleai.io/deployment-id": str(deployment_id),
                    "waddleai.io/deployment-name": deployment.name,
                },
            },
            "spec": {
                "type": "LoadBalancer",
                "selector": {"app": f"ollama-{deployment.name}"},
                "ports": [{"name": "ollama-api", "protocol": "TCP", "port": 11434, "targetPort": 11434}],
                "sessionAffinity": "ClientIP",
                "sessionAffinityConfig": {"clientIP": {"timeoutSeconds": 3600}},
            },
        }

        return yaml.dump(service, default_flow_style=False)

    def generate_model_specific_metallb_services(self, deployment_id: int) -> str:
        """
        Generate individual MetalLB Services for each model on a deployment.

        This creates separate LoadBalancer IPs for each model, enabling
        direct model-to-IP routing without HTTP inspection.

        Example:
        - llama3.2 → 192.168.1.100:11434
        - mistral → 192.168.1.101:11434
        """
        db = self.db

        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return ""

        # Get all models on this deployment
        models = db(db.ollama_models.deployment_id == deployment_id).select()

        if not models:
            return ""

        services = []

        for model in models:
            # Create a Service for each model
            service = {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": f"ollama-{deployment.name}-{model.model_name.replace('.', '-')}",
                    "namespace": "waddleai",
                    "labels": {
                        "app": f"ollama-{deployment.name}",
                        "managed-by": "waddleai",
                        "service-type": "ollama",
                        "model": model.model_name,
                    },
                    "annotations": {
                        # MetalLB configuration - unique IP per model
                        "metallb.universe.tf/allow-shared-ip": f"ollama-{deployment.name}-{model.model_name}",
                        # Model routing information
                        "waddleai.io/model": model.model_name,
                        "waddleai.io/model-tag": model.model_tag or "latest",
                        "waddleai.io/deployment-id": str(deployment_id),
                        "waddleai.io/deployment-name": deployment.name,
                        "waddleai.io/model-id": str(model.id),
                    },
                },
                "spec": {
                    "type": "LoadBalancer",
                    "selector": {"app": f"ollama-{deployment.name}"},
                    "ports": [{"name": "ollama-api", "protocol": "TCP", "port": 11434, "targetPort": 11434}],
                    "sessionAffinity": "ClientIP",
                    "sessionAffinityConfig": {"clientIP": {"timeoutSeconds": 3600}},
                },
            }

            services.append(service)

        return "---\n".join(yaml.dump(s, default_flow_style=False) for s in services)

    def export_metallb_config(self) -> str:
        """
        Export complete MetalLB configuration for all Ollama deployments.

        Returns YAML with all LoadBalancer Services across all deployments.
        """
        db = self.db

        deployments = db(db.ollama_deployments.status.belongs(["running", "pending"])).select()

        all_services = []

        for deployment in deployments:
            # Get model-specific services
            model_services_yaml = self.generate_model_specific_metallb_services(deployment.id)
            if model_services_yaml:
                # Parse and add to list
                import yaml

                services = list(yaml.safe_load_all(model_services_yaml))
                all_services.extend(services)

        if not all_services:
            return ""

        return "---\n".join(yaml.dump(s, default_flow_style=False) for s in all_services)

    def _ollama_pvc_and_container(self, deployment) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build the shared RWX-PVC + hardened container specs for DaemonSet/pool manifests.

        Factored out so ``generate_daemonset_manifest`` and
        ``generate_pool_manifest`` render the identical hardened container
        (non-root, dropped caps, health probes) from one place.
        """
        gpu_config = deployment.gpu_config or {}
        resource_limits = deployment.resource_limits or {}
        gpu_count = gpu_config.get("gpu_count", gpu_config.get("count", 1))
        storage_size = resource_limits.get("shared_storage_size", "200Gi")
        storage_class = gpu_config.get("storage_class", "")
        namespace = deployment.namespace if hasattr(deployment, "namespace") else "waddleai"

        pvc: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": f"ollama-{deployment.name}-models",
                "namespace": namespace,
                "labels": {"app": f"ollama-{deployment.name}", "managed-by": "waddleai"},
            },
            "spec": {
                "accessModes": ["ReadWriteMany"],
                "resources": {"requests": {"storage": storage_size}},
            },
        }
        if storage_class:
            pvc["spec"]["storageClassName"] = storage_class

        container: dict[str, Any] = {
            "name": "ollama",
            "image": "ollama/ollama:0.7.1",
            "ports": [{"containerPort": 11434, "name": "http"}],
            "env": [
                {"name": "OLLAMA_HOST", "value": "0.0.0.0"},
                {"name": "OLLAMA_MODELS", "value": "/models"},
                {"name": "HOME", "value": "/tmp"},
            ],
            "volumeMounts": [
                {"name": "ollama-models", "mountPath": "/models"},
                {"name": "tmp", "mountPath": "/tmp"},
            ],
            "resources": {
                "requests": {
                    "memory": resource_limits.get("memory_limit", "16Gi"),
                    "cpu": resource_limits.get("cpu_limit", "4"),
                    "nvidia.com/gpu": str(gpu_count),
                },
                "limits": {
                    "memory": resource_limits.get("memory_limit", "16Gi"),
                    "cpu": resource_limits.get("cpu_limit", "4"),
                    "nvidia.com/gpu": str(gpu_count),
                },
            },
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": False,
                "capabilities": {"drop": ["ALL"]},
            },
            "livenessProbe": {
                "httpGet": {"path": "/", "port": 11434},
                "initialDelaySeconds": 60,
                "periodSeconds": 30,
                "timeoutSeconds": 15,
                "failureThreshold": 5,
            },
            "readinessProbe": {
                "httpGet": {"path": "/", "port": 11434},
                "initialDelaySeconds": 30,
                "periodSeconds": 10,
                "timeoutSeconds": 5,
                "failureThreshold": 3,
            },
        }
        return pvc, container

    def generate_daemonset_manifest(self, deployment_id: int) -> str:
        """
        Generate Kubernetes DaemonSet manifest for multi-GPU-node Ollama deployment.

        Creates DaemonSet (one pod per GPU node) + shared ReadWriteMany PVC
        so models are stored once and served from all GPU nodes.
        """
        db = self.db

        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return ""

        gpu_config = deployment.gpu_config or {}
        namespace = deployment.namespace if hasattr(deployment, "namespace") else "waddleai"
        tolerations = gpu_config.get("tolerations", [
            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
        ])
        node_selector = gpu_config.get("node_selector", {"gpu": "true"})
        pvc, container = self._ollama_pvc_and_container(deployment)

        daemonset = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {
                "name": f"ollama-{deployment.name}",
                "namespace": namespace,
                "labels": {"app": f"ollama-{deployment.name}", "managed-by": "waddleai"},
            },
            "spec": {
                "selector": {"matchLabels": {"app": f"ollama-{deployment.name}"}},
                "template": {
                    "metadata": {"labels": {"app": f"ollama-{deployment.name}"}},
                    "spec": {
                        "securityContext": {"fsGroup": 1000, "runAsNonRoot": True, "runAsUser": 1000},
                        "nodeSelector": node_selector,
                        "tolerations": tolerations,
                        "containers": [container],
                        "volumes": [
                            {
                                "name": "ollama-models",
                                "persistentVolumeClaim": {"claimName": f"ollama-{deployment.name}-models"},
                            },
                            {"name": "tmp", "emptyDir": {}},
                        ],
                    },
                },
            },
        }

        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": f"ollama-{deployment.name}",
                "namespace": namespace,
                "labels": {"app": f"ollama-{deployment.name}", "managed-by": "waddleai"},
            },
            "spec": {
                "type": "ClusterIP",
                "selector": {"app": f"ollama-{deployment.name}"},
                "ports": [{"name": "http", "port": 11434, "targetPort": 11434, "protocol": "TCP"}],
            },
        }

        manifests = [pvc, daemonset, service]
        return "---\n".join(yaml.dump(m, default_flow_style=False) for m in manifests)

    def generate_pool_manifest(self, deployment_id: int) -> str:
        """Generate a pool-mode (Deployment+nodeSelector) manifest, for mixed-GPU clusters.

        Same shared RWX PVC + hardened container as ``generate_daemonset_manifest``,
        but a fixed ``replicas`` count pinned to a node class via ``nodeSelector``
        instead of one pod per matching node — set ``pool_mode=True`` on the
        deployment's config to select this mode (migration 013).
        """
        db = self.db

        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return ""

        gpu_config = deployment.gpu_config or {}
        namespace = deployment.namespace if hasattr(deployment, "namespace") else "waddleai"
        node_selector = gpu_config.get("node_selector", {"gpu": "true"})
        replicas = gpu_config.get("replicas", 1)
        pvc, container = self._ollama_pvc_and_container(deployment)

        pool_deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": f"ollama-{deployment.name}",
                "namespace": namespace,
                "labels": {"app": f"ollama-{deployment.name}", "managed-by": "waddleai"},
            },
            "spec": {
                "replicas": replicas,
                "selector": {"matchLabels": {"app": f"ollama-{deployment.name}"}},
                "template": {
                    "metadata": {"labels": {"app": f"ollama-{deployment.name}"}},
                    "spec": {
                        "securityContext": {
                            "fsGroup": 1000, "runAsNonRoot": True, "runAsUser": 1000,
                        },
                        "nodeSelector": node_selector,
                        "containers": [container],
                        "volumes": [
                            {
                                "name": "ollama-models",
                                "persistentVolumeClaim": {
                                    "claimName": f"ollama-{deployment.name}-models",
                                },
                            },
                            {"name": "tmp", "emptyDir": {}},
                        ],
                    },
                },
            },
        }

        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": f"ollama-{deployment.name}",
                "namespace": namespace,
                "labels": {"app": f"ollama-{deployment.name}", "managed-by": "waddleai"},
            },
            "spec": {
                "type": "ClusterIP",
                "selector": {"app": f"ollama-{deployment.name}"},
                "ports": [{"name": "http", "port": 11434, "targetPort": 11434, "protocol": "TCP"}],
            },
        }

        manifests = [pvc, pool_deployment, service]
        return "---\n".join(yaml.dump(m, default_flow_style=False) for m in manifests)

    # Container Management (Orchestrated Mode)

    def start_deployment(self, deployment_id: int) -> Dict[str, Any]:
        """Start an Ollama deployment (orchestrated mode)"""
        if self.mode == DeploymentMode.MANUAL:
            return {"success": False, "error": "Orchestrated mode not enabled"}

        db = self.db
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return {"success": False, "error": "Deployment not found"}

        if not self.docker_client:
            return {"success": False, "error": "Docker client not available"}

        try:
            container_name = f"waddleai-ollama-{deployment.name}"

            # Check if container exists
            try:
                container = self.docker_client.containers.get(container_name)
                if container.status != "running":
                    container.start()
            except Exception:
                # Create and start new container
                config = deployment.docker_compose_config or {}

                # Build run parameters
                run_params = {
                    "image": config.get("image", "ollama/ollama:latest"),
                    "name": container_name,
                    "detach": True,
                    "environment": config.get("environment", {}),
                    "ports": {"11434/tcp": deployment.endpoint_url.split(":")[-1] or 11434},
                    "restart_policy": {"Name": "unless-stopped"},
                }

                # Add volumes
                volumes = config.get("volumes", [])
                if volumes:
                    run_params["volumes"] = {}
                    for vol in volumes:
                        if ":" in vol:
                            src, dst = vol.split(":", 1)
                            run_params["volumes"][src] = {"bind": dst, "mode": "rw"}

                self.docker_client.containers.run(**run_params)

            # Update status
            db(db.ollama_deployments.id == deployment_id).update(status="running", health_status="healthy")
            db.commit()

            logger.info(f"Started Ollama deployment: {deployment.name}")
            return {"success": True, "message": "Deployment started"}

        except Exception as e:
            logger.error(f"Failed to start deployment: {e}")
            db(db.ollama_deployments.id == deployment_id).update(status="error", health_status="unhealthy")
            db.commit()
            return {"success": False, "error": str(e)}

    def stop_deployment(self, deployment_id: int) -> Dict[str, Any]:
        """Stop an Ollama deployment (orchestrated mode)"""
        if self.mode == DeploymentMode.MANUAL:
            return {"success": False, "error": "Orchestrated mode not enabled"}

        db = self.db
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return {"success": False, "error": "Deployment not found"}

        if not self.docker_client:
            return {"success": False, "error": "Docker client not available"}

        try:
            container_name = f"waddleai-ollama-{deployment.name}"
            container = self.docker_client.containers.get(container_name)
            container.stop()

            db(db.ollama_deployments.id == deployment_id).update(status="stopped", health_status="unknown")
            db.commit()

            logger.info(f"Stopped Ollama deployment: {deployment.name}")
            return {"success": True, "message": "Deployment stopped"}

        except Exception as e:
            logger.error(f"Failed to stop deployment: {e}")
            return {"success": False, "error": str(e)}

    def restart_deployment(self, deployment_id: int) -> Dict[str, Any]:
        """Restart an Ollama deployment (orchestrated mode)"""
        stop_result = self.stop_deployment(deployment_id)
        if not stop_result.get("success"):
            return stop_result
        return self.start_deployment(deployment_id)

    def get_logs(self, deployment_id: int, lines: int = 100) -> str:
        """Get container logs (orchestrated mode)"""
        if self.mode == DeploymentMode.MANUAL:
            return "Orchestrated mode not enabled"

        db = self.db
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return "Deployment not found"

        if not self.docker_client:
            return "Docker client not available"

        try:
            container_name = f"waddleai-ollama-{deployment.name}"
            container = self.docker_client.containers.get(container_name)
            return container.logs(tail=lines).decode("utf-8")
        except Exception as e:
            return f"Error: {e}"

    # Health and Monitoring

    def health_check(self, deployment_id: int) -> Dict[str, Any]:
        """Perform health check on Ollama deployment"""
        db = self.db

        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return {"healthy": False, "status": "not_found"}

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(f"{deployment.endpoint_url}/api/tags")
                healthy = response.status_code == 200

                # Update health status
                db(db.ollama_deployments.id == deployment_id).update(
                    health_status="healthy" if healthy else "unhealthy", last_health_check=datetime.utcnow()
                )
                db.commit()

                return {
                    "healthy": healthy,
                    "status": "healthy" if healthy else "unhealthy",
                    "endpoint": deployment.endpoint_url,
                    "checked_at": datetime.utcnow().isoformat(),
                }
        except Exception as e:
            db(db.ollama_deployments.id == deployment_id).update(
                health_status="unhealthy", last_health_check=datetime.utcnow()
            )
            db.commit()
            return {"healthy": False, "status": "error", "error": str(e)}

    # Model Management

    def list_models(self, deployment_id: int) -> List[OllamaModel]:
        """List models on an Ollama deployment"""
        db = self.db

        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return []

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(f"{deployment.endpoint_url}/api/tags")
                if response.status_code != 200:
                    return []

                data = response.json()
                models = []
                for m in data.get("models", []):
                    models.append(
                        OllamaModel(
                            name=m.get("name", ""),
                            size=m.get("size", 0),
                            digest=m.get("digest", ""),
                            modified_at=m.get("modified_at", ""),
                            details=m.get("details", {}),
                        )
                    )
                return models
        except Exception as e:
            logger.error(f"Failed to list models: {e}")
            return []

    def pull_model(self, deployment_id: int, model_name: str) -> PullStatus:
        """Pull a model to an Ollama deployment"""
        db = self.db

        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return PullStatus(model=model_name, status="error", error="Deployment not found")

        # Update deployment status
        db(db.ollama_deployments.id == deployment_id).update(status="pulling")
        db.commit()

        try:
            with httpx.Client(timeout=3600.0) as client:  # Long timeout for model pulls
                response = client.post(f"{deployment.endpoint_url}/api/pull", json={"name": model_name}, timeout=3600.0)

                if response.status_code == 200:
                    # Track model in database
                    existing = (
                        db((db.ollama_models.deployment_id == deployment_id) & (db.ollama_models.name == model_name))
                        .select()
                        .first()
                    )

                    if not existing:
                        db.ollama_models.insert(
                            deployment_id=deployment_id, name=model_name, size=0, pulled_at=datetime.utcnow()
                        )

                    db(db.ollama_deployments.id == deployment_id).update(status="running")
                    db.commit()

                    return PullStatus(model=model_name, status="completed", progress=100.0, completed=True)
                else:
                    db(db.ollama_deployments.id == deployment_id).update(status="running")
                    db.commit()
                    return PullStatus(model=model_name, status="error", error=f"HTTP {response.status_code}")

        except Exception as e:
            logger.error(f"Failed to pull model: {e}")
            db(db.ollama_deployments.id == deployment_id).update(status="running")
            db.commit()
            return PullStatus(model=model_name, status="error", error=str(e))

    def remove_model(self, deployment_id: int, model_name: str) -> bool:
        """Remove a model from an Ollama deployment"""
        db = self.db

        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return False

        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.delete(f"{deployment.endpoint_url}/api/delete", json={"name": model_name})

                if response.status_code == 200:
                    # Remove from database
                    db(
                        (db.ollama_models.deployment_id == deployment_id) & (db.ollama_models.name == model_name)
                    ).delete()
                    db.commit()
                    return True
                return False

        except Exception as e:
            logger.error(f"Failed to remove model: {e}")
            return False

    # InferenceFleetBackend interface (spec §10.1)
    #
    # Deployments are the unit this manager already tracks, so a deployment
    # *is* a fleet node at this layer: `node_id`/`Endpoint.node_id` are the
    # deployment name, `NodeInfo.node_uid` reads the migration-013 column
    # (populated by live K8s node introspection — a follow-up, not faked
    # here), and VRAM fields default to 0/"unknown" rather than invented
    # numbers when no metrics source is wired up yet.

    def _node_info_from_deployment(self, deployment, loaded_models: list[str]) -> NodeInfo:
        """Map an ``ollama_deployments`` row to a ``NodeInfo`` value object."""
        deployment_type = deployment.deployment_type or ""
        kind = "k8s" if deployment_type.startswith("kubernetes") else "external"
        return NodeInfo(
            node_id=deployment.name,
            node_uid=getattr(deployment, "node_uid", None),
            kind=kind,
            loaded_models=loaded_models,
            vram_total_mb=0,
            vram_free_mb=0,
            healthy=deployment.health_status == "healthy",
        )

    def _models_for_deployment(self, deployment_id: int) -> list[str]:
        """Return tracked model names for a deployment (DB-backed, not a live pull)."""
        db = self.db
        rows = db(db.ollama_models.deployment_id == deployment_id).select()
        return [row.model_name for row in rows]

    async def provision(self, spec: ProvisionSpec) -> list[NodeInfo]:
        """Create an Ollama deployment for ``spec`` and return its node.

        ``spec.models`` is not eagerly pulled here — call ``place_model``
        per model afterward so pulls stay lazy (§10.4).
        """

        def _provision() -> list[NodeInfo]:
            gpu_count = int(spec.constraints.get("gpu_count", 0))
            pool_mode = spec.mode == "pool"
            config = OllamaDeploymentConfig(
                name=spec.name,
                deployment_type="kubernetes-daemonset" if not pool_mode else "kubernetes",
                gpu_count=gpu_count,
                namespace=spec.constraints.get("namespace", "waddleai"),
                pool_mode=pool_mode,
                replicas=int(spec.constraints.get("replicas", 1)),
            )
            result = self.create_deployment(config)
            if not result.get("success"):
                raise RuntimeError(result.get("error") or "Ollama provision failed")
            db = self.db
            deployment = db(db.ollama_deployments.id == result["deployment_id"]).select().first()
            return [self._node_info_from_deployment(deployment, [])]

        return await asyncio.to_thread(_provision)

    async def deprovision(self, node_id: str) -> None:
        """Delete the deployment named ``node_id``. No-op if it no longer exists."""

        def _deprovision() -> None:
            db = self.db
            deployment = db(db.ollama_deployments.name == node_id).select().first()
            if deployment is None:
                return
            self.delete_deployment(deployment.id)

        await asyncio.to_thread(_deprovision)

    async def health(self) -> FleetHealth:
        """Aggregate ``health_check`` across every tracked deployment."""

        def _health() -> FleetHealth:
            db = self.db
            deployments = db(db.ollama_deployments.id > 0).select()
            total = 0
            healthy_count = 0
            for deployment in deployments:
                total += 1
                if self.health_check(deployment.id).get("healthy"):
                    healthy_count += 1
            return FleetHealth(
                backend_id=self.fleet_backend_id,
                healthy=(healthy_count == total),
                node_count=total,
                detail={"healthy_nodes": healthy_count},
            )

        return await asyncio.to_thread(_health)

    async def list_nodes(self) -> list[NodeInfo]:
        """Return every tracked Ollama deployment as a ``NodeInfo``."""

        def _list() -> list[NodeInfo]:
            db = self.db
            deployments = db(db.ollama_deployments.id > 0).select()
            return [
                self._node_info_from_deployment(d, self._models_for_deployment(d.id))
                for d in deployments
            ]

        return await asyncio.to_thread(_list)

    async def place_model(self, model: str, constraints: dict[str, Any]) -> ModelPlacement:
        """Pull ``model`` onto the target deployment (``node_id`` constraint, or first running)."""

        def _place() -> ModelPlacement:
            db = self.db
            node_id = constraints.get("node_id")
            if node_id:
                deployment = db(db.ollama_deployments.name == node_id).select().first()
            else:
                deployment = db(db.ollama_deployments.status == "running").select().first()
            if deployment is None:
                raise RuntimeError(f"No available Ollama deployment to place model {model!r}")

            result = self.pull_model(deployment.id, model)
            if result.error:
                raise RuntimeError(f"Failed to place model {model!r}: {result.error}")
            status = "placed" if result.completed else "pulling"
            return ModelPlacement(model=model, node_id=deployment.name, status=status)

        return await asyncio.to_thread(_place)

    async def endpoints_for(self, model: str) -> list[Endpoint]:
        """Return endpoints for deployments that currently have ``model`` loaded."""

        def _endpoints() -> list[Endpoint]:
            db = self.db
            deployments = db(db.ollama_deployments.id > 0).select()
            endpoints = []
            for deployment in deployments:
                loaded = self._models_for_deployment(deployment.id)
                if model not in loaded:
                    continue
                endpoints.append(
                    Endpoint(
                        url=deployment.endpoint_url,
                        node_id=deployment.name,
                        loaded_models=loaded,
                        healthy=deployment.health_status == "healthy",
                    )
                )
            return endpoints

        return await asyncio.to_thread(_endpoints)
