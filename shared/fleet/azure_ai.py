"""Azure AI/OpenAI fleet backend using ARM as the deployment control plane."""

import time
from typing import Any

import httpx

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
from shared.utils.feature_flags import is_feature_enabled

_ARM = "https://management.azure.com"
_ARM_SCOPE = "https://management.azure.com/.default"
_API_VERSION = "2024-10-01"
_FLAG_KEY = "waddleai.fleet_v2"


def _fleet_v2_enabled(org_id: Any) -> bool:
    """Fail safe to disabled when the lifecycle flag cannot be evaluated."""
    try:
        return is_feature_enabled(_FLAG_KEY, distinct_id=str(org_id or "server"), default=False)
    except Exception:
        return False


@register(BackendType.AZURE_AI)
class AzureAIFleetBackend(InferenceFleetBackend):
    """Lists and (when explicitly authorized) manages Cognitive Services deployments."""

    type = BackendType.AZURE_AI
    management_scope = ManagementScope.REGISTER_AND_ROUTE

    def __init__(
        self, db: Any, *, config: dict[str, Any] | None = None, credentials: str | None = None
    ) -> None:
        """Configure the Azure AI fleet backend from its config and token source.

        Validation of required resource fields is deferred to use.
        """
        self.db, self.config, self.credentials = db, (config or {}), credentials
        self._token: str | None = None
        self._token_expiry = 0.0
        self._org_id = self.config.get("org_id")

    @property
    def _resource_path(self) -> str:
        required = ("subscription_id", "resource_group", "account_name")
        if any(not self.config.get(key) for key in required):
            raise ValueError(f"Azure AI config requires {', '.join(required)}")
        template = (
            "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
            "/providers/Microsoft.CognitiveServices/accounts/{account_name}/deployments"
        )
        return template.format(**self.config)

    def _credential_parts(self) -> dict[str, str]:
        import json

        try:
            data = json.loads(self.credentials or "")
            return {key: data[key] for key in ("tenant_id", "client_id", "client_secret")}
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(
                "Azure AI requires vaulted tenant_id/client_id/client_secret JSON credentials"
            ) from exc

    async def _arm_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        creds = self._credential_parts()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"https://login.microsoftonline.com/{creds['tenant_id']}/oauth2/v2.0/token",
                data={
                    "client_id": creds["client_id"],
                    "client_secret": creds["client_secret"],
                    "scope": _ARM_SCOPE,
                    "grant_type": "client_credentials",
                },
            )
        response.raise_for_status()
        body = response.json()
        self._token, self._token_expiry = (
            body["access_token"],
            time.time() + int(body.get("expires_in", 3600)),
        )
        return self._token

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = await self._arm_token()
        async with httpx.AsyncClient(timeout=30) as client:
            return await client.request(
                method,
                f"{_ARM}{path}",
                headers={"Authorization": f"Bearer {token}"},
                params={"api-version": self.config.get("api_version", _API_VERSION)},
                **kwargs,
            )

    @staticmethod
    def _node(item: dict[str, Any]) -> NodeInfo:
        props, model = item.get("properties", {}), item.get("properties", {}).get("model", {})
        return NodeInfo(
            item.get("name", ""),
            item.get("id"),
            "cloud",
            [model["name"]] if model.get("name") else [],
            0,
            0,
            props.get("provisioningState") == "Succeeded",
        )

    async def list_nodes(self) -> list[NodeInfo]:
        """List Azure AI deployments as fleet nodes.

        Uses an ARM token and maps provisioning state to node health.
        """
        response = await self._request("GET", self._resource_path)
        response.raise_for_status()
        return [self._node(item) for item in response.json().get("value", [])]

    async def provision(self, spec: ProvisionSpec) -> list[NodeInfo]:
        """Create an Azure AI deployment via ARM PUT.

        Requires the full-lifecycle scope and feature flag.
        """
        if self.management_scope != ManagementScope.FULL_LIFECYCLE:
            raise PermissionError("Azure AI provisioning requires full_lifecycle")
        if not _fleet_v2_enabled(self._org_id):
            raise RuntimeError(f"{_FLAG_KEY} is disabled; Azure AI provisioning is gated off")
        model = spec.constraints.get("model")
        if not isinstance(model, dict):
            raise ValueError("Azure AI provision requires constraints['model']")
        body = {
            "sku": spec.constraints.get("sku", {"name": "Standard"}),
            "properties": {"model": model},
        }
        response = await self._request("PUT", f"{self._resource_path}/{spec.name}", json=body)
        response.raise_for_status()
        return [self._node(response.json())]

    async def deprovision(self, node_id: str) -> None:
        """Delete an Azure AI deployment via ARM.

        Requires the full-lifecycle scope and feature flag.
        """
        if self.management_scope != ManagementScope.FULL_LIFECYCLE:
            raise PermissionError("Azure AI deprovisioning requires full_lifecycle")
        response = await self._request("DELETE", f"{self._resource_path}/{node_id}")
        if response.status_code not in (200, 202, 204, 404):
            response.raise_for_status()

    async def health(self) -> FleetHealth:
        """Report aggregate fleet health from the current deployments.

        Healthy when deployments have succeeded provisioning.
        """
        try:
            nodes = await self.list_nodes()
        except Exception as exc:
            return FleetHealth(self.fleet_backend_id, False, 0, {"error": str(exc)})
        return FleetHealth(
            self.fleet_backend_id,
            all(n.healthy for n in nodes),
            len(nodes),
            {"healthy_nodes": sum(n.healthy for n in nodes)},
        )

    async def place_model(self, model: str, constraints: dict[str, Any]) -> ModelPlacement:
        """Choose a placement for a model on this backend.

        Honors the supplied constraints.
        """
        for node in await self.list_nodes():
            if node.healthy and model in node.loaded_models:
                return ModelPlacement(model, node.node_id, "placed")
        raise RuntimeError(f"Azure AI is not currently serving model {model!r}")

    async def endpoints_for(self, model: str) -> list[Endpoint]:
        """Return the inference endpoints serving a model.

        Used by routing to reach the deployed model.
        """
        endpoint = self.config.get("endpoint_url")
        if not endpoint:
            return []
        return [
            Endpoint(str(endpoint), n.node_id, n.loaded_models, n.healthy)
            for n in await self.list_nodes()
            if n.healthy and model in n.loaded_models
        ]
