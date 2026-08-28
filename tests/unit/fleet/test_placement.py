"""Tests for shared.fleet.placement -- affinity, hot-pin, lazy pull, origin deny-list."""

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from shared.fleet.base import (
    BackendType,
    Endpoint,
    ManagementScope,
    ModelPlacement,
    NodeInfo,
)
from shared.fleet.caps import CapExceededError, InsufficientCapacityError
from shared.fleet.placement import ModelRegistryEntry, PlacementEngine, is_denied_origin
from shared.routing.capability import ModelOffer
from tests.conformance._fake_dal import FakeDAL
from tests.unit.routing.conftest import FakeAsyncRedis

_ORG_ID = 1


@dataclass
class _LicenseInfo:
    """Minimal stand-in for penguin_licensing.LicenseClient's validate() return shape."""

    tier: str


class _FakeLicenseClient:
    """Sync stand-in for penguin_licensing.LicenseClient, mirroring its .validate() shape."""

    def __init__(self, tier: str = "professional") -> None:
        """Configure the tier this client reports."""
        self._tier = tier

    def validate(self) -> _LicenseInfo:
        """Return a LicenseInfo-shaped result for the configured tier."""
        return _LicenseInfo(tier=self._tier)


def _engine(
    valkey=None, tier: str = "professional", hot_model_pins: dict[str, str] | None = None
) -> PlacementEngine:
    """A PlacementEngine with `professional` tier by default.

    Professional keeps enforce_model_cap a no-op-by-limit so these tests
    isolate placement behavior, not cap arithmetic (covered by
    test_caps.py and TestPlaceModelRealCapEnforcerIntegration below).
    """
    return PlacementEngine(
        db=None,
        valkey=valkey,
        license_client=_FakeLicenseClient(tier=tier),
        hot_model_pins=hot_model_pins,
    )


def _endpoint(node_id: str, healthy: bool = True, loaded: list[str] | None = None) -> Endpoint:
    """Build a routable Endpoint for a node, defaulting to one loaded model."""
    return Endpoint(
        url=f"http://{node_id}", node_id=node_id, loaded_models=loaded or ["m1"], healthy=healthy
    )


class _FakeBackend:
    """A minimal InferenceFleetBackend stand-in with AsyncMock hooks."""

    type = BackendType.OLLAMA
    management_scope = ManagementScope.FULL_LIFECYCLE

    def __init__(self, fleet_backend_id: int = 1) -> None:
        """Wire default AsyncMock behavior for every interface method used in tests."""
        self.fleet_backend_id = fleet_backend_id
        self.endpoints_for = AsyncMock(return_value=[])
        self.list_nodes = AsyncMock(return_value=[])
        self.place_model = AsyncMock(
            return_value=ModelPlacement(model="m1", node_id="n1", status="placed")
        )


class TestSelectEndpointAffinity:
    """select_endpoint: session affinity pin/fallthrough, no-session load-balancing."""

    async def test_repeated_session_id_returns_same_healthy_node(self) -> None:
        """A repeated session_id pins to the same node while it stays healthy."""
        engine = _engine(valkey=FakeAsyncRedis())
        endpoints = [_endpoint("n1"), _endpoint("n2")]

        first = await engine.select_endpoint("m1", "sess-1", endpoints)
        second = await engine.select_endpoint("m1", "sess-1", endpoints)

        assert first is not None
        assert second is not None
        assert first.node_id == second.node_id

    async def test_falls_through_to_load_balanced_when_pinned_node_unhealthy(self) -> None:
        """An unhealthy pinned node falls through to a load-balanced healthy choice."""
        engine = _engine(valkey=FakeAsyncRedis())

        # First call pins to n1 (least-loaded of a single-endpoint pool).
        await engine.select_endpoint("m1", "sess-1", [_endpoint("n1")])

        # n1 is now unhealthy; only n2 is a viable healthy choice.
        chosen = await engine.select_endpoint(
            "m1", "sess-1", [_endpoint("n1", healthy=False), _endpoint("n2")]
        )
        assert chosen is not None
        assert chosen.node_id == "n2"

    async def test_no_session_id_still_load_balances(self) -> None:
        """Without a session_id, the least-loaded endpoint is chosen."""
        engine = _engine()
        chosen = await engine.select_endpoint(
            "m1", None, [_endpoint("n1", loaded=["m1", "m2"]), _endpoint("n2", loaded=["m1"])]
        )
        assert chosen is not None
        assert chosen.node_id == "n2"  # fewer loaded_models == less loaded

    async def test_empty_endpoints_returns_none(self) -> None:
        """No candidate endpoints returns None, not an error."""
        engine = _engine()
        assert await engine.select_endpoint("m1", "sess-1", []) is None

    async def test_no_valkey_client_skips_affinity_without_error(self) -> None:
        """A None valkey client degrades to plain load-balancing, no crash."""
        engine = _engine(valkey=None)
        chosen = await engine.select_endpoint("m1", "sess-1", [_endpoint("n1")])
        assert chosen is not None
        assert chosen.node_id == "n1"


class TestHotModelPin:
    """select_endpoint: operator-configured hot-model node pins (§10.4)."""

    async def test_hot_model_prefers_pinned_node_over_least_loaded(self) -> None:
        """A hot model is routed to its configured pin, not the least-loaded endpoint."""
        engine = _engine(hot_model_pins={"hot-model": "n2"})
        endpoints = [
            _endpoint("n1", loaded=["hot-model"]),  # least loaded, but not the pin
            _endpoint("n2", loaded=["hot-model", "other"]),  # the configured pin
        ]
        chosen = await engine.select_endpoint("hot-model", None, endpoints)
        assert chosen is not None
        assert chosen.node_id == "n2"

    async def test_non_hot_model_ignores_pin_config(self) -> None:
        """A model with no pin entry falls back to plain load-balancing."""
        engine = _engine(hot_model_pins={"hot-model": "n2"})
        endpoints = [
            _endpoint("n1", loaded=["cold-model"]),
            _endpoint("n2", loaded=["cold-model", "x"]),
        ]
        chosen = await engine.select_endpoint("cold-model", None, endpoints)
        assert chosen is not None
        assert chosen.node_id == "n1"  # least loaded -- the hot-model pin doesn't apply


class TestEnsurePlaced:
    """ensure_placed: lazy-pull a cold model, skip register_and_route backends."""

    async def test_cold_model_triggers_lazy_pull(self) -> None:
        """A model with no current endpoint triggers place_model(lazy=True)."""
        engine = _engine()
        backend = _FakeBackend()
        # First aggregation call (pre-pull): empty. Second (post-pull): populated.
        backend.endpoints_for = AsyncMock(side_effect=[[], [_endpoint("n1")]])

        result = await engine.ensure_placed("cold-model", [backend])

        backend.place_model.assert_awaited_once_with("cold-model", {"lazy": True})
        assert len(result) == 1
        assert result[0].node_id == "n1"

    async def test_warm_model_never_triggers_a_pull(self) -> None:
        """A model already served by an endpoint never triggers a pull."""
        engine = _engine()
        backend = _FakeBackend()
        backend.endpoints_for = AsyncMock(return_value=[_endpoint("n1")])

        result = await engine.ensure_placed("warm-model", [backend])

        backend.place_model.assert_not_awaited()
        assert len(result) == 1

    async def test_register_and_route_backend_never_gets_a_lazy_pull(self) -> None:
        """WaddleAI never pushes a placement onto a backend it doesn't lifecycle."""
        engine = _engine()
        backend = _FakeBackend()
        backend.management_scope = ManagementScope.REGISTER_AND_ROUTE
        backend.endpoints_for = AsyncMock(return_value=[])

        result = await engine.ensure_placed("cold-model", [backend])

        backend.place_model.assert_not_awaited()
        assert result == []


class TestOriginDenyList:
    """is_denied_origin + place_model's use of it (spec §2.2)."""

    def test_known_prc_origins_denied(self) -> None:
        """Every named §2.2 PRC-origin organization is denied."""
        for origin in ("Alibaba", "DeepSeek", "Zhipu AI", "Kuaishou", "01.AI", "Moonshot AI"):
            assert is_denied_origin(origin) is True

    def test_allowed_origins_not_denied(self) -> None:
        """Non-PRC origins already seeded in model_registry are never denied."""
        for origin in ("Google", "Microsoft", "Meta", "IBM", "Mistral", "HuggingFace", "Nomic"):
            assert is_denied_origin(origin) is False

    def test_none_and_empty_never_denied(self) -> None:
        """A missing/blank origin is never treated as denied."""
        assert is_denied_origin(None) is False
        assert is_denied_origin("") is False

    async def test_place_model_denied_never_dispatches_to_backend(self) -> None:
        """A deny-listed origin returns status=denied and never calls the backend."""
        engine = _engine()
        backend = _FakeBackend()
        registry_entry = ModelRegistryEntry(
            name="glm-4", origin="Zhipu AI", is_utility=False, min_vram=8
        )

        result = await engine.place_model(
            _ORG_ID, "glm-4", {}, backend, registry_entry=registry_entry
        )

        assert result.status == "denied"
        backend.place_model.assert_not_awaited()

    async def test_allowed_origin_dispatches_to_backend(self) -> None:
        """An allowed origin proceeds to a normal placement."""
        engine = _engine()
        backend = _FakeBackend()
        backend.list_nodes = AsyncMock(
            return_value=[NodeInfo("n1", "uid-1", "k8s", [], 16384, 10240, True)]
        )
        registry_entry = ModelRegistryEntry(
            name="gemma4:e2b", origin="Google", is_utility=False, min_vram=8
        )

        result = await engine.place_model(
            _ORG_ID, "gemma4:e2b", {}, backend, registry_entry=registry_entry
        )

        assert result.status == "placed"
        backend.place_model.assert_awaited_once()


class TestPlaceModelCapsAndCapacity:
    """place_model's cap-check and VRAM-capacity admission before dispatch."""

    async def test_cap_exceeded_propagates_and_never_dispatches(self) -> None:
        """A CapExceededError from the cap enforcer propagates, backend never called."""
        engine = _engine(tier="community")
        stub = _StubEnforcer(raise_on_model_cap=True)
        engine._cap_enforcer = lambda org_id: stub  # type: ignore[method-assign]
        backend = _FakeBackend()
        registry_entry = ModelRegistryEntry(
            name="m4", origin="Google", is_utility=False, min_vram=None
        )

        with pytest.raises(CapExceededError):
            await engine.place_model(_ORG_ID, "m4", {}, backend, registry_entry=registry_entry)
        backend.place_model.assert_not_awaited()

    async def test_utility_model_skips_cap_check(self) -> None:
        """A utility model's placement never even calls the cap enforcer."""
        engine = _engine(tier="community")
        stub = _StubEnforcer(raise_on_model_cap=True)
        engine._cap_enforcer = lambda org_id: stub  # type: ignore[method-assign]
        backend = _FakeBackend()
        registry_entry = ModelRegistryEntry(
            name="routing-classifier", origin="Google", is_utility=True, min_vram=None
        )

        result = await engine.place_model(
            _ORG_ID, "routing-classifier", {}, backend, registry_entry=registry_entry
        )
        assert result.status == "placed"
        assert stub.enforce_model_cap_calls == 0

    async def test_insufficient_capacity_raises_and_never_dispatches(self) -> None:
        """No node with enough free VRAM raises InsufficientCapacityError."""
        engine = _engine()
        backend = _FakeBackend()
        backend.list_nodes = AsyncMock(
            return_value=[NodeInfo("n1", "uid-1", "k8s", [], 4096, 2048, True)]
        )
        registry_entry = ModelRegistryEntry(
            name="big-model", origin="Google", is_utility=False, min_vram=32
        )

        with pytest.raises(InsufficientCapacityError):
            await engine.place_model(
                _ORG_ID, "big-model", {}, backend, registry_entry=registry_entry
            )
        backend.place_model.assert_not_awaited()

    async def test_capable_node_id_is_threaded_into_constraints(self) -> None:
        """A VRAM-fit node pick is passed to the backend as a node_id constraint."""
        engine = _engine()
        backend = _FakeBackend()
        backend.list_nodes = AsyncMock(
            return_value=[NodeInfo("n1", "uid-1", "k8s", [], 32768, 16384, True)]
        )
        registry_entry = ModelRegistryEntry(
            name="big-model", origin="Google", is_utility=False, min_vram=8
        )

        await engine.place_model(_ORG_ID, "big-model", {}, backend, registry_entry=registry_entry)

        backend.place_model.assert_awaited_once_with("big-model", {"node_id": "n1"})

    async def test_explicit_node_id_constraint_skips_capacity_pick(self) -> None:
        """A caller-supplied node_id bypasses the VRAM-fit lookup entirely."""
        engine = _engine()
        backend = _FakeBackend()
        registry_entry = ModelRegistryEntry(
            name="big-model", origin="Google", is_utility=False, min_vram=8
        )

        await engine.place_model(
            _ORG_ID,
            "big-model",
            {"node_id": "explicit-node"},
            backend,
            registry_entry=registry_entry,
        )

        backend.list_nodes.assert_not_awaited()
        backend.place_model.assert_awaited_once_with("big-model", {"node_id": "explicit-node"})

    async def test_unregistered_model_skips_all_checks_and_dispatches(self) -> None:
        """A model absent from model_registry proceeds unchecked (a registration gap)."""
        engine = _engine()
        backend = _FakeBackend()

        result = await engine.place_model(
            _ORG_ID, "unregistered-model", {}, backend, registry_entry=None
        )

        assert result.status == "placed"
        backend.place_model.assert_awaited_once_with("unregistered-model", {})


class TestPlaceModelRealCapEnforcerIntegration:
    """place_model against a real (FakeDAL-backed) CapEnforcer, not a stub.

    Covers the actual `_cap_enforcer(org_id)` construction path -- the
    other cap tests above stub it out to isolate placement-only behavior.
    """

    async def test_over_cap_under_community_raises_via_real_enforcer(self, monkeypatch) -> None:
        """A real CapEnforcer still raises for an org already at its model cap."""
        monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
        db = FakeDAL()
        backend_id = db.fleet_backends.insert(org_id=_ORG_ID, name="b1", type="ollama")
        deployment_id = db.ollama_deployments.insert(
            name="d1", fleet_backend_id=backend_id, endpoint_url="http://d1"
        )
        for name in ("m1", "m2", "m3"):
            db.ollama_models.insert(
                deployment_id=deployment_id, model_name=name, status="available"
            )
            db.model_registry.insert(name=name, origin="Google", is_utility=False, min_vram=None)
        db.model_registry.insert(name="m4", origin="Google", is_utility=False, min_vram=None)

        engine = PlacementEngine(
            db=db, valkey=None, license_client=_FakeLicenseClient(tier="community")
        )
        backend = _FakeBackend()
        registry_entry = ModelRegistryEntry(
            name="m4", origin="Google", is_utility=False, min_vram=None
        )

        with pytest.raises(CapExceededError, match="3 registered models"):
            await engine.place_model(_ORG_ID, "m4", {}, backend, registry_entry=registry_entry)
        backend.place_model.assert_not_awaited()

    async def test_under_cap_dispatches_via_real_enforcer(self, monkeypatch) -> None:
        """A real CapEnforcer allows placement when the org is under its model cap."""
        monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
        db = FakeDAL()
        db.model_registry.insert(name="m1", origin="Google", is_utility=False, min_vram=None)

        engine = PlacementEngine(
            db=db, valkey=None, license_client=_FakeLicenseClient(tier="community")
        )
        backend = _FakeBackend()
        registry_entry = ModelRegistryEntry(
            name="m1", origin="Google", is_utility=False, min_vram=None
        )

        result = await engine.place_model(_ORG_ID, "m1", {}, backend, registry_entry=registry_entry)
        assert result.status == "placed"


class _StubEnforcer:
    """A CapEnforcer stand-in that either always raises or always allows."""

    def __init__(self, raise_on_model_cap: bool) -> None:
        """Configure whether enforce_model_cap raises on every call."""
        self._raise = raise_on_model_cap
        self.enforce_model_cap_calls = 0

    async def enforce_model_cap(self, model: str) -> None:
        """Raise CapExceededError when configured to, else no-op."""
        self.enforce_model_cap_calls += 1
        if self._raise:
            raise CapExceededError("Free tier limited to 3 registered models")


class TestAnnotateOffers:
    """annotate_offers: local-offer availability corrected against live fleet state."""

    async def test_local_offer_marked_unavailable_with_no_live_endpoint(self) -> None:
        """A local offer with zero fleet endpoints is marked unavailable."""
        engine = _engine()
        backend = _FakeBackend()
        backend.endpoints_for = AsyncMock(return_value=[])
        offers = [ModelOffer(model_name="cold-local", location="local", available=True)]

        annotated = await engine.annotate_offers(offers, [backend])

        assert annotated[0].available is False

    async def test_local_offer_stays_available_with_a_healthy_endpoint(self) -> None:
        """A local offer with a healthy endpoint stays available."""
        engine = _engine()
        backend = _FakeBackend()
        backend.endpoints_for = AsyncMock(return_value=[_endpoint("n1", healthy=True)])
        offers = [ModelOffer(model_name="warm-local", location="local", available=True)]

        annotated = await engine.annotate_offers(offers, [backend])

        assert annotated[0].available is True

    async def test_commercial_offer_untouched(self) -> None:
        """A commercial offer is returned unchanged -- no fleet lookup applies."""
        engine = _engine()
        offers = [ModelOffer(model_name="gpt-4o", location="commercial", available=True)]

        annotated = await engine.annotate_offers(offers, [])

        assert annotated == offers


class TestEndpointsForAggregation:
    """endpoints_for: aggregation across backends, resilient to one backend failing."""

    async def test_aggregates_across_multiple_backends(self) -> None:
        """Endpoints from every backend are combined into one list."""
        engine = _engine()
        backend_a = _FakeBackend(fleet_backend_id=1)
        backend_a.endpoints_for = AsyncMock(return_value=[_endpoint("a1")])
        backend_b = _FakeBackend(fleet_backend_id=2)
        backend_b.endpoints_for = AsyncMock(return_value=[_endpoint("b1")])

        endpoints = await engine.endpoints_for("m1", [backend_a, backend_b])

        assert {e.node_id for e in endpoints} == {"a1", "b1"}

    async def test_one_backend_failing_does_not_fail_aggregation(self) -> None:
        """A single backend's exception is swallowed, other backends still contribute."""
        engine = _engine()
        backend_a = _FakeBackend(fleet_backend_id=1)
        backend_a.endpoints_for = AsyncMock(side_effect=RuntimeError("unreachable"))
        backend_b = _FakeBackend(fleet_backend_id=2)
        backend_b.endpoints_for = AsyncMock(return_value=[_endpoint("b1")])

        endpoints = await engine.endpoints_for("m1", [backend_a, backend_b])

        assert [e.node_id for e in endpoints] == ["b1"]
