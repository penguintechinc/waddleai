"""§10.5 cross-module fleet acceptance checks that do not need a cluster.

Real kind, Trivy, mTLS, and cloud-account checks are deployment gates. This
suite covers their deterministic application-side counterpart: registry
construction, all public backend types, cap enforcement, and idle lifecycle
recovery through the placement engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from shared.fleet.base import (
    BackendType,
    Endpoint,
    FleetHealth,
    InferenceFleetBackend,
    ManagementScope,
    ModelPlacement,
    NodeInfo,
)
from shared.fleet.caps import CapEnforcer, CapExceededError
from shared.fleet.idle_teardown import IdleTeardownController
from shared.fleet.placement import PlacementEngine
from shared.fleet.registry import build_backend
from tests.conformance._fake_dal import FakeDAL


@dataclass
class _FleetRow:
    id: int
    type: str
    management_scope: str = "register_and_route"
    config: dict | None = None
    credentials_ref: str | None = None


@dataclass
class _LicenseInfo:
    tier: str


class _CommunityLicense:
    def validate(self):
        return _LicenseInfo("community")


class _LifecycleBackend(InferenceFleetBackend):
    type = BackendType.AZURE_AI
    management_scope = ManagementScope.FULL_LIFECYCLE
    fleet_backend_id = 77

    def __init__(self):
        self.nodes: list[NodeInfo] = []
        self.provision_mock = AsyncMock(side_effect=self._provision)
        self.deprovision_mock = AsyncMock(side_effect=self._deprovision)

    async def _provision(self, spec):
        self.nodes = [NodeInfo("cloud-1", None, "cloud", spec.models, 0, 0, True)]
        return self.nodes

    async def _deprovision(self, node_id):
        self.nodes = []

    async def provision(self, spec):
        return await self.provision_mock(spec)

    async def deprovision(self, node_id):
        await self.deprovision_mock(node_id)

    async def health(self):
        return FleetHealth(self.fleet_backend_id, bool(self.nodes), len(self.nodes), {})

    async def list_nodes(self):
        return self.nodes

    async def place_model(self, model, constraints):
        return ModelPlacement(model, "cloud-1", "placed")

    async def endpoints_for(self, model):
        return [
            Endpoint("https://cloud.example", n.node_id, n.loaded_models, n.healthy)
            for n in self.nodes
            if model in n.loaded_models
        ]


@pytest.mark.parametrize(
    ("backend_type", "config"),
    [
        (BackendType.OLLAMA, {}),
        (BackendType.LLAMACPP, {}),
        (BackendType.EXO, {"endpoint_url": "https://exo.example"}),
        (BackendType.VERTEX_AI, {}),
        (BackendType.BEDROCK, {}),
        (BackendType.AZURE_AI, {}),
        (BackendType.OLLAMA_CLOUD, {"endpoint_url": "https://ollama.example"}),
    ],
)
def test_public_cloud_and_external_types_build_through_registry(backend_type, config):
    """Every non-local public type has a lazy registry implementation."""
    backend = build_backend(None, _FleetRow(id=1, type=backend_type.value, config=config))
    assert backend.type == backend_type


@pytest.mark.asyncio
async def test_community_cap_and_on_demand_redeploy_compose(monkeypatch):
    """The sixth node is denied; an idle cloud model is later redeployed and audited."""
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
    cap = CapEnforcer(FakeDAL(), _CommunityLicense(), org_id=1)
    with pytest.raises(CapExceededError, match="5 inference nodes"):
        await cap.enforce_node_cap(6)

    audit = AsyncMock()
    controller = IdleTeardownController(None, None, audit, idle_seconds=30)
    backend = _LifecycleBackend()
    engine = PlacementEngine(None, None, _CommunityLicense(), idle_teardown=controller)
    endpoints = await engine.ensure_placed("model-a", [backend])

    backend.provision_mock.assert_awaited_once()
    assert endpoints[0].node_id == "cloud-1"
    assert audit.await_args.kwargs["action"] == "redeploy_on_demand"
