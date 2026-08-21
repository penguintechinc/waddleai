"""Unit tests for ``OllamaDeploymentManager`` (services/management/app/services/ollama_manager.py).

Exercises deployment CRUD, manifest generation, Docker-orchestrated container
lifecycle, model pull/list/remove over HTTP, health checks, and the
``InferenceFleetBackend`` async adapter methods -- all against an in-memory
fake DAL plus fake Docker/httpx clients. No real Docker socket, no network.
"""

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import yaml

from services.management.app.services.ollama_manager import (
    DeploymentMode,
    OllamaDeploymentConfig,
    OllamaDeploymentManager,
    OllamaModel,
    PullStatus,
)
from shared.fleet.base import Endpoint, FleetHealth, ModelPlacement, NodeInfo, ProvisionSpec

# ---------------------------------------------------------------------------
# Fake in-memory PyDAL-style DB -- real insert/select/update/delete/count
# semantics over plain dicts, so tests exercise the manager's actual query
# composition instead of stubbing every call site with a MagicMock.
# ---------------------------------------------------------------------------


class FakeRow:
    """Minimal PyDAL ``Row`` stand-in.

    Plain attribute storage; a field that was never inserted raises
    ``AttributeError`` on access, mirroring real ``Row`` semantics and the
    source's own ``hasattr(deployment, "namespace")`` checks.
    """

    def __init__(self, **fields: Any) -> None:
        """Store every keyword argument as a plain instance attribute."""
        self.__dict__.update(fields)


class _Field:
    """A queryable column reference: ``table.field`` returns one of these."""

    def __init__(self, table_name: str, field_name: str) -> None:
        self.table_name = table_name
        self.field_name = field_name

    def __eq__(self, other: object) -> "_Query":  # type: ignore[override]
        return _Query(self.table_name, lambda row: getattr(row, self.field_name, None) == other)

    def __gt__(self, other: object) -> "_Query":
        return _Query(
            self.table_name, lambda row: (getattr(row, self.field_name, None) or 0) > other
        )

    def belongs(self, values: list) -> "_Query":
        """Return a predicate matching rows whose field is one of ``values``."""
        return _Query(self.table_name, lambda row: getattr(row, self.field_name, None) in values)


class _Query:
    """A composed predicate over a single table, combinable with ``&``."""

    def __init__(self, table_name: str, predicate) -> None:
        self.table_name = table_name
        self.predicate = predicate

    def __and__(self, other: "_Query") -> "_Query":
        return _Query(self.table_name, lambda row: self.predicate(row) and other.predicate(row))


class _Rows(list):
    """A ``.select()`` result: list of ``FakeRow`` with PyDAL's ``.first()`` helper."""

    def first(self) -> FakeRow | None:
        """Return the first matching row, or ``None`` if the result set is empty."""
        return self[0] if self else None


class _Table:
    """``db.<table_name>``: field access builds ``_Field``s; ``.insert()`` writes a row."""

    def __init__(self, db: "FakeDB", name: str) -> None:
        self._db = db
        self._name = name

    def __getattr__(self, name: str) -> _Field:
        if name.startswith("_"):
            raise AttributeError(name)
        return _Field(self._name, name)

    def insert(self, **fields: Any) -> int:
        """Insert a new row and return its auto-incremented id."""
        return self._db._insert(self._name, fields)


class _Set:
    """``db(query)`` result: supports ``.select()``, ``.update()``, ``.delete()``, ``.count()``."""

    def __init__(self, db: "FakeDB", query: _Query) -> None:
        self._db = db
        self._query = query

    def _matches(self) -> list[FakeRow]:
        table = self._db._tables.get(self._query.table_name, {})
        return [row for row in table.values() if self._query.predicate(row)]

    def select(self) -> _Rows:
        """Return matching rows as a PyDAL-style ``Rows`` list."""
        return _Rows(self._matches())

    def update(self, **fields: Any) -> int:
        """Update matching rows in place and return the count updated."""
        rows = self._matches()
        for row in rows:
            row.__dict__.update(fields)
        return len(rows)

    def delete(self) -> int:
        """Delete matching rows and return the count deleted."""
        table = self._db._tables.get(self._query.table_name, {})
        rows = self._matches()
        for row in rows:
            del table[row.id]
        return len(rows)

    def count(self) -> int:
        """Return the count of matching rows."""
        return len(self._matches())


class FakeDB:
    """In-memory stand-in for the PyDAL ``db`` handle used throughout ``ollama_manager``."""

    def __init__(self) -> None:
        """Start with no tables and a zeroed commit counter."""
        self._tables: dict[str, dict[int, FakeRow]] = {}
        self._next_id: dict[str, int] = {}
        self.commit_count = 0

    def __getattr__(self, name: str) -> _Table:
        """Return a ``_Table`` for ``db.<table_name>`` access, auto-creating the table."""
        if name.startswith("_"):
            raise AttributeError(name)
        self._tables.setdefault(name, {})
        return _Table(self, name)

    def __call__(self, query: _Query) -> _Set:
        """Support ``db(query)`` -> a queryable ``_Set``, like real PyDAL."""
        return _Set(self, query)

    def commit(self) -> None:
        """Record a commit call (no-op storage-wise; state is already applied)."""
        self.commit_count += 1

    def _insert(self, table_name: str, fields: dict[str, Any]) -> int:
        table = self._tables.setdefault(table_name, {})
        next_id = self._next_id.get(table_name, 0) + 1
        self._next_id[table_name] = next_id
        table[next_id] = FakeRow(id=next_id, **fields)
        return next_id


# ---------------------------------------------------------------------------
# Fake Docker client -- stands in for ``docker.from_env()``'s ``.containers``.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FakeContainer:
    """A single in-memory container: tracks status transitions and log output."""

    name: str
    status: str = "created"
    log_output: bytes = b"line1\nline2\n"

    def start(self) -> None:
        """Transition to running, as the real docker-py container does."""
        self.status = "running"

    def stop(self) -> None:
        """Transition to stopped."""
        self.status = "stopped"

    def logs(self, tail: int = 100) -> bytes:
        """Return the canned log bytes (mirrors docker-py's ``.logs()``)."""
        return self.log_output


class FakeContainers:
    """``docker_client.containers``: ``.get()``/``.run()`` over an in-memory dict."""

    def __init__(self) -> None:
        """Start with an empty container store and no recorded run() calls."""
        self.store: dict[str, FakeContainer] = {}
        self.run_calls: list[dict[str, Any]] = []

    def get(self, name: str) -> FakeContainer:
        """Return the named container or raise if it doesn't exist (like docker-py's NotFound)."""
        if name not in self.store:
            raise LookupError(f"no such container: {name}")
        return self.store[name]

    def run(self, **kwargs: Any) -> FakeContainer:
        """Create and start a new container, recording the call for assertions."""
        self.run_calls.append(kwargs)
        container = FakeContainer(name=kwargs["name"], status="running")
        self.store[kwargs["name"]] = container
        return container


@dataclass(slots=True)
class FakeDockerClient:
    """Stand-in for the object returned by ``docker.from_env()``."""

    containers: FakeContainers = field(default_factory=FakeContainers)


def _http_client_mock(*, get=None, post=None, delete=None, side_effect=None) -> MagicMock:
    """Build a MagicMock standing in for ``with httpx.Client(...) as client:``.

    Matches the mocking idiom already used in test_ollama_routes.py: patch
    ``ollama_manager.httpx.Client`` to return this, and the context value
    (``__enter__.return_value``) exposes ``.get``/``.post``/``.delete``.
    """
    client = MagicMock()
    ctx = client.__enter__.return_value
    if side_effect is not None:
        ctx.get.side_effect = side_effect
        ctx.post.side_effect = side_effect
        ctx.delete.side_effect = side_effect
    if get is not None:
        ctx.get.return_value = get
    if post is not None:
        ctx.post.return_value = post
    if delete is not None:
        ctx.delete.return_value = delete
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> FakeDB:
    """A fresh in-memory fake DB per test."""
    return FakeDB()


@pytest.fixture
def manager(db: FakeDB) -> OllamaDeploymentManager:
    """A manager in BOTH mode (manual + orchestrated) wired to the fake DB."""
    return OllamaDeploymentManager(db, mode=DeploymentMode.BOTH)


@pytest.fixture
def manual_manager(db: FakeDB) -> OllamaDeploymentManager:
    """A manager in MANUAL-only mode -- orchestrated operations must refuse."""
    return OllamaDeploymentManager(db, mode=DeploymentMode.MANUAL)


def _seed_deployment(db: FakeDB, **overrides: Any) -> int:
    """Insert a representative ``ollama_deployments`` row and return its id."""
    fields = {
        "name": "test-ollama",
        "endpoint_url": "http://localhost:11434",
        "deployment_type": "docker",
        "docker_compose_config": {
            "image": "ollama/ollama:latest",
            "environment": {"OLLAMA_HOST": "0.0.0.0"},  # nosec B104 # noqa: S104 -- container listen address inside its own pod network namespace, mirrors source's own _generate_docker_config output
            "volumes": ["ollama-test-ollama-data:/root/.ollama", "/host/models:/models"],
        },
        "gpu_config": {"gpu_count": 0, "gpu_ids": []},
        "resource_limits": {"cpu_limit": "4", "memory_limit": "8g"},
        "status": "pending",
        "health_status": "unknown",
        "auto_start": True,
        "pool_mode": False,
    }
    fields.update(overrides)
    return db.ollama_deployments.insert(**fields)


def _seed_model(db: FakeDB, deployment_id: int, model_name: str, **overrides: Any) -> int:
    """Insert a representative ``ollama_models`` row and return its id."""
    fields = {"deployment_id": deployment_id, "model_name": model_name, "model_tag": "latest"}
    fields.update(overrides)
    return db.ollama_models.insert(**fields)


@pytest.fixture
def sample_config() -> OllamaDeploymentConfig:
    """A representative deployment config for create/update tests."""
    return OllamaDeploymentConfig(name="test-ollama", gpu_count=0)


# ---------------------------------------------------------------------------
# create_deployment / update_deployment / delete_deployment
# ---------------------------------------------------------------------------


def test_create_deployment_success_inserts_row(manager, db, sample_config):
    """A new deployment name inserts a row and returns its generated id."""
    result = manager.create_deployment(sample_config)

    assert result["success"] is True
    assert result["deployment_id"] == 1
    row = db._tables["ollama_deployments"][1]
    assert row.status == "pending"
    assert db.commit_count == 1


def test_create_deployment_duplicate_name_rejected(manager, db, sample_config):
    """A second deployment with the same name is rejected without inserting."""
    manager.create_deployment(sample_config)
    result = manager.create_deployment(sample_config)

    assert result["success"] is False
    assert "already exists" in result["error"]
    assert len(db._tables["ollama_deployments"]) == 1


def test_create_deployment_with_gpu_sets_deploy_reservations(manager, sample_config):
    """GPU count > 0 threads through to the generated docker-compose reservation block."""
    sample_config.gpu_count = 2
    sample_config.gpu_ids = ["0", "1"]
    generated = manager._generate_docker_config(sample_config)

    assert generated["deploy"]["resources"]["reservations"]["devices"][0]["count"] == 2


def test_generate_docker_config_skips_limits_block_when_unset(manager, sample_config):
    """Empty cpu/memory limits skip the resource-limits branch entirely."""
    sample_config.cpu_limit = ""
    sample_config.memory_limit = ""
    generated = manager._generate_docker_config(sample_config)

    assert "deploy" not in generated


def test_generate_docker_config_cpu_only_omits_memory_key(manager, sample_config):
    """cpu_limit set / memory_limit empty: only the 'cpus' limit key is written."""
    sample_config.cpu_limit = "4"
    sample_config.memory_limit = ""
    generated = manager._generate_docker_config(sample_config)

    limits = generated["deploy"]["resources"]["limits"]
    assert limits == {"cpus": "4"}


def test_generate_docker_config_memory_only_omits_cpu_key(manager, sample_config):
    """memory_limit set / cpu_limit empty: only the 'memory' limit key is written."""
    sample_config.cpu_limit = ""
    sample_config.memory_limit = "8g"
    generated = manager._generate_docker_config(sample_config)

    limits = generated["deploy"]["resources"]["limits"]
    assert limits == {"memory": "8g"}


def test_update_deployment_not_found(manager, sample_config):
    """Updating a nonexistent deployment id returns a not-found error."""
    result = manager.update_deployment(999, sample_config)

    assert result == {"success": False, "error": "Deployment not found"}


def test_update_deployment_success(manager, db, sample_config):
    """Updating an existing deployment persists the new config fields."""
    dep_id = _seed_deployment(db)
    sample_config.endpoint_url = "http://newhost:11434"

    result = manager.update_deployment(dep_id, sample_config)

    assert result["success"] is True
    row = db._tables["ollama_deployments"][dep_id]
    assert row.endpoint_url == "http://newhost:11434"


def test_delete_deployment_not_found(manager):
    """Deleting a nonexistent deployment id returns a not-found error."""
    assert manager.delete_deployment(999) == {"success": False, "error": "Deployment not found"}


def test_delete_deployment_removes_deployment_and_models(manager, db):
    """Deleting a stopped deployment removes it and its tracked models, no stop attempted."""
    dep_id = _seed_deployment(db, status="stopped")
    _seed_model(db, dep_id, "llama3.2")

    result = manager.delete_deployment(dep_id)

    assert result["success"] is True
    assert dep_id not in db._tables["ollama_deployments"]
    assert db._tables["ollama_models"] == {}


def test_delete_deployment_stops_running_container_first(manager, db):
    """Deleting a running deployment (orchestrated mode) stops its container first."""
    dep_id = _seed_deployment(db, status="running")
    manager._docker_client = FakeDockerClient()
    manager._docker_client.containers.store["waddleai-ollama-test-ollama"] = FakeContainer(
        name="waddleai-ollama-test-ollama", status="running"
    )

    result = manager.delete_deployment(dep_id)

    assert result["success"] is True
    assert manager._docker_client.containers.store["waddleai-ollama-test-ollama"].status == (
        "stopped"
    )


# ---------------------------------------------------------------------------
# generate_docker_compose / generate_k8s_manifest
# ---------------------------------------------------------------------------


def test_generate_docker_compose_not_found(manager):
    """Missing deployment returns an empty string, not an exception."""
    assert manager.generate_docker_compose(999) == ""


def test_generate_docker_compose_success(manager, db):
    """Generated compose YAML nests the service under the deployment's name."""
    dep_id = _seed_deployment(db)
    compose_yaml = manager.generate_docker_compose(dep_id)
    parsed = yaml.safe_load(compose_yaml)

    assert "ollama-test-ollama" in parsed["services"]


def test_generate_k8s_manifest_not_found(manager):
    """Missing deployment returns an empty string."""
    assert manager.generate_k8s_manifest(999) == ""


def test_generate_k8s_manifest_sets_resource_limits_and_gpu(manager, db):
    """Manifest includes cpu/memory limits and the nvidia.com/gpu limit when gpu_count > 0."""
    dep_id = _seed_deployment(
        db,
        gpu_config={"gpu_count": 2, "gpu_ids": ["0", "1"]},
        resource_limits={"cpu_limit": "4", "memory_limit": "16Gi"},
    )
    manifest_yaml = manager.generate_k8s_manifest(dep_id)
    docs = list(yaml.safe_load_all(manifest_yaml))
    deployment_doc = next(d for d in docs if d["kind"] == "Deployment")
    limits = deployment_doc["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]

    assert limits["cpu"] == "4"
    assert limits["memory"] == "16Gi"
    assert limits["nvidia.com/gpu"] == "2"
    kinds = {d["kind"] for d in docs}
    assert kinds == {"PersistentVolumeClaim", "Deployment", "Service"}


def test_generate_k8s_manifest_without_gpu_omits_gpu_limit(manager, db):
    """No GPU configured means no nvidia.com/gpu key in resource limits."""
    dep_id = _seed_deployment(db, gpu_config={}, resource_limits={})
    manifest_yaml = manager.generate_k8s_manifest(dep_id)
    docs = list(yaml.safe_load_all(manifest_yaml))
    deployment_doc = next(d for d in docs if d["kind"] == "Deployment")
    limits = deployment_doc["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]

    assert "nvidia.com/gpu" not in limits


# ---------------------------------------------------------------------------
# generate_metallb_service / generate_model_specific_metallb_services / export_metallb_config
# ---------------------------------------------------------------------------


def test_generate_metallb_service_not_found(manager):
    """Missing deployment returns an empty string."""
    assert manager.generate_metallb_service(999) == ""


def test_generate_metallb_service_lists_model_names(manager, db):
    """Service annotation carries a comma-joined list of the deployment's model names."""
    dep_id = _seed_deployment(db)
    _seed_model(db, dep_id, "llama3.2")
    _seed_model(db, dep_id, "mistral")

    service_yaml = manager.generate_metallb_service(dep_id)
    parsed = yaml.safe_load(service_yaml)

    annotations = parsed["metadata"]["annotations"]
    assert set(annotations["waddleai.io/models"].split(",")) == {"llama3.2", "mistral"}


def test_generate_model_specific_metallb_services_not_found(manager):
    """Missing deployment returns an empty string."""
    assert manager.generate_model_specific_metallb_services(999) == ""


def test_generate_model_specific_metallb_services_empty_models(manager, db):
    """A deployment with no tracked models returns an empty string."""
    dep_id = _seed_deployment(db)
    assert manager.generate_model_specific_metallb_services(dep_id) == ""


def test_generate_model_specific_metallb_services_defaults_tag_to_latest(manager, db):
    """A model row with no model_tag falls back to 'latest' in the annotation."""
    dep_id = _seed_deployment(db)
    _seed_model(db, dep_id, "llama3.2", model_tag=None)

    services_yaml = manager.generate_model_specific_metallb_services(dep_id)
    docs = list(yaml.safe_load_all(services_yaml))

    assert docs[0]["metadata"]["annotations"]["waddleai.io/model-tag"] == "latest"


def test_export_metallb_config_empty_when_no_deployments(manager):
    """No running/pending deployments produces an empty string."""
    assert manager.export_metallb_config() == ""


def test_export_metallb_config_aggregates_running_and_pending(manager, db):
    """Services from both running and pending deployments are concatenated."""
    running_id = _seed_deployment(db, name="running-dep", status="running")
    pending_id = _seed_deployment(db, name="pending-dep", status="pending")
    _seed_deployment(db, name="stopped-dep", status="stopped")
    _seed_model(db, running_id, "llama3.2")
    _seed_model(db, pending_id, "mistral")

    config_yaml = manager.export_metallb_config()
    docs = list(yaml.safe_load_all(config_yaml))
    names = {d["metadata"]["name"] for d in docs}

    assert any("running-dep" in n for n in names)
    assert any("pending-dep" in n for n in names)
    assert not any("stopped-dep" in n for n in names)


def test_export_metallb_config_skips_deployments_with_no_models(manager, db):
    """A running deployment with zero tracked models contributes no service docs."""
    _seed_deployment(db, name="modelless-dep", status="running")

    assert manager.export_metallb_config() == ""


# ---------------------------------------------------------------------------
# generate_daemonset_manifest / generate_pool_manifest
# ---------------------------------------------------------------------------


def test_generate_daemonset_manifest_not_found(manager):
    """Missing deployment returns an empty string."""
    assert manager.generate_daemonset_manifest(999) == ""


def test_generate_daemonset_manifest_uses_defaults_and_default_namespace(manager, db):
    """No node_selector/tolerations/namespace on the row falls back to defaults."""
    dep_id = _seed_deployment(db, gpu_config={})
    manifest_yaml = manager.generate_daemonset_manifest(dep_id)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")

    assert ds["metadata"]["namespace"] == "waddleai"
    assert ds["spec"]["template"]["spec"]["nodeSelector"] == {"gpu": "true"}
    assert ds["spec"]["template"]["spec"]["tolerations"][0]["key"] == "nvidia.com/gpu"


def test_generate_daemonset_manifest_custom_namespace_and_storage_class(manager, db):
    """Explicit namespace and storage_class both flow into the manifest."""
    dep_id = _seed_deployment(
        db,
        namespace="custom-ns",
        gpu_config={"node_selector": {"waddleai/gpu-tier": "a100"}, "storage_class": "fast-ssd"},
    )
    manifest_yaml = manager.generate_daemonset_manifest(dep_id)
    docs = list(yaml.safe_load_all(manifest_yaml))
    pvc = next(d for d in docs if d["kind"] == "PersistentVolumeClaim")
    ds = next(d for d in docs if d["kind"] == "DaemonSet")

    assert pvc["metadata"]["namespace"] == "custom-ns"
    assert pvc["spec"]["storageClassName"] == "fast-ssd"
    assert ds["spec"]["template"]["spec"]["nodeSelector"] == {"waddleai/gpu-tier": "a100"}


def test_generate_pool_manifest_not_found(manager):
    """Missing deployment returns an empty string."""
    assert manager.generate_pool_manifest(999) == ""


def test_generate_pool_manifest_uses_replicas_and_gpu_count_fallback(manager, db):
    """Pool mode Deployment uses gpu_config's replicas and falls back gpu_count -> count -> 1."""
    dep_id = _seed_deployment(db, pool_mode=True, gpu_config={"replicas": 3, "count": 4})
    manifest_yaml = manager.generate_pool_manifest(dep_id)
    docs = list(yaml.safe_load_all(manifest_yaml))
    deployment_doc = next(d for d in docs if d["kind"] == "Deployment")
    container = deployment_doc["spec"]["template"]["spec"]["containers"][0]

    assert deployment_doc["spec"]["replicas"] == 3
    assert container["resources"]["requests"]["nvidia.com/gpu"] == "4"


def test_ollama_pvc_and_container_defaults_gpu_count_to_one(manager, db):
    """With neither gpu_count nor count set, the shared container defaults to 1 GPU."""
    dep_id = _seed_deployment(db, gpu_config={})
    deployment = db(db.ollama_deployments.id == dep_id).select().first()

    _pvc, container = manager._ollama_pvc_and_container(deployment)

    assert container["resources"]["requests"]["nvidia.com/gpu"] == "1"


# ---------------------------------------------------------------------------
# start_deployment / stop_deployment / restart_deployment / get_logs
# ---------------------------------------------------------------------------


def test_start_deployment_manual_mode_rejected(manual_manager, db):
    """MANUAL mode refuses orchestrated start without checking the deployment."""
    result = manual_manager.start_deployment(1)
    assert result == {"success": False, "error": "Orchestrated mode not enabled"}


def test_start_deployment_not_found(manager):
    """Missing deployment returns a not-found error."""
    result = manager.start_deployment(999)
    assert result == {"success": False, "error": "Deployment not found"}


def test_start_deployment_docker_unavailable(manager, db, monkeypatch):
    """No Docker client available surfaces as a clean error, not a crash."""
    dep_id = _seed_deployment(db)
    monkeypatch.setattr(type(manager), "docker_client", property(lambda self: None))

    result = manager.start_deployment(dep_id)

    assert result == {"success": False, "error": "Docker client not available"}


def test_start_deployment_existing_stopped_container_is_started(manager, db):
    """An existing non-running container is started in place, not recreated."""
    dep_id = _seed_deployment(db)
    manager._docker_client = FakeDockerClient()
    manager._docker_client.containers.store["waddleai-ollama-test-ollama"] = FakeContainer(
        name="waddleai-ollama-test-ollama", status="exited"
    )

    result = manager.start_deployment(dep_id)

    assert result["success"] is True
    assert manager._docker_client.containers.store["waddleai-ollama-test-ollama"].status == (
        "running"
    )
    assert manager._docker_client.containers.run_calls == []
    row = db._tables["ollama_deployments"][dep_id]
    assert row.status == "running"
    assert row.health_status == "healthy"


def test_start_deployment_already_running_container_is_left_alone(manager, db):
    """An existing already-running container is neither restarted nor recreated."""
    dep_id = _seed_deployment(db)
    manager._docker_client = FakeDockerClient()
    manager._docker_client.containers.store["waddleai-ollama-test-ollama"] = FakeContainer(
        name="waddleai-ollama-test-ollama", status="running"
    )

    result = manager.start_deployment(dep_id)

    assert result["success"] is True
    assert manager._docker_client.containers.run_calls == []
    row = db._tables["ollama_deployments"][dep_id]
    assert row.status == "running"


def test_start_deployment_missing_container_is_created_with_volumes(manager, db):
    """No existing container: a new one is run with parsed volume bind mounts."""
    dep_id = _seed_deployment(db)
    manager._docker_client = FakeDockerClient()

    result = manager.start_deployment(dep_id)

    assert result["success"] is True
    assert len(manager._docker_client.containers.run_calls) == 1
    run_kwargs = manager._docker_client.containers.run_calls[0]
    assert run_kwargs["volumes"]["ollama-test-ollama-data"] == {
        "bind": "/root/.ollama",
        "mode": "rw",
    }
    assert run_kwargs["volumes"]["/host/models"] == {"bind": "/models", "mode": "rw"}


def test_start_deployment_no_volumes_configured_skips_volume_block(manager, db):
    """An empty volumes list skips the volume-parsing branch; run() gets no 'volumes' kwarg."""
    dep_id = _seed_deployment(
        db,
        docker_compose_config={
            "image": "ollama/ollama:latest",
            "environment": {},
            "volumes": [],
        },
    )
    manager._docker_client = FakeDockerClient()

    result = manager.start_deployment(dep_id)

    assert result["success"] is True
    run_kwargs = manager._docker_client.containers.run_calls[0]
    assert "volumes" not in run_kwargs


def test_start_deployment_volume_entry_without_colon_is_skipped(manager, db):
    """A volume entry with no ':' is skipped, leaving only well-formed entries bound."""
    dep_id = _seed_deployment(
        db,
        docker_compose_config={
            "image": "ollama/ollama:latest",
            "environment": {},
            "volumes": ["not-a-bind-mount", "ollama-test-ollama-data:/root/.ollama"],
        },
    )
    manager._docker_client = FakeDockerClient()

    result = manager.start_deployment(dep_id)

    assert result["success"] is True
    run_kwargs = manager._docker_client.containers.run_calls[0]
    assert run_kwargs["volumes"] == {
        "ollama-test-ollama-data": {"bind": "/root/.ollama", "mode": "rw"}
    }


def test_start_deployment_docker_error_marks_deployment_errored(manager, db):
    """A Docker API failure marks the deployment errored and returns the error string."""
    dep_id = _seed_deployment(db)
    docker_client = FakeDockerClient()

    def _boom(**kwargs):
        raise RuntimeError("docker daemon unreachable")

    docker_client.containers.run = _boom
    manager._docker_client = docker_client

    result = manager.start_deployment(dep_id)

    assert result == {"success": False, "error": "docker daemon unreachable"}
    row = db._tables["ollama_deployments"][dep_id]
    assert row.status == "error"
    assert row.health_status == "unhealthy"


def test_stop_deployment_manual_mode_rejected(manual_manager):
    """MANUAL mode refuses orchestrated stop."""
    assert manual_manager.stop_deployment(1) == {
        "success": False,
        "error": "Orchestrated mode not enabled",
    }


def test_stop_deployment_not_found(manager):
    """Missing deployment returns a not-found error."""
    assert manager.stop_deployment(999) == {"success": False, "error": "Deployment not found"}


def test_stop_deployment_docker_unavailable(manager, db, monkeypatch):
    """No Docker client available surfaces as a clean error."""
    dep_id = _seed_deployment(db)
    monkeypatch.setattr(type(manager), "docker_client", property(lambda self: None))

    assert manager.stop_deployment(dep_id) == {
        "success": False,
        "error": "Docker client not available",
    }


def test_stop_deployment_success(manager, db):
    """Stopping an existing container updates the deployment's DB status."""
    dep_id = _seed_deployment(db, status="running")
    manager._docker_client = FakeDockerClient()
    manager._docker_client.containers.store["waddleai-ollama-test-ollama"] = FakeContainer(
        name="waddleai-ollama-test-ollama", status="running"
    )

    result = manager.stop_deployment(dep_id)

    assert result["success"] is True
    row = db._tables["ollama_deployments"][dep_id]
    assert row.status == "stopped"
    assert row.health_status == "unknown"


def test_stop_deployment_missing_container_returns_error(manager, db):
    """Stopping a container that never existed surfaces the lookup error."""
    dep_id = _seed_deployment(db)
    manager._docker_client = FakeDockerClient()

    result = manager.stop_deployment(dep_id)

    assert result["success"] is False
    assert "no such container" in result["error"]


def test_restart_deployment_propagates_stop_failure(manager, db):
    """If stop fails, restart short-circuits and never attempts start."""
    dep_id = _seed_deployment(db)
    manager._docker_client = FakeDockerClient()  # container missing -> stop fails

    result = manager.restart_deployment(dep_id)

    assert result["success"] is False
    assert manager._docker_client.containers.run_calls == []


def test_restart_deployment_stops_then_starts(manager, db):
    """A successful stop is followed by a start call, ending in 'running'."""
    dep_id = _seed_deployment(db, status="running")
    manager._docker_client = FakeDockerClient()
    manager._docker_client.containers.store["waddleai-ollama-test-ollama"] = FakeContainer(
        name="waddleai-ollama-test-ollama", status="running"
    )

    result = manager.restart_deployment(dep_id)

    assert result["success"] is True
    row = db._tables["ollama_deployments"][dep_id]
    assert row.status == "running"


def test_get_logs_manual_mode_message(manual_manager):
    """MANUAL mode returns an explanatory message instead of raising."""
    assert manual_manager.get_logs(1) == "Orchestrated mode not enabled"


def test_get_logs_not_found(manager):
    """Missing deployment returns a not-found message."""
    assert manager.get_logs(999) == "Deployment not found"


def test_get_logs_docker_unavailable(manager, db, monkeypatch):
    """No Docker client returns a clean unavailable message."""
    dep_id = _seed_deployment(db)
    monkeypatch.setattr(type(manager), "docker_client", property(lambda self: None))

    assert manager.get_logs(dep_id) == "Docker client not available"


def test_get_logs_success_decodes_bytes(manager, db):
    """Container logs are decoded from bytes to a UTF-8 string."""
    dep_id = _seed_deployment(db)
    manager._docker_client = FakeDockerClient()
    manager._docker_client.containers.store["waddleai-ollama-test-ollama"] = FakeContainer(
        name="waddleai-ollama-test-ollama", log_output=b"hello ollama\n"
    )

    assert manager.get_logs(dep_id, lines=50) == "hello ollama\n"


def test_get_logs_missing_container_returns_error_string(manager, db):
    """Missing container surfaces the lookup failure as an 'Error: ...' string."""
    dep_id = _seed_deployment(db)
    manager._docker_client = FakeDockerClient()

    assert manager.get_logs(dep_id).startswith("Error:")


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


def test_health_check_not_found(manager):
    """Missing deployment returns not-found without touching HTTP."""
    assert manager.health_check(999) == {"healthy": False, "status": "not_found"}


def test_health_check_success_marks_healthy(manager, db):
    """A 200 response from /api/tags marks the deployment healthy and persists it."""
    dep_id = _seed_deployment(db)
    mock_client = _http_client_mock(get=MagicMock(status_code=200))

    with patch(
        "services.management.app.services.ollama_manager.httpx.Client", return_value=mock_client
    ):
        result = manager.health_check(dep_id)

    assert result["healthy"] is True
    assert result["status"] == "healthy"
    row = db._tables["ollama_deployments"][dep_id]
    assert row.health_status == "healthy"


def test_health_check_non_200_marks_unhealthy(manager, db):
    """A non-200 response marks the deployment unhealthy without raising."""
    dep_id = _seed_deployment(db)
    mock_client = _http_client_mock(get=MagicMock(status_code=503))

    with patch(
        "services.management.app.services.ollama_manager.httpx.Client", return_value=mock_client
    ):
        result = manager.health_check(dep_id)

    assert result["healthy"] is False
    assert result["status"] == "unhealthy"


def test_health_check_connection_refused_fails_gracefully(manager, db):
    """A connection-refused error is caught, reported, and persisted as unhealthy."""
    dep_id = _seed_deployment(db)
    mock_client = _http_client_mock(side_effect=httpx.ConnectError("Connection refused"))

    with patch(
        "services.management.app.services.ollama_manager.httpx.Client", return_value=mock_client
    ):
        result = manager.health_check(dep_id)

    assert result == {"healthy": False, "status": "error", "error": "Connection refused"}
    row = db._tables["ollama_deployments"][dep_id]
    assert row.health_status == "unhealthy"


# ---------------------------------------------------------------------------
# list_models
# ---------------------------------------------------------------------------


def test_list_models_not_found(manager):
    """Missing deployment returns an empty list."""
    assert manager.list_models(999) == []


def test_list_models_success_parses_response(manager, db):
    """A 200 /api/tags response is parsed into OllamaModel value objects."""
    dep_id = _seed_deployment(db)
    payload = MagicMock(status_code=200)
    payload.json.return_value = {
        "models": [
            {
                "name": "llama3.2",
                "size": 123,
                "digest": "sha256:abc",
                "modified_at": "2026-01-01",
                "details": {"family": "llama"},
            }
        ]
    }
    mock_client = _http_client_mock(get=payload)

    with patch(
        "services.management.app.services.ollama_manager.httpx.Client", return_value=mock_client
    ):
        models = manager.list_models(dep_id)

    assert models == [
        OllamaModel(
            name="llama3.2",
            size=123,
            digest="sha256:abc",
            modified_at="2026-01-01",
            details={"family": "llama"},
        )
    ]


def test_list_models_404_returns_empty_list(manager, db):
    """A 404 (model/endpoint not found) response yields an empty list, not an error."""
    dep_id = _seed_deployment(db)
    mock_client = _http_client_mock(get=MagicMock(status_code=404))

    with patch(
        "services.management.app.services.ollama_manager.httpx.Client", return_value=mock_client
    ):
        assert manager.list_models(dep_id) == []


def test_list_models_timeout_returns_empty_list(manager, db):
    """A timeout talking to the deployment is swallowed into an empty list."""
    dep_id = _seed_deployment(db)
    mock_client = _http_client_mock(side_effect=httpx.TimeoutException("timed out"))

    with patch(
        "services.management.app.services.ollama_manager.httpx.Client", return_value=mock_client
    ):
        assert manager.list_models(dep_id) == []


# ---------------------------------------------------------------------------
# pull_model
# ---------------------------------------------------------------------------


def test_pull_model_not_found(manager):
    """Missing deployment returns an error PullStatus without touching HTTP."""
    result = manager.pull_model(999, "llama3.2")
    assert result == PullStatus(model="llama3.2", status="error", error="Deployment not found")


def test_pull_model_success_inserts_new_model_row(manager, db):
    """A successful pull marks completion and tracks a new model row."""
    dep_id = _seed_deployment(db)
    mock_client = _http_client_mock(post=MagicMock(status_code=200))

    with patch(
        "services.management.app.services.ollama_manager.httpx.Client", return_value=mock_client
    ):
        result = manager.pull_model(dep_id, "llama3.2")

    assert result == PullStatus(
        model="llama3.2", status="completed", progress=100.0, completed=True
    )
    model_rows = list(db._tables["ollama_models"].values())
    assert model_rows[0].name == "llama3.2"
    row = db._tables["ollama_deployments"][dep_id]
    assert row.status == "running"


def test_pull_model_success_skips_insert_for_existing_model(manager, db):
    """Re-pulling an already-tracked model does not insert a duplicate row."""
    dep_id = _seed_deployment(db)
    _seed_model(db, dep_id, "llama3.2", name="llama3.2")
    mock_client = _http_client_mock(post=MagicMock(status_code=200))

    with patch(
        "services.management.app.services.ollama_manager.httpx.Client", return_value=mock_client
    ):
        manager.pull_model(dep_id, "llama3.2")

    assert len(db._tables["ollama_models"]) == 1


def test_pull_model_non_200_returns_error_status(manager, db):
    """A non-200 pull response reports the HTTP status and reverts to running."""
    dep_id = _seed_deployment(db)
    mock_client = _http_client_mock(post=MagicMock(status_code=404))

    with patch(
        "services.management.app.services.ollama_manager.httpx.Client", return_value=mock_client
    ):
        result = manager.pull_model(dep_id, "does-not-exist")

    assert result.status == "error"
    assert result.error == "HTTP 404"
    row = db._tables["ollama_deployments"][dep_id]
    assert row.status == "running"


def test_pull_model_timeout_returns_error_status(manager, db):
    """A pull timeout is caught and reported as an error PullStatus."""
    dep_id = _seed_deployment(db)
    mock_client = _http_client_mock(side_effect=httpx.TimeoutException("timed out"))

    with patch(
        "services.management.app.services.ollama_manager.httpx.Client", return_value=mock_client
    ):
        result = manager.pull_model(dep_id, "llama3.2")

    assert result.status == "error"
    assert result.error == "timed out"


# ---------------------------------------------------------------------------
# remove_model
# ---------------------------------------------------------------------------


def test_remove_model_not_found(manager):
    """Missing deployment returns False without touching HTTP."""
    assert manager.remove_model(999, "llama3.2") is False


def test_remove_model_success_deletes_row(manager, db):
    """A 200 delete response removes the tracked model row and returns True."""
    dep_id = _seed_deployment(db)
    _seed_model(db, dep_id, "llama3.2", name="llama3.2")
    mock_client = _http_client_mock(delete=MagicMock(status_code=200))

    with patch(
        "services.management.app.services.ollama_manager.httpx.Client", return_value=mock_client
    ):
        result = manager.remove_model(dep_id, "llama3.2")

    assert result is True
    assert db._tables["ollama_models"] == {}


def test_remove_model_non_200_returns_false(manager, db):
    """A non-200 delete response leaves the row intact and returns False."""
    dep_id = _seed_deployment(db)
    _seed_model(db, dep_id, "llama3.2", name="llama3.2")
    mock_client = _http_client_mock(delete=MagicMock(status_code=404))

    with patch(
        "services.management.app.services.ollama_manager.httpx.Client", return_value=mock_client
    ):
        result = manager.remove_model(dep_id, "llama3.2")

    assert result is False
    assert len(db._tables["ollama_models"]) == 1


def test_remove_model_connection_refused_returns_false(manager, db):
    """A connection error while removing a model is swallowed into False."""
    dep_id = _seed_deployment(db)
    mock_client = _http_client_mock(side_effect=httpx.ConnectError("Connection refused"))

    with patch(
        "services.management.app.services.ollama_manager.httpx.Client", return_value=mock_client
    ):
        assert manager.remove_model(dep_id, "llama3.2") is False


# ---------------------------------------------------------------------------
# InferenceFleetBackend adapter methods
# ---------------------------------------------------------------------------


def test_node_info_from_deployment_kubernetes_kind(manager):
    """A kubernetes*-typed deployment maps to NodeInfo.kind == 'k8s'."""
    row = FakeRow(
        name="dep-1",
        deployment_type="kubernetes-daemonset",
        health_status="healthy",
    )
    node = manager._node_info_from_deployment(row, ["llama3.2"])
    assert node == NodeInfo(
        node_id="dep-1",
        node_uid=None,
        kind="k8s",
        loaded_models=["llama3.2"],
        vram_total_mb=0,
        vram_free_mb=0,
        healthy=True,
    )


def test_node_info_from_deployment_external_kind_and_unhealthy(manager):
    """A non-kubernetes deployment_type maps to 'external'; unhealthy status carries through."""
    row = FakeRow(name="dep-2", deployment_type="external", health_status="unhealthy")
    node = manager._node_info_from_deployment(row, [])
    assert node.kind == "external"
    assert node.healthy is False


async def test_provision_creates_deployment_and_returns_node(manager, db):
    """provision() creates a deployment from the spec and returns its NodeInfo."""
    spec = ProvisionSpec(
        name="fleet-a", models=["llama3.2"], mode="daemonset", constraints={"gpu_count": 2}
    )

    nodes = await manager.provision(spec)

    assert len(nodes) == 1
    assert nodes[0].node_id == "fleet-a"
    row = db._tables["ollama_deployments"][1]
    assert row.deployment_type == "kubernetes-daemonset"


async def test_provision_pool_mode_sets_deployment_type(manager, db):
    """Mode == 'pool' selects pool-mode deployment_type and persists pool_mode.

    Note: `create_deployment`'s insert doesn't include `namespace`/`replicas`
    fields even though `OllamaDeploymentConfig` carries them (source gap, not
    fixed here per "don't modify the module under test") -- only
    deployment_type/pool_mode are asserted since those are what's actually
    persisted.
    """
    spec = ProvisionSpec(
        name="fleet-pool", models=[], mode="pool", constraints={"replicas": 3, "namespace": "ns2"}
    )

    await manager.provision(spec)

    row = db._tables["ollama_deployments"][1]
    assert row.deployment_type == "kubernetes"
    assert row.pool_mode is True
    assert not hasattr(row, "namespace")


async def test_provision_raises_when_create_deployment_fails(manager, db):
    """A duplicate-name failure from create_deployment surfaces as a RuntimeError."""
    _seed_deployment(db, name="dup")
    spec = ProvisionSpec(name="dup", models=[], mode="daemonset", constraints={})

    with pytest.raises(RuntimeError, match="already exists"):
        await manager.provision(spec)


async def test_deprovision_deletes_matching_deployment(manager, db):
    """deprovision() looks the node up by name and deletes it."""
    dep_id = _seed_deployment(db, name="node-x", status="stopped")

    await manager.deprovision("node-x")

    assert dep_id not in db._tables["ollama_deployments"]


async def test_deprovision_noop_when_not_found(manager, db):
    """deprovision() on an unknown node_id is a silent no-op."""
    await manager.deprovision("does-not-exist")
    assert db._tables.get("ollama_deployments", {}) == {}


async def test_health_aggregates_across_deployments(manager, db, monkeypatch):
    """health() aggregates per-deployment health_check() results into a FleetHealth."""
    dep1 = _seed_deployment(db, name="dep-1")
    dep2 = _seed_deployment(db, name="dep-2")

    def _fake_health_check(deployment_id):
        return {"healthy": deployment_id == dep1}

    monkeypatch.setattr(manager, "health_check", _fake_health_check)

    result = await manager.health()

    assert result == FleetHealth(
        backend_id=0, healthy=False, node_count=2, detail={"healthy_nodes": 1}
    )
    assert {dep1, dep2} == {dep1, dep2}  # sanity: both seeded


async def test_health_vacuously_healthy_with_no_deployments(manager):
    """Zero tracked deployments reports healthy=True (0 == 0), node_count=0."""
    result = await manager.health()
    assert result == FleetHealth(
        backend_id=0, healthy=True, node_count=0, detail={"healthy_nodes": 0}
    )


async def test_list_nodes_returns_node_info_per_deployment(manager, db):
    """list_nodes() maps every tracked deployment to a NodeInfo with its loaded models."""
    dep_id = _seed_deployment(db, name="dep-1", deployment_type="external")
    _seed_model(db, dep_id, "llama3.2")

    nodes = await manager.list_nodes()

    assert len(nodes) == 1
    assert nodes[0].node_id == "dep-1"
    assert nodes[0].loaded_models == ["llama3.2"]


async def test_place_model_targets_explicit_node_id(manager, db, monkeypatch):
    """place_model() with a node_id constraint pulls onto that specific deployment."""
    dep_id = _seed_deployment(db, name="dep-target", status="stopped")
    calls = []

    def _fake_pull(deployment_id, model_name):
        calls.append((deployment_id, model_name))
        return PullStatus(model=model_name, status="completed", completed=True)

    monkeypatch.setattr(manager, "pull_model", _fake_pull)

    placement = await manager.place_model("llama3.2", {"node_id": "dep-target"})

    assert calls == [(dep_id, "llama3.2")]
    assert placement == ModelPlacement(model="llama3.2", node_id="dep-target", status="placed")


async def test_place_model_falls_back_to_first_running_deployment(manager, db, monkeypatch):
    """Without a node_id constraint, placement targets the first 'running' deployment."""
    _seed_deployment(db, name="stopped-dep", status="stopped")
    _seed_deployment(db, name="running-dep", status="running")
    monkeypatch.setattr(
        manager,
        "pull_model",
        lambda deployment_id, model_name: PullStatus(model=model_name, status="pulling"),
    )

    placement = await manager.place_model("mistral", {})

    assert placement.node_id == "running-dep"
    assert placement.status == "pulling"


async def test_place_model_no_available_deployment_raises(manager):
    """No matching deployment raises a descriptive RuntimeError."""
    with pytest.raises(RuntimeError, match="No available Ollama deployment"):
        await manager.place_model("llama3.2", {})


async def test_place_model_pull_failure_raises(manager, db, monkeypatch):
    """A pull_model() error result is surfaced as a RuntimeError, not a partial success."""
    _seed_deployment(db, name="dep-1", status="running")
    monkeypatch.setattr(
        manager,
        "pull_model",
        lambda deployment_id, model_name: PullStatus(
            model=model_name, status="error", error="HTTP 500"
        ),
    )

    with pytest.raises(RuntimeError, match="HTTP 500"):
        await manager.place_model("llama3.2", {})


async def test_endpoints_for_returns_only_deployments_with_model_loaded(manager, db):
    """endpoints_for() filters to deployments whose tracked models include the target."""
    dep_with = _seed_deployment(db, name="has-model", endpoint_url="http://a:11434")
    _seed_deployment(db, name="without-model", endpoint_url="http://b:11434")
    _seed_model(db, dep_with, "llama3.2")

    endpoints = await manager.endpoints_for("llama3.2")

    assert endpoints == [
        Endpoint(
            url="http://a:11434", node_id="has-model", loaded_models=["llama3.2"], healthy=False
        )
    ]


async def test_endpoints_for_empty_when_model_nowhere_loaded(manager, db):
    """No deployment has the model loaded -> empty endpoint list."""
    dep_id = _seed_deployment(db, name="dep-1")
    _seed_model(db, dep_id, "mistral")

    endpoints = await manager.endpoints_for("llama3.2")

    assert endpoints == []


# ---------------------------------------------------------------------------
# docker_client lazy property
# ---------------------------------------------------------------------------


def test_docker_client_none_in_manual_mode(manual_manager):
    """MANUAL mode never attempts to build a Docker client."""
    assert manual_manager.docker_client is None


def test_docker_client_builds_and_caches(manager, monkeypatch):
    """A successful docker.from_env() is cached; the constructor runs only once."""
    sentinel = FakeDockerClient()
    calls = []

    def _from_env():
        calls.append(1)
        return sentinel

    monkeypatch.setattr("docker.from_env", _from_env)

    first = manager.docker_client
    second = manager.docker_client

    assert first is sentinel
    assert second is sentinel
    assert len(calls) == 1


def test_docker_client_returns_none_and_logs_warning_on_failure(manager, monkeypatch, caplog):
    """A docker.from_env() failure is caught, logged, and yields None (not a crash)."""

    def _boom():
        raise RuntimeError("no docker socket")

    monkeypatch.setattr("docker.from_env", _boom)

    import logging

    with caplog.at_level(logging.WARNING):
        result = manager.docker_client

    assert result is None
    assert any("Failed to initialize Docker client" in r.message for r in caplog.records)
