"""
llama.cpp Deployment Manager

Manages llama-server instances in two modes:
- kubernetes: Creates/removes K8s DaemonSets targeting GPU-labelled nodes
- remote:     Registers an existing llama-server endpoint after a health check
"""

import logging
import re

import requests
import yaml

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


class LlamaCppManager:
    """Manages llama-server deployment lifecycle."""

    def __init__(self, db):
        self.db = db

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
                {"name": "tmp", "mountPath": "/tmp"},
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
                "--host", "0.0.0.0",
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
                {"name": "tmp", "mountPath": "/tmp"},
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
