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
        """Return DaemonSet + Service YAML for the given deployment."""
        ds_name = deployment.k8s_daemonset_name or self._daemonset_name(deployment.name)
        namespace = deployment.k8s_namespace or "waddleai"
        node_selector = deployment.node_selector or {}

        daemonset = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": ds_name, "namespace": namespace},
            "spec": {
                "selector": {"matchLabels": {"app": ds_name}},
                "template": {
                    "metadata": {"labels": {"app": ds_name}},
                    "spec": {
                        "nodeSelector": node_selector,
                        "initContainers": [
                            {
                                "name": "download-model",
                                "image": "curlimages/curl:latest",
                                # Vuln D fix: use argv format, no shell metacharacter parsing
                                "command": [
                                    "curl",
                                    "-fsSL",
                                    "-o", f"/models/{deployment.model_filename}",
                                    deployment.model_url,
                                ],
                                "volumeMounts": [{"name": "model-storage", "mountPath": "/models"}],
                            }
                        ],
                        "containers": [
                            {
                                "name": "llama-server",
                                "image": "ghcr.io/ggerganov/llama.cpp:server",
                                "args": [
                                    "--model", f"/models/{deployment.model_filename}",
                                    "--n-gpu-layers", str(deployment.n_gpu_layers),
                                    "--ctx-size", str(deployment.n_ctx),
                                    "--port", "8080",
                                    "--host", "0.0.0.0",
                                ],
                                "ports": [{"containerPort": 8080}],
                                "resources": {
                                    "limits": {"nvidia.com/gpu": str(deployment.gpu_count)}
                                },
                                "volumeMounts": [{"name": "model-storage", "mountPath": "/models"}],
                            }
                        ],
                        "volumes": [{"name": "model-storage", "emptyDir": {}}],
                    },
                },
            },
        }

        if deployment.node_affinity:
            daemonset["spec"]["template"]["spec"]["affinity"] = {
                "nodeAffinity": deployment.node_affinity
            }

        service = {
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
