"""Contract tests for Azure AI's ARM-backed fleet implementation."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.fleet.azure_ai import AzureAIFleetBackend
from shared.fleet.base import BackendType, ManagementScope, ProvisionSpec


def _backend() -> AzureAIFleetBackend:
    return AzureAIFleetBackend(
        None,
        config={
            "subscription_id": "sub",
            "resource_group": "rg",
            "account_name": "acct",
            "endpoint_url": "https://acct.openai.azure.com",
        },
        credentials=json.dumps(
            {"tenant_id": "tenant", "client_id": "client", "client_secret": "secret"}
        ),
    )


def _client(responses):
    client = MagicMock()
    client.post = AsyncMock(return_value=responses[0])
    client.request = AsyncMock(return_value=responses[-1])
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _response(payload, status=200):
    response = MagicMock(status_code=status)
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def test_type_and_resource_path():
    """Verify: type and resource path."""
    backend = _backend()
    assert backend.type == BackendType.AZURE_AI
    assert backend._resource_path.endswith("accounts/acct/deployments")


async def test_list_nodes_uses_arm_token_and_marks_succeeded_healthy():
    """Verify: list nodes uses arm token and marks succeeded healthy."""
    backend = _backend()
    token = _response({"access_token": "arm-token", "expires_in": 3600})
    deployments = _response(
        {
            "value": [
                {
                    "id": "/id",
                    "name": "gpt",
                    "properties": {"provisioningState": "Succeeded", "model": {"name": "gpt-4o"}},
                }
            ]
        }
    )
    client = _client([token, deployments])
    with patch("httpx.AsyncClient", return_value=client):
        nodes = await backend.list_nodes()
    assert nodes[0].healthy is True
    assert nodes[0].loaded_models == ["gpt-4o"]
    assert client.post.call_args.kwargs["data"]["scope"] == "https://management.azure.com/.default"
    assert client.request.call_args.args[0] == "GET"
    assert client.request.call_args.kwargs["headers"] == {"Authorization": "Bearer arm-token"}


async def test_lifecycle_requires_full_lifecycle_scope():
    """Verify: lifecycle requires full lifecycle scope."""
    backend = _backend()
    with pytest.raises(PermissionError, match="full_lifecycle"):
        await backend.provision(ProvisionSpec("gpt", [], "cloud", {"model": {"name": "gpt-4o"}}))
    with pytest.raises(PermissionError, match="full_lifecycle"):
        await backend.deprovision("gpt")


async def test_full_lifecycle_uses_arm_put():
    """Verify: full lifecycle uses arm put."""
    backend = _backend()
    backend.management_scope = ManagementScope.FULL_LIFECYCLE
    token = _response({"access_token": "arm-token"})
    created = _response(
        {
            "id": "/id",
            "name": "gpt",
            "properties": {"provisioningState": "Creating", "model": {"name": "gpt-4o"}},
        }
    )
    client = _client([token, created])
    with (
        patch("shared.fleet.azure_ai._fleet_v2_enabled", return_value=True),
        patch("httpx.AsyncClient", return_value=client),
    ):
        nodes = await backend.provision(
            ProvisionSpec("gpt", [], "cloud", {"model": {"name": "gpt-4o"}})
        )
    assert nodes[0].healthy is False
    assert client.request.call_args.args[0] == "PUT"


async def test_lifecycle_provision_is_feature_flag_gated():
    """Verify: lifecycle provision is feature flag gated."""
    backend = _backend()
    backend.management_scope = ManagementScope.FULL_LIFECYCLE
    with (
        patch("shared.fleet.azure_ai._fleet_v2_enabled", return_value=False),
        pytest.raises(RuntimeError, match="gated off"),
    ):
        await backend.provision(ProvisionSpec("gpt", [], "cloud", {"model": {"name": "gpt-4o"}}))
