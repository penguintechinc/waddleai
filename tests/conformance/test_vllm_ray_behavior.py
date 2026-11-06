"""Behavioral conformance for ``VllmRayBackend``: the six ABC methods + registry build path."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from shared.fleet import registry
from shared.fleet.base import FleetHealth, ProvisionSpec
from shared.fleet.enums import DeploymentStatus as S
from shared.fleet.enums import GpuVendor
from shared.fleet.vllm_ray.backend import VllmRayBackend
from shared.fleet.vllm_ray.ray_client import HttpRayServeClient
from tests.conformance._fake_dal import FakeDAL
from tests.conformance._fake_ray import FakeRayServeClient

CFG: dict[str, Any] = {
    "head_url": "http://head:8265",
    "serve_base_url": "http://head:8000",
    "org_id": 1,
}


@pytest.fixture
def vllm_ray_backend() -> VllmRayBackend:
    """Backend over an empty FakeDAL and an in-memory Ray client."""
    b = VllmRayBackend(FakeDAL(), dict(CFG), None, FakeRayServeClient())
    b.fleet_backend_id = 3
    return b


def _insert(b: VllmRayBackend, public_id: str, status: S, **over: Any) -> None:
    """Insert one minimal deployment row owned by ``b``."""
    vals: dict[str, Any] = {
        "public_id": public_id,
        "org_id": 1,
        "fleet_backend_id": b.fleet_backend_id,
        "name": public_id,
        "slug": public_id,
        "served_model_name": "m1",
        "vendor": int(GpuVendor.NVIDIA),
        "gpu_class": "nvidia-a100-80g",
        "status": int(status),
        "serve_app_name": f"vllm-{public_id}",
        "endpoint_url": f"http://head:8000/{public_id}",
        "created_at": datetime.now(UTC).replace(tzinfo=None),
    }
    vals.update(over)
    b.db.vllm_deployments.insert(**vals)


async def test_six_methods_on_empty_fleet(vllm_ray_backend: VllmRayBackend) -> None:
    """Empty fleet: no nodes/endpoints, health reports zero nodes, the two refusals hold."""
    assert await vllm_ray_backend.list_nodes() == []
    assert await vllm_ray_backend.endpoints_for("any") == []
    h = await vllm_ray_backend.health()
    assert isinstance(h, FleetHealth)
    assert h.backend_id == vllm_ray_backend.fleet_backend_id
    assert h.node_count == 0
    with pytest.raises(PermissionError):
        await vllm_ray_backend.deprovision("n")
    with pytest.raises(RuntimeError, match="Deploy it from the GPU Fleet page"):
        await vllm_ray_backend.place_model("m", {"lazy": True})


async def test_endpoints_and_place_model_use_serving_rows_only(
    vllm_ray_backend: VllmRayBackend,
) -> None:
    """SERVING rows are routable and placeable; STOPPED rows are not."""
    _insert(vllm_ray_backend, "up", S.SERVING)
    _insert(vllm_ray_backend, "down", S.STOPPED, serve_app_name="vllm-down")
    eps = await vllm_ray_backend.endpoints_for("m1")
    assert [e.url for e in eps] == ["http://head:8000/up"]
    placement = await vllm_ray_backend.place_model("m1", {})
    assert placement.node_id == "vllm-up"
    assert placement.status == "placed"


async def test_provision_deploys_listed_deployments(vllm_ray_backend: VllmRayBackend) -> None:
    """provision() deploys exactly the listed public ids and returns list_nodes()."""
    _insert(vllm_ray_backend, "a", S.PENDING)
    _insert(vllm_ray_backend, "b", S.PENDING, serve_app_name="vllm-b")
    spec = ProvisionSpec(
        name="x", models=[], mode="full_lifecycle", constraints={"deployment_public_ids": ["a"]}
    )
    with patch.object(VllmRayBackend, "deploy", new=AsyncMock()) as deploy:
        nodes = await vllm_ray_backend.provision(spec)
    assert nodes == []
    assert [c.args[0].public_id for c in deploy.await_args_list] == ["a"]


async def test_provision_rejects_missing_or_unknown_ids(vllm_ray_backend: VllmRayBackend) -> None:
    """No ids or an unknown id fails loudly."""
    with pytest.raises(ValueError, match="deployment_public_ids"):
        await vllm_ray_backend.provision(
            ProvisionSpec(name="x", models=[], mode="m", constraints={})
        )
    with pytest.raises(ValueError, match="unknown deployment"):
        await vllm_ray_backend.provision(
            ProvisionSpec(
                name="x", models=[], mode="m", constraints={"deployment_public_ids": ["zz"]}
            )
        )


async def test_collect_inventory_drives_list_nodes_and_health(
    vllm_ray_backend: VllmRayBackend,
) -> None:
    """A collected A100 payload surfaces as a node, and health sees the node."""
    client = vllm_ray_backend._client()
    assert isinstance(client, FakeRayServeClient)
    client.inventory_payloads = [
        {
            "node_uid": "n1",
            "node_name": "gpu-1",
            "vendor": "nvidia",
            "raw": "0, NVIDIA A100-SXM4-80GB, 81920, 1000, 00000000:00:04.0",
        }
    ]
    recs = await vllm_ray_backend.collect_inventory()
    assert len(recs) == 1
    nodes = await vllm_ray_backend.list_nodes()
    assert [n.node_id.split("/")[0] for n in nodes] == ["gpu-1"]
    assert (await vllm_ray_backend.health()).node_count == 1


def test_registry_build_backend_vllm_ray_decrypts_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """build_backend resolves type, decrypts the token, builds the authenticated HTTP client."""
    row = SimpleNamespace(
        id=7,
        type="vllm_ray",
        management_scope="full_lifecycle",
        config=dict(CFG),
        credentials_ref="enc:xyz",
    )
    caplog.set_level(logging.DEBUG)
    with patch.object(registry, "decrypt_credential", return_value="SECRET-RAY-TOKEN-9999"):
        backend = registry.build_backend(FakeDAL(), row)
    assert isinstance(backend, VllmRayBackend)
    assert backend.fleet_backend_id == 7
    assert isinstance(backend._client(), HttpRayServeClient)
    assert "SECRET-RAY-TOKEN" not in caplog.text
    assert "SECRET-RAY-TOKEN" not in repr(backend)
    assert "SECRET-RAY-TOKEN" not in repr(backend._client())
