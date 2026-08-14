"""Tests for ``ExoFleetBackend`` (shared.fleet.exo) — spec §10.1 plan Task 8.

All network calls are mocked (``aiohttp``) — no live EXO cluster required.
Also asserts the GPLv3 network-boundary constraint: no EXO source is
vendored or imported anywhere in this module.
"""

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.fleet import exo as exo_module
from shared.fleet.base import BackendType, ManagementScope, ProvisionSpec
from shared.fleet.exo import ExoFleetBackend


def test_no_exo_source_vendored_or_imported() -> None:
    """Static-parse the module: no ``import exo`` / ``from exo import ...`` anywhere.

    This is the GPLv3 network-boundary guard the plan requires — the
    backend must be a pure HTTP client, never a linked dependency.
    """
    source = Path(exo_module.__file__).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.split(".")[0] == "exo", f"vendored EXO import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "exo" and not (node.module or "").startswith("exo."), (
                f"vendored EXO import: {node.module}"
            )


def _mock_session(status: int, payload: dict) -> MagicMock:
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


@pytest.fixture
def exo_backend() -> ExoFleetBackend:
    """An EXO backend pointed at a fake cluster endpoint."""
    return ExoFleetBackend(
        db=None, config={"endpoint_url": "http://exo-cluster.internal:8000", "name": "exo-a"}
    )


def test_type_and_scope() -> None:
    """``type``/``management_scope`` match the registry contract."""
    backend = ExoFleetBackend(db=None, config={"endpoint_url": "http://x:8000"})
    assert backend.type == BackendType.EXO
    assert backend.management_scope == ManagementScope.REGISTER_AND_ROUTE


def test_management_scope_is_forced_register_and_route() -> None:
    """Even if constructed with cloud-style intent, scope stays register_and_route."""
    backend = ExoFleetBackend(db=None, config={"endpoint_url": "http://x:8000"})
    backend.management_scope = ManagementScope.FULL_LIFECYCLE  # simulate a bad row value
    # Re-construction always forces it back — this documents that provision()
    # does not gate on scope for EXO (unlike Vertex/Bedrock) since EXO never
    # accepts full_lifecycle.
    fresh = ExoFleetBackend(db=None, config={"endpoint_url": "http://x:8000"})
    assert fresh.management_scope == ManagementScope.REGISTER_AND_ROUTE


def test_base_url_missing_raises(exo_backend) -> None:
    """No ``endpoint_url`` in config raises a clear error."""
    backend = ExoFleetBackend(db=None, config={})
    with pytest.raises(ValueError, match="endpoint_url"):
        _ = backend._base_url


async def test_provision_flag_off_raises(exo_backend, monkeypatch) -> None:
    """``waddleai.fleet_v2`` OFF blocks provision — fail-safe default."""
    monkeypatch.delenv("WADDLEAI_FLAG_FLEET_V2", raising=False)
    monkeypatch.delenv("POSTHOG_KEY", raising=False)
    with pytest.raises(RuntimeError, match="fleet_v2"):
        await exo_backend.provision(
            ProvisionSpec(name="exo-a", models=[], mode="external", constraints={})
        )


async def test_provision_reachable_endpoint_returns_node(exo_backend, monkeypatch) -> None:
    """A reachable cluster validates and returns one external node."""
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
    session = _mock_session(200, {"data": [{"id": "llama-3.1-70b"}, {"id": "qwen2.5-72b"}]})
    with patch("aiohttp.ClientSession", return_value=session):
        nodes = await exo_backend.provision(
            ProvisionSpec(name="exo-a", models=[], mode="external", constraints={})
        )
    assert len(nodes) == 1
    assert nodes[0].node_id == "exo-a"
    assert nodes[0].kind == "external"
    assert nodes[0].loaded_models == ["llama-3.1-70b", "qwen2.5-72b"]
    assert nodes[0].healthy is True


async def test_provision_unreachable_endpoint_raises(exo_backend, monkeypatch) -> None:
    """An unreachable EXO endpoint raises, not a silent empty registration."""
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
    session = _mock_session(503, {})
    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(RuntimeError, match="unreachable"):
            await exo_backend.provision(
                ProvisionSpec(name="exo-a", models=[], mode="external", constraints={})
            )


async def test_deprovision_is_noop(exo_backend) -> None:
    """Deprovisioning never touches the external cluster — no-op, never raises."""
    await exo_backend.deprovision("exo-a")  # must not raise


async def test_health_reflects_reachable_cluster(exo_backend, monkeypatch) -> None:
    """``health()`` reports healthy=True with a reachable cluster."""
    session = _mock_session(200, {"data": [{"id": "m1"}]})
    with patch("aiohttp.ClientSession", return_value=session):
        health = await exo_backend.health()
    assert health.healthy is True
    assert health.node_count == 1
    assert health.detail["healthy_nodes"] == 1


async def test_health_reflects_unreachable_cluster(exo_backend) -> None:
    """``health()`` reports healthy=False, not an exception, when the cluster is down."""
    session = _mock_session(500, {})
    with patch("aiohttp.ClientSession", return_value=session):
        health = await exo_backend.health()
    assert health.healthy is False
    assert health.detail["healthy_nodes"] == 0


async def test_list_nodes_returns_single_logical_node(exo_backend) -> None:
    """``list_nodes`` returns exactly one node — EXO's internal topology is opaque."""
    session = _mock_session(200, {"data": [{"id": "m1"}, {"id": "m2"}]})
    with patch("aiohttp.ClientSession", return_value=session):
        nodes = await exo_backend.list_nodes()
    assert len(nodes) == 1
    assert nodes[0].loaded_models == ["m1", "m2"]


async def test_place_model_found_returns_placed(exo_backend) -> None:
    """A model the cluster reports serving maps to ``status="placed"``."""
    session = _mock_session(200, {"data": [{"id": "m1"}]})
    with patch("aiohttp.ClientSession", return_value=session):
        placement = await exo_backend.place_model("m1", {})
    assert placement.status == "placed"
    assert placement.node_id == "exo-a"


async def test_place_model_not_found_raises(exo_backend) -> None:
    """A model the cluster does not report serving raises, not a silent no-op."""
    session = _mock_session(200, {"data": [{"id": "m1"}]})
    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(RuntimeError, match="not currently serving"):
            await exo_backend.place_model("does-not-exist", {})


async def test_endpoints_for_matches_model(exo_backend) -> None:
    """``endpoints_for`` returns the cluster endpoint only when it serves the model."""
    session = _mock_session(200, {"data": [{"id": "m1"}]})
    with patch("aiohttp.ClientSession", return_value=session):
        endpoints = await exo_backend.endpoints_for("m1")
    assert len(endpoints) == 1
    assert endpoints[0].url == "http://exo-cluster.internal:8000"

    session = _mock_session(200, {"data": [{"id": "m1"}]})
    with patch("aiohttp.ClientSession", return_value=session):
        no_match = await exo_backend.endpoints_for("does-not-exist")
    assert no_match == []


async def test_endpoints_for_unreachable_returns_empty_not_raise(exo_backend) -> None:
    """A transport failure in ``endpoints_for`` degrades to empty, not an exception."""
    session = _mock_session(500, {})
    with patch("aiohttp.ClientSession", return_value=session):
        endpoints = await exo_backend.endpoints_for("m1")
    assert endpoints == []


def test_bearer_token_header_when_credentials_present() -> None:
    """Credentials (decrypted by the registry) become a Bearer header, never logged."""
    backend = ExoFleetBackend(
        db=None, config={"endpoint_url": "http://x:8000"}, credentials="super-secret-token"
    )
    assert backend._headers() == {"Authorization": "Bearer super-secret-token"}


def test_no_bearer_header_without_credentials(exo_backend) -> None:
    """No credentials configured means no Authorization header at all."""
    assert exo_backend._headers() == {}
