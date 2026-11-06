"""llama.cpp Deployment Manager.

Manages llama-server instances in two modes:
- kubernetes: Creates/removes K8s DaemonSets targeting GPU-labelled nodes
- remote:     Registers an existing llama-server endpoint after a health check
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

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
from shared.fleet.enums import LlamaCppTopology
from shared.fleet.llamacpp_rpc import RPC_AUTH_REFUSAL, RpcLayout, RpcRefused
from shared.fleet.registry import register
from shared.fleet.vllm_ray.coexistence import (
    default_list_nodes,
    refuse_plain_gpu_for_target_nodes,
)
from shared.fleet.vllm_ray.slug import derive_slug

logger = logging.getLogger(__name__)

# Hardened image references — digest-pinned, security-vetted
LLAMACPP_IMAGE: str = (
    "ghcr.io/ggml-org/llama.cpp@sha256:"
    "51570a4f93c5ce81ac6f2b1ea16a58771cfded2adb34241df7e75329b24fe76e"
)
CURL_DOWNLOADER_IMAGE: str = (
    "curlimages/curl@sha256:7c12af72ceb38b7432ab85e1a265cff6ae58e06f95539d539b654f2cfa64bb13"
)

# --- Multi-node RPC topology (Plan F) -------------------------------------------------------
RPC_PORT: int = 50052
RPC_SERVER_BIN = "/usr/local/bin/rpc-server"
LLAMA_SERVER_BIN = "/usr/local/bin/llama-server"
# Device names as llama.cpp's rpc-server addresses them; verified per image by F4 spike S1.
RPC_DEVICE_BY_VENDOR: dict[int, str] = {1: "CUDA0", 2: "ROCm0", 3: "SYCL0"}
# AMD / Intel device-plugin resources; NVIDIA is per class (see ``resource_for``).
RPC_RESOURCE_BY_VENDOR: dict[int, str] = {2: "amd.com/gpu", 3: "gpu.intel.com/i915"}
_NVIDIA = 1
_ROLE_LABEL = "waddleai.penguintech.io/llamacpp-role"
_DEPLOYMENT_LABEL = "waddleai.penguintech.io/llamacpp-deployment"
_OWNER_LABEL = "waddleai.penguintech.io/llamacpp-public-id"
_KEYS_DIR = "/secrets/deployment-keys"
_MAX_RPC_BASE_NAME = 48  # <base>-rpc-<i>-<ordinal> must stay a valid 63-char pod hostname
_WAIT_ATTEMPTS = 600  # x 2 s: a worker that never comes up fails the head loudly (~20 min)
_RPC_WAIT_SCRIPT = (
    "set -eu\n"
    "for w in $WORKERS; do\n"
    "  n=0\n"
    '  until curl -sS --connect-timeout 2 "telnet://$w" </dev/null >/dev/null 2>&1; do\n'
    "    n=$((n+1))\n"
    f'    if [ "$n" -ge {_WAIT_ATTEMPTS} ]; then\n'
    '      echo "worker $w never became reachable" >&2; exit 1\n'
    "    fi\n"
    "    sleep 2\n"
    "  done\n"
    "done"
)
_RPC_DEFAULT_PROXY_SELECTOR = {
    "app.kubernetes.io/name": "waddleai",
    "app.kubernetes.io/component": "proxy",
}
_RPC_DEFAULT_MANAGEMENT_SELECTOR = {
    "app.kubernetes.io/name": "waddleai",
    "app.kubernetes.io/component": "management",
}


_FAILED_WAITING = frozenset(
    {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError"}
)


@dataclass(slots=True, frozen=True)
class RpcObservation:
    """What Kubernetes currently reports for one RPC deployment (input to status derivation)."""

    workers_total: int
    workers_up: int
    workers_down: list[str]
    head_ready: bool
    failed_message: str | None


def resource_for(vendor: int, gpu_class: str) -> str:
    """Kubernetes extended-resource name for one GPU of ``gpu_class`` (AI nodes are per class).

    NVIDIA AI nodes advertise ``nvidia.com/<class without vendor prefix>`` (nvidia-rtx4090 ->
    nvidia.com/rtx4090); AMD and Intel use their plugin's single resource.
    """
    if vendor == _NVIDIA:
        short = gpu_class.removeprefix("nvidia-")
        if not short or short == gpu_class:
            raise ValueError(f"cannot derive an NVIDIA GPU resource from class {gpu_class!r}")
        return f"nvidia.com/{short}"
    try:
        return RPC_RESOURCE_BY_VENDOR[vendor]
    except KeyError:
        raise ValueError(f"unknown GPU vendor {vendor}") from None


def _topology_selector(key: str, default: dict[str, str]) -> dict[str, str]:
    """A pod selector from the chart's CILIUM_TOPOLOGY JSON, else the shipped default."""
    try:
        parsed = json.loads(os.environ.get("CILIUM_TOPOLOGY", "") or "{}")
        selector = parsed.get("selectors", {}).get(key)
    except (ValueError, AttributeError):
        selector = None
    return dict(selector) if isinstance(selector, dict) and selector else dict(default)


def _mutual_auth_from_env() -> bool:
    """``WADDLEAI_FLEET_MUTUAL_AUTH`` as set by the chart; anything but ``true`` fails closed."""
    return os.environ.get("WADDLEAI_FLEET_MUTUAL_AUTH", "").strip().lower() == "true"


def _hardened_container_security() -> dict[str, Any]:
    """Container securityContext shared by every RPC pod (rootless, read-only rootfs)."""
    return {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }


def _pod_security() -> dict[str, Any]:
    """Pod securityContext meeting Pod Security Admission ``restricted``."""
    return {
        "fsGroup": 1000,
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def get_k8s_apps_client():
    """Return a configured AppsV1Api client."""
    from kubernetes import client  # type: ignore[import]
    from kubernetes import config as k8s_config

    try:
        k8s_config.load_incluster_config()
    except Exception:
        logger.debug("Not running in-cluster, falling back to kubeconfig")
        k8s_config.load_kube_config()
    return client.AppsV1Api()


def get_k8s_core_client():
    """Return a configured CoreV1Api client."""
    from kubernetes import client  # type: ignore[import]
    from kubernetes import config as k8s_config

    try:
        k8s_config.load_incluster_config()
    except Exception:
        logger.debug("Not running in-cluster, falling back to kubeconfig")
        k8s_config.load_kube_config()
    return client.CoreV1Api()


def get_k8s_custom_client():
    """Return a configured CustomObjectsApi client (CiliumNetworkPolicy objects)."""
    from kubernetes import client  # type: ignore[import]
    from kubernetes import config as k8s_config

    try:
        k8s_config.load_incluster_config()
    except Exception:
        logger.debug("Not running in-cluster, falling back to kubeconfig")
        k8s_config.load_kube_config()
    return client.CustomObjectsApi()


def _list_candidate_nodes(node_selector: dict[str, str]) -> list[Any]:
    """Resolve candidate K8s nodes for the co-tenancy guard (test seam; raises -> fail closed)."""
    return default_list_nodes(node_selector, get_k8s_core_client())


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
        """Bind the DAL handle and optional backend config/credentials for this fleet instance."""
        self.db = db
        # registry.build_backend construction contract (§10.1 Task 3).
        # llama.cpp has no cloud credentials of its own; `credentials` is
        # accepted for interface uniformity and currently unused.
        self.config = config or {}
        self.credentials = credentials
        # Fail closed: only an explicit True (config, else the chart's env) enables RPC layouts.
        configured = self.config.get("mutual_auth_enabled")
        self.mutual_auth_enabled: bool = (
            _mutual_auth_from_env() if configured is None else configured is True
        )

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
                {"name": "tmp", "mountPath": "/tmp"},  # nosec B108 # noqa: S108 -- pod volumeMount path in a generated manifest, not a host temp file; required because the container runs readOnlyRootFilesystem
            ],
        }

        llama_server_container: dict = {
            "name": "llama-server",
            "image": LLAMACPP_IMAGE,
            "args": [
                "--model",
                f"/models/{deployment.model_filename}",
                "--n-gpu-layers",
                str(deployment.n_gpu_layers),
                "--ctx-size",
                str(deployment.n_ctx),
                "--port",
                "8080",
                "--host",
                "0.0.0.0",  # nosec B104 # noqa: S104 -- container listen address inside its own pod network namespace; reachability is governed by Service + CiliumNetworkPolicy
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
                {"name": "tmp", "mountPath": "/tmp"},  # nosec B108 # noqa: S108 -- pod volumeMount path in a generated manifest, not a host temp file
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

        if getattr(deployment, "topology", None) == LlamaCppTopology.RPC_MULTI_NODE:
            self.remove_rpc(deployment)
            return

        namespace = deployment.k8s_namespace or "waddleai"
        ds_name = deployment.k8s_daemonset_name or self._daemonset_name(deployment.name)

        apps_client = get_k8s_apps_client()
        core_client = get_k8s_core_client()

        apps_client.delete_namespaced_daemon_set(name=ds_name, namespace=namespace)
        core_client.delete_namespaced_service(name=f"{ds_name}-svc", namespace=namespace)

        db = self.db
        db(db.llamacpp_deployments.id == deployment.id).update(status="stopped")
        logger.info(f"Removed llama.cpp DaemonSet {ds_name}")

    # --- Multi-node RPC topology (Plan F, F3) ------------------------------------------------

    def _rpc_names(self, deployment) -> tuple[str, str]:
        """``(base name, namespace)`` for an RPC deployment, refusing names that overflow DNS."""
        base = deployment.k8s_daemonset_name or self._daemonset_name(deployment.name)
        if len(base) > _MAX_RPC_BASE_NAME:
            raise ValueError(
                f"deployment name too long for an RPC layout: '{base}' is {len(base)} characters; "
                f"shorten the name so the object name stays within {_MAX_RPC_BASE_NAME}."
            )
        return base, deployment.k8s_namespace or "waddleai"

    @staticmethod
    def _rpc_worker_groups(layout: RpcLayout) -> list[tuple[str, str, int]]:
        """Worker StatefulSet groups ``(node_uid, gpu_class, replicas)`` in stable order."""
        counts: dict[tuple[str, str], int] = {}
        for worker in layout.workers:
            key = (worker.node_uid, worker.gpu_class)
            counts[key] = counts.get(key, 0) + 1
        return [(uid, cls, n) for (uid, cls), n in sorted(counts.items())]

    def export_rpc_manifests(
        self,
        deployment,
        layout: RpcLayout,
        hostnames: dict[str, str],
        image: str,
        n_ctx: int,
    ) -> list[dict]:
        """Render the head Deployment, worker StatefulSets, Services and per-deployment CNP.

        One rpc-server pod per planned GPU (a StatefulSet per worker node/class, all behind one
        headless Service) and one pinned llama-server head. Refuses before rendering anything when
        mutual auth is off, the split does not match the devices, or the image is not digest-pinned.
        """
        if not self.mutual_auth_enabled:
            raise RpcRefused(RPC_AUTH_REFUSAL)
        devices = len(layout.workers) + layout.head_gpus
        if len(layout.tensor_split) != devices:
            raise ValueError(
                f"tensor_split length {len(layout.tensor_split)} must equal device count {devices}"
            )
        if "@sha256:" not in image:
            raise ValueError("RPC image must be pinned by digest (name@sha256:...)")
        base, namespace = self._rpc_names(deployment)
        missing = {layout.head_node_uid, *(w.node_uid for w in layout.workers)} - set(hostnames)
        if missing:
            raise ValueError(f"no Kubernetes hostname for node(s) {sorted(missing)}")

        groups = self._rpc_worker_groups(layout)
        group_index = {(uid, cls): i for i, (uid, cls, _n) in enumerate(groups)}
        next_ordinal: dict[tuple[str, str], int] = {}
        fqdns: list[str] = []
        for worker in layout.workers:  # device order: --rpc must follow tensor_split order
            key = (worker.node_uid, worker.gpu_class)
            ordinal = next_ordinal.get(key, 0)
            next_ordinal[key] = ordinal + 1
            pod = f"{base}-rpc-{group_index[key]}-{ordinal}"
            fqdns.append(f"{pod}.{base}-rpc.{namespace}.svc:{RPC_PORT}")

        pid = str(deployment.public_id)
        slug = derive_slug(deployment.name, deployment.public_id)
        objects: list[dict] = [
            self._rpc_head_service(base, namespace, pid),
            self._rpc_worker_service(base, namespace, pid),
        ]
        for index, (node_uid, gpu_class, replicas) in enumerate(groups):
            objects.append(
                self._rpc_worker_statefulset(
                    base,
                    namespace,
                    pid,
                    index,
                    node_uid,
                    gpu_class,
                    replicas,
                    layout,
                    hostnames,
                    image,
                )
            )
        objects.append(
            self._rpc_head_deployment(
                deployment, base, namespace, layout, hostnames, image, n_ctx, fqdns, slug
            )
        )
        objects.append(self._rpc_worker_policy(base, namespace, pid))
        objects.append(self._rpc_head_policy(deployment, base, namespace))
        return objects

    @staticmethod
    def _rpc_labels(base: str, role: str, pid: str) -> dict[str, str]:
        """Pod/Service selector labels for one role of an RPC deployment."""
        return {_DEPLOYMENT_LABEL: base, _ROLE_LABEL: role, _OWNER_LABEL: pid}

    def _rpc_head_service(self, base: str, namespace: str, pid: str) -> dict:
        """ClusterIP Service in front of the head (the endpoint the proxy routes to)."""
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": f"{base}-svc", "namespace": namespace},
            "spec": {
                "type": "ClusterIP",
                "selector": self._rpc_labels(base, "head", pid),
                "ports": [{"port": 8080, "targetPort": 8080}],
            },
        }

    def _rpc_worker_service(self, base: str, namespace: str, pid: str) -> dict:
        """Headless Service giving every rpc-server pod a stable DNS name."""
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": f"{base}-rpc", "namespace": namespace},
            "spec": {
                "clusterIP": "None",
                "publishNotReadyAddresses": True,
                "selector": self._rpc_labels(base, "worker", pid),
                "ports": [{"port": RPC_PORT, "targetPort": RPC_PORT, "name": "rpc"}],
            },
        }

    def _rpc_worker_statefulset(
        self,
        base: str,
        namespace: str,
        pid: str,
        index: int,
        node_uid: str,
        gpu_class: str,
        replicas: int,
        layout: RpcLayout,
        hostnames: dict[str, str],
        image: str,
    ) -> dict:
        """One StatefulSet of single-GPU rpc-server pods pinned to one node (and class)."""
        resource = resource_for(layout.vendor, gpu_class)
        labels = self._rpc_labels(base, "worker", pid)
        container = {
            "name": "rpc-server",
            "image": image,
            "command": [RPC_SERVER_BIN],
            "args": [
                "-H",
                "0.0.0.0",  # nosec B104 # noqa: S104 -- pod-netns listen address; Cilium mutual auth + CNP admit only the head
                "-p",
                str(RPC_PORT),
                "-d",
                RPC_DEVICE_BY_VENDOR[layout.vendor],
            ],
            "ports": [{"name": "rpc", "containerPort": RPC_PORT, "protocol": "TCP"}],
            "securityContext": _hardened_container_security(),
            "resources": {
                "requests": {"cpu": "500m", "memory": "2Gi", resource: "1"},
                "limits": {"cpu": "2000m", "memory": "8Gi", resource: "1"},
            },
            "readinessProbe": {
                "tcpSocket": {"port": RPC_PORT},
                "initialDelaySeconds": 5,
                "periodSeconds": 10,
            },
            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],  # nosec B108 # noqa: S108 -- pod volumeMount path in a generated manifest
        }
        return {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": f"{base}-rpc-{index}", "namespace": namespace, "labels": labels},
            "spec": {
                "serviceName": f"{base}-rpc",
                "replicas": replicas,
                "podManagementPolicy": "Parallel",
                "selector": {
                    "matchLabels": {**labels, "waddleai.penguintech.io/rpc-group": str(index)}
                },
                "template": {
                    "metadata": {
                        "labels": {**labels, "waddleai.penguintech.io/rpc-group": str(index)}
                    },
                    "spec": {
                        "securityContext": _pod_security(),
                        "nodeSelector": {"kubernetes.io/hostname": hostnames[node_uid]},
                        "containers": [container],
                        "volumes": [{"name": "tmp", "emptyDir": {}}],
                    },
                },
            },
        }

    def _rpc_head_deployment(
        self,
        deployment,
        base: str,
        namespace: str,
        layout: RpcLayout,
        hostnames: dict[str, str],
        image: str,
        n_ctx: int,
        fqdns: list[str],
        slug: str,
    ) -> dict:
        """The llama-server head: pinned to its planned node, key file from the shared Secret."""
        pid = str(deployment.public_id)
        from services.management.app.services.fleet_secrets import DEPLOYMENT_KEYS_SECRET

        labels = {
            **self._rpc_labels(base, "head", pid),
            "app.kubernetes.io/component": "llamacpp",  # admitted by waddleai-allow-fleet-ingress
        }
        head_resource = resource_for(layout.vendor, layout.head_gpu_class)
        claim = getattr(deployment, "model_cache_claim", None)
        model_volume: dict = (
            {"name": "llamacpp-models", "persistentVolumeClaim": {"claimName": claim}}
            if claim
            else {"name": "llamacpp-models", "emptyDir": {}}
        )
        if not claim:
            logger.warning(
                "No model_cache_claim set for RPC deployment %s; model caching is disabled.",
                deployment.name,
            )
        tmp_mount = {"name": "tmp", "mountPath": "/tmp"}  # nosec B108 # noqa: S108 -- pod volumeMount path in a generated manifest
        download = {
            "name": "download-model",
            "image": CURL_DOWNLOADER_IMAGE,
            "env": [
                {"name": "MODEL_URL", "value": deployment.model_url},
                {"name": "MODEL_FILE", "value": deployment.model_filename},
            ],
            "command": [
                "/bin/sh",
                "-c",
                'set -eu\nif [ -f "/models/$MODEL_FILE" ]; then\n'
                '  echo "$MODEL_FILE already cached, skipping download"\nelse\n'
                '  curl -fsSL -o "/models/$MODEL_FILE" "$MODEL_URL"\nfi',
            ],
            "securityContext": _hardened_container_security(),
            "volumeMounts": [{"name": "llamacpp-models", "mountPath": "/models"}, tmp_mount],
        }
        wait_workers = {
            "name": "wait-workers",
            "image": CURL_DOWNLOADER_IMAGE,
            "env": [{"name": "WORKERS", "value": " ".join(fqdns)}],
            "command": ["/bin/sh", "-c", _RPC_WAIT_SCRIPT],
            "securityContext": _hardened_container_security(),
        }
        server = {
            "name": "llama-server",
            "image": image,
            "command": [LLAMA_SERVER_BIN],
            "args": [
                "--model",
                f"/models/{deployment.model_filename}",
                "--n-gpu-layers",
                "999",
                "--ctx-size",
                str(n_ctx),
                "--port",
                "8080",
                "--host",
                "0.0.0.0",  # nosec B104 # noqa: S104 -- pod-netns listen address; reach is governed by Service + CiliumNetworkPolicy
                "--rpc",
                ",".join(fqdns),
                "--tensor-split",
                ",".join(str(share) for share in layout.tensor_split),
                "--api-key-file",
                f"{_KEYS_DIR}/{slug}",
            ],
            "ports": [{"name": "http", "containerPort": 8080, "protocol": "TCP"}],
            "securityContext": _hardened_container_security(),
            "resources": {
                "requests": {
                    "cpu": getattr(deployment, "cpu_request", None) or "2000m",
                    "memory": getattr(deployment, "memory_request", None) or "8Gi",
                    head_resource: str(layout.head_gpus),
                },
                "limits": {
                    "cpu": getattr(deployment, "cpu_limit", None) or "4000m",
                    "memory": getattr(deployment, "memory_limit", None) or "16Gi",
                    head_resource: str(layout.head_gpus),
                },
            },
            "livenessProbe": {
                "httpGet": {"path": "/health", "port": 8080},
                "initialDelaySeconds": 120,
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
                {"name": "deployment-key", "mountPath": _KEYS_DIR, "readOnly": True},
                tmp_mount,
            ],
        }
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": base, "namespace": namespace, "labels": labels},
            "spec": {
                "replicas": 1,
                "strategy": {"type": "Recreate"},  # one head owns the GPUs; never two at once
                "selector": {"matchLabels": self._rpc_labels(base, "head", pid)},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "securityContext": _pod_security(),
                        "nodeSelector": {"kubernetes.io/hostname": hostnames[layout.head_node_uid]},
                        "initContainers": [download, wait_workers],
                        "containers": [server],
                        "volumes": [
                            model_volume,
                            {"name": "tmp", "emptyDir": {}},
                            {
                                # only THIS deployment's key (items), never the whole shared Secret
                                "name": "deployment-key",
                                "secret": {
                                    "secretName": DEPLOYMENT_KEYS_SECRET,
                                    "items": [{"key": slug, "path": slug}],
                                },
                            },
                        ],
                    },
                },
            },
        }

    def _rpc_head_policy(self, deployment, base: str, namespace: str) -> dict:
        pid = str(deployment.public_id)
        """Head CNP: ingress from the proxy and management only; egress to DNS, workers, source.

        Every allowed flow requires Cilium mutual auth. The default-deny egress would otherwise
        leave the head unable to resolve its workers, reach them, or fetch the model.
        """
        auth = {"mode": "required"}
        proxy = _topology_selector("aiproxy", _RPC_DEFAULT_PROXY_SELECTOR)
        management = _topology_selector("management", _RPC_DEFAULT_MANAGEMENT_SELECTOR)
        head_labels = self._rpc_labels(base, "head", pid)
        worker_labels = self._rpc_labels(base, "worker", pid)
        head_egress: list[dict[str, Any]] = [
            {
                "toEndpoints": [
                    {
                        "matchLabels": {
                            "k8s:io.kubernetes.pod.namespace": "kube-system",
                            "k8s-app": "kube-dns",
                        }
                    }
                ],
                "toPorts": [
                    {
                        "ports": [{"port": "53", "protocol": "ANY"}],
                        "rules": {"dns": [{"matchPattern": "*"}]},
                    }
                ],
            },
            {
                "toEndpoints": [{"matchLabels": worker_labels}],
                "authentication": auth,
                "toPorts": [{"ports": [{"port": str(RPC_PORT), "protocol": "TCP"}]}],
            },
        ]
        host = urlsplit(deployment.model_url or "").hostname
        if host:
            fqdns = [{"matchName": host}]
            if host.endswith("huggingface.co"):
                fqdns.append({"matchPattern": "*.hf.co"})  # HF redirects weights to its CDN
            head_egress.append(
                {"toFQDNs": fqdns, "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]}
            )
        return {
            "apiVersion": "cilium.io/v2",
            "kind": "CiliumNetworkPolicy",
            "metadata": {"name": f"{base}-head-allow", "namespace": namespace},
            "spec": {
                "description": "llama.cpp RPC head: proxy/management in, workers and model out",
                "endpointSelector": {"matchLabels": head_labels},
                "ingress": [
                    {
                        "fromEndpoints": [{"matchLabels": proxy}, {"matchLabels": management}],
                        "authentication": auth,
                        "toPorts": [{"ports": [{"port": "8080", "protocol": "TCP"}]}],
                    }
                ],
                "egress": head_egress,
            },
        }

    def _rpc_worker_policy(self, base: str, namespace: str, pid: str) -> dict:
        """Worker CNP: the RPC port admits only this deployment's head (rpc-server has no auth)."""
        auth = {"mode": "required"}
        return {
            "apiVersion": "cilium.io/v2",
            "kind": "CiliumNetworkPolicy",
            "metadata": {"name": f"{base}-rpc-allow", "namespace": namespace},
            "spec": {
                "description": "rpc-server pods: no auth/TLS of their own, so only the head",
                "endpointSelector": {"matchLabels": self._rpc_labels(base, "worker", pid)},
                "ingress": [
                    {
                        "fromEndpoints": [{"matchLabels": self._rpc_labels(base, "head", pid)}],
                        "authentication": auth,
                        "toPorts": [{"ports": [{"port": str(RPC_PORT), "protocol": "TCP"}]}],
                    }
                ],
            },
        }

    def deploy_rpc(
        self, deployment, layout: RpcLayout, hostnames: dict[str, str], image: str
    ) -> None:
        """Create the RPC objects (policies first, head last); roll everything back on failure.

        Refuses on disabled mutual auth before any Kubernetes client is built. Policies go in
        first so no pod ever starts without its network policy.
        """
        if not self.mutual_auth_enabled:
            raise RpcRefused(RPC_AUTH_REFUSAL)
        manifests = self.export_rpc_manifests(
            deployment, layout, hostnames, image, int(deployment.n_ctx or 4096)
        )
        base, namespace = self._rpc_names(deployment)
        order = {"CiliumNetworkPolicy": 0, "Service": 1, "StatefulSet": 2, "Deployment": 3}
        manifests.sort(key=lambda doc: order[doc["kind"]])
        apps, core, custom = get_k8s_apps_client(), get_k8s_core_client(), get_k8s_custom_client()
        created: list[dict] = []
        try:
            for doc in manifests:
                kind, name = doc["kind"], doc["metadata"]["name"]
                if kind == "CiliumNetworkPolicy":
                    custom.create_namespaced_custom_object(
                        "cilium.io", "v2", namespace, "ciliumnetworkpolicies", doc
                    )
                elif kind == "Service":
                    core.create_namespaced_service(namespace=namespace, body=doc)
                elif kind == "StatefulSet":
                    apps.create_namespaced_stateful_set(namespace=namespace, body=doc)
                else:
                    apps.create_namespaced_deployment(namespace=namespace, body=doc)
                created.append(doc)
                logger.info("created %s %s for RPC deployment %s", kind, name, base)
        except Exception:
            logger.error("RPC deploy of %s failed; rolling back %d objects", base, len(created))
            self._rpc_delete_objects(apps, core, custom, namespace, created)
            raise

        shares = [round(w.tensor_split_share, 4) for w in layout.workers]
        del shares  # the persisted per-worker split is written by the API layer with the layout
        db = self.db
        db(db.llamacpp_deployments.id == deployment.id).update(
            status="deploying",
            endpoint_url=f"http://{base}-svc.{namespace}:8080",
            k8s_daemonset_name=base,
            tensor_split=",".join(str(share) for share in layout.tensor_split),
        )
        logger.info("Deployed llama.cpp RPC deployment %s in %s", base, namespace)

    @staticmethod
    def _rpc_delete_objects(apps, core, custom, namespace: str, docs: list[dict]) -> list[str]:
        """Delete ``docs`` in reverse creation order; log each failure, return the failed names."""
        failed: list[str] = []
        for doc in reversed(docs):
            kind, name = doc["kind"], doc["metadata"]["name"]
            try:
                if kind == "CiliumNetworkPolicy":
                    custom.delete_namespaced_custom_object(
                        "cilium.io", "v2", namespace, "ciliumnetworkpolicies", name
                    )
                elif kind == "Service":
                    core.delete_namespaced_service(name=name, namespace=namespace)
                elif kind == "StatefulSet":
                    apps.delete_namespaced_stateful_set(name=name, namespace=namespace)
                else:
                    apps.delete_namespaced_deployment(name=name, namespace=namespace)
            except Exception as exc:
                if getattr(exc, "status", None) == 404:
                    continue
                logger.error("failed to delete %s %s: %s", kind, name, exc)
                failed.append(f"{kind}/{name}")
        return failed

    def rpc_observe(self, deployment) -> RpcObservation:
        """Read worker and head readiness (and any terminal pod failure) for an RPC deployment.

        Worker pods are matched to their StatefulSet's expected names, so a pod that has not been
        created yet counts as down just like an unready one. Raises on Kubernetes API errors.
        """
        base, namespace = self._rpc_names(deployment)
        selector = f"{_DEPLOYMENT_LABEL}={base},{_OWNER_LABEL}={deployment.public_id}"
        apps, core = get_k8s_apps_client(), get_k8s_core_client()
        expected: list[str] = []
        for sts in apps.list_namespaced_stateful_set(
            namespace=namespace, label_selector=selector
        ).items:
            expected.extend(f"{sts.metadata.name}-{i}" for i in range(int(sts.spec.replicas or 0)))
        ready_workers: set[str] = set()
        head_ready = False
        failed: str | None = None
        for pod in core.list_namespaced_pod(namespace=namespace, label_selector=selector).items:
            name = pod.metadata.name
            ready = any(
                c.type == "Ready" and c.status == "True" for c in (pod.status.conditions or [])
            )
            if (pod.metadata.labels or {}).get(_ROLE_LABEL) == "head":
                head_ready = head_ready or ready
            elif ready:
                ready_workers.add(name)
            statuses = [
                *(pod.status.init_container_statuses or []),
                *(pod.status.container_statuses or []),
            ]
            for status in statuses:
                waiting = getattr(getattr(status, "state", None), "waiting", None)
                if waiting is not None and waiting.reason in _FAILED_WAITING and failed is None:
                    failed = f"{waiting.reason} on {name}"
        head = apps.read_namespaced_deployment(name=base, namespace=namespace)
        for cond in (head.status.conditions or []) if head.status else []:
            if cond.reason == "ProgressDeadlineExceeded" and failed is None:
                failed = f"ProgressDeadlineExceeded on {base}"
        down = sorted(set(expected) - ready_workers)
        return RpcObservation(
            workers_total=len(expected),
            workers_up=len(expected) - len(down),
            workers_down=down,
            head_ready=head_ready,
            failed_message=failed,
        )

    def remove_rpc(self, deployment) -> None:
        """Delete every Kubernetes object of an RPC deployment and mark it stopped.

        Worker StatefulSets are found by label (their count is a layout detail), the rest by
        name. Every deletion is attempted; a non-404 failure raises after the rest completed.
        """
        base, namespace = self._rpc_names(deployment)
        apps, core, custom = get_k8s_apps_client(), get_k8s_core_client(), get_k8s_custom_client()
        selector = f"{_DEPLOYMENT_LABEL}={base},{_OWNER_LABEL}={deployment.public_id}"
        statefulsets = apps.list_namespaced_stateful_set(
            namespace=namespace, label_selector=selector
        )
        docs: list[dict] = [
            {"kind": "CiliumNetworkPolicy", "metadata": {"name": f"{base}-head-allow"}},
            {"kind": "CiliumNetworkPolicy", "metadata": {"name": f"{base}-rpc-allow"}},
            {"kind": "Service", "metadata": {"name": f"{base}-svc"}},
            {"kind": "Service", "metadata": {"name": f"{base}-rpc"}},
            *(
                {"kind": "StatefulSet", "metadata": {"name": sts.metadata.name}}
                for sts in statefulsets.items
            ),
            {"kind": "Deployment", "metadata": {"name": base}},
        ]
        failed = self._rpc_delete_objects(apps, core, custom, namespace, docs)
        if failed:
            raise RuntimeError(f"could not delete {', '.join(failed)}")
        db = self.db
        db(db.llamacpp_deployments.id == deployment.id).update(status="stopped")
        logger.info("Removed llama.cpp RPC deployment %s", base)

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

    def _refuse_plain_gpu_on_ai_node(self, config: LlamaCppDeploymentConfig) -> str | None:
        """Refuse a plain ``nvidia.com/gpu`` request aimed at a renamed-resource AI node.

        Deliberately NOT feature-flag gated: a node advertising per-class GPU resources
        can never schedule a plain request, so the refusal applies whenever the
        selector shows one.
        """
        return refuse_plain_gpu_for_target_nodes(
            dict(config.node_selector or {}),
            config.gpu_count,
            _list_candidate_nodes,
            kubernetes=config.deployment_type == "kubernetes",
        )

    def create_deployment(self, config: LlamaCppDeploymentConfig) -> dict[str, Any]:
        """Insert a new ``llamacpp_deployments`` row. Does not deploy it — see provision()."""
        db = self.db

        refusal = self._refuse_plain_gpu_on_ai_node(config)
        if refusal:
            return {"success": False, "error": refusal}
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
