"""
llama.cpp Deployment Manager

Manages llama-server instances in two modes:
- kubernetes: Creates/removes K8s DaemonSets targeting GPU-labelled nodes
- remote:     Registers an existing llama-server endpoint after a health check
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

import requests
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

# Hardened image references — digest-pinned, security-vetted
LLAMACPP_IMAGE: str = (
    "ghcr.io/ggml-org/llama.cpp@sha256:"
    "51570a4f93c5ce81ac6f2b1ea16a58771cfded2adb34241df7e75329b24fe76e"
)
CURL_DOWNLOADER_IMAGE: str = (
    "curlimages/curl@sha256:"
    "7c12af72ceb38b7432ab85e1a265cff6ae58e06f95539d539b654f2cfa64bb13"
)


def get_k8s_apps_client():
    """Return a configured AppsV1Api client."""
    from kubernetes import client, config as k8s_config  # type: ignore[import]
    try:
        k8s_config.load_incluster_config()
    except Exception:
        logger.debug("Not running in-cluster, falling back to kubeconfig")
        k8s_config.load_kube_config()
    return client.AppsV1Api()


def get_k8s_core_client():
    """Return a configured CoreV1Api client."""
    from kubernetes import client, config as k8s_config  # type: ignore[import]
    try:
        k8s_config.load_incluster_config()
    except Exception:
        logger.debug("Not running in-cluster, falling back to kubeconfig")
        k8s_config.load_kube_config()
    return client.CoreV1Api()


@dataclass(slots=True)
class LlamaCppDeploymentConfig:
    """Configuration for creating a llama.cpp deployment record.

    Mirrors the fields ``api/v1/llamacpp.py``'s create route already
    inserts directly — factored out here so ``LlamaCppManager.provision``
    (the ``InferenceFleetBackend`` entry point) has the same single
    construction path instead of duplicating the insert.
    """

    name: str
    model_name: str
    deployment_type: str = "kubernetes"  # kubernetes | remote
    model_url: str | None = None
    model_filename: str | None = None
    n_ctx: int = 4096
    n_gpu_layers: int = -1
    gpu_count: int = 1
    endpoint_url: str | None = None
    k8s_namespace: str = "waddleai"
    node_selector: dict[str, str] | None = None
    node_affinity: dict[str, Any] | None = None


@register(BackendType.LLAMACPP)
class LlamaCppManager(InferenceFleetBackend):
    """Manages llama-server deployment lifecycle.

    Implements ``InferenceFleetBackend`` (spec §10.1) by wrapping the
    existing sync PyDAL/K8s/HTTP methods below in ``asyncio.to_thread`` —
    a restructure, not a rewrite; every pre-existing method keeps its exact
    signature and behavior for the legacy ``api/v1/llamacpp.py`` routes.
    """

    type = BackendType.LLAMACPP
    management_scope = ManagementScope.FULL_LIFECYCLE

    def __init__(self, db, *, config: dict[str, Any] | None = None, credentials: str | None = None):
        self.db = db
        # registry.build_backend construction contract (§10.1 Task 3).
        # llama.cpp has no cloud credentials of its own; `credentials` is
        # accepted for interface uniformity and currently unused.
        self.config = config or {}
        self.credentials = credentials

    def _daemonset_name(self, deployment_name: str) -> str:
        """Generate a K8s-safe DaemonSet name from a deployment name."""
        sanitised = re.sub(r"[^a-z0-9-]", "-", deployment_name.lower())
        sanitised = re.sub(r"-+", "-", sanitised).strip("-")
        return f"waddleai-llamacpp-{sanitised}"

    def export_k8s_manifest(self, deployment) -> str:
        """Return DaemonSet + Service YAML for the given deployment.

        Manifests are hardened to match k8s/helm/waddleai/templates/llamacpp-daemonset.yaml:
        - Digest-pinned images (ggml-org/llama.cpp and curlimages/curl)
        - PersistentVolumeClaim-backed model cache (falls back to emptyDir with warning)
        - Cache-skip guard in init container (env vars, no shell interpolation)
        - Full securityContext (pod + container level)
        - CPU/memory resource requests/limits
        - Health check probes
        """
        ds_name = deployment.k8s_daemonset_name or self._daemonset_name(deployment.name)
        namespace = deployment.k8s_namespace or "waddleai"
        node_selector = deployment.node_selector or {}

        # Resource defaults if not provided on deployment
        cpu_request: str = deployment.cpu_request or "2000m"
        cpu_limit: str = deployment.cpu_limit or "4000m"
        memory_request: str = deployment.memory_request or "8Gi"
        memory_limit: str = deployment.memory_limit or "16Gi"

        # Model volume: PVC if cache claim provided, otherwise emptyDir with warning
        model_volume: dict
        if deployment.model_cache_claim:
            model_volume = {
                "name": "llamacpp-models",
                "persistentVolumeClaim": {"claimName": deployment.model_cache_claim},
            }
        else:
            logger.warning(
                "No model_cache_claim set for deployment %s; model caching is disabled. "
                "Set model_cache_claim to enable PVC-backed cache for faster restarts.",
                deployment.name,
            )
            model_volume = {"name": "llamacpp-models", "emptyDir": {}}

        # Cache-skip guard: init container checks if model already cached before download
        # Uses env vars (MODEL_URL, MODEL_FILE) to avoid shell injection, matches Helm template
        cache_skip_guard_script: str = (
            "set -eu\n"
            'if [ -f "/models/$MODEL_FILE" ]; then\n'
            '  echo "$MODEL_FILE already cached, skipping download"\n'
            "else\n"
            '  echo "Downloading $MODEL_FILE"\n'
            '  curl -fsSL -o "/models/$MODEL_FILE" "$MODEL_URL"\n'
            "fi"
        )

        init_container: dict = {
            "name": "download-model",
            "image": CURL_DOWNLOADER_IMAGE,
            "env": [
                {"name": "MODEL_URL", "value": deployment.model_url},
                {"name": "MODEL_FILE", "value": deployment.model_filename},
            ],
            "command": ["/bin/sh", "-c", cache_skip_guard_script],
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
            "volumeMounts": [
                {"name": "llamacpp-models", "mountPath": "/models"},
                {"name": "tmp", "mountPath": "/tmp"},  # nosec B108 -- pod volumeMount path in a generated manifest, not a host temp file; required because the container runs readOnlyRootFilesystem
            ],
        }

        llama_server_container: dict = {
            "name": "llama-server",
            "image": LLAMACPP_IMAGE,
            "args": [
                "--model", f"/models/{deployment.model_filename}",
                "--n-gpu-layers", str(deployment.n_gpu_layers),
                "--ctx-size", str(deployment.n_ctx),
                "--port", "8080",
                "--host", "0.0.0.0",  # nosec B104 -- container listen address inside its own pod network namespace; reachability is governed by Service + CiliumNetworkPolicy
            ],
            "ports": [{"name": "http", "containerPort": 8080, "protocol": "TCP"}],
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
            "resources": {
                "requests": {
                    "cpu": cpu_request,
                    "memory": memory_request,
                    "nvidia.com/gpu": str(deployment.gpu_count),
                },
                "limits": {
                    "cpu": cpu_limit,
                    "memory": memory_limit,
                    "nvidia.com/gpu": str(deployment.gpu_count),
                },
            },
            "livenessProbe": {
                "httpGet": {"path": "/health", "port": 8080},
                "initialDelaySeconds": 60,
                "periodSeconds": 30,
                "timeoutSeconds": 15,
                "failureThreshold": 5,
            },
            "readinessProbe": {
                "httpGet": {"path": "/health", "port": 8080},
                "initialDelaySeconds": 30,
                "periodSeconds": 10,
                "timeoutSeconds": 5,
                "failureThreshold": 3,
            },
            "volumeMounts": [
                {"name": "llamacpp-models", "mountPath": "/models"},
                {"name": "tmp", "mountPath": "/tmp"},  # nosec B108 -- pod volumeMount path in a generated manifest, not a host temp file
            ],
        }

        daemonset: dict = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": ds_name, "namespace": namespace},
            "spec": {
                "selector": {"matchLabels": {"app": ds_name}},
                "template": {
                    "metadata": {"labels": {"app": ds_name}},
                    "spec": {
                        "securityContext": {
                            "fsGroup": 1000,
                            "runAsNonRoot": True,
                            "runAsUser": 1000,
                        },
                        "nodeSelector": node_selector,
                        "initContainers": [init_container],
                        "containers": [llama_server_container],
                        "volumes": [
                            model_volume,
                            {"name": "tmp", "emptyDir": {}},
                        ],
                    },
                },
            },
        }

        if deployment.node_affinity:
            daemonset["spec"]["template"]["spec"]["affinity"] = {
                "nodeAffinity": deployment.node_affinity
            }

        service: dict = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": f"{ds_name}-svc", "namespace": namespace},
            "spec": {
                "selector": {"app": ds_name},
                "ports": [{"port": 8080, "targetPort": 8080}],
            },
        }

        return yaml.dump_all([daemonset, service], default_flow_style=False)

    def deploy_daemonset(self, deployment) -> None:
        """Create the DaemonSet and Service in K8s. Raises on K8s API error."""
        manifest_yaml = self.export_k8s_manifest(deployment)
        docs = list(yaml.safe_load_all(manifest_yaml))
        ds_doc = next(d for d in docs if d["kind"] == "DaemonSet")
        svc_doc = next(d for d in docs if d["kind"] == "Service")

        namespace = deployment.k8s_namespace or "waddleai"
        ds_name = deployment.k8s_daemonset_name or self._daemonset_name(deployment.name)
        apps_client = get_k8s_apps_client()

        try:
            apps_client.create_namespaced_daemon_set(
                namespace=namespace,
                body=ds_doc,
            )
        except Exception as e:
            logger.error(f"Failed to create DaemonSet {ds_name}: {e}")
            raise

        core_client = get_k8s_core_client()
        try:
            core_client.create_namespaced_service(
                namespace=namespace,
                body=svc_doc,
            )
        except Exception as e:
            logger.error(f"Failed to create Service for {ds_name}: {e}")
            raise

        svc_endpoint = f"http://{ds_name}-svc.{namespace}:8080"
        db = self.db
        db(db.llamacpp_deployments.id == deployment.id).update(
            status="deploying",
            endpoint_url=svc_endpoint,
            k8s_daemonset_name=ds_name,
        )
        logger.info(f"Deployed llama.cpp DaemonSet {ds_name} in {namespace}")

    def remove_daemonset(self, deployment, force: bool = False) -> None:
        """Delete the DaemonSet and Service. Requires force=True if status is running."""
        if deployment.status == "running" and not force:
            raise ValueError(
                f"Deployment '{deployment.name}' is running. Pass force=True to remove it."
            )

        namespace = deployment.k8s_namespace or "waddleai"
        ds_name = deployment.k8s_daemonset_name or self._daemonset_name(deployment.name)

        apps_client = get_k8s_apps_client()
        core_client = get_k8s_core_client()

        apps_client.delete_namespaced_daemon_set(name=ds_name, namespace=namespace)
        core_client.delete_namespaced_service(name=f"{ds_name}-svc", namespace=namespace)

        db = self.db
        db(db.llamacpp_deployments.id == deployment.id).update(status="stopped")
        logger.info(f"Removed llama.cpp DaemonSet {ds_name}")

    def register_remote(self, deployment) -> None:
        """Register a remote llama-server endpoint after verifying it is reachable."""
        url = deployment.endpoint_url
        try:
            resp = requests.get(f"{url}/health", timeout=10)
            if resp.status_code != 200:
                raise ValueError(f"Health check returned HTTP {resp.status_code}")
        except Exception as exc:
            raise ValueError(f"Endpoint {url} unreachable: {exc}") from exc

        db = self.db
        db(db.llamacpp_deployments.id == deployment.id).update(status="running")
        logger.info(f"Registered remote llama.cpp endpoint: {url}")

    # Deployment CRUD (factored out of api/v1/llamacpp.py's create/delete
    # routes so InferenceFleetBackend.provision/deprovision have one path)

    def create_deployment(self, config: LlamaCppDeploymentConfig) -> dict[str, Any]:
        """Insert a new ``llamacpp_deployments`` row. Does not deploy it — see provision()."""
        db = self.db
        existing = db(db.llamacpp_deployments.name == config.name).select().first()
        if existing:
            return {"success": False, "error": "Deployment with this name already exists"}

        deployment_id = db.llamacpp_deployments.insert(
            name=config.name,
            deployment_type=config.deployment_type,
            status="pending",
            model_name=config.model_name,
            model_url=config.model_url,
            model_filename=config.model_filename,
            n_ctx=config.n_ctx,
            n_gpu_layers=config.n_gpu_layers,
            gpu_count=config.gpu_count,
            endpoint_url=config.endpoint_url,
            k8s_namespace=config.k8s_namespace,
            k8s_daemonset_name=self._daemonset_name(config.name),
            node_selector=config.node_selector,
            node_affinity=config.node_affinity,
        )
        db.commit()
        logger.info(f"Created llama.cpp deployment: {config.name}")
        return {"success": True, "deployment_id": deployment_id, "name": config.name}

    def delete_deployment(self, deployment_id: int, force: bool = False) -> dict[str, Any]:
        """Delete a deployment, tearing down its DaemonSet first if running and forced."""
        db = self.db
        deployment = db(db.llamacpp_deployments.id == deployment_id).select().first()
        if not deployment:
            return {"success": False, "error": "Deployment not found"}

        if deployment.status == "running":
            if not force:
                return {
                    "success": False,
                    "error": f"Deployment '{deployment.name}' is running. Pass force=True.",
                }
            if deployment.deployment_type == "kubernetes":
                try:
                    self.remove_daemonset(deployment, force=True)
                except Exception as exc:
                    logger.warning(f"Error during forced removal of {deployment.name}: {exc}")

        db(db.llamacpp_deployments.id == deployment_id).delete()
        db.commit()
        logger.info(f"Deleted llama.cpp deployment: {deployment.name}")
        return {"success": True}

    # InferenceFleetBackend interface (spec §10.1)
    #
    # Each llamacpp_deployments row is single-model (unlike Ollama's
    # multi-model-per-node), so `loaded_models` is a 0-or-1-element list and
    # `place_model`/`endpoints_for` match on `model_name` directly rather
    # than a join table. VRAM fields default to 0 (unknown) — no metrics
    # source is wired up yet, same caveat as the Ollama backend.

    def _node_info_from_deployment(self, deployment) -> NodeInfo:
        """Map a ``llamacpp_deployments`` row to a ``NodeInfo`` value object."""
        kind = "k8s" if deployment.deployment_type == "kubernetes" else "external"
        loaded_models = [deployment.model_name] if deployment.model_name else []
        return NodeInfo(
            node_id=deployment.name,
            node_uid=getattr(deployment, "node_uid", None),
            kind=kind,
            loaded_models=loaded_models,
            vram_total_mb=0,
            vram_free_mb=0,
            healthy=deployment.status == "running",
        )

    def _reachable(self, deployment) -> bool:
        """Same reachability check as the ``/health`` route: GET {endpoint}/health."""
        if not deployment.endpoint_url:
            return False
        try:
            resp = requests.get(f"{deployment.endpoint_url}/health", timeout=10)
            return resp.status_code == 200
        except Exception:
            return False

    async def provision(self, spec: ProvisionSpec) -> list[NodeInfo]:
        """Create and deploy a llama.cpp instance for ``spec``.

        ``spec.mode`` selects ``kubernetes`` (DaemonSet, the default) or
        ``remote`` (register-and-health-check an existing endpoint).
        """

        def _provision() -> list[NodeInfo]:
            deployment_type = spec.mode or "kubernetes"
            model_name = spec.models[0] if spec.models else spec.constraints.get("model_name", "")
            config = LlamaCppDeploymentConfig(
                name=spec.name,
                model_name=model_name,
                deployment_type=deployment_type,
                model_url=spec.constraints.get("model_url"),
                model_filename=spec.constraints.get("model_filename"),
                gpu_count=int(spec.constraints.get("gpu_count", 1)),
                endpoint_url=spec.constraints.get("endpoint_url"),
                k8s_namespace=spec.constraints.get("namespace", "waddleai"),
            )
            result = self.create_deployment(config)
            if not result.get("success"):
                raise RuntimeError(result.get("error") or "llama.cpp provision failed")

            db = self.db
            deployment = db(db.llamacpp_deployments.id == result["deployment_id"]).select().first()
            if deployment_type == "kubernetes":
                self.deploy_daemonset(deployment)
            else:
                self.register_remote(deployment)
            deployment = db(db.llamacpp_deployments.id == deployment.id).select().first()
            return [self._node_info_from_deployment(deployment)]

        return await asyncio.to_thread(_provision)

    async def deprovision(self, node_id: str) -> None:
        """Delete the deployment named ``node_id`` (force-removing its DaemonSet)."""

        def _deprovision() -> None:
            db = self.db
            deployment = db(db.llamacpp_deployments.name == node_id).select().first()
            if deployment is None:
                return
            self.delete_deployment(deployment.id, force=True)

        await asyncio.to_thread(_deprovision)

    async def health(self) -> FleetHealth:
        """Aggregate the ``/health`` reachability check across every deployment."""

        def _health() -> FleetHealth:
            db = self.db
            deployments = db(db.llamacpp_deployments.id > 0).select()
            total = 0
            healthy_count = 0
            for deployment in deployments:
                total += 1
                if self._reachable(deployment):
                    healthy_count += 1
            return FleetHealth(
                backend_id=self.fleet_backend_id,
                healthy=(healthy_count == total),
                node_count=total,
                detail={"healthy_nodes": healthy_count},
            )

        return await asyncio.to_thread(_health)

    async def list_nodes(self) -> list[NodeInfo]:
        """Return every tracked llama.cpp deployment as a ``NodeInfo``."""

        def _list() -> list[NodeInfo]:
            db = self.db
            deployments = db(db.llamacpp_deployments.id > 0).select()
            return [self._node_info_from_deployment(d) for d in deployments]

        return await asyncio.to_thread(_list)

    async def place_model(self, model: str, constraints: dict[str, Any]) -> ModelPlacement:
        """Find the (single-model) deployment already serving ``model``.

        llama.cpp deployments are provisioned pre-bound to one model, so
        placement here is lookup, not a live pull like Ollama's.
        """

        def _place() -> ModelPlacement:
            db = self.db
            deployment = db(db.llamacpp_deployments.model_name == model).select().first()
            if deployment is None:
                raise RuntimeError(f"No llama.cpp deployment serving model {model!r}")
            status = "placed" if deployment.status == "running" else "pulling"
            return ModelPlacement(model=model, node_id=deployment.name, status=status)

        return await asyncio.to_thread(_place)

    async def endpoints_for(self, model: str) -> list[Endpoint]:
        """Return endpoints for deployments currently serving ``model``."""

        def _endpoints() -> list[Endpoint]:
            db = self.db
            deployments = db(db.llamacpp_deployments.model_name == model).select()
            return [
                Endpoint(
                    url=d.endpoint_url,
                    node_id=d.name,
                    loaded_models=[d.model_name],
                    healthy=d.status == "running",
                )
                for d in deployments
                if d.endpoint_url
            ]

        return await asyncio.to_thread(_endpoints)
