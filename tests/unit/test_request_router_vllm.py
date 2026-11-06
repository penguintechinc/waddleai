"""Router/connector isolation + vLLM routing rules: org ownership, routability, no wildcard."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from shared.utils.llm_connectors import (
    LLMConnectionManager,
    OpenAIConnector,
    VllmConnector,
)
from shared.utils.request_router import LLMRequestRouter, ProviderStats

pytestmark = pytest.mark.usefixtures("restore_obs_meter_cache")


def _vllm(
    name: str, models: list[str], owner: int | None = 1, status: int | None = 5
) -> VllmConnector:
    """A vLLM connector for deployment ``name``."""
    return VllmConnector(
        name,
        {
            "endpoint_url": "http://x/v1",
            "api_key": "k",
            "model_list": models,
            "owner_org_id": owner,
            "management_capabilities": {"local_deployment_id": 1, "fleet_status": status},
        },
    )


def _openai(name: str = "openai", models: list[str] | None = None, owner: int | None = None) -> Any:
    """A commercial connector."""
    return OpenAIConnector(
        name,
        {
            "endpoint_url": "http://x/v1",
            "api_key": "k",
            "model_list": models if models is not None else ["m"],
            "owner_org_id": owner,
        },
    )


def _router(connectors: dict[str, Any]) -> LLMRequestRouter:
    """A router over a manager whose connectors are the given ones."""
    manager = MagicMock()
    manager.connectors = connectors
    router = LLMRequestRouter(llm_manager=manager, db=MagicMock())
    router.provider_stats = {name: ProviderStats() for name in connectors}
    return router


def _manager(connectors: dict[str, Any]) -> LLMConnectionManager:
    """A real manager with preset connectors (no DB)."""
    mgr = LLMConnectionManager.__new__(LLMConnectionManager)
    mgr.connectors = connectors
    return mgr


def test_router_skips_non_routable_vllm_connector() -> None:
    """A non-SERVING vLLM connector is never offered; the commercial one is chosen."""
    router = _router({"vllm-1-x": _vllm("vllm-1-x", ["m"], status=6), "openai": _openai()})
    assert router.select_provider("m", org_id=1) == ("openai", "m")
    only_vllm = _router({"vllm-1-x": _vllm("vllm-1-x", ["m"], status=3)})
    assert only_vllm.select_provider("m", org_id=1) is None


def test_router_vllm_not_implicit_when_commercial_serves_same_model() -> None:
    """Same served name as a commercial model: vLLM only through an explicit destination."""
    # commercial listed FIRST so strategy selection alone would pick it
    router = _router({"openai": _openai(), "vllm-1-x": _vllm("vllm-1-x", ["m"])})
    assert router.select_provider("m", org_id=1) == ("openai", "m")
    assert router.select_provider("m", org_id=1, destination="vllm-1-x") == ("vllm-1-x", "m")


def test_router_vllm_selected_when_only_server_of_model() -> None:
    """With no commercial server of the model, a SERVING vLLM connector is selectable."""
    router = _router({"vllm-1-x": _vllm("vllm-1-x", ["only-local"])})
    assert router.select_provider("only-local", org_id=1) == ("vllm-1-x", "only-local")


def test_destination_never_overrides_breaker_or_routability() -> None:
    """An open breaker or non-routable deployment is not selected even as the destination."""
    from datetime import datetime

    conns = {"vllm-1-x": _vllm("vllm-1-x", ["m"]), "openai": _openai()}
    router = _router(conns)
    router.provider_stats["vllm-1-x"] = ProviderStats(
        consecutive_failures=3, last_failure=datetime.utcnow()
    )
    assert router.select_provider("m", org_id=1, destination="vllm-1-x") == ("openai", "m")
    conns2 = {"vllm-1-x": _vllm("vllm-1-x", ["m"], status=6), "openai": _openai()}
    assert _router(conns2).select_provider("m", org_id=1, destination="vllm-1-x") == (
        "openai",
        "m",
    )


def test_affinity_eligible_includes_vllm() -> None:
    """Session-affinity eligibility covers vLLM connectors."""
    router = _router({"vllm-1-x": _vllm("vllm-1-x", ["m"])})
    assert router._is_affinity_eligible("vllm-1-x") is True


# --- org isolation: a foreign-owned link is unreachable through EVERY lookup path ---


@pytest.mark.parametrize("requesting_org", [2, None])
def test_foreign_owned_link_never_selected_by_router(requesting_org: int | None) -> None:
    """Router selection, including an explicit destination, skips another org's link."""
    router = _router({"vllm-1-x": _vllm("vllm-1-x", ["m"], owner=1)})
    assert router.select_provider("m", org_id=requesting_org) is None
    assert router.select_provider("m", org_id=requesting_org, destination="vllm-1-x") is None
    assert router._get_available_providers("m", requesting_org, "vllm-1-x") == []
    assert router.select_provider("m", org_id=1) == ("vllm-1-x", "m")


def test_foreign_owned_link_hidden_from_manager_lookups() -> None:
    """get_connector / get_connector_for_model never return another org's connector."""
    mgr = _manager({"vllm-1-x": _vllm("vllm-1-x", ["m"], owner=1)})
    assert mgr.get_connector("vllm-1-x", org_id=2) is None
    assert mgr.get_connector("vllm-1-x") is None  # unknown org: fail closed
    assert mgr.get_connector_for_model("m", org_id=2) is None
    assert mgr.get_connector_for_model("m") is None
    assert mgr.get_connector("vllm-1-x", org_id=1) is not None
    assert mgr.get_connector_for_model("m", org_id=1) is not None


async def test_foreign_owned_link_models_not_listed() -> None:
    """/v1/models listing skips connectors owned by another org."""
    mine, theirs = _vllm("vllm-1-x", ["m"], owner=1), _vllm("vllm-2-y", ["n"], owner=2)
    mine.list_models = _async_return([{"id": "m"}])  # type: ignore[method-assign]
    theirs.list_models = _async_return([{"id": "n"}])  # type: ignore[method-assign]
    mgr = _manager({"vllm-1-x": mine, "vllm-2-y": theirs})
    assert await mgr.list_all_models(org_id=1) == [{"id": "m"}]
    assert await mgr.list_all_models(org_id=2) == [{"id": "n"}]
    assert await mgr.list_all_models() == []


def _async_return(value: Any) -> Any:
    """An async callable returning ``value``."""

    async def _inner(*_a: Any, **_k: Any) -> Any:
        return value

    return _inner


def test_empty_model_list_on_owned_or_vllm_link_matches_nothing() -> None:
    """An empty model_list is a wildcard for shared commercial links only."""
    owned_empty = _openai("o", models=[], owner=1)
    vllm_empty = _vllm("v", [], owner=None)
    shared_empty = _openai("s", models=[])
    router = _router({"o": owned_empty, "v": vllm_empty})
    assert router.select_provider("anything", org_id=1) is None
    assert _router({"s": shared_empty}).select_provider("anything", org_id=1) == (
        "s",
        "anything",
    )
    mgr = _manager({"o": owned_empty, "v": vllm_empty})
    assert mgr.get_connector_for_model("anything", org_id=1) is None
    assert _manager({"s": shared_empty}).get_connector_for_model("anything") is shared_empty


async def test_dispatch_stage_refuses_foreign_owned_connector() -> None:
    """Even if a stale name selects another org's connector, DispatchStage refuses it."""
    from proxy.apps.proxy_server.pipeline.stages import DispatchStage, PipelineContext

    foreign = _vllm("vllm-1-x", ["m"], owner=1)
    router = MagicMock()
    router.select_provider.return_value = ("vllm-1-x", "m")
    stage = DispatchStage("dispatch", router, {"vllm-1-x": foreign})
    ctx = PipelineContext(
        user=MagicMock(organization_id=2),
        body={},
        model="m",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert await stage._prepare_dispatch(ctx) is None
    assert ctx.blocked and ctx.block_reason == "no_connector"
    router.select_provider.assert_called_once()
    assert router.select_provider.call_args.kwargs["org_id"] == 2
