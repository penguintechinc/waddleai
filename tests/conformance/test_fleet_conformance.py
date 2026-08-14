"""Cross-backend conformance suite for ``InferenceFleetBackend`` (spec §10.5).

Parametrized over every backend that implements the interface: Ollama
(Task 4) and llama.cpp (Task 5) so far. Runs against the in-memory
``FakeDAL`` (see ``_fake_dal.py``) rather than a live kind cluster or
Docker/K8s daemon — this is the unit-level conformance pass; the
real-infra acceptance pass lives in
``tests/integration/test_fleet_acceptance.py`` (§10.5, out of scope here).
"""

from unittest.mock import MagicMock, patch

import pytest
import yaml

from services.management.app.services.llamacpp_manager import LlamaCppManager
from services.management.app.services.ollama_manager import (
    OllamaDeploymentConfig,
    OllamaDeploymentManager,
    PullStatus,
)
from shared.fleet.base import BackendType, ProvisionSpec
from tests.conformance._fake_dal import FakeDAL


@pytest.fixture
def ollama_backend() -> OllamaDeploymentManager:
    """A fresh ``OllamaDeploymentManager`` bound to an empty ``FakeDAL``."""
    return OllamaDeploymentManager(db=FakeDAL())


@pytest.fixture
def llamacpp_backend() -> LlamaCppManager:
    """A fresh ``LlamaCppManager`` bound to an empty ``FakeDAL``."""
    return LlamaCppManager(db=FakeDAL())


# BACKENDS: (fixture_name, expected BackendType) — extended per backend as
# each is wired into the interface.
BACKENDS = [
    ("ollama_backend", BackendType.OLLAMA),
    ("llamacpp_backend", BackendType.LLAMACPP),
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
