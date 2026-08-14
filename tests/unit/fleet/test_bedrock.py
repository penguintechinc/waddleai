"""Tests for ``BedrockFleetBackend`` (shared.fleet.bedrock) — spec §10.1 plan Task 11.

All ``boto3`` calls are mocked — no live AWS account required. Follows the
same ``patch("<module>.boto3")`` pattern already used for
``shared.utils.llm_connectors.BedrockConnector`` (see ``test_llm_connectors.py``).
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from shared.fleet.base import BackendType, ManagementScope, ProvisionSpec
from shared.fleet.bedrock import BedrockFleetBackend

_INSERVICE = {
    "provisionedModelSummaries": [
        {
            "provisionedModelName": "node-a",
            "provisionedModelArn": "arn:aws:bedrock:us-east-1:1234:provisioned-model/node-a",
            "modelId": "anthropic.claude-3-haiku-20240307-v1:0",
            "status": "InService",
        }
    ]
}


@pytest.fixture
def bedrock_backend() -> BedrockFleetBackend:
    """A Bedrock backend with explicit (fake) credentials."""
    return BedrockFleetBackend(
        db=None,
        config={"region": "us-east-1"},
        credentials=json.dumps(
            {"aws_access_key_id": "AKIAFAKE", "aws_secret_access_key": "fake-secret"}
        ),
    )


def test_type_and_default_scope() -> None:
    """``type`` matches the registry key; default scope is the safe register_and_route."""
    backend = BedrockFleetBackend(db=None)
    assert backend.type == BackendType.BEDROCK
    assert backend.management_scope == ManagementScope.REGISTER_AND_ROUTE


def test_credentials_none_uses_ambient_chain() -> None:
    """No explicit credentials means boto3's own default chain — not an error."""
    backend = BedrockFleetBackend(db=None, credentials=None)
    assert backend._aws_creds is None


def test_credentials_malformed_json_raises() -> None:
    """Non-JSON credentials raise a clear error rather than failing deep in boto3."""
    with pytest.raises(ValueError, match="JSON object"):
        BedrockFleetBackend(db=None, credentials="not-json")


def test_credentials_missing_fields_raises() -> None:
    """Credentials JSON missing required AWS fields raises immediately."""
    with pytest.raises(ValueError, match="aws_access_key_id"):
        BedrockFleetBackend(db=None, credentials=json.dumps({"aws_secret_access_key": "x"}))


async def test_list_nodes_maps_summaries(bedrock_backend) -> None:
    """``list_nodes`` maps provisioned-throughput summaries to ``NodeInfo(kind="cloud")``."""
    mock_client = MagicMock()
    mock_client.list_provisioned_model_throughputs.return_value = _INSERVICE
    with patch("shared.fleet.bedrock.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        nodes = await bedrock_backend.list_nodes()

    assert len(nodes) == 1
    assert nodes[0].node_id == "node-a"
    assert nodes[0].kind == "cloud"
    assert nodes[0].loaded_models == ["anthropic.claude-3-haiku-20240307-v1:0"]
    assert nodes[0].healthy is True


async def test_provision_flag_off_raises(bedrock_backend, monkeypatch) -> None:
    """``waddleai.fleet_v2`` OFF blocks provisioning — fail-safe default."""
    monkeypatch.delenv("WADDLEAI_FLAG_FLEET_V2", raising=False)
    monkeypatch.delenv("POSTHOG_KEY", raising=False)
    bedrock_backend.management_scope = ManagementScope.FULL_LIFECYCLE
    spec = ProvisionSpec(name="node-a", models=[], mode="cloud", constraints={"model_id": "m1"})
    with pytest.raises(RuntimeError, match="fleet_v2"):
        await bedrock_backend.provision(spec)


async def test_provision_register_and_route_refused(bedrock_backend, monkeypatch) -> None:
    """``provision`` on a register_and_route-scoped backend is refused, not silently ignored."""
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
    assert bedrock_backend.management_scope == ManagementScope.REGISTER_AND_ROUTE
    spec = ProvisionSpec(name="node-a", models=[], mode="cloud", constraints={"model_id": "m1"})
    with pytest.raises(PermissionError, match="full_lifecycle"):
        await bedrock_backend.provision(spec)


async def test_provision_full_lifecycle_creates_provisioned_throughput(
    bedrock_backend, monkeypatch
) -> None:
    """``full_lifecycle`` provision calls ``create_provisioned_model_throughput``."""
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
    bedrock_backend.management_scope = ManagementScope.FULL_LIFECYCLE
    mock_client = MagicMock()
    mock_client.create_provisioned_model_throughput.return_value = {
        "provisionedModelArn": "arn:aws:bedrock:us-east-1:1234:provisioned-model/node-a"
    }
    spec = ProvisionSpec(
        name="node-a",
        models=["anthropic.claude-3-haiku-20240307-v1:0"],
        mode="cloud",
        constraints={"model_id": "anthropic.claude-3-haiku-20240307-v1:0", "model_units": 2},
    )
    with patch("shared.fleet.bedrock.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        nodes = await bedrock_backend.provision(spec)

    mock_client.create_provisioned_model_throughput.assert_called_once_with(
        modelUnits=2,
        provisionedModelName="node-a",
        modelId="anthropic.claude-3-haiku-20240307-v1:0",
    )
    assert len(nodes) == 1
    assert nodes[0].node_id == "node-a"
    assert nodes[0].kind == "cloud"
    # Freshly created capacity is not InService yet.
    assert nodes[0].healthy is False


async def test_provision_missing_model_id_raises(bedrock_backend, monkeypatch) -> None:
    """Missing ``constraints['model_id']`` raises before touching boto3."""
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
    bedrock_backend.management_scope = ManagementScope.FULL_LIFECYCLE
    spec = ProvisionSpec(name="node-a", models=[], mode="cloud", constraints={})
    with pytest.raises(ValueError, match="model_id"):
        await bedrock_backend.provision(spec)


async def test_deprovision_register_and_route_refused(bedrock_backend) -> None:
    """``deprovision`` on a register_and_route-scoped backend is refused."""
    assert bedrock_backend.management_scope == ManagementScope.REGISTER_AND_ROUTE
    with pytest.raises(PermissionError, match="full_lifecycle"):
        await bedrock_backend.deprovision("node-a")


async def test_deprovision_full_lifecycle_deletes(bedrock_backend) -> None:
    """``full_lifecycle`` deprovision calls ``delete_provisioned_model_throughput``."""
    bedrock_backend.management_scope = ManagementScope.FULL_LIFECYCLE
    mock_client = MagicMock()
    with patch("shared.fleet.bedrock.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        await bedrock_backend.deprovision("node-a")
    mock_client.delete_provisioned_model_throughput.assert_called_once_with(provisionedModelId="node-a")


async def test_deprovision_already_gone_is_noop(bedrock_backend) -> None:
    """A ``ResourceNotFoundException`` from AWS is swallowed as a no-op, not raised."""
    bedrock_backend.management_scope = ManagementScope.FULL_LIFECYCLE

    class _FakeResourceNotFoundError(Exception):
        """Stand-in for boto3's ``client.exceptions.ResourceNotFoundException``."""

    mock_client = MagicMock()
    mock_client.exceptions.ResourceNotFoundException = _FakeResourceNotFoundError
    mock_client.delete_provisioned_model_throughput.side_effect = _FakeResourceNotFoundError("gone")
    with patch("shared.fleet.bedrock.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        await bedrock_backend.deprovision("node-a")  # must not raise


async def test_health_aggregates_across_nodes(bedrock_backend) -> None:
    """``health()`` is unhealthy overall if any provisioned model isn't InService."""
    mock_client = MagicMock()
    mock_client.list_provisioned_model_throughputs.return_value = {
        "provisionedModelSummaries": [
            {"provisionedModelName": "a", "modelId": "m1", "status": "InService"},
            {"provisionedModelName": "b", "modelId": "m2", "status": "Creating"},
        ]
    }
    with patch("shared.fleet.bedrock.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        health = await bedrock_backend.health()

    assert health.node_count == 2
    assert health.healthy is False
    assert health.detail["healthy_nodes"] == 1


async def test_place_model_returns_placed_for_healthy_node(bedrock_backend) -> None:
    """A model on an InService provisioned throughput maps to ``status="placed"``."""
    mock_client = MagicMock()
    mock_client.list_provisioned_model_throughputs.return_value = _INSERVICE
    with patch("shared.fleet.bedrock.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        placement = await bedrock_backend.place_model("anthropic.claude-3-haiku-20240307-v1:0", {})

    assert placement.status == "placed"
    assert placement.node_id == "node-a"


async def test_place_model_no_match_raises(bedrock_backend) -> None:
    """No provisioned throughput serving the model raises, not a silent no-op."""
    mock_client = MagicMock()
    mock_client.list_provisioned_model_throughputs.return_value = {"provisionedModelSummaries": []}
    with patch("shared.fleet.bedrock.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        with pytest.raises(RuntimeError, match="No Bedrock provisioned throughput"):
            await bedrock_backend.place_model("does-not-exist", {})


async def test_endpoints_for_uses_arn_as_url(bedrock_backend) -> None:
    """``endpoints_for`` surfaces the provisioned-model ARN as the routable ``url``."""
    mock_client = MagicMock()
    mock_client.list_provisioned_model_throughputs.return_value = _INSERVICE
    with patch("shared.fleet.bedrock.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        endpoints = await bedrock_backend.endpoints_for("anthropic.claude-3-haiku-20240307-v1:0")

    assert len(endpoints) == 1
    assert endpoints[0].url == "arn:aws:bedrock:us-east-1:1234:provisioned-model/node-a"
    assert endpoints[0].node_id == "node-a"


async def test_client_built_once_and_cached(bedrock_backend) -> None:
    """The boto3 client is constructed once and reused across calls (asyncio.to_thread, cached)."""
    mock_client = MagicMock()
    mock_client.list_provisioned_model_throughputs.return_value = {"provisionedModelSummaries": []}
    with patch("shared.fleet.bedrock.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        await bedrock_backend.list_nodes()
        await bedrock_backend.list_nodes()

    expected_kwargs = {
        "region_name": "us-east-1",
        "aws_access_key_id": "AKIAFAKE",
        "aws_secret_access_key": "fake-secret",
    }
    mock_boto3.client.assert_called_once_with("bedrock", **expected_kwargs)
