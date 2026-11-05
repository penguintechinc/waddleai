"""Tests for the hosted Ollama register-and-route backend."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.fleet.base import BackendType, ManagementScope, ProvisionSpec
from shared.fleet.ollama_cloud import OllamaCloudFleetBackend


def _session(status=200, payload=None):
    response = AsyncMock(status=status)
    response.json = AsyncMock(return_value=payload or {"models": [{"name": "gemma4:e4b"}]})
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get.return_value = response
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _backend():
    return OllamaCloudFleetBackend(
        None, config={"endpoint_url": "https://ollama.com", "name": "hosted"}, credentials="secret"
    )


def test_type_scope_and_https_boundary():
    """Verify: type scope and https boundary."""
    assert _backend().type == BackendType.OLLAMA_CLOUD
    assert _backend().management_scope == ManagementScope.REGISTER_AND_ROUTE
    with pytest.raises(ValueError, match="HTTPS"):
        _ = OllamaCloudFleetBackend(
            None, config={"endpoint_url": "http://host"}, credentials="x"
        )._base_url
    with pytest.raises(ValueError, match="private or local"):
        _ = OllamaCloudFleetBackend(
            None, config={"endpoint_url": "https://127.0.0.1:11434"}, credentials="x"
        )._base_url


async def test_models_route_with_bearer_auth():
    """Verify: models route with bearer auth."""
    backend = _backend()
    with patch("aiohttp.ClientSession", return_value=_session()) as client:
        endpoints = await backend.endpoints_for("gemma4:e4b")
    assert endpoints[0].node_id == "hosted"
    assert client.call_args.kwargs["headers"] == {"Authorization": "Bearer secret"}


@pytest.mark.parametrize("method", ["provision", "deprovision"])
async def test_lifecycle_is_rejected(method):
    """Verify: lifecycle is rejected."""
    backend = _backend()
    with pytest.raises(PermissionError, match="register_and_route"):
        if method == "provision":
            await backend.provision(ProvisionSpec("x", [], "cloud", {}))
        else:
            await backend.deprovision("x")
