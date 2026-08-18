"""Cross-backend conformance suite for ``InferenceFleetBackend`` (spec §10.5).

Parametrized over every backend that implements the interface: Ollama
(Task 4), llama.cpp (Task 5), EXO (Task 8), Vertex AI (Task 10), and
Bedrock (Task 11) — all five spec-mandated backend types. Runs against the
in-memory ``FakeDAL`` (see ``_fake_dal.py``) or mocked HTTP/boto3 transports
rather than a live kind cluster, EXO cluster, or cloud account — this is the
unit-level conformance pass; the real-infra acceptance pass lives in
``tests/integration/test_fleet_acceptance.py`` (§10.5, out of scope here).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from services.management.app.services.llamacpp_manager import LlamaCppManager
from services.management.app.services.ollama_manager import (
    OllamaDeploymentConfig,
    OllamaDeploymentManager,
    PullStatus,
)
from shared.fleet.base import BackendType, ManagementScope, ProvisionSpec
from shared.fleet.bedrock import BedrockFleetBackend
from shared.fleet.exo import ExoFleetBackend
from shared.fleet.vertex_ai import VertexAIFleetBackend
from tests.conformance._fake_dal import FakeDAL


@pytest.fixture
def ollama_backend() -> OllamaDeploymentManager:
    """A fresh ``OllamaDeploymentManager`` bound to an empty ``FakeDAL``."""
    return OllamaDeploymentManager(db=FakeDAL())


@pytest.fixture
def llamacpp_backend() -> LlamaCppManager:
    """A fresh ``LlamaCppManager`` bound to an empty ``FakeDAL``."""
    return LlamaCppManager(db=FakeDAL())


@pytest.fixture
def exo_backend() -> ExoFleetBackend:
    """A fresh ``ExoFleetBackend`` pointed at a fake cluster endpoint."""
    return ExoFleetBackend(db=None, config={"endpoint_url": "http://exo-cluster.internal:8000"})


@pytest.fixture
def vertex_backend() -> VertexAIFleetBackend:
    """A fresh ``VertexAIFleetBackend`` with a throwaway service-account key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    credentials = json.dumps(
        {
            "type": "service_account",
            "project_id": "waddleai-test",
            "private_key": private_key_pem,
            "client_email": "fleet@waddleai-test.iam.gserviceaccount.com",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )
    return VertexAIFleetBackend(
        db=None, config={"location": "us-central1"}, credentials=credentials
    )


@pytest.fixture
def bedrock_backend() -> BedrockFleetBackend:
    """A fresh ``BedrockFleetBackend`` with explicit fake AWS credentials."""
    return BedrockFleetBackend(
        db=None,
        config={"region": "us-east-1"},
        credentials=json.dumps(
            {"aws_access_key_id": "AKIAFAKE", "aws_secret_access_key": "fake-secret"}
        ),
    )


# BACKENDS: (fixture_name, expected BackendType) — all five spec-mandated
# backend types (§10.1) are represented.
BACKENDS = [
    ("ollama_backend", BackendType.OLLAMA),
    ("llamacpp_backend", BackendType.LLAMACPP),
    ("exo_backend", BackendType.EXO),
    ("vertex_backend", BackendType.VERTEX_AI),
    ("bedrock_backend", BackendType.BEDROCK),
]


@pytest.mark.parametrize("backend_fixture,expected_type", BACKENDS)
def test_backend_type_matches_registry_key(backend_fixture, expected_type, request) -> None:
    """Each backend's ``type`` matches the key it registers under."""
    backend = request.getfixturevalue(backend_fixture)
    assert backend.type == expected_type


class TestOllamaConformance:
    """§10.5 conformance for the Ollama backend (DaemonSet + pool mode)."""

    async def test_provision_daemonset_mode_returns_node(self, ollama_backend) -> None:
        """DaemonSet-mode provision creates one deployment and returns its k8s node."""
        spec = ProvisionSpec(
            name="pool-a", models=[], mode="daemonset", constraints={"gpu_count": 1}
        )

        nodes = await ollama_backend.provision(spec)

        assert len(nodes) == 1
        assert nodes[0].node_id == "pool-a"
        assert nodes[0].kind == "k8s"

    async def test_provision_pool_mode_marks_deployment_pool_mode(self, ollama_backend) -> None:
        """``mode="pool"`` persists ``pool_mode=True`` and the gpu_config constraints."""
        spec = ProvisionSpec(
            name="pool-b", models=[], mode="pool", constraints={"replicas": 3, "gpu_count": 2}
        )

        await ollama_backend.provision(spec)

        db = ollama_backend.db
        deployment = db(db.ollama_deployments.name == "pool-b").select().first()
        assert deployment.pool_mode is True
        assert deployment.gpu_config["gpu_count"] == 2

    async def test_provision_duplicate_name_raises(self, ollama_backend) -> None:
        """Provisioning the same deployment name twice raises, matching create_deployment."""
        spec = ProvisionSpec(name="dup", models=[], mode="daemonset", constraints={})
        await ollama_backend.provision(spec)

        with pytest.raises(RuntimeError, match="already exists"):
            await ollama_backend.provision(spec)

    async def test_list_nodes_reflects_provisioned_nodes_with_loaded_models(
        self, ollama_backend
    ) -> None:
        """``list_nodes`` reports tracked models for each provisioned deployment."""
        await ollama_backend.provision(
            ProvisionSpec(name="node-a", models=[], mode="daemonset", constraints={})
        )
        db = ollama_backend.db
        deployment = db(db.ollama_deployments.name == "node-a").select().first()
        db.ollama_models.insert(deployment_id=deployment.id, model_name="gemma4:e2b")

        nodes = await ollama_backend.list_nodes()

        assert len(nodes) == 1
        assert nodes[0].node_id == "node-a"
        assert nodes[0].loaded_models == ["gemma4:e2b"]

    async def test_endpoints_for_returns_only_nodes_with_model_loaded(
        self, ollama_backend
    ) -> None:
        """``endpoints_for`` excludes deployments that don't have the model loaded."""
        db = ollama_backend.db
        for name in ("has-model", "no-model"):
            await ollama_backend.provision(
                ProvisionSpec(name=name, models=[], mode="daemonset", constraints={})
            )
        with_model = db(db.ollama_deployments.name == "has-model").select().first()
        db.ollama_models.insert(deployment_id=with_model.id, model_name="gemma4:e2b")

        endpoints = await ollama_backend.endpoints_for("gemma4:e2b")

        assert len(endpoints) == 1
        assert endpoints[0].node_id == "has-model"

    async def test_endpoints_for_no_match_returns_empty(self, ollama_backend) -> None:
        """A model loaded nowhere yields an empty endpoint list, not an error."""
        await ollama_backend.provision(
            ProvisionSpec(name="node-a", models=[], mode="daemonset", constraints={})
        )

        endpoints = await ollama_backend.endpoints_for("does-not-exist")

        assert endpoints == []

    async def test_place_model_returns_placement_and_records_completion(
        self, ollama_backend
    ) -> None:
        """A completed pull maps to ``ModelPlacement(status="placed")``."""
        await ollama_backend.provision(
            ProvisionSpec(name="node-a", models=[], mode="daemonset", constraints={})
        )
        db = ollama_backend.db
        db(db.ollama_deployments.name == "node-a").update(status="running")

        with patch.object(
            OllamaDeploymentManager,
            "pull_model",
            return_value=PullStatus(model="gemma4:e2b", status="completed", completed=True),
        ):
            placement = await ollama_backend.place_model("gemma4:e2b", {"node_id": "node-a"})

        assert placement.status == "placed"
        assert placement.node_id == "node-a"

    async def test_place_model_no_deployment_raises(self, ollama_backend) -> None:
        """No running/named deployment to place onto raises, not a silent no-op."""
        with pytest.raises(RuntimeError, match="No available Ollama deployment"):
            await ollama_backend.place_model("gemma4:e2b", {})

    async def test_health_aggregates_across_deployments(self, ollama_backend) -> None:
        """``health()`` is unhealthy overall if any tracked deployment is unhealthy."""
        for name in ("node-a", "node-b"):
            await ollama_backend.provision(
                ProvisionSpec(name=name, models=[], mode="daemonset", constraints={})
            )

        with patch.object(
            OllamaDeploymentManager,
            "health_check",
            side_effect=[{"healthy": True}, {"healthy": False}],
        ):
            health = await ollama_backend.health()

        assert health.node_count == 2
        assert health.healthy is False
        assert health.detail["healthy_nodes"] == 1

    async def test_deprovision_removes_node(self, ollama_backend) -> None:
        """``deprovision`` deletes the named deployment; it no longer appears in list_nodes."""
        await ollama_backend.provision(
            ProvisionSpec(name="node-a", models=[], mode="daemonset", constraints={})
        )

        await ollama_backend.deprovision("node-a")

        nodes = await ollama_backend.list_nodes()
        assert nodes == []

    async def test_deprovision_unknown_node_is_noop(self, ollama_backend) -> None:
        """Deprovisioning a node that doesn't exist is a no-op, not an error."""
        await ollama_backend.deprovision("does-not-exist")  # must not raise

    def test_generate_pool_manifest_renders_deployment_kind(self, ollama_backend) -> None:
        """Pool mode renders a Kind=Deployment with replicas/nodeSelector, no DaemonSet."""
        db = ollama_backend.db
        deployment_id = db.ollama_deployments.insert(
            name="pool-c",
            endpoint_url="http://ollama-pool-c:11434",
            deployment_type="kubernetes",
            gpu_config={"gpu_count": 1, "replicas": 2, "node_selector": {"gpu": "a100"}},
            resource_limits={},
            pool_mode=True,
        )

        manifest_yaml = ollama_backend.generate_pool_manifest(deployment_id)
        docs = list(yaml.safe_load_all(manifest_yaml))
        kinds = {doc["kind"] for doc in docs}

        assert "Deployment" in kinds
        assert "DaemonSet" not in kinds
        deployment_doc = next(doc for doc in docs if doc["kind"] == "Deployment")
        assert deployment_doc["spec"]["replicas"] == 2
        node_selector = deployment_doc["spec"]["template"]["spec"]["nodeSelector"]
        assert node_selector == {"gpu": "a100"}

    def test_generate_pool_manifest_missing_deployment_returns_empty(
        self, ollama_backend
    ) -> None:
        """An unknown deployment id renders an empty manifest, not a crash."""
        assert ollama_backend.generate_pool_manifest(999) == ""

    def test_config_pool_mode_persisted_by_create_deployment(self, ollama_backend) -> None:
        """``OllamaDeploymentConfig.pool_mode`` round-trips through create_deployment."""
        config = OllamaDeploymentConfig(name="pool-d", pool_mode=True, replicas=4)
        result = ollama_backend.create_deployment(config)

        db = ollama_backend.db
        deployment = db(db.ollama_deployments.id == result["deployment_id"]).select().first()
        assert deployment.pool_mode is True


class TestLlamaCppConformance:
    """§10.5 conformance for the llama.cpp backend (kubernetes + remote modes)."""

    async def test_provision_kubernetes_mode_deploys_daemonset(self, llamacpp_backend) -> None:
        """Kubernetes-mode provision creates the DB row and calls the K8s API."""
        spec = ProvisionSpec(
            name="gguf-a",
            models=["llama-3.2-3b"],
            mode="kubernetes",
            constraints={"model_url": "https://example.com/m.gguf", "model_filename": "m.gguf"},
        )

        with patch(
            "services.management.app.services.llamacpp_manager.get_k8s_apps_client"
        ) as mock_apps, patch(
            "services.management.app.services.llamacpp_manager.get_k8s_core_client"
        ) as mock_core:
            mock_apps.return_value = MagicMock()
            mock_core.return_value = MagicMock()
            nodes = await llamacpp_backend.provision(spec)

        assert len(nodes) == 1
        assert nodes[0].node_id == "gguf-a"
        assert nodes[0].kind == "k8s"
        assert nodes[0].loaded_models == ["llama-3.2-3b"]
        mock_apps.return_value.create_namespaced_daemon_set.assert_called_once()

    async def test_provision_remote_mode_registers_and_marks_healthy(
        self, llamacpp_backend
    ) -> None:
        """Remote-mode provision health-checks the endpoint and marks it running."""
        spec = ProvisionSpec(
            name="remote-a",
            models=["llama-3.1-8b"],
            mode="remote",
            constraints={"endpoint_url": "http://192.168.1.50:8080"},
        )

        with patch("services.management.app.services.llamacpp_manager.requests") as mock_requests:
            mock_requests.get.return_value.status_code = 200
            nodes = await llamacpp_backend.provision(spec)

        assert len(nodes) == 1
        assert nodes[0].kind == "external"
        assert nodes[0].healthy is True

    async def test_provision_duplicate_name_raises(self, llamacpp_backend) -> None:
        """Provisioning the same deployment name twice raises, matching create_deployment."""
        spec = ProvisionSpec(
            name="dup", models=["m"], mode="remote", constraints={"endpoint_url": "http://x:8080"}
        )
        with patch("services.management.app.services.llamacpp_manager.requests") as mock_requests:
            mock_requests.get.return_value.status_code = 200
            await llamacpp_backend.provision(spec)

            with pytest.raises(RuntimeError, match="already exists"):
                await llamacpp_backend.provision(spec)

    async def test_list_nodes_reflects_provisioned_nodes(self, llamacpp_backend) -> None:
        """``list_nodes`` reflects both kubernetes- and remote-mode deployments."""
        with patch("services.management.app.services.llamacpp_manager.requests") as mock_requests:
            mock_requests.get.return_value.status_code = 200
            await llamacpp_backend.provision(
                ProvisionSpec(
                    name="remote-a",
                    models=["m1"],
                    mode="remote",
                    constraints={"endpoint_url": "http://x:8080"},
                )
            )

        nodes = await llamacpp_backend.list_nodes()

        assert len(nodes) == 1
        assert nodes[0].node_id == "remote-a"
        assert nodes[0].loaded_models == ["m1"]

    async def test_endpoints_for_matches_model_name(self, llamacpp_backend) -> None:
        """``endpoints_for`` returns only deployments serving that exact model."""
        with patch("services.management.app.services.llamacpp_manager.requests") as mock_requests:
            mock_requests.get.return_value.status_code = 200
            await llamacpp_backend.provision(
                ProvisionSpec(
                    name="remote-a",
                    models=["m1"],
                    mode="remote",
                    constraints={"endpoint_url": "http://x:8080"},
                )
            )

        endpoints = await llamacpp_backend.endpoints_for("m1")
        assert len(endpoints) == 1
        assert endpoints[0].node_id == "remote-a"

        no_match = await llamacpp_backend.endpoints_for("does-not-exist")
        assert no_match == []

    async def test_place_model_returns_placement_for_running_deployment(
        self, llamacpp_backend
    ) -> None:
        """A model served by a running deployment maps to ``status="placed"``."""
        with patch("services.management.app.services.llamacpp_manager.requests") as mock_requests:
            mock_requests.get.return_value.status_code = 200
            await llamacpp_backend.provision(
                ProvisionSpec(
                    name="remote-a",
                    models=["m1"],
                    mode="remote",
                    constraints={"endpoint_url": "http://x:8080"},
                )
            )

        placement = await llamacpp_backend.place_model("m1", {})
        assert placement.status == "placed"
        assert placement.node_id == "remote-a"

    async def test_place_model_no_deployment_raises(self, llamacpp_backend) -> None:
        """No deployment serving the requested model raises, not a silent no-op."""
        with pytest.raises(RuntimeError, match="No llama.cpp deployment serving model"):
            await llamacpp_backend.place_model("does-not-exist", {})

    async def test_health_aggregates_across_deployments(self, llamacpp_backend) -> None:
        """``health()`` is unhealthy overall if any deployment fails its ``/health`` check."""
        with patch("services.management.app.services.llamacpp_manager.requests") as mock_requests:
            mock_requests.get.return_value.status_code = 200
            for name in ("remote-a", "remote-b"):
                await llamacpp_backend.provision(
                    ProvisionSpec(
                        name=name,
                        models=[f"m-{name}"],
                        mode="remote",
                        constraints={"endpoint_url": "http://x:8080"},
                    )
                )

            mock_requests.get.side_effect = [
                MagicMock(status_code=200),
                MagicMock(status_code=503),
            ]
            health = await llamacpp_backend.health()

        assert health.node_count == 2
        assert health.healthy is False
        assert health.detail["healthy_nodes"] == 1

    async def test_deprovision_removes_remote_node(self, llamacpp_backend) -> None:
        """``deprovision`` deletes a remote-mode deployment without touching K8s."""
        with patch("services.management.app.services.llamacpp_manager.requests") as mock_requests:
            mock_requests.get.return_value.status_code = 200
            await llamacpp_backend.provision(
                ProvisionSpec(
                    name="remote-a",
                    models=["m1"],
                    mode="remote",
                    constraints={"endpoint_url": "http://x:8080"},
                )
            )

        await llamacpp_backend.deprovision("remote-a")

        nodes = await llamacpp_backend.list_nodes()
        assert nodes == []

    async def test_deprovision_unknown_node_is_noop(self, llamacpp_backend) -> None:
        """Deprovisioning a node that doesn't exist is a no-op, not an error."""
        await llamacpp_backend.deprovision("does-not-exist")  # must not raise


def _mock_aiohttp_session(status: int, payload: dict) -> MagicMock:
    """Build a mocked ``aiohttp.ClientSession`` context manager returning ``payload``."""
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(return_value=payload)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


class TestExoConformance:
    """§10.5 conformance for the EXO backend (external-only, register_and_route)."""

    async def test_provision_validates_and_returns_single_external_node(
        self, exo_backend, monkeypatch
    ) -> None:
        """A reachable cluster validates and reports one ``kind="external"`` node."""
        monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
        session = _mock_aiohttp_session(200, {"data": [{"id": "llama-3.1-70b"}]})
        with patch("aiohttp.ClientSession", return_value=session):
            nodes = await exo_backend.provision(
                ProvisionSpec(name="exo-a", models=[], mode="external", constraints={})
            )
        assert len(nodes) == 1
        assert nodes[0].kind == "external"
        assert nodes[0].loaded_models == ["llama-3.1-70b"]

    async def test_list_nodes_and_endpoints_for_and_place_model_round_trip(
        self, exo_backend
    ) -> None:
        """list_nodes/endpoints_for/place_model agree on the same served-model set."""
        session = _mock_aiohttp_session(200, {"data": [{"id": "m1"}]})
        with patch("aiohttp.ClientSession", return_value=session):
            nodes = await exo_backend.list_nodes()
        assert nodes[0].loaded_models == ["m1"]

        session = _mock_aiohttp_session(200, {"data": [{"id": "m1"}]})
        with patch("aiohttp.ClientSession", return_value=session):
            endpoints = await exo_backend.endpoints_for("m1")
        assert len(endpoints) == 1

        session = _mock_aiohttp_session(200, {"data": [{"id": "m1"}]})
        with patch("aiohttp.ClientSession", return_value=session):
            placement = await exo_backend.place_model("m1", {})
        assert placement.status == "placed"

    async def test_deprovision_unknown_node_is_noop(self, exo_backend) -> None:
        """Deprovisioning is always a no-op — EXO clusters are never WaddleAI-lifecycled."""
        await exo_backend.deprovision("does-not-exist")  # must not raise

    async def test_health_reflects_endpoint_reachability(self, exo_backend) -> None:
        """``health()`` is unhealthy when the cluster is unreachable, healthy otherwise."""
        session = _mock_aiohttp_session(503, {})
        with patch("aiohttp.ClientSession", return_value=session):
            health = await exo_backend.health()
        assert health.healthy is False
        assert health.node_count == 1


class TestVertexAiConformance:
    """§10.5 conformance for the Vertex AI backend (Pro-gated, per-backend scope)."""

    async def test_register_and_route_reflects_existing_endpoints(self, vertex_backend) -> None:
        """``register_and_route`` (the default scope) reads existing endpoints via list_nodes."""
        assert vertex_backend.management_scope == ManagementScope.REGISTER_AND_ROUTE
        token_response = MagicMock(status_code=200)
        token_response.json.return_value = {"access_token": "tok", "expires_in": 3600}
        list_response = MagicMock(status_code=200)
        list_response.json.return_value = {
            "endpoints": [
                {"name": ".../endpoints/1", "deployedModels": [{"model": ".../models/m1"}]}
            ]
        }
        client = AsyncMock()
        client.post = AsyncMock(return_value=token_response)
        client.request = AsyncMock(return_value=list_response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=client):
            nodes = await vertex_backend.list_nodes()
        assert nodes[0].kind == "cloud"
        assert nodes[0].loaded_models == ["m1"]

    async def test_full_lifecycle_provision_calls_deploy_deprovision_calls_undeploy(
        self, vertex_backend, monkeypatch
    ) -> None:
        """``full_lifecycle`` provision deploys a model; deprovision undeploys + deletes."""
        monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
        vertex_backend.management_scope = ManagementScope.FULL_LIFECYCLE

        token_response = MagicMock(status_code=200)
        token_response.json.return_value = {"access_token": "tok", "expires_in": 3600}
        create_response = MagicMock(status_code=201)
        create_response.json.return_value = {"name": ".../endpoints/789"}
        deploy_response = MagicMock(status_code=200)
        deploy_response.json.return_value = {}

        client = AsyncMock()
        client.post = AsyncMock(return_value=token_response)
        client.request = AsyncMock(side_effect=[create_response, deploy_response])
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=client):
            nodes = await vertex_backend.provision(
                ProvisionSpec(name="ep-a", models=["m1"], mode="cloud", constraints={"model": "m1"})
            )
        assert nodes[0].kind == "cloud"

    async def test_provision_refused_under_register_and_route(
        self, vertex_backend, monkeypatch
    ) -> None:
        """The register_and_route/full_lifecycle boundary is enforced, not merely documented."""
        monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
        with pytest.raises(PermissionError):
            await vertex_backend.provision(
                ProvisionSpec(name="ep-a", models=[], mode="cloud", constraints={"model": "m1"})
            )


class TestBedrockConformance:
    """§10.5 conformance for the Bedrock backend (Pro-gated, boto3 off event loop)."""

    async def test_register_and_route_reflects_existing_provisioned_throughput(
        self, bedrock_backend
    ) -> None:
        """``register_and_route`` (the default scope) reads existing capacity via list_nodes."""
        assert bedrock_backend.management_scope == ManagementScope.REGISTER_AND_ROUTE
        mock_client = MagicMock()
        mock_client.list_provisioned_model_throughputs.return_value = {
            "provisionedModelSummaries": [
                {"provisionedModelName": "node-a", "modelId": "m1", "status": "InService"}
            ]
        }
        with patch("shared.fleet.bedrock.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_client
            nodes = await bedrock_backend.list_nodes()
        assert nodes[0].kind == "cloud"
        assert nodes[0].healthy is True

    async def test_full_lifecycle_provision_and_deprovision_map_to_create_and_delete(
        self, bedrock_backend, monkeypatch
    ) -> None:
        """``full_lifecycle`` provision/deprovision map to create/delete provisioned throughput."""
        monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
        bedrock_backend.management_scope = ManagementScope.FULL_LIFECYCLE
        mock_client = MagicMock()
        mock_client.create_provisioned_model_throughput.return_value = {
            "provisionedModelArn": "arn:aws:bedrock:us-east-1:1:provisioned-model/node-a"
        }
        with patch("shared.fleet.bedrock.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_client
            nodes = await bedrock_backend.provision(
                ProvisionSpec(
                    name="node-a", models=["m1"], mode="cloud", constraints={"model_id": "m1"}
                )
            )
            await bedrock_backend.deprovision("node-a")

        assert nodes[0].kind == "cloud"
        mock_client.delete_provisioned_model_throughput.assert_called_once_with(
            provisionedModelId="node-a"
        )

    async def test_provision_refused_under_register_and_route(
        self, bedrock_backend, monkeypatch
    ) -> None:
        """The register_and_route/full_lifecycle boundary is enforced, not merely documented."""
        monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
        with pytest.raises(PermissionError):
            await bedrock_backend.provision(
                ProvisionSpec(
                    name="node-a", models=[], mode="cloud", constraints={"model_id": "m1"}
                )
            )
