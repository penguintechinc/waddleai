"""vLLM offers: one LOCAL_PROVIDERS constant, SERVING-only availability, org isolation (AM3)."""

from __future__ import annotations

from typing import Any

import pytest

from shared.fleet.enums import DeploymentStatus as S
from shared.fleet.placement import PlacementEngine
from shared.fleet.vllm_ray.backend import VllmRayBackend
from shared.routing import offers as offers_module
from shared.routing.offers import load_offers_from_model_configs
from shared.routing.providers import LOCAL_PROVIDERS
from tests.conformance._fake_dal import FakeDAL
from tests.conformance._fake_ray import FakeRayServeClient
from tests.conformance.test_vllm_ray_behavior import CFG, _insert

pytestmark = pytest.mark.usefixtures("restore_obs_meter_cache")


def _config(model: str, providers: list[str]) -> dict[str, Any]:
    """A model_configs row."""
    return {
        "model_name": model,
        "preferred_providers": providers,
        "cost_per_token": {},
        "context_length": 8192,
        "capabilities": [],
        "enabled": True,
    }


def _backend() -> VllmRayBackend:
    """A real vLLM backend over an in-memory DAL."""
    backend = VllmRayBackend(FakeDAL(), dict(CFG), None, FakeRayServeClient())
    backend.fleet_backend_id = 3
    return backend


async def _offers(fake_db: Any, backend: VllmRayBackend | None = None, **kw: Any) -> list[Any]:
    """Load offers, annotating with real placement when a backend is given."""
    if backend is None:
        return await load_offers_from_model_configs(fake_db, **kw)
    engine = PlacementEngine(None, None, None)
    return await load_offers_from_model_configs(fake_db, placement=engine, backends=[backend], **kw)


def test_local_providers_contents_disjoint_from_commercial() -> None:
    """LOCAL_PROVIDERS is the single constant; offers and stages import it."""
    from proxy.apps.proxy_server.pipeline import stages

    assert LOCAL_PROVIDERS == frozenset({"ollama", "llamacpp", "vllm"})
    assert LOCAL_PROVIDERS.isdisjoint(stages._COMMERCIAL_PROVIDERS)
    assert stages.LOCAL_PROVIDERS is LOCAL_PROVIDERS
    assert offers_module.LOCAL_PROVIDERS is LOCAL_PROVIDERS


async def test_vllm_only_model_config_is_local_offer(fake_db) -> None:
    """A model whose preferred provider is vllm is a local offer."""
    fake_db.seed("model_configs", [_config("m1", ["vllm"])])
    assert (await _offers(fake_db))[0].location == "local"


async def test_mixed_model_config_stays_commercial(fake_db) -> None:
    """A model preferring a commercial provider stays commercial (review focus 4)."""
    fake_db.seed("model_configs", [_config("m1", ["openai"])])
    assert (await _offers(fake_db))[0].location == "commercial"


@pytest.mark.parametrize(
    ("status", "available"),
    [(S.SERVING, True), (S.DEGRADED, False), (S.STOPPED, False), (S.FAILED, False)],
)
async def test_deployment_status_drives_offer_availability(fake_db, status, available) -> None:
    """Only a SERVING deployment makes its local offer available; the link row is untouched."""
    backend = _backend()
    _insert(backend, "d1", status)
    fake_db.seed("model_configs", [_config("m1", ["vllm"])])
    fake_db.seed("connection_links", [{"name": "vllm-1-d1", "enabled": True, "model_list": ["m1"]}])
    offers = await _offers(fake_db, backend)
    assert offers[0].available is available
    assert fake_db._tables["connection_links"]  # review focus 2: row kept


async def test_deleted_deployment_offer_unavailable(fake_db) -> None:
    """No deployment row (deleted) -> the local offer is unavailable, never stale-available."""
    fake_db.seed("model_configs", [_config("m1", ["vllm"])])
    offers = await _offers(fake_db, _backend())
    assert offers[0].available is False


async def test_foreign_org_vllm_link_never_offered(fake_db) -> None:
    """A model served only by another org's owned link is absent from this org's offers."""
    fake_db.seed("model_configs", [_config("m1", ["vllm"]), _config("m2", ["vllm"])])
    fake_db.seed(
        "connection_links",
        [
            {"name": "a", "enabled": True, "model_list": ["m1"], "owner_org_id": 2},
            {"name": "b", "enabled": True, "model_list": ["m2"], "owner_org_id": 1},
        ],
    )
    names = {o.model_name for o in await _offers(fake_db, org_id=1)}
    assert names == {"m2"}
    assert {o.model_name for o in await _offers(fake_db, org_id=2)} == {"m1"}
    # a shared (unowned) link makes the model visible to everyone
    fake_db.seed("connection_links", [{"name": "c", "enabled": True, "model_list": ["m1"]}])
    assert {o.model_name for o in await _offers(fake_db, org_id=1)} == {"m1", "m2"}


async def test_model_configs_owner_org_filters_other_orgs_offers(fake_db) -> None:
    """Only shared rows and the requesting org's own rows are offered (owner_org_id read)."""
    fake_db.seed(
        "model_configs",
        [
            {**_config("shared-m", ["openai"]), "owner_org_id": None},
            {**_config("a-local", ["vllm"]), "owner_org_id": 1, "deployment_public_id": "x"},
            {**_config("b-local", ["vllm"]), "owner_org_id": 2, "deployment_public_id": "y"},
        ],
    )
    assert {o.model_name for o in await _offers(fake_db, org_id=1)} == {"shared-m", "a-local"}
    assert {o.model_name for o in await _offers(fake_db, org_id=2)} == {"shared-m", "b-local"}
    assert {o.model_name for o in await _offers(fake_db, org_id=3)} == {"shared-m"}
    # no org (e.g. the gRPC adapter): owned rows are never offered
    assert {o.model_name for o in await _offers(fake_db)} == {"shared-m"}


async def test_org_owned_local_offer_never_shadows_commercial_for_other_orgs(fake_db) -> None:
    """Same model name: org 1's local row is invisible to org 2, whose offer stays commercial."""
    fake_db.seed(
        "model_configs",
        [
            {**_config("m", ["openai"]), "owner_org_id": None},
            {**_config("m", ["vllm"]), "owner_org_id": 1},
        ],
    )
    other = await _offers(fake_db, org_id=2)
    assert [(o.model_name, o.location) for o in other] == [("m", "commercial")]
    owner = await _offers(fake_db, org_id=1)
    assert [(o.model_name, o.location) for o in owner] == [("m", "local")]  # owner's row wins


async def test_stage_load_offers_applies_the_same_owner_filter(fake_db) -> None:
    """RoutingStage._load_offers (the proxy reader) filters model_configs by owner too."""
    from proxy.apps.proxy_server.pipeline.stages import RoutingStage

    fake_db.seed(
        "model_configs",
        [
            {**_config("shared-m", ["openai"])},
            {**_config("a-local", ["vllm"]), "owner_org_id": 1},
            {**_config("b-local", ["vllm"]), "owner_org_id": 2},
        ],
    )
    stage = RoutingStage("routing", engine=None, db=fake_db, flag=None)  # type: ignore[arg-type]
    assert {o.model_name for o in await stage._load_offers(1)} == {"shared-m", "a-local"}
    assert {o.model_name for o in await stage._load_offers(0)} == {"shared-m"}


def test_prefer_local_fallback_ignores_other_orgs_owned_commercial_rows(fake_db) -> None:
    """A fallback model visible only as another org's row does not satisfy the rule factory."""
    from types import SimpleNamespace

    from shared.routing.prefer_local import create_prefer_local_rule

    fake_db.seed("model_configs", [{**_config("gpt-x", ["openai"]), "owner_org_id": 2}])
    dep = SimpleNamespace(slug="s", served_model_name="m", public_id="p")
    with pytest.raises(ValueError, match="commercial model"):
        create_prefer_local_rule(fake_db, 1, dep, "gpt-x", {"model_prefix": "a"})


async def test_foreign_only_models_degrades_safely() -> None:
    """No links table or a failing query yields no hidden models (dispatch still isolates)."""
    from shared.routing.providers import foreign_only_models

    assert await foreign_only_models(object(), 1) == set()

    class Broken:
        connection_links = type("T", (), {"enabled": True})()

        def __call__(self, _q: Any) -> Any:
            raise RuntimeError("db down")

    assert await foreign_only_models(Broken(), 1) == set()
