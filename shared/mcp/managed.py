"""Safe managed-MCP lifecycle primitives (Wave 5).

This module deliberately has no Kubernetes client dependency.  The management
service supplies a narrowly-scoped driver whose service account and namespace
policy are configured by the operator.  Keeping that boundary explicit avoids
turning an MCP registration request into arbitrary workload execution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

_IMAGE_DIGEST = re.compile(r"^[a-z0-9][a-z0-9./_-]*@sha256:[0-9a-f]{64}$")
_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


class ManagedMcpValidationError(ValueError):
    """Raised before an unsafe managed-MCP request reaches a deployment driver."""


@dataclass(frozen=True, slots=True)
class ManagedMcpSpec:
    """Specification of a managed MCP server to run.

    Validated against allowed namespaces and a safe contract.
    """

    name: str
    image: str
    namespace: str
    transport: str = "streamable_http"
    network_policy: str = "restricted-egress"
    service_account: str = "waddleai-managed-mcp"

    def validate(self, allowed_namespaces: set[str]) -> None:
        """Validate the spec against the allowed namespaces.

        Raises on any unsafe or disallowed value.
        """
        if not _NAME.fullmatch(self.name):
            raise ManagedMcpValidationError("name must be a DNS label")
        if not _IMAGE_DIGEST.fullmatch(self.image):
            raise ManagedMcpValidationError("image must be pinned by sha256 digest")
        if self.namespace not in allowed_namespaces:
            raise ManagedMcpValidationError("namespace is not approved for managed MCP")
        if self.transport not in {"streamable_http", "stdio"}:
            raise ManagedMcpValidationError("unsupported MCP transport")
        if self.network_policy != "restricted-egress":
            raise ManagedMcpValidationError("managed MCP requires restricted-egress policy")
        if self.service_account != "waddleai-managed-mcp":
            raise ManagedMcpValidationError("managed MCP service account is operator controlled")


@dataclass(frozen=True, slots=True)
class ManagedMcpEndpoint:
    """Result of provisioning: the live endpoint and discovered tools.

    Returned once discovery has succeeded.
    """

    name: str
    namespace: str
    url: str
    transport: str
    status: str
    discovered_tools: tuple[str, ...]


class ManagedMcpDriver(Protocol):
    """The only capability the lifecycle service requires from a cluster adapter."""

    async def create(self, spec: ManagedMcpSpec) -> None:
        """Create the workload for a spec."""

    async def wait_ready(self, spec: ManagedMcpSpec) -> str:
        """Wait until the workload is ready and return its URL."""

    async def discover_tools(self, endpoint_url: str, transport: str) -> list[str]:
        """Discover tool names from the endpoint."""

    async def delete(self, spec: ManagedMcpSpec) -> None:
        """Delete the workload for a spec."""


class ManagedMcpLifecycle:
    """Provision, verify, discover, and clean up an immutable managed MCP server."""

    def __init__(self, driver: ManagedMcpDriver, *, allowed_namespaces: set[str]) -> None:
        """Bind a driver and the allowed namespaces.

        The lifecycle only provisions within these namespaces.
        """
        self._driver = driver
        self._allowed_namespaces = frozenset(allowed_namespaces)

    async def provision(self, spec: ManagedMcpSpec) -> ManagedMcpEndpoint:
        """Provision a server: validate, create, wait ready, then discover tools.

        Cleans up the workload if any step fails.
        """
        spec.validate(set(self._allowed_namespaces))
        created = False
        try:
            await self._driver.create(spec)
            created = True
            url = await self._driver.wait_ready(spec)
            tools = await self._driver.discover_tools(url, spec.transport)
            return ManagedMcpEndpoint(
                name=spec.name,
                namespace=spec.namespace,
                url=url,
                transport=spec.transport,
                status="active",
                discovered_tools=tuple(sorted(set(tools))),
            )
        except Exception:
            if created:
                await self._driver.delete(spec)
            raise

    async def cleanup(self, spec: ManagedMcpSpec) -> None:
        """Delete the workload for a spec.

        Safe to call after a partial provision.
        """
        spec.validate(set(self._allowed_namespaces))
        await self._driver.delete(spec)
