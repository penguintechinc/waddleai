"""Tests for ``VertexAIFleetBackend`` (shared.fleet.vertex_ai) — spec §10.1 plan Task 10.

All HTTP calls (OAuth2 token mint + Vertex AI REST API) are mocked via
``httpx.AsyncClient`` — no live GCP project required.
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from shared.fleet.base import BackendType, ManagementScope, ProvisionSpec
from shared.fleet.vertex_ai import VertexAIFleetBackend


def _throwaway_rsa_private_key_pem() -> str:
    """A real (test-only) RSA private key PEM — jwt.encode(algorithm="RS256") needs a parseable key.

    Generated fresh per test session; never used outside this test file.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


_FAKE_SA = {
    "type": "service_account",
    "project_id": "waddleai-test",
    "private_key_id": "abc123",
    "private_key": _throwaway_rsa_private_key_pem(),
    "client_email": "fleet@waddleai-test.iam.gserviceaccount.com",
    "token_uri": "https://oauth2.googleapis.com/token",
}

# A single Vertex AI endpoint serving one deployed model ("m1") — reused by
# the place_model/endpoints_for tests below.
_SINGLE_ENDPOINT_WITH_M1 = {
    "endpoints": [{"name": ".../endpoints/1", "deployedModels": [{"model": ".../models/m1"}]}]
}


def _mock_response(status_code: int, payload: dict) -> MagicMock:
    """Build a mocked ``httpx.Response``."""
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


@pytest.fixture
def vertex_backend() -> VertexAIFleetBackend:
    """A Vertex AI backend with a fake service-account key and explicit location."""
    return VertexAIFleetBackend(
        db=None,
        config={"location": "us-central1"},
        credentials=json.dumps(_FAKE_SA),
    )


def test_type_and_default_scope() -> None:
    """``type`` matches the registry key; default scope is the safe register_and_route."""
    backend = VertexAIFleetBackend(db=None)
    assert backend.type == BackendType.VERTEX_AI
    assert backend.management_scope == ManagementScope.REGISTER_AND_ROUTE


def test_project_id_derived_from_service_account(vertex_backend) -> None:
    """``project_id`` falls back to the service-account key's own project when unset in config."""
    assert vertex_backend.project_id == "waddleai-test"


def test_project_id_config_overrides_service_account() -> None:
    """An explicit ``config['project_id']`` wins over the service-account key's project."""
    backend = VertexAIFleetBackend(
        db=None, config={"project_id": "other-project"}, credentials=json.dumps(_FAKE_SA)
    )
    assert backend.project_id == "other-project"


def test_credentials_malformed_json_raises() -> None:
    """Non-JSON credentials raise immediately, not deep inside a token request."""
    with pytest.raises(ValueError, match="JSON service-account key"):
        VertexAIFleetBackend(db=None, credentials="not-json")


def test_credentials_missing_fields_raises() -> None:
    """A service-account JSON missing required fields raises immediately."""
    with pytest.raises(ValueError, match="missing fields"):
        VertexAIFleetBackend(db=None, credentials=json.dumps({"client_email": "x@y.com"}))


def test_base_url_missing_project_raises() -> None:
    """No credentials and no configured project_id raises when the base URL is needed."""
    backend = VertexAIFleetBackend(db=None, credentials=None)
    with pytest.raises(ValueError, match="project_id"):
        _ = backend._base_url


async def test_get_access_token_no_credentials_raises() -> None:
    """Minting a token with no configured credentials raises a clear error."""
    backend = VertexAIFleetBackend(db=None, credentials=None)
    with pytest.raises(ValueError, match="no credentials configured"):
        await backend._get_access_token()


def _patch_token_and_requests(token_status: int = 200, request_responses=None):
    """Patch ``httpx.AsyncClient`` for both the token-mint POST and REST calls.

    ``request_responses`` is an iterable of ``httpx.Response`` mocks returned
    in order by ``AsyncClient.request`` (used for the actual Vertex REST
    calls issued after the token is minted).
    """
    token_response = _mock_response(
        token_status, {"access_token": "fake-access-token", "expires_in": 3600}
    )

    client = AsyncMock()
    client.post = AsyncMock(return_value=token_response)
    if request_responses is not None:
        client.request = AsyncMock(side_effect=list(request_responses))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


async def test_get_access_token_mints_and_caches(vertex_backend) -> None:
    """A successful token mint is cached — the second call doesn't re-POST."""
    client = _patch_token_and_requests()
    with patch("httpx.AsyncClient", return_value=client):
        token1 = await vertex_backend._get_access_token()
        token2 = await vertex_backend._get_access_token()

    assert token1 == "fake-access-token"
    assert token2 == "fake-access-token"
    client.post.assert_called_once()  # second call served from cache


async def test_get_access_token_expired_remints(vertex_backend) -> None:
    """An expired cached token triggers a fresh mint."""
    client = _patch_token_and_requests()
    with patch("httpx.AsyncClient", return_value=client):
        await vertex_backend._get_access_token()
        vertex_backend._token_expiry = time.time() - 1  # force expiry
        await vertex_backend._get_access_token()

    assert client.post.call_count == 2


async def test_get_access_token_failure_raises(vertex_backend) -> None:
    """A non-200 token endpoint response raises rather than returning a bad token."""
    client = _patch_token_and_requests(token_status=401)
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(RuntimeError, match="token exchange failed"):
            await vertex_backend._get_access_token()


async def test_list_nodes_maps_endpoints(vertex_backend) -> None:
    """``list_nodes`` maps Vertex endpoints (with deployed models) to ``NodeInfo(kind="cloud")``."""
    list_response = _mock_response(
        200,
        {
            "endpoints": [
                {
                    "name": "projects/waddleai-test/locations/us-central1/endpoints/123",
                    "deployedModels": [
                        {"model": "projects/waddleai-test/locations/us-central1/models/456"}
                    ],
                }
            ]
        },
    )
    client = _patch_token_and_requests(request_responses=[list_response])
    with patch("httpx.AsyncClient", return_value=client):
        nodes = await vertex_backend.list_nodes()

    assert len(nodes) == 1
    assert nodes[0].node_id == "123"
    assert nodes[0].kind == "cloud"
    assert nodes[0].loaded_models == ["456"]
    assert nodes[0].healthy is True


async def test_list_nodes_non_200_raises(vertex_backend) -> None:
    """A failed list-endpoints call raises rather than returning an empty list silently."""
    client = _patch_token_and_requests(request_responses=[_mock_response(500, {})])
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(RuntimeError, match="list endpoints failed"):
            await vertex_backend.list_nodes()


async def test_provision_flag_off_raises(vertex_backend, monkeypatch) -> None:
    """``waddleai.fleet_v2`` OFF blocks provisioning — fail-safe default."""
    monkeypatch.delenv("WADDLEAI_FLAG_FLEET_V2", raising=False)
    monkeypatch.delenv("POSTHOG_KEY", raising=False)
    vertex_backend.management_scope = ManagementScope.FULL_LIFECYCLE
    spec = ProvisionSpec(
        name="ep-a",
        models=[],
        mode="cloud",
        constraints={"model": "projects/p/locations/l/models/m"},
    )
    with pytest.raises(RuntimeError, match="fleet_v2"):
        await vertex_backend.provision(spec)


async def test_provision_register_and_route_refused(vertex_backend, monkeypatch) -> None:
    """``provision`` on a register_and_route-scoped backend is refused."""
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
    assert vertex_backend.management_scope == ManagementScope.REGISTER_AND_ROUTE
    spec = ProvisionSpec(name="ep-a", models=[], mode="cloud", constraints={"model": "m"})
    with pytest.raises(PermissionError, match="full_lifecycle"):
        await vertex_backend.provision(spec)


async def test_provision_missing_model_raises(vertex_backend, monkeypatch) -> None:
    """Missing ``constraints['model']`` raises before any HTTP call."""
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
    vertex_backend.management_scope = ManagementScope.FULL_LIFECYCLE
    spec = ProvisionSpec(name="ep-a", models=[], mode="cloud", constraints={})
    with pytest.raises(ValueError, match="constraints\\['model'\\]"):
        await vertex_backend.provision(spec)


async def test_provision_full_lifecycle_creates_and_deploys(vertex_backend, monkeypatch) -> None:
    """``full_lifecycle`` provision creates an endpoint then deploys the model to it."""
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
    vertex_backend.management_scope = ManagementScope.FULL_LIFECYCLE
    create_resp = _mock_response(
        201, {"name": "projects/waddleai-test/locations/us-central1/endpoints/789"}
    )
    deploy_resp = _mock_response(200, {})
    client = _patch_token_and_requests(request_responses=[create_resp, deploy_resp])
    spec = ProvisionSpec(
        name="ep-a",
        models=["m1"],
        mode="cloud",
        constraints={"model": "projects/waddleai-test/locations/us-central1/models/m1"},
    )
    with patch("httpx.AsyncClient", return_value=client):
        nodes = await vertex_backend.provision(spec)

    assert len(nodes) == 1
    assert nodes[0].node_id == "789"
    assert nodes[0].kind == "cloud"
    assert nodes[0].loaded_models == ["m1"]
    assert nodes[0].healthy is True
    assert client.request.call_count == 2


async def test_provision_endpoint_creation_failure_raises(vertex_backend, monkeypatch) -> None:
    """A failed endpoint-creation call raises rather than proceeding to deploy."""
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
    vertex_backend.management_scope = ManagementScope.FULL_LIFECYCLE
    client = _patch_token_and_requests(request_responses=[_mock_response(500, {})])
    spec = ProvisionSpec(name="ep-a", models=[], mode="cloud", constraints={"model": "m1"})
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(RuntimeError, match="endpoint creation failed"):
            await vertex_backend.provision(spec)


async def test_deprovision_register_and_route_refused(vertex_backend) -> None:
    """``deprovision`` on a register_and_route-scoped backend is refused."""
    with pytest.raises(PermissionError, match="full_lifecycle"):
        await vertex_backend.deprovision("789")


async def test_deprovision_full_lifecycle_undeploys_then_deletes(vertex_backend) -> None:
    """``full_lifecycle`` deprovision undeploys every deployed model, then deletes the endpoint."""
    vertex_backend.management_scope = ManagementScope.FULL_LIFECYCLE
    get_resp = _mock_response(200, {"deployedModels": [{"id": "dm1"}]})
    undeploy_resp = _mock_response(200, {})
    delete_resp = _mock_response(204, {})
    client = _patch_token_and_requests(request_responses=[get_resp, undeploy_resp, delete_resp])
    with patch("httpx.AsyncClient", return_value=client):
        await vertex_backend.deprovision("789")

    assert client.request.call_count == 3


async def test_deprovision_already_gone_is_noop(vertex_backend) -> None:
    """A 404 on lookup means the endpoint is already gone — no-op, not an error."""
    vertex_backend.management_scope = ManagementScope.FULL_LIFECYCLE
    client = _patch_token_and_requests(request_responses=[_mock_response(404, {})])
    with patch("httpx.AsyncClient", return_value=client):
        await vertex_backend.deprovision("789")  # must not raise
    assert client.request.call_count == 1


async def test_health_aggregates_across_endpoints(vertex_backend) -> None:
    """``health()`` reflects list_nodes() aggregation."""
    list_response = _mock_response(
        200,
        {
            "endpoints": [
                {"name": ".../endpoints/1", "deployedModels": [{"model": ".../models/m1"}]},
            ]
        },
    )
    client = _patch_token_and_requests(request_responses=[list_response])
    with patch("httpx.AsyncClient", return_value=client):
        health = await vertex_backend.health()

    assert health.node_count == 1
    assert health.healthy is True
    assert health.detail["healthy_nodes"] == 1


async def test_health_failure_reports_unhealthy_not_raise(vertex_backend) -> None:
    """A transport/HTTP failure surfaces as an unhealthy ``FleetHealth``, not an exception."""
    client = _patch_token_and_requests(request_responses=[_mock_response(500, {})])
    with patch("httpx.AsyncClient", return_value=client):
        health = await vertex_backend.health()

    assert health.healthy is False
    assert health.node_count == 0


async def test_place_model_returns_placed(vertex_backend) -> None:
    """A model on a healthy endpoint maps to ``status="placed"``."""
    list_response = _mock_response(200, _SINGLE_ENDPOINT_WITH_M1)
    client = _patch_token_and_requests(request_responses=[list_response])
    with patch("httpx.AsyncClient", return_value=client):
        placement = await vertex_backend.place_model("m1", {})

    assert placement.status == "placed"
    assert placement.node_id == "1"


async def test_place_model_no_match_raises(vertex_backend) -> None:
    """No endpoint serving the model raises, not a silent no-op."""
    list_response = _mock_response(200, {"endpoints": []})
    client = _patch_token_and_requests(request_responses=[list_response])
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(RuntimeError, match="No Vertex AI endpoint"):
            await vertex_backend.place_model("does-not-exist", {})


async def test_endpoints_for_matches_model(vertex_backend) -> None:
    """``endpoints_for`` returns only endpoints serving the given model."""
    list_response = _mock_response(200, _SINGLE_ENDPOINT_WITH_M1)
    client = _patch_token_and_requests(request_responses=[list_response])
    with patch("httpx.AsyncClient", return_value=client):
        endpoints = await vertex_backend.endpoints_for("m1")

    assert len(endpoints) == 1
    assert endpoints[0].node_id == "1"
