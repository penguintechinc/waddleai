"""Tests for the shared full-lifecycle idle teardown controller."""

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
from shared.fleet.idle_teardown import IdleTeardownController


class _Backend(InferenceFleetBackend):
    type = BackendType.AZURE_AI

    def __init__(self, scope=ManagementScope.FULL_LIFECYCLE, nodes=None):
        self.management_scope = scope
        self.fleet_backend_id = 12
        self.nodes = (
            nodes
            if nodes is not None
            else [NodeInfo("node-1", None, "cloud", ["model-a"], 0, 0, True)]
        )
        self.deprovision_mock = AsyncMock(side_effect=self._deprovision)
        self.provision_mock = AsyncMock(side_effect=self._provision)

    async def _deprovision(self, node_id):
        self.nodes = [node for node in self.nodes if node.node_id != node_id]

    async def _provision(self, spec):
        node = NodeInfo("node-new", None, "cloud", spec.models, 0, 0, True)
        self.nodes = [node]
        return [node]

    async def deprovision(self, node_id):
        return await self.deprovision_mock(node_id)

    async def provision(self, spec):
        return await self.provision_mock(spec)

    async def list_nodes(self):
        return self.nodes

    async def health(self):
        return FleetHealth(self.fleet_backend_id, True, len(self.nodes), {})

    async def place_model(self, model, constraints):
        return ModelPlacement(model, "node-1", "placed")

    async def endpoints_for(self, model):
        return [
            Endpoint("https://endpoint", node.node_id, node.loaded_models, node.healthy)
            for node in self.nodes
            if model in node.loaded_models
        ]


@pytest.mark.asyncio
async def test_idle_node_is_torn_down_and_audited():
    """Verify: idle node is torn down and audited."""
    now = [100.0]
    audit = AsyncMock()
    backend = _Backend()
    controller = IdleTeardownController(None, None, audit, idle_seconds=10, clock=lambda: now[0])
    await controller.record_activity(backend, "node-1")
    now[0] = 111.0
    assert await controller.sweep([backend]) == [(12, "node-1")]
    backend.deprovision_mock.assert_awaited_once_with("node-1")
    assert audit.await_args.kwargs["action"] == "idle_teardown"


@pytest.mark.asyncio
async def test_activity_reset_and_registered_target_are_never_torn_down():
    """Verify: activity reset and registered target are never torn down."""
    now = [100.0]
    audit = AsyncMock()
    backend = _Backend(scope=ManagementScope.REGISTER_AND_ROUTE)
    controller = IdleTeardownController(None, None, audit, idle_seconds=10, clock=lambda: now[0])
    await controller.record_activity(backend, "node-1")
    now[0] = 200.0
    assert await controller.sweep([backend]) == []
    backend.deprovision_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_demand_redeploy_is_audited_after_teardown():
    """Verify: on demand redeploy is audited after teardown."""
    audit = AsyncMock()
    backend = _Backend(nodes=[])
    controller = IdleTeardownController(None, None, audit)
    assert await controller.ensure_deployed(backend, "model-a") is True
    backend.provision_mock.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "redeploy_on_demand"


@pytest.mark.asyncio
async def test_teardown_failure_is_audited_and_retried():
    """Verify: teardown failure is audited and retried."""
    now = [100.0]
    audit = AsyncMock()
    backend = _Backend()
    backend.deprovision_mock.side_effect = RuntimeError("provider unavailable")
    controller = IdleTeardownController(None, None, audit, idle_seconds=10, clock=lambda: now[0])
    await controller.record_activity(backend, "node-1")
    now[0] = 111.0
    assert await controller.sweep([backend]) == []
    assert audit.await_args.kwargs["action"] == "idle_teardown_failed"
    await controller.sweep([backend])
    assert backend.deprovision_mock.await_count == 2
