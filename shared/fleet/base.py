"""``InferenceFleetBackend`` ABC and its value types (spec §10.1).

Every inference fleet backend — Ollama, llama.cpp, EXO, Vertex AI, Bedrock —
implements this six-method interface so the placement engine and the
``fleet_backends`` API can treat them uniformly regardless of whether the
backend is a K8s DaemonSet, an external cluster, or a Professional-gated
cloud endpoint. Behavior lives entirely in the concrete backends; this
module is a pure contract.
"""

import abc
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class BackendType(StrEnum):
    """Inference fleet backend types recognized by the registry (§10.1)."""

    OLLAMA = "ollama"
    LLAMACPP = "llamacpp"
    EXO = "exo"
    VERTEX_AI = "vertex_ai"
    BEDROCK = "bedrock"


class ManagementScope(StrEnum):
    """How much lifecycle control WaddleAI has over a backend's nodes.

    ``register_and_route`` backends are only routed to and health-checked;
    WaddleAI never provisions or tears them down (e.g. EXO, cloud endpoints
    an org already manages). ``full_lifecycle`` backends are provisioned,
    scaled, and idle-torn-down by WaddleAI itself.
    """

    REGISTER_AND_ROUTE = "register_and_route"
    FULL_LIFECYCLE = "full_lifecycle"


@dataclass(slots=True)
class Endpoint:
    """A single routable inference endpoint on a fleet node.

    Returned by ``endpoints_for(model)`` for placement-aware dispatch — the
    router picks among these for a given model request.
    """

    url: str
    node_id: str
    loaded_models: list[str]
    healthy: bool


@dataclass(slots=True)
class NodeInfo:
    """A physical, virtual, or cloud node participating in the fleet.

    ``node_uid`` is the Kubernetes node UID for in-cluster nodes, the
    registered endpoint identifier for external nodes, or ``None`` when the
    backend cannot resolve a stable identifier.
    """

    node_id: str
    node_uid: str | None
    kind: str  # "k8s" | "external" | "cloud"
    loaded_models: list[str]
    vram_total_mb: int
    vram_free_mb: int
    healthy: bool


@dataclass(slots=True)
class ModelPlacement:
    """Result of placing (or attempting to place) a model on a node."""

    model: str
    node_id: str
    status: str  # "placed" | "pulling" | "denied"


@dataclass(slots=True)
class ProvisionSpec:
    """Desired-state spec for provisioning fleet capacity."""

    name: str
    models: list[str]
    mode: str
    constraints: dict[str, Any]


@dataclass(slots=True)
class FleetHealth:
    """Aggregate health snapshot for a single ``fleet_backends`` row."""

    backend_id: int
    healthy: bool
    node_count: int
    detail: dict[str, Any]


class InferenceFleetBackend(abc.ABC):
    """Pluggable interface every inference fleet backend implements.

    Concrete subclasses set the ``type`` and ``management_scope`` class (or
    instance) attributes and implement all six abstract methods; the
    registry factory (``shared.fleet.registry``) is the only place callers
    should construct one. ``fleet_backend_id`` is applied post-construction
    by the registry from the owning ``fleet_backends`` row; it defaults to
    ``0`` for legacy call sites that construct a backend directly instead of
    through the registry.
    """

    type: BackendType
    fleet_backend_id: int = 0
    management_scope: ManagementScope

    @abc.abstractmethod
    async def provision(self, spec: ProvisionSpec) -> list[NodeInfo]:
        """Bring up fleet capacity matching ``spec`` and return the resulting nodes."""
        raise NotImplementedError

    @abc.abstractmethod
    async def deprovision(self, node_id: str) -> None:
        """Tear down the given node. No-op if it no longer exists."""
        raise NotImplementedError

    @abc.abstractmethod
    async def health(self) -> FleetHealth:
        """Return an aggregate health snapshot for this backend."""
        raise NotImplementedError

    @abc.abstractmethod
    async def list_nodes(self) -> list[NodeInfo]:
        """Return every node currently known to this backend."""
        raise NotImplementedError

    @abc.abstractmethod
    async def place_model(self, model: str, constraints: dict[str, Any]) -> ModelPlacement:
        """Place ``model`` on a suitable node, pulling it if necessary."""
        raise NotImplementedError

    @abc.abstractmethod
    async def endpoints_for(self, model: str) -> list[Endpoint]:
        """Return routable endpoints that currently have ``model`` loaded."""
        raise NotImplementedError
