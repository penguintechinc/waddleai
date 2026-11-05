"""In-cluster Kubernetes driver for managed MCP workloads (Wave 5)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from .managed import ManagedMcpSpec


class KubernetesManagedMcpDriver:
    """Minimal, least-privilege Kubernetes API driver.

    The Management deployment's service account must be granted only
    deployments/services/networkpolicies in the approved MCP namespace.
    """

    def __init__(
        self,
        *,
        api_url: str = "https://kubernetes.default.svc",
        token_path: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token"),
        ca_path: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"),
        discover: Callable[[str, str], Awaitable[list[str]]] | None = None,
    ) -> None:
        """Configure the Kubernetes driver with its client and defaults.

        Generated workloads are non-privileged and network restricted.
        """
        self.api_url, self.token_path, self.ca_path, self.discover = (
            api_url.rstrip("/"),
            token_path,
            ca_path,
            discover,
        )

    def _client(self) -> httpx.AsyncClient:
        token = self.token_path.read_text(encoding="utf-8").strip()
        return httpx.AsyncClient(
            base_url=self.api_url,
            verify=str(self.ca_path),
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )

    @staticmethod
    def _labels(spec: ManagedMcpSpec) -> dict[str, str]:
        return {"app.kubernetes.io/name": spec.name, "app.kubernetes.io/managed-by": "waddleai"}

    def _deployment(self, spec: ManagedMcpSpec) -> dict[str, Any]:
        labels = self._labels(spec)
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": spec.name, "namespace": spec.namespace, "labels": labels},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "serviceAccountName": spec.service_account,
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "server",
                                "image": spec.image,
                                "ports": [{"containerPort": 8080, "name": "mcp"}],
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "runAsNonRoot": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "128Mi"},
                                    "limits": {"cpu": "1", "memory": "512Mi"},
                                },
                            }
                        ],
                    },
                },
            },
        }

    def _network_policy(self, spec: ManagedMcpSpec) -> dict[str, Any]:
        """Default-deny policy: only WaddleAI-labelled pods may call MCP; DNS only egress."""
        labels = self._labels(spec)
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": f"{spec.name}-restricted", "namespace": spec.namespace},
            "spec": {
                "podSelector": {"matchLabels": labels},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [
                    {
                        "from": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {"app.kubernetes.io/part-of": "waddleai"}
                                }
                            }
                        ],
                        "ports": [{"protocol": "TCP", "port": 8080}],
                    }
                ],
                "egress": [
                    {
                        "to": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                                }
                            }
                        ],
                        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
                    }
                ],
            },
        }

    async def create(self, spec: ManagedMcpSpec) -> None:
        """Create the Kubernetes workload for a managed MCP server.

        Applies the restricted security and network settings.
        """
        try:
            async with self._client() as client:
                response = await client.post(
                    f"/apis/apps/v1/namespaces/{spec.namespace}/deployments",
                    json=self._deployment(spec),
                )
                response.raise_for_status()
                labels = self._labels(spec)
                service = {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": spec.name, "namespace": spec.namespace, "labels": labels},
                    "spec": {
                        "selector": labels,
                        "ports": [{"name": "mcp", "port": 8080, "targetPort": "mcp"}],
                    },
                }
                response = await client.post(
                    f"/api/v1/namespaces/{spec.namespace}/services", json=service
                )
                response.raise_for_status()
                response = await client.post(
                    f"/apis/networking.k8s.io/v1/namespaces/{spec.namespace}/networkpolicies",
                    json=self._network_policy(spec),
                )
                response.raise_for_status()
        except Exception:
            await self.delete(spec)
            raise

    async def wait_ready(self, spec: ManagedMcpSpec) -> str:
        """Wait until the workload is ready and return its endpoint URL.

        Raises if it does not become ready in time.
        """
        async with self._client() as client:
            for _ in range(30):
                response = await client.get(
                    f"/apis/apps/v1/namespaces/{spec.namespace}/deployments/{spec.name}"
                )
                response.raise_for_status()
                if response.json().get("status", {}).get("availableReplicas", 0) >= 1:
                    return f"http://{spec.name}.{spec.namespace}.svc.cluster.local:8080/mcp"
                await asyncio.sleep(1)
        raise TimeoutError(f"managed MCP {spec.name} did not become ready")

    async def discover_tools(self, endpoint_url: str, transport: str) -> list[str]:
        """Discover the tool names exposed by the MCP endpoint.

        Used to verify the server before marking it active.
        """
        if self.discover is None:
            raise RuntimeError("managed MCP tool discovery adapter is not configured")
        return await self.discover(endpoint_url, transport)

    async def delete(self, spec: ManagedMcpSpec) -> None:
        """Delete the workload created for a managed MCP server.

        Used for cleanup on failure and on removal.
        """
        async with self._client() as client:
            for path in (
                f"/apis/apps/v1/namespaces/{spec.namespace}/deployments/{spec.name}",
                f"/api/v1/namespaces/{spec.namespace}/services/{spec.name}",
                f"/apis/networking.k8s.io/v1/namespaces/{spec.namespace}/networkpolicies/{spec.name}-restricted",
            ):
                response = await client.delete(path)
                if response.status_code not in {200, 202, 404}:
                    response.raise_for_status()
