"""Tests for the fleet backend registry/factory (shared.fleet.registry)."""

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from shared.fleet import registry as fleet_registry
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


@dataclass(slots=True)
class _Row:
    """Minimal stand-in for a ``fleet_backends`` DB row."""

    id: int
    type: str
    management_scope: str
    config: dict[str, Any] | None = field(default_factory=dict)
    credentials_ref: str | None = None


class _DummyBackend(InferenceFleetBackend):
    """A trivial backend used to exercise the registry without real infra."""

    type = BackendType.OLLAMA

    def __init__(self, db: Any, config: dict[str, Any], credentials: str | None) -> None:
        self.db = db
        self.config = config
        self.credentials = credentials

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


@pytest.fixture(autouse=True)
def _clean_registry():
    """Snapshot/restore ``_REGISTRY`` so tests don't leak dummy classes."""
    original = dict(fleet_registry._REGISTRY)
    yield
    fleet_registry._REGISTRY.clear()
    fleet_registry._REGISTRY.update(original)


def test_build_backend_returns_registered_class() -> None:
    """``build_backend`` returns an instance of the class registered for ``type``."""
    fleet_registry._REGISTRY[BackendType.OLLAMA] = _DummyBackend
    row = _Row(id=1, type="ollama", management_scope="full_lifecycle")

    backend = fleet_registry.build_backend(db="fake-db", row=row)

    assert isinstance(backend, _DummyBackend)
    assert backend.db == "fake-db"


def test_build_backend_resolves_config() -> None:
    """The row's ``config`` dict is passed through unchanged."""
    fleet_registry._REGISTRY[BackendType.OLLAMA] = _DummyBackend
    row = _Row(id=1, type="ollama", management_scope="full_lifecycle", config={"pool": "gpu-a"})

    backend = fleet_registry.build_backend(db=None, row=row)

    assert backend.config == {"pool": "gpu-a"}


def test_build_backend_decrypts_credentials_and_never_logs_plaintext(caplog) -> None:
    """``credentials_ref`` is decrypted, passed to the backend, and logged masked only."""
    fleet_registry._REGISTRY[BackendType.OLLAMA] = _DummyBackend
    row = _Row(
        id=1,
        type="ollama",
        management_scope="full_lifecycle",
        credentials_ref="enc:doesnotmatter",
    )

    with patch.object(fleet_registry, "decrypt_credential", return_value="super-secret-token"):
        with caplog.at_level("DEBUG"):
            backend = fleet_registry.build_backend(db=None, row=row)

    assert backend.credentials == "super-secret-token"
    assert "super-secret-token" not in caplog.text
    assert "*" in caplog.text  # masked form logged instead


def test_build_backend_unknown_type_raises_value_error() -> None:
    """A ``type`` string outside ``BackendType`` raises ``ValueError``."""
    row = _Row(id=1, type="not-a-real-backend", management_scope="full_lifecycle")

    with pytest.raises(ValueError, match="Unknown fleet backend type"):
        fleet_registry.build_backend(db=None, row=row)


def test_build_backend_unregistered_type_raises_value_error() -> None:
    """A valid ``BackendType`` with no registered class raises ``ValueError``."""
    fleet_registry._REGISTRY.pop(BackendType.EXO, None)
    row = _Row(id=1, type="exo", management_scope="register_and_route")

    with patch.object(fleet_registry, "_ensure_imported"):
        with pytest.raises(ValueError, match="No backend implementation registered"):
            fleet_registry.build_backend(db=None, row=row)


def test_build_backend_applies_management_scope_from_row() -> None:
    """The row's ``management_scope`` lands on the constructed instance."""
    fleet_registry._REGISTRY[BackendType.OLLAMA] = _DummyBackend
    row = _Row(id=1, type="ollama", management_scope="register_and_route")

    backend = fleet_registry.build_backend(db=None, row=row)

    assert backend.management_scope == ManagementScope.REGISTER_AND_ROUTE


def test_build_backend_applies_fleet_backend_id_from_row() -> None:
    """The row's ``id`` lands on ``backend.fleet_backend_id`` for health() to report."""
    fleet_registry._REGISTRY[BackendType.OLLAMA] = _DummyBackend
    row = _Row(id=42, type="ollama", management_scope="full_lifecycle")

    backend = fleet_registry.build_backend(db=None, row=row)

    assert backend.fleet_backend_id == 42


def test_register_decorator_populates_registry() -> None:
    """The ``@register`` decorator stores the class under its BackendType."""

    @fleet_registry.register(BackendType.EXO)
    class _ExoStub(_DummyBackend):
        type = BackendType.EXO

    assert fleet_registry._REGISTRY[BackendType.EXO] is _ExoStub


def test_ensure_imported_swallows_missing_optional_module(caplog) -> None:
    """A cloud backend module that fails to import degrades gracefully, not a crash."""
    fleet_registry._REGISTRY.pop(BackendType.BEDROCK, None)
    with caplog.at_level("WARNING"):
        fleet_registry._ensure_imported(BackendType.BEDROCK)
    # shared.fleet.bedrock does not exist yet on this branch — import fails,
    # is logged, and does not raise.
    assert BackendType.BEDROCK not in fleet_registry._REGISTRY
