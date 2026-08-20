"""Tests for shared.fleet.caps -- Free-tier node/model caps + VRAM capacity."""

from dataclasses import dataclass

import pytest

from shared.fleet.base import NodeInfo
from shared.fleet.caps import (
    CapEnforcer,
    CapExceededError,
    count_managed_nodes,
    fits_capacity,
    select_capable_node,
)
from tests.conformance._fake_dal import FakeDAL


@dataclass
class _LicenseInfo:
    """Minimal stand-in for penguin_licensing.LicenseClient's validate() return shape."""

    tier: str


class _FakeLicenseClient:
    """Sync stand-in for penguin_licensing.LicenseClient, mirroring its .validate() shape."""

    def __init__(self, tier: str = "community", raises: bool = False) -> None:
        """Configure the tier this client reports, or force validate() to raise."""
        self.tier = tier
        self.raises = raises
        self.calls = 0

    def validate(self) -> _LicenseInfo:
        """Return a LicenseInfo-shaped result, or raise if configured to fail."""
        self.calls += 1
        if self.raises:
            raise RuntimeError("license server unreachable")
        return _LicenseInfo(tier=self.tier)


def _enable_flag(monkeypatch) -> None:
    """Turn `waddleai.fleet_v2` on for the duration of one test."""
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")


def _seed_registry(db: FakeDAL, name: str, is_utility: bool = False) -> None:
    """Insert a minimal model_registry row for cap-check lookups."""
    db.model_registry.insert(name=name, origin="Google", is_utility=is_utility, min_vram=8)


class TestEnforceNodeCap:
    """enforce_node_cap: Free-tier ceiling, tier-uncapped, flag-off no-op, fail-safe."""

    async def test_raises_at_sixth_node_under_community(self, monkeypatch) -> None:
        """The 6th distinct node under community raises with the tier limit named."""
        _enable_flag(monkeypatch)
        enforcer = CapEnforcer(FakeDAL(), _FakeLicenseClient(tier="community"), org_id=1)

        for count in range(1, 6):
            await enforcer.enforce_node_cap(count)  # 1..5 all pass

        with pytest.raises(CapExceededError, match="5 inference nodes"):
            await enforcer.enforce_node_cap(6)

    async def test_uncapped_under_professional(self, monkeypatch) -> None:
        """Professional tier never raises regardless of node count."""
        _enable_flag(monkeypatch)
        enforcer = CapEnforcer(FakeDAL(), _FakeLicenseClient(tier="professional"), org_id=1)
        await enforcer.enforce_node_cap(500)  # never raises

    async def test_uncapped_under_enterprise(self, monkeypatch) -> None:
        """Enterprise tier never raises regardless of node count."""
        _enable_flag(monkeypatch)
        enforcer = CapEnforcer(FakeDAL(), _FakeLicenseClient(tier="enterprise"), org_id=1)
        await enforcer.enforce_node_cap(500)

    async def test_flag_off_is_a_noop(self, monkeypatch) -> None:
        """`waddleai.fleet_v2` off -- the legacy single-backend path is unaffected."""
        monkeypatch.delenv("WADDLEAI_FLAG_FLEET_V2", raising=False)
        enforcer = CapEnforcer(FakeDAL(), _FakeLicenseClient(tier="community"), org_id=1)
        await enforcer.enforce_node_cap(999)  # no raise -- legacy path unaffected

    async def test_license_error_fails_safe_to_community(self, monkeypatch) -> None:
        """A license-client error is treated as community tier, not bypassed."""
        _enable_flag(monkeypatch)
        enforcer = CapEnforcer(FakeDAL(), _FakeLicenseClient(raises=True), org_id=1)
        with pytest.raises(CapExceededError):
            await enforcer.enforce_node_cap(6)


class TestEnforceModelCap:
    """enforce_model_cap: Free-tier ceiling, re-placement/utility no-ops, tier-uncapped."""

    def _db_with_placed_models(
        self, model_names: list[str], utility_names: list[str] | None = None
    ) -> FakeDAL:
        """A FakeDAL with one org, one Ollama deployment, and the given placed models."""
        db = FakeDAL()
        backend_id = db.fleet_backends.insert(org_id=1, name="b1", type="ollama")
        deployment_id = db.ollama_deployments.insert(
            name="d1", fleet_backend_id=backend_id, endpoint_url="http://d1"
        )
        for name in model_names:
            db.ollama_models.insert(
                deployment_id=deployment_id, model_name=name, status="available"
            )
        for name in model_names + (utility_names or []):
            is_utility = name in (utility_names or [])
            _seed_registry(db, name, is_utility=is_utility)
        return db

    async def test_raises_at_fourth_non_utility_model(self, monkeypatch) -> None:
        """Placing a 4th distinct non-utility model under community raises."""
        _enable_flag(monkeypatch)
        db = self._db_with_placed_models(["m1", "m2", "m3"])
        enforcer = CapEnforcer(db, _FakeLicenseClient(tier="community"), org_id=1)

        with pytest.raises(CapExceededError, match="3 registered models"):
            await enforcer.enforce_model_cap("m4")

    async def test_re_placing_existing_model_never_raises(self, monkeypatch) -> None:
        """Re-placing an already-counted model never grows the set, never raises."""
        _enable_flag(monkeypatch)
        db = self._db_with_placed_models(["m1", "m2", "m3"])
        enforcer = CapEnforcer(db, _FakeLicenseClient(tier="community"), org_id=1)

        await enforcer.enforce_model_cap("m1")  # already counted -- must not raise

    async def test_utility_models_excluded_from_cap(self, monkeypatch) -> None:
        """A utility model (is_utility=True) never counts toward the model cap."""
        _enable_flag(monkeypatch)
        db = self._db_with_placed_models(["m1", "m2", "m3"], utility_names=["guard-model"])
        enforcer = CapEnforcer(db, _FakeLicenseClient(tier="community"), org_id=1)

        await enforcer.enforce_model_cap("guard-model")  # utility -- never counts

    async def test_uncapped_under_professional(self, monkeypatch) -> None:
        """Professional tier never raises regardless of registered model count."""
        _enable_flag(monkeypatch)
        db = self._db_with_placed_models(["m1", "m2", "m3", "m4", "m5"])
        enforcer = CapEnforcer(db, _FakeLicenseClient(tier="professional"), org_id=1)
        await enforcer.enforce_model_cap("m6")

    async def test_flag_off_is_a_noop(self, monkeypatch) -> None:
        """`waddleai.fleet_v2` off -- the legacy single-backend path is unaffected."""
        monkeypatch.delenv("WADDLEAI_FLAG_FLEET_V2", raising=False)
        db = self._db_with_placed_models(["m1", "m2", "m3"])
        enforcer = CapEnforcer(db, _FakeLicenseClient(tier="community"), org_id=1)
        await enforcer.enforce_model_cap("m4")


class TestCountManagedNodes:
    """count_managed_nodes: distinct-node counting across backends, incl. cloud/external."""

    def test_sums_distinct_k8s_nodes_by_uid(self) -> None:
        """The same node_uid reported by two backends counts once."""
        backend_a = [NodeInfo("n1", "uid-1", "k8s", [], 0, 0, True)]
        backend_b = [NodeInfo("n1", "uid-1", "k8s", [], 0, 0, True)]  # same UID
        assert count_managed_nodes([backend_a, backend_b]) == 1

    def test_counts_cloud_endpoints_as_managed_nodes(self) -> None:
        """Cloud endpoints (Vertex/Bedrock) count as managed nodes for Pro metering."""
        nodes = [
            NodeInfo("n1", "uid-1", "k8s", [], 0, 0, True),
            NodeInfo("vertex-ep-1", None, "cloud", [], 0, 0, True),
            NodeInfo("bedrock-ep-1", None, "cloud", [], 0, 0, True),
        ]
        assert count_managed_nodes([nodes]) == 3

    def test_external_nodes_counted_by_registered_endpoint(self) -> None:
        """An external node with no UID falls back to kind:node_id as its identity."""
        nodes = [NodeInfo("exo-cluster-a", None, "external", [], 0, 0, True)]
        assert count_managed_nodes([nodes]) == 1

    def test_empty_input(self) -> None:
        """No backends at all counts as zero managed nodes."""
        assert count_managed_nodes([]) == 0


class TestCapacity:
    """fits_capacity / select_capable_node: VRAM-headroom admission checks."""

    def test_fits_capacity_true_when_enough_free_vram(self) -> None:
        """A node with more free VRAM than required fits."""
        node = NodeInfo("n1", "uid-1", "k8s", [], 16384, 10240, True)
        assert fits_capacity(node, min_vram_gb=8) is True

    def test_fits_capacity_false_when_insufficient_free_vram(self) -> None:
        """A node with less free VRAM than required does not fit."""
        node = NodeInfo("n1", "uid-1", "k8s", [], 16384, 2048, True)
        assert fits_capacity(node, min_vram_gb=8) is False

    def test_fits_capacity_true_when_min_vram_unset(self) -> None:
        """An unset (None) min_vram requirement always fits -- unknown, not zero."""
        node = NodeInfo("n1", "uid-1", "k8s", [], 16384, 0, True)
        assert fits_capacity(node, min_vram_gb=None) is True

    def test_select_capable_node_skips_unhealthy_and_undersized(self) -> None:
        """The first healthy, sufficiently-provisioned node is chosen."""
        nodes = [
            NodeInfo("n1", "uid-1", "k8s", [], 16384, 2048, True),  # too little free VRAM
            NodeInfo("n2", "uid-2", "k8s", [], 16384, 10240, False),  # unhealthy
            NodeInfo("n3", "uid-3", "k8s", [], 16384, 10240, True),  # qualifies
        ]
        chosen = select_capable_node(nodes, min_vram_gb=8)
        assert chosen is not None
        assert chosen.node_id == "n3"

    def test_select_capable_node_returns_none_when_none_qualify(self) -> None:
        """No qualifying node returns None rather than an arbitrary pick."""
        nodes = [NodeInfo("n1", "uid-1", "k8s", [], 16384, 1024, True)]
        assert select_capable_node(nodes, min_vram_gb=8) is None


class TestMinVramAndUtilityLookup:
    """CapEnforcer.min_vram_for / is_utility_model: model_registry lookups."""

    async def test_min_vram_for_returns_registry_value(self, monkeypatch) -> None:
        """A registered model's min_vram (GB) is returned as-is."""
        db = FakeDAL()
        _seed_registry(db, "gemma4:e2b")
        enforcer = CapEnforcer(db, _FakeLicenseClient(), org_id=1)
        assert await enforcer.min_vram_for("gemma4:e2b") == 8

    async def test_min_vram_for_unregistered_model_is_none(self, monkeypatch) -> None:
        """A model with no model_registry row returns None, not zero."""
        enforcer = CapEnforcer(FakeDAL(), _FakeLicenseClient(), org_id=1)
        assert await enforcer.min_vram_for("unknown-model") is None

    async def test_is_utility_model(self, monkeypatch) -> None:
        """is_utility_model reflects the registry row, and is False for unknown models."""
        db = FakeDAL()
        _seed_registry(db, "shieldgemma:2b", is_utility=True)
        enforcer = CapEnforcer(db, _FakeLicenseClient(), org_id=1)
        assert await enforcer.is_utility_model("shieldgemma:2b") is True
        assert await enforcer.is_utility_model("unknown-model") is False
