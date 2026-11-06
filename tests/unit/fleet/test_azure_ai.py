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


def _gai(*addrs):
    import socket

    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443)) for a in addrs]


def _with_endpoint(url):
    backend = _backend()
    backend.config["endpoint_url"] = url
    return backend


def _healthy(backend):
    from shared.fleet.base import NodeInfo

    backend.list_nodes = AsyncMock(
        return_value=[NodeInfo("d", "id", "cloud", ["gpt-4o"], 0, 0, True)]
    )
    return backend


@pytest.mark.parametrize(
    "url",
    [
        "http://acct.openai.azure.com",  # not HTTPS
        "https://evil.example.com",  # outside allowed suffixes
        "https://openai.azure.com.evil.example",  # suffix spoof
        "https://169.254.169.254",  # metadata IP literal
        "https://10.0.0.5",  # private IP literal
        "https://user:pw@acct.openai.azure.com",  # credentials
    ],
)
async def test_bad_endpoint_url_rejected_at_request_time(url):
    """Verify: non-HTTPS, private/metadata IPs, foreign hosts never reach routing (SSRF)."""
    with pytest.raises(ValueError):
        await _healthy(_with_endpoint(url)).endpoints_for("gpt-4o")


@pytest.mark.parametrize("url", ["https://evil.example.com", "http://a.openai.azure.com"])
def test_validate_endpoint_url_rejects(url):
    """Verify: create-time validator rejects bad hosts/schemes."""
    from shared.fleet.azure_ai import validate_endpoint_url

    with pytest.raises(ValueError):
        validate_endpoint_url(url)


@pytest.mark.parametrize(
    "host",
    ["a.openai.azure.com", "a.cognitiveservices.azure.com", "a.services.ai.azure.com"],
)
async def test_valid_azure_host_accepted(host):
    """Verify: each allowed suffix with a public resolution yields an endpoint."""
    with patch("shared.fleet.egress_guard.socket.getaddrinfo", return_value=_gai("104.18.7.1")):
        eps = await _healthy(_with_endpoint(f"https://{host}/")).endpoints_for("gpt-4o")
    assert [e.url for e in eps] == [f"https://{host}"]


async def test_azure_host_resolving_private_rejected():
    """Verify: an allowed-suffix name that resolves to a metadata IP is rejected."""
    with patch(
        "shared.fleet.egress_guard.socket.getaddrinfo", return_value=_gai("169.254.169.254")
    ):
        with pytest.raises(ValueError, match="private or local"):
            await _healthy(_backend()).endpoints_for("gpt-4o")


async def test_azure_rebind_between_validation_and_request_rejected():
    """Verify: a rebind after validation is caught by the pinned resolver at connect time."""
    from shared.fleet.egress_guard import PinnedResolver, resolve_public_addresses

    with patch("shared.fleet.egress_guard.socket.getaddrinfo") as gai:
        gai.return_value = _gai("104.18.7.1")
        assert await resolve_public_addresses("a.openai.azure.com", 443) == ["104.18.7.1"]
        gai.return_value = _gai("169.254.169.254")
        with pytest.raises(ValueError, match="private or local"):
            await PinnedResolver("Azure AI").resolve("a.openai.azure.com", 443)
