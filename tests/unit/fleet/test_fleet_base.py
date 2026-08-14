"""Contract tests for ``InferenceFleetBackend`` and its value types."""

import abc
import dataclasses

import pytest

from shared.fleet.base import (
    BackendType,
    Endpoint,
    FleetHealth,
    InferenceFleetBackend,
    ManagementScope,
    ModelPlacement,
    NodeInfo,
    ProvisionSpec,
)


def test_is_abstract_base_class() -> None:
    """``InferenceFleetBackend`` cannot be instantiated directly."""
    assert issubclass(InferenceFleetBackend, abc.ABC)
    with pytest.raises(TypeError):
        InferenceFleetBackend()  # type: ignore[abstract]


class _CompleteBackend(InferenceFleetBackend):
    """Minimal concrete subclass implementing all six abstract methods."""

    type = BackendType.OLLAMA
    management_scope = ManagementScope.FULL_LIFECYCLE

    async def provision(self, spec: ProvisionSpec) -> list[NodeInfo]:
        return []

    async def deprovision(self, node_id: str) -> None:
        return None

    async def health(self) -> FleetHealth:
        return FleetHealth(backend_id=1, healthy=True, node_count=0, detail={})

    async def list_nodes(self) -> list[NodeInfo]:
        return []

    async def place_model(self, model: str, constraints: dict) -> ModelPlacement:
        return ModelPlacement(model=model, node_id="n1", status="placed")

    async def endpoints_for(self, model: str) -> list[Endpoint]:
        return []


def test_complete_subclass_instantiates() -> None:
    """A subclass implementing every abstract method can be instantiated."""
    backend = _CompleteBackend()
    assert backend.type == BackendType.OLLAMA
    assert backend.management_scope == ManagementScope.FULL_LIFECYCLE


@pytest.mark.parametrize(
    "omit",
    ["provision", "deprovision", "health", "list_nodes", "place_model", "endpoints_for"],
)
def test_omitting_any_method_blocks_instantiation(omit: str) -> None:
    """Omitting any single abstract method raises TypeError at instantiation."""
    namespace = {
        name: getattr(_CompleteBackend, name)
        for name in (
            "provision",
            "deprovision",
            "health",
            "list_nodes",
            "place_model",
            "endpoints_for",
        )
        if name != omit
    }
    namespace["type"] = BackendType.OLLAMA
    namespace["management_scope"] = ManagementScope.FULL_LIFECYCLE
    incomplete = type("_IncompleteBackend", (InferenceFleetBackend,), namespace)
    with pytest.raises(TypeError):
        incomplete()  # type: ignore[abstract]


def test_backend_type_enum_values() -> None:
    """``BackendType`` has exactly the five spec-mandated members."""
    assert {member.value for member in BackendType} == {
        "ollama",
        "llamacpp",
        "exo",
        "vertex_ai",
        "bedrock",
    }


def test_management_scope_enum_values() -> None:
    """``ManagementScope`` has exactly the two spec-mandated members."""
    assert {member.value for member in ManagementScope} == {
        "register_and_route",
        "full_lifecycle",
    }


@pytest.mark.parametrize(
    "cls",
    [Endpoint, NodeInfo, ModelPlacement, ProvisionSpec, FleetHealth],
)
def test_value_types_are_slotted(cls: type) -> None:
    """Value types are ``@dataclass(slots=True)`` — no ``__dict__``, real ``__slots__``."""
    assert dataclasses.is_dataclass(cls)
    assert hasattr(cls, "__slots__")


def test_endpoint_round_trips_fields() -> None:
    """``Endpoint`` preserves all constructor fields."""
    ep = Endpoint(url="http://n1:11434", node_id="n1", loaded_models=["gemma4:e2b"], healthy=True)
    assert not hasattr(ep, "__dict__")
    assert ep.url == "http://n1:11434"
    assert ep.node_id == "n1"
    assert ep.loaded_models == ["gemma4:e2b"]
    assert ep.healthy is True


def test_node_info_round_trips_fields() -> None:
    """``NodeInfo`` preserves all constructor fields, including optional ``node_uid``."""
    node = NodeInfo(
        node_id="n1",
        node_uid="uid-123",
        kind="k8s",
        loaded_models=["gemma4:e2b"],
        vram_total_mb=24576,
        vram_free_mb=8192,
        healthy=True,
    )
    assert not hasattr(node, "__dict__")
    assert node.node_uid == "uid-123"
    assert node.kind == "k8s"
    assert node.vram_free_mb == 8192


def test_model_placement_round_trips_fields() -> None:
    """``ModelPlacement`` preserves all constructor fields."""
    placement = ModelPlacement(model="gemma4:e2b", node_id="n1", status="placed")
    assert not hasattr(placement, "__dict__")
    assert placement.status == "placed"


def test_provision_spec_round_trips_fields() -> None:
    """``ProvisionSpec`` preserves all constructor fields."""
    spec = ProvisionSpec(
        name="pool-a", models=["gemma4:e2b"], mode="pool", constraints={"gpu": "a100"}
    )
    assert not hasattr(spec, "__dict__")
    assert spec.mode == "pool"
    assert spec.constraints == {"gpu": "a100"}


def test_fleet_health_round_trips_fields() -> None:
    """``FleetHealth`` preserves all constructor fields."""
    health = FleetHealth(
        backend_id=42, healthy=False, node_count=3, detail={"reason": "unreachable"}
    )
    assert not hasattr(health, "__dict__")
    assert health.backend_id == 42
    assert health.healthy is False
    assert health.node_count == 3
