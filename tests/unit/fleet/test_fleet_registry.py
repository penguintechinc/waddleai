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
from tests.conformance._fake_dal import FakeDAL


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


def _insert_fleet_backend(db: FakeDAL, **overrides: Any) -> int:
    """Insert a `fleet_backends` row with sensible defaults, returning its id."""
    fields = {
        "org_id": 1,
        "name": "backend",
        "type": "ollama",
        "management_scope": "full_lifecycle",
        "status": "active",
    }
    fields.update(overrides)
    return db.fleet_backends.insert(**fields)


async def test_build_backends_for_org_constructs_every_org_row() -> None:
    """Every non-disabled ``fleet_backends`` row for the org becomes a backend instance."""
    fleet_registry._REGISTRY[BackendType.OLLAMA] = _DummyBackend
    db = FakeDAL()
    _insert_fleet_backend(db, org_id=1, name="a")
    _insert_fleet_backend(db, org_id=1, name="b")
    _insert_fleet_backend(db, org_id=2, name="other-org")

    backends = await fleet_registry.build_backends_for_org(db, org_id=1)

    assert len(backends) == 2
    assert all(isinstance(b, _DummyBackend) for b in backends)


async def test_build_backends_for_org_excludes_disabled_rows() -> None:
    """A `status="disabled"` row is never constructed."""
    fleet_registry._REGISTRY[BackendType.OLLAMA] = _DummyBackend
    db = FakeDAL()
    _insert_fleet_backend(db, name="active", status="active")
    _insert_fleet_backend(db, name="off", status="disabled")

    backends = await fleet_registry.build_backends_for_org(db, org_id=1)

    assert len(backends) == 1


async def test_build_backends_for_org_skips_a_row_that_fails_to_construct(caplog) -> None:
    """One bad row (e.g. unregistered type) is logged and skipped, not fatal to the rest."""
    fleet_registry._REGISTRY[BackendType.OLLAMA] = _DummyBackend
    fleet_registry._REGISTRY.pop(BackendType.EXO, None)
    db = FakeDAL()
    _insert_fleet_backend(db, name="good", type="ollama")
    _insert_fleet_backend(db, name="bad", type="exo", management_scope="register_and_route")

    with patch.object(fleet_registry, "_ensure_imported"), caplog.at_level("WARNING"):
        backends = await fleet_registry.build_backends_for_org(db, org_id=1)

    assert len(backends) == 1
    assert isinstance(backends[0], _DummyBackend)
    assert "skipping org=1" in caplog.text


async def test_build_backends_for_org_no_rows_returns_empty_list() -> None:
    """An org with no registered backends returns an empty list, not an error."""
    backends = await fleet_registry.build_backends_for_org(FakeDAL(), org_id=999)
    assert backends == []


def test_ensure_imported_swallows_missing_optional_module(caplog) -> None:
    """A backend module that fails to import degrades gracefully, not a crash.

    All five ``_MODULE_MAP`` entries now resolve to real modules, so this
    points ``BackendType.BEDROCK`` at a deliberately nonexistent module for
    the duration of the test rather than relying on one of the real modules
    being absent.
    """
    fleet_registry._REGISTRY.pop(BackendType.BEDROCK, None)
    original_module = fleet_registry._MODULE_MAP[BackendType.BEDROCK]
    fleet_registry._MODULE_MAP[BackendType.BEDROCK] = "shared.fleet._does_not_exist"
    try:
        with caplog.at_level("WARNING"):
            fleet_registry._ensure_imported(BackendType.BEDROCK)
        # Import fails, is logged, and does not raise.
        assert BackendType.BEDROCK not in fleet_registry._REGISTRY
    finally:
        fleet_registry._MODULE_MAP[BackendType.BEDROCK] = original_module
