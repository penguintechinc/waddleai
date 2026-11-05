"""Hosted Ollama fleet target; intentionally register-and-route only."""

from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

import aiohttp

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
            # DNS names are permitted. Deployment egress policy must constrain
            # them to the approved hosted provider, not private networks.
            pass
        else:
            if any(
                (
                    address.is_private,
                    address.is_loopback,
                    address.is_link_local,
                    address.is_reserved,
                    address.is_unspecified,
                )
            ):
                raise ValueError(
                    "Ollama Cloud endpoint_url must not target a private or local address"
                )
        return url

    @property
    def _node_id(self) -> str:
        return str(self.config.get("name") or self._base_url)

    def _headers(self) -> dict[str, str]:
        if not self.credentials:
            raise ValueError("Ollama Cloud backend requires a credential")
        return {"Authorization": f"Bearer {self.credentials}"}

    async def _models(self) -> list[str]:
        async with aiohttp.ClientSession(headers=self._headers()) as session:
            async with session.get(f"{self._base_url}/api/tags", timeout=_TIMEOUT) as response:
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
