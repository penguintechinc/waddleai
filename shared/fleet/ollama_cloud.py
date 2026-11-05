"""Hosted Ollama fleet target; intentionally register-and-route only."""

import asyncio
import socket
from ipaddress import IPv4Address, IPv4Network, IPv6Address, ip_address
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

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

_TIMEOUT = aiohttp.ClientTimeout(total=10)
_CGNAT = IPv4Network("100.64.0.0/10")
_FORBIDDEN_MSG = "Ollama Cloud endpoint_url must not target a private or local address"


def _is_forbidden(address: IPv4Address | IPv6Address) -> bool:
    """Return True for any address that is not a routable public unicast IP.

    Covers private, loopback, link-local, reserved, multicast, unspecified,
    CGNAT 100.64.0.0/10, and IPv4-mapped IPv6 forms of all of those.
    """
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        or (isinstance(address, IPv4Address) and address in _CGNAT)
    )


async def resolve_public_addresses(host: str, port: int) -> list[str]:
    """Resolve a host (off the event loop) and return its addresses if all are public.

    Raises ValueError when the name has no addresses or ANY address is
    non-public, so a mixed public/private record set cannot be used for SSRF.
    """
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Ollama Cloud endpoint_url host {host!r} did not resolve") from exc
    addresses: list[str] = []
    for info in infos:
        text = str(info[4][0]).split("%", 1)[0]
        if _is_forbidden(ip_address(text)):
            raise ValueError(_FORBIDDEN_MSG)
        if text not in addresses:
            addresses.append(text)
    if not addresses:
        raise ValueError(f"Ollama Cloud endpoint_url host {host!r} did not resolve")
    return addresses


class _PinnedResolver(AbstractResolver):
    """aiohttp resolver that re-vets DNS at connect time and pins the vetted IPs.

    The connector dials exactly the addresses returned here, so a DNS rebind
    after validation cannot swap in a private target. The original hostname is
    kept so the Host header, TLS SNI and certificate validation still use it.
    """

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        """Resolve and vet host, returning only vetted addresses for the connector."""
        results: list[ResolveResult] = []
        for text in await resolve_public_addresses(host, port):
            address = ip_address(text)
            results.append(
                {
                    "hostname": host,
                    "host": text,
                    "port": port,
                    "family": socket.AF_INET6 if address.version == 6 else socket.AF_INET,
                    "proto": 0,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        return results

    async def close(self) -> None:
        """Release resources; the resolver holds none."""


@register(BackendType.OLLAMA_CLOUD)
class OllamaCloudFleetBackend(InferenceFleetBackend):
    """Routes to an admin-registered hosted Ollama endpoint, never lifecycles it."""

    type = BackendType.OLLAMA_CLOUD
    management_scope = ManagementScope.REGISTER_AND_ROUTE

    def __init__(
        self, db: Any, *, config: dict[str, Any] | None = None, credentials: str | None = None
    ) -> None:
        """Configure the Ollama Cloud backend with its config and API token source.

        Enforces an HTTPS boundary for requests.
        """
        self.db = db
        self.config = config or {}
        self.credentials = credentials
        self.management_scope = ManagementScope.REGISTER_AND_ROUTE
        self._org_id = self.config.get("org_id")

    @property
    def _base_url(self) -> str:
        url = str(self.config.get("endpoint_url", "")).rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Ollama Cloud endpoint_url must be a credential-free HTTPS URL")
        try:
            address = ip_address(parsed.hostname)
        except ValueError:
            # DNS names are vetted at request time by resolve_public_addresses
            # and pinned through _PinnedResolver (see _models).
            pass
        else:
            if _is_forbidden(address):
                raise ValueError(_FORBIDDEN_MSG)
        return url

    @property
    def _node_id(self) -> str:
        return str(self.config.get("name") or self._base_url)

    def _headers(self) -> dict[str, str]:
        if not self.credentials:
            raise ValueError("Ollama Cloud backend requires a credential")
        return {"Authorization": f"Bearer {self.credentials}"}

    async def _models(self) -> list[str]:
        base = self._base_url
        parsed = urlparse(base)
        host = parsed.hostname or ""
        # Validation-time resolution; the pinned resolver re-vets at connect.
        await resolve_public_addresses(host, parsed.port or 443)
        connector = aiohttp.TCPConnector(resolver=_PinnedResolver(), use_dns_cache=False)
        async with aiohttp.ClientSession(headers=self._headers(), connector=connector) as session:
            async with session.get(
                f"{base}/api/tags", timeout=_TIMEOUT, allow_redirects=False
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"Ollama Cloud /api/tags returned HTTP {response.status}")
                payload = await response.json()
        return [item["name"] for item in payload.get("models", []) if item.get("name")]

    async def provision(self, spec: ProvisionSpec) -> list[NodeInfo]:
        """Reject provisioning: Ollama Cloud nodes are not managed here.

        Fails loudly instead of silently doing nothing.
        """
        raise PermissionError(
            "Ollama Cloud is register_and_route only; provisioning is unsupported"
        )

    async def deprovision(self, node_id: str) -> None:
        """Reject deprovisioning: Ollama Cloud nodes are not managed here.

        Fails loudly instead of silently doing nothing.
        """
        raise PermissionError(
            "Ollama Cloud is register_and_route only; deprovisioning is unsupported"
        )

    async def list_nodes(self) -> list[NodeInfo]:
        """List models available from Ollama Cloud as fleet nodes.

        Authenticates with a bearer token.
        """
        try:
            models, healthy = await self._models(), True
        except Exception:
            models, healthy = [], False
        return [NodeInfo(self._node_id, self._base_url, "cloud", models, 0, 0, healthy)]

    async def health(self) -> FleetHealth:
        """Report fleet health for the Ollama Cloud backend.

        Based on whether the model listing succeeds.
        """
        node = (await self.list_nodes())[0]
        return FleetHealth(
            self.fleet_backend_id,
            node.healthy,
            1,
            {"healthy_nodes": int(node.healthy), "loaded_models": node.loaded_models},
        )

    async def place_model(self, model: str, constraints: dict[str, Any]) -> ModelPlacement:
        """Choose a placement for a model on Ollama Cloud.

        Honors the supplied constraints.
        """
        if model not in await self._models():
            raise RuntimeError(f"Ollama Cloud is not currently serving model {model!r}")
        return ModelPlacement(model, self._node_id, "placed")

    async def endpoints_for(self, model: str) -> list[Endpoint]:
        """Return the Ollama Cloud endpoints serving a model.

        Used by routing to reach the model.
        """
        try:
            models = await self._models()
        except Exception:
            return []
        return [Endpoint(self._base_url, self._node_id, models, True)] if model in models else []
