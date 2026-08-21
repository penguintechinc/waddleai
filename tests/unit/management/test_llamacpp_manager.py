"""Unit tests for LlamaCppManager."""

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from services.management.app.services.llamacpp_manager import (
    LlamaCppDeploymentConfig,
    LlamaCppManager,
)
from shared.fleet.base import Endpoint, FleetHealth, ModelPlacement, NodeInfo, ProvisionSpec

# ---------------------------------------------------------------------------
# Fake in-memory PyDAL-style DB -- real insert/select/update/delete semantics
# over plain dicts, so CRUD/InferenceFleetBackend tests exercise the manager's
# actual query composition instead of stubbing every call site with a
# MagicMock. Shape mirrors test_ollama_manager.py's FakeDB (not imported --
# each manager's tests own their fake independently); FakeRow here defaults
# any never-inserted column to None (a nullable DB column), matching this
# module's `deployment.field or default` access pattern -- llamacpp_manager,
# unlike ollama_manager, never does its own hasattr() checks.
# ---------------------------------------------------------------------------


class FakeRow:
    """Minimal PyDAL ``Row`` stand-in; an unset field reads back as ``None``."""

    def __init__(self, **fields: Any) -> None:
        """Store every keyword argument as a plain instance attribute."""
        self.__dict__.update(fields)

    def __getattr__(self, name: str) -> Any:
        """Unset attributes default to ``None``, mirroring a nullable column."""
        if name.startswith("_"):
            raise AttributeError(name)
        return None


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
    """``db(query)`` result: supports ``.select()``, ``.update()``, ``.delete()``."""

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


class FakeDB:
    """In-memory stand-in for the PyDAL ``db`` handle used throughout ``llamacpp_manager``."""

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


def _seed_llamacpp_deployment(db: "FakeDB", **overrides: Any) -> int:
    """Insert a representative ``llamacpp_deployments`` row and return its id."""
    fields = {
        "name": "llama-dep",
        "deployment_type": "kubernetes",
        "status": "pending",
        "model_name": "llama-3.2-3b-instruct",
        "model_url": "https://example.com/model.gguf",
        "model_filename": "model.gguf",
        "n_ctx": 4096,
        "n_gpu_layers": -1,
        "gpu_count": 1,
        "endpoint_url": None,
        "k8s_namespace": "waddleai",
        "k8s_daemonset_name": None,
        "node_selector": None,
        "node_affinity": None,
        "model_cache_claim": None,
        "cpu_request": None,
        "cpu_limit": None,
        "memory_request": None,
        "memory_limit": None,
        "node_uid": None,
    }
    fields.update(overrides)
    return db.llamacpp_deployments.insert(**fields)


@pytest.fixture
def db() -> FakeDB:
    """A fresh in-memory fake DB per test (CRUD / InferenceFleetBackend tests)."""
    return FakeDB()


@pytest.fixture
def fleet_manager(db: FakeDB) -> LlamaCppManager:
    """A manager wired to the fake DB, for tests needing real query semantics."""
    return LlamaCppManager(db)


@pytest.fixture
def mock_db():
    """Return a bare MagicMock standing in for the PyDAL db object."""
    db = MagicMock()
    return db


@pytest.fixture
def manager(mock_db):
    """Build a LlamaCppManager wired to the mock db, for tests exercising its methods."""
    from services.management.app.services.llamacpp_manager import LlamaCppManager

    return LlamaCppManager(mock_db)


@pytest.fixture
def k8s_deployment():
    """Minimal deployment record for K8s mode."""
    dep = MagicMock()
    dep.id = 1
    dep.name = "llama-3b"
    dep.deployment_type = "kubernetes"
    dep.model_name = "llama-3.2-3b-instruct"
    dep.model_url = "https://huggingface.co/example/llama-3.2-3b.gguf"
    dep.model_filename = "llama-3.2-3b.gguf"
    dep.n_ctx = 4096
    dep.n_gpu_layers = -1
    dep.gpu_count = 1
    dep.k8s_namespace = "waddleai"
    dep.k8s_daemonset_name = "waddleai-llamacpp-llama-3b"
    dep.node_selector = {"waddleai/gpu-tier": "a100"}
    dep.node_affinity = None
    dep.endpoint_url = None
    dep.status = "pending"
    # Hardening attributes (defaults when not provided)
    dep.model_cache_claim = None
    dep.cpu_request = None
    dep.cpu_limit = None
    dep.memory_request = None
    dep.memory_limit = None
    return dep


@pytest.fixture
def remote_deployment():
    """Minimal deployment record for remote (non-K8s) mode."""
    dep = MagicMock()
    dep.id = 2
    dep.name = "remote-llama"
    dep.deployment_type = "remote"
    dep.model_name = "llama-3.1-8b-instruct"
    dep.endpoint_url = "http://192.168.1.50:8080"
    dep.status = "pending"
    return dep


def test_generate_daemonset_name(manager):
    """Daemonset name is prefixed `waddleai-llamacpp-` and keeps a plain model name intact."""
    name = manager._daemonset_name("my-model")
    assert name == "waddleai-llamacpp-my-model"


def test_generate_daemonset_name_sanitises_special_chars(manager):
    """Daemonset name lowercases and strips spaces/dots/`!` into K8s-safe dashes."""
    name = manager._daemonset_name("My Model v2.0!")
    assert name == "waddleai-llamacpp-my-model-v2-0"


def test_export_k8s_manifest_contains_daemonset(manager, k8s_deployment):
    """Exported manifest is a multi-doc YAML including both a DaemonSet and a Service."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    kinds = [d["kind"] for d in docs]
    assert "DaemonSet" in kinds
    assert "Service" in kinds


def test_export_k8s_manifest_node_selector(manager, k8s_deployment):
    """DaemonSet pod template carries the deployment's node_selector unchanged."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    node_sel = ds["spec"]["template"]["spec"]["nodeSelector"]
    assert node_sel == {"waddleai/gpu-tier": "a100"}


def test_export_k8s_manifest_gpu_resource(manager, k8s_deployment):
    """Container resource limits request `nvidia.com/gpu` matching the deployment's gpu_count."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    container = ds["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["limits"]["nvidia.com/gpu"] == "1"


def test_export_k8s_manifest_init_container_download_url(manager, k8s_deployment):
    """URL should be passed via env var (not in command) for security."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    init_c = ds["spec"]["template"]["spec"]["initContainers"][0]
    env_vars = {e["name"]: e["value"] for e in init_c["env"]}
    assert env_vars["MODEL_URL"] == k8s_deployment.model_url


def test_export_k8s_manifest_service_port(manager, k8s_deployment):
    """Generated Service exposes port 8080, the llama.cpp server's fixed listen port."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    svc = next(d for d in docs if d["kind"] == "Service")
    assert svc["spec"]["ports"][0]["port"] == 8080


def test_deploy_daemonset_calls_k8s_api(manager, k8s_deployment, mock_db):
    """deploy_daemonset creates exactly one K8s DaemonSet and one Service via the apps/core APIs."""
    with (
        patch("services.management.app.services.llamacpp_manager.get_k8s_apps_client") as mock_apps,
        patch("services.management.app.services.llamacpp_manager.get_k8s_core_client") as mock_core,
    ):
        mock_apps.return_value = MagicMock()
        mock_core.return_value = MagicMock()
        manager.deploy_daemonset(k8s_deployment)

    mock_apps.return_value.create_namespaced_daemon_set.assert_called_once()
    mock_core.return_value.create_namespaced_service.assert_called_once()


def test_deploy_daemonset_k8s_error_propagates(manager, k8s_deployment):
    """A K8s API failure during DaemonSet creation propagates unchanged, not swallowed."""
    with patch(
        "services.management.app.services.llamacpp_manager.get_k8s_apps_client"
    ) as mock_apps:
        mock_apps.return_value.create_namespaced_daemon_set.side_effect = Exception(
            "k8s unavailable"
        )
        with pytest.raises(Exception, match="k8s unavailable"):
            manager.deploy_daemonset(k8s_deployment)


def test_remove_daemonset_running_without_force_raises(manager, k8s_deployment):
    """Removing a running deployment without force=True is refused with a ValueError."""
    k8s_deployment.status = "running"
    with pytest.raises(ValueError, match="force=True"):
        manager.remove_daemonset(k8s_deployment, force=False)


def test_remove_daemonset_running_with_force_deletes(manager, k8s_deployment):
    """force=True on a running deployment deletes both the DaemonSet and its Service."""
    k8s_deployment.status = "running"
    with (
        patch("services.management.app.services.llamacpp_manager.get_k8s_apps_client") as mock_apps,
        patch("services.management.app.services.llamacpp_manager.get_k8s_core_client") as mock_core,
    ):
        mock_apps.return_value = MagicMock()
        mock_core.return_value = MagicMock()
        manager.remove_daemonset(k8s_deployment, force=True)

    mock_apps.return_value.delete_namespaced_daemon_set.assert_called_once()
    mock_core.return_value.delete_namespaced_service.assert_called_once()


def test_register_remote_healthy_sets_running(manager, remote_deployment, mock_db):
    """A healthy remote endpoint (200) transitions the deployment's status to `running`."""
    with patch("services.management.app.services.llamacpp_manager.requests") as mock_req:
        mock_req.get.return_value.status_code = 200
        manager.register_remote(remote_deployment)

    mock_db(mock_db.llamacpp_deployments.id == remote_deployment.id).update.assert_called_once_with(
        status="running"
    )


def test_register_remote_unhealthy_raises(manager, remote_deployment):
    """An unreachable remote endpoint raises a ValueError instead of registering silently."""
    with patch("services.management.app.services.llamacpp_manager.requests") as mock_req:
        mock_req.get.side_effect = Exception("connection refused")
        with pytest.raises(ValueError, match="unreachable"):
            manager.register_remote(remote_deployment)


def test_register_remote_non_200_status_raises(manager, remote_deployment):
    """A non-200 (but non-exception) health response is also treated as unreachable."""
    with patch("services.management.app.services.llamacpp_manager.requests") as mock_req:
        mock_req.get.return_value.status_code = 503
        with pytest.raises(ValueError, match="unreachable"):
            manager.register_remote(remote_deployment)


def test_deploy_daemonset_service_creation_error_propagates(manager, k8s_deployment):
    """A K8s API failure creating the Service propagates unchanged after the DaemonSet succeeds."""
    with (
        patch("services.management.app.services.llamacpp_manager.get_k8s_apps_client") as mock_apps,
        patch("services.management.app.services.llamacpp_manager.get_k8s_core_client") as mock_core,
    ):
        mock_apps.return_value = MagicMock()
        mock_core.return_value.create_namespaced_service.side_effect = Exception(
            "service quota exceeded"
        )
        with pytest.raises(Exception, match="service quota exceeded"):
            manager.deploy_daemonset(k8s_deployment)

    mock_apps.return_value.create_namespaced_daemon_set.assert_called_once()


def test_export_k8s_manifest_includes_node_affinity_when_set(manager, k8s_deployment):
    """A deployment's node_affinity, when set, is added to the pod spec's affinity block."""
    k8s_deployment.node_affinity = {
        "requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchExpressions": [{"key": "gpu", "operator": "Exists"}]}]
        }
    }
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    affinity = ds["spec"]["template"]["spec"]["affinity"]
    assert affinity["nodeAffinity"] == k8s_deployment.node_affinity


# ---------------------------------------------------------------------------
# get_k8s_apps_client / get_k8s_core_client -- real loader bodies
# (in-cluster config -> kubeconfig fallback)
# ---------------------------------------------------------------------------


class TestK8sClientLoaders:
    """Exercise the real get_k8s_apps_client()/get_k8s_core_client() loader bodies."""

    def test_get_k8s_apps_client_in_cluster_success(self, monkeypatch):
        """A successful in-cluster config load skips the kubeconfig fallback."""
        import kubernetes

        from services.management.app.services import llamacpp_manager as mod

        monkeypatch.setattr(kubernetes.config, "load_incluster_config", MagicMock())
        monkeypatch.setattr(kubernetes.config, "load_kube_config", MagicMock())
        fake_api = MagicMock()
        monkeypatch.setattr(kubernetes.client, "AppsV1Api", MagicMock(return_value=fake_api))

        result = mod.get_k8s_apps_client()

        assert result is fake_api
        kubernetes.config.load_incluster_config.assert_called_once()
        kubernetes.config.load_kube_config.assert_not_called()

    def test_get_k8s_apps_client_falls_back_to_kubeconfig(self, monkeypatch):
        """An in-cluster config failure falls back to loading local kubeconfig."""
        import kubernetes

        from services.management.app.services import llamacpp_manager as mod

        monkeypatch.setattr(
            kubernetes.config,
            "load_incluster_config",
            MagicMock(side_effect=Exception("not in cluster")),
        )
        monkeypatch.setattr(kubernetes.config, "load_kube_config", MagicMock())
        fake_api = MagicMock()
        monkeypatch.setattr(kubernetes.client, "AppsV1Api", MagicMock(return_value=fake_api))

        result = mod.get_k8s_apps_client()

        assert result is fake_api
        kubernetes.config.load_kube_config.assert_called_once()

    def test_get_k8s_core_client_in_cluster_success(self, monkeypatch):
        """A successful in-cluster config load skips the kubeconfig fallback (core client)."""
        import kubernetes

        from services.management.app.services import llamacpp_manager as mod

        monkeypatch.setattr(kubernetes.config, "load_incluster_config", MagicMock())
        monkeypatch.setattr(kubernetes.config, "load_kube_config", MagicMock())
        fake_api = MagicMock()
        monkeypatch.setattr(kubernetes.client, "CoreV1Api", MagicMock(return_value=fake_api))

        result = mod.get_k8s_core_client()

        assert result is fake_api
        kubernetes.config.load_kube_config.assert_not_called()

    def test_get_k8s_core_client_falls_back_to_kubeconfig(self, monkeypatch):
        """An in-cluster config failure falls back to loading local kubeconfig (core client)."""
        import kubernetes

        from services.management.app.services import llamacpp_manager as mod

        monkeypatch.setattr(
            kubernetes.config,
            "load_incluster_config",
            MagicMock(side_effect=Exception("not in cluster")),
        )
        monkeypatch.setattr(kubernetes.config, "load_kube_config", MagicMock())
        fake_api = MagicMock()
        monkeypatch.setattr(kubernetes.client, "CoreV1Api", MagicMock(return_value=fake_api))

        result = mod.get_k8s_core_client()

        assert result is fake_api
        kubernetes.config.load_kube_config.assert_called_once()


# ---------------------------------------------------------------------------
# create_deployment / delete_deployment (CRUD, against FakeDB)
# ---------------------------------------------------------------------------


def test_create_deployment_success_inserts_row(fleet_manager, db):
    """create_deployment() inserts a pending row and returns its generated id/name."""
    config = LlamaCppDeploymentConfig(name="new-dep", model_name="llama-3.2-3b-instruct")

    result = fleet_manager.create_deployment(config)

    assert result["success"] is True
    assert result["name"] == "new-dep"
    row = db._tables["llamacpp_deployments"][result["deployment_id"]]
    assert row.status == "pending"
    assert row.k8s_daemonset_name == "waddleai-llamacpp-new-dep"


def test_create_deployment_duplicate_name_rejected(fleet_manager, db):
    """A second create_deployment() call reusing an existing name is rejected, no insert."""
    _seed_llamacpp_deployment(db, name="dup")
    config = LlamaCppDeploymentConfig(name="dup", model_name="m")

    result = fleet_manager.create_deployment(config)

    assert result == {"success": False, "error": "Deployment with this name already exists"}
    assert len(db._tables["llamacpp_deployments"]) == 1


def test_delete_deployment_not_found(fleet_manager):
    """Deleting an unknown id returns a not-found error without touching the DB."""
    result = fleet_manager.delete_deployment(999)
    assert result == {"success": False, "error": "Deployment not found"}


def test_delete_deployment_running_without_force_rejected(fleet_manager, db):
    """A running deployment cannot be deleted without force=True."""
    dep_id = _seed_llamacpp_deployment(db, name="live", status="running")

    result = fleet_manager.delete_deployment(dep_id, force=False)

    assert result["success"] is False
    assert "force=True" in result["error"]
    assert dep_id in db._tables["llamacpp_deployments"]


def test_delete_deployment_removes_service_and_deployment(fleet_manager, db):
    """force=True on a running k8s deployment tears down the DaemonSet, then deletes the row."""
    dep_id = _seed_llamacpp_deployment(
        db, name="live", status="running", deployment_type="kubernetes"
    )

    with (
        patch("services.management.app.services.llamacpp_manager.get_k8s_apps_client") as mock_apps,
        patch("services.management.app.services.llamacpp_manager.get_k8s_core_client") as mock_core,
    ):
        mock_apps.return_value = MagicMock()
        mock_core.return_value = MagicMock()
        result = fleet_manager.delete_deployment(dep_id, force=True)

    assert result == {"success": True}
    mock_apps.return_value.delete_namespaced_daemon_set.assert_called_once()
    mock_core.return_value.delete_namespaced_service.assert_called_once()
    assert dep_id not in db._tables["llamacpp_deployments"]


def test_delete_deployment_remote_type_force_skips_daemonset_removal(fleet_manager, db):
    """force=True on a running *remote* deployment deletes the row without touching K8s."""
    dep_id = _seed_llamacpp_deployment(
        db, name="remote-dep", status="running", deployment_type="remote"
    )

    with patch(
        "services.management.app.services.llamacpp_manager.get_k8s_apps_client"
    ) as mock_apps:
        result = fleet_manager.delete_deployment(dep_id, force=True)

    mock_apps.assert_not_called()
    assert result == {"success": True}
    assert dep_id not in db._tables["llamacpp_deployments"]


def test_delete_deployment_force_removal_error_is_logged_and_continues(fleet_manager, db, caplog):
    """A DaemonSet-teardown failure during forced delete is logged but doesn't block deletion."""
    dep_id = _seed_llamacpp_deployment(
        db, name="live", status="running", deployment_type="kubernetes"
    )

    with (
        patch("services.management.app.services.llamacpp_manager.get_k8s_apps_client") as mock_apps,
        patch("services.management.app.services.llamacpp_manager.get_k8s_core_client") as mock_core,
    ):
        mock_apps.return_value.delete_namespaced_daemon_set.side_effect = Exception(
            "teardown failed"
        )
        mock_core.return_value = MagicMock()
        with caplog.at_level(logging.WARNING):
            result = fleet_manager.delete_deployment(dep_id, force=True)

    assert result == {"success": True}
    assert dep_id not in db._tables["llamacpp_deployments"]
    assert any("Error during forced removal" in r.message for r in caplog.records)


def test_delete_deployment_not_running_deletes_without_force(fleet_manager, db):
    """A non-running deployment is deleted outright; force is irrelevant."""
    dep_id = _seed_llamacpp_deployment(db, name="stopped-dep", status="stopped")

    result = fleet_manager.delete_deployment(dep_id, force=False)

    assert result == {"success": True}
    assert dep_id not in db._tables["llamacpp_deployments"]


# ---------------------------------------------------------------------------
# _node_info_from_deployment / _reachable (InferenceFleetBackend helpers)
# ---------------------------------------------------------------------------


def test_node_info_from_deployment_kubernetes_kind_with_node_uid(fleet_manager, db):
    """A kubernetes-typed running deployment maps to kind='k8s', healthy=True, node_uid carried."""
    dep_id = _seed_llamacpp_deployment(
        db, name="dep-1", deployment_type="kubernetes", status="running", node_uid="uid-123"
    )
    row = db._tables["llamacpp_deployments"][dep_id]

    node = fleet_manager._node_info_from_deployment(row)

    assert node == NodeInfo(
        node_id="dep-1",
        node_uid="uid-123",
        kind="k8s",
        loaded_models=["llama-3.2-3b-instruct"],
        vram_total_mb=0,
        vram_free_mb=0,
        healthy=True,
    )


def test_node_info_from_deployment_remote_kind_empty_model_name(fleet_manager, db):
    """A remote-typed, non-running deployment maps to kind='external'; empty model_name -> []."""
    dep_id = _seed_llamacpp_deployment(
        db, name="dep-2", deployment_type="remote", model_name="", status="pending"
    )
    row = db._tables["llamacpp_deployments"][dep_id]

    node = fleet_manager._node_info_from_deployment(row)

    assert node.kind == "external"
    assert node.loaded_models == []
    assert node.healthy is False


def test_reachable_no_endpoint_url_returns_false(fleet_manager, db):
    """A deployment with no endpoint_url is unreachable without making an HTTP call."""
    dep_id = _seed_llamacpp_deployment(db, name="dep-1", endpoint_url=None)
    row = db._tables["llamacpp_deployments"][dep_id]

    with patch("services.management.app.services.llamacpp_manager.requests") as mock_req:
        assert fleet_manager._reachable(row) is False
    mock_req.get.assert_not_called()


def test_reachable_200_returns_true(fleet_manager, db):
    """A 200 response from GET {endpoint}/health marks the deployment reachable."""
    dep_id = _seed_llamacpp_deployment(db, name="dep-1", endpoint_url="http://a:8080")
    row = db._tables["llamacpp_deployments"][dep_id]

    with patch("services.management.app.services.llamacpp_manager.requests") as mock_req:
        mock_req.get.return_value.status_code = 200
        assert fleet_manager._reachable(row) is True


def test_reachable_non_200_returns_false(fleet_manager, db):
    """A non-200 response marks the deployment unreachable."""
    dep_id = _seed_llamacpp_deployment(db, name="dep-1", endpoint_url="http://a:8080")
    row = db._tables["llamacpp_deployments"][dep_id]

    with patch("services.management.app.services.llamacpp_manager.requests") as mock_req:
        mock_req.get.return_value.status_code = 503
        assert fleet_manager._reachable(row) is False


def test_reachable_connection_error_returns_false(fleet_manager, db):
    """A connection error during the health GET is swallowed into False, not raised."""
    dep_id = _seed_llamacpp_deployment(db, name="dep-1", endpoint_url="http://a:8080")
    row = db._tables["llamacpp_deployments"][dep_id]

    with patch("services.management.app.services.llamacpp_manager.requests") as mock_req:
        mock_req.get.side_effect = Exception("connection refused")
        assert fleet_manager._reachable(row) is False


# ---------------------------------------------------------------------------
# InferenceFleetBackend adapter methods: provision / deprovision / health /
# list_nodes / place_model / endpoints_for
# ---------------------------------------------------------------------------


async def test_provision_creates_deployment_with_gpu_resources_when_requested(fleet_manager, db):
    """provision() in kubernetes mode creates+deploys a DaemonSet, persisting gpu_count."""
    spec = ProvisionSpec(
        name="fleet-a",
        models=["llama-3.2-3b-instruct"],
        mode="kubernetes",
        constraints={
            "model_url": "https://example.com/model.gguf",
            "model_filename": "model.gguf",
            "gpu_count": 2,
            "namespace": "waddleai",
        },
    )

    with (
        patch("services.management.app.services.llamacpp_manager.get_k8s_apps_client") as mock_apps,
        patch("services.management.app.services.llamacpp_manager.get_k8s_core_client") as mock_core,
    ):
        mock_apps.return_value = MagicMock()
        mock_core.return_value = MagicMock()
        nodes = await fleet_manager.provision(spec)

    assert len(nodes) == 1
    assert nodes[0].node_id == "fleet-a"
    row = db._tables["llamacpp_deployments"][1]
    assert row.gpu_count == 2
    assert row.status == "deploying"
    mock_apps.return_value.create_namespaced_daemon_set.assert_called_once()


async def test_provision_remote_mode_registers_and_returns_node(fleet_manager, db):
    """provision() in remote mode registers the endpoint via a health check, no K8s calls."""
    spec = ProvisionSpec(
        name="remote-fleet",
        models=["llama-3.1-8b-instruct"],
        mode="remote",
        constraints={"endpoint_url": "http://192.168.1.50:8080"},
    )

    with patch("services.management.app.services.llamacpp_manager.requests") as mock_req:
        mock_req.get.return_value.status_code = 200
        nodes = await fleet_manager.provision(spec)

    assert nodes[0].node_id == "remote-fleet"
    assert nodes[0].healthy is True
    row = db._tables["llamacpp_deployments"][1]
    assert row.status == "running"


async def test_provision_model_name_falls_back_to_constraints_when_no_models(fleet_manager, db):
    """When spec.models is empty, provision() reads model_name from constraints instead."""
    spec = ProvisionSpec(
        name="fallback-fleet",
        models=[],
        mode="remote",
        constraints={"model_name": "custom-model", "endpoint_url": "http://x:8080"},
    )

    with patch("services.management.app.services.llamacpp_manager.requests") as mock_req:
        mock_req.get.return_value.status_code = 200
        await fleet_manager.provision(spec)

    row = db._tables["llamacpp_deployments"][1]
    assert row.model_name == "custom-model"


async def test_provision_surfaces_k8s_quota_exceeded_error(fleet_manager, db):
    """A K8s quota-exceeded error from DaemonSet creation propagates unwrapped from provision()."""
    spec = ProvisionSpec(name="fleet-b", models=["m"], mode="kubernetes", constraints={})

    with patch(
        "services.management.app.services.llamacpp_manager.get_k8s_apps_client"
    ) as mock_apps:
        mock_apps.return_value.create_namespaced_daemon_set.side_effect = Exception(
            "exceeded quota: pods=10, used: 10, limited: 10"
        )
        with pytest.raises(Exception, match="exceeded quota"):
            await fleet_manager.provision(spec)


async def test_provision_raises_runtime_error_on_duplicate_name(fleet_manager, db):
    """A duplicate deployment name from create_deployment() surfaces as a RuntimeError."""
    _seed_llamacpp_deployment(db, name="dup")
    spec = ProvisionSpec(name="dup", models=["m"], mode="kubernetes", constraints={})

    with pytest.raises(RuntimeError, match="already exists"):
        await fleet_manager.provision(spec)


async def test_deprovision_force_removes_running_deployment(fleet_manager, db):
    """deprovision() looks the node up by name and force-deletes it, tearing down its DaemonSet."""
    dep_id = _seed_llamacpp_deployment(
        db, name="node-x", status="running", deployment_type="kubernetes"
    )

    with (
        patch("services.management.app.services.llamacpp_manager.get_k8s_apps_client") as mock_apps,
        patch("services.management.app.services.llamacpp_manager.get_k8s_core_client") as mock_core,
    ):
        mock_apps.return_value = MagicMock()
        mock_core.return_value = MagicMock()
        await fleet_manager.deprovision("node-x")

    assert dep_id not in db._tables["llamacpp_deployments"]
    mock_apps.return_value.delete_namespaced_daemon_set.assert_called_once()


async def test_deprovision_noop_when_not_found(fleet_manager, db):
    """deprovision() on an unknown node_id is a silent no-op (no delete, no error)."""
    await fleet_manager.deprovision("does-not-exist")
    assert db._tables.get("llamacpp_deployments", {}) == {}


async def test_health_reports_healthy_when_pod_ready(fleet_manager, db):
    """health() reports healthy=True when every deployment's /health check succeeds."""
    _seed_llamacpp_deployment(db, name="dep-1", endpoint_url="http://a:8080", status="running")

    with patch("services.management.app.services.llamacpp_manager.requests") as mock_req:
        mock_req.get.return_value.status_code = 200
        result = await fleet_manager.health()

    assert result == FleetHealth(
        backend_id=0, healthy=True, node_count=1, detail={"healthy_nodes": 1}
    )


async def test_health_reports_unhealthy_when_pod_not_ready(fleet_manager, db):
    """health() reports healthy=False (and the correct partial count) when a pod isn't ready."""
    _seed_llamacpp_deployment(db, name="dep-1", endpoint_url="http://a:8080")
    _seed_llamacpp_deployment(db, name="dep-2", endpoint_url=None)

    with patch("services.management.app.services.llamacpp_manager.requests") as mock_req:
        mock_req.get.return_value.status_code = 200
        result = await fleet_manager.health()

    assert result == FleetHealth(
        backend_id=0, healthy=False, node_count=2, detail={"healthy_nodes": 1}
    )


async def test_health_vacuously_healthy_with_no_deployments(fleet_manager):
    """Zero tracked deployments reports healthy=True (0 == 0), node_count=0."""
    result = await fleet_manager.health()
    assert result == FleetHealth(
        backend_id=0, healthy=True, node_count=0, detail={"healthy_nodes": 0}
    )


async def test_list_nodes_returns_node_info_per_deployment(fleet_manager, db):
    """list_nodes() maps every tracked deployment to a NodeInfo including its model."""
    _seed_llamacpp_deployment(
        db, name="dep-1", model_name="llama-3.2-3b-instruct", status="running"
    )

    nodes = await fleet_manager.list_nodes()

    assert len(nodes) == 1
    assert nodes[0].node_id == "dep-1"
    assert nodes[0].loaded_models == ["llama-3.2-3b-instruct"]
    assert nodes[0].healthy is True


async def test_place_model_running_deployment_returns_placed(fleet_manager, db):
    """A running deployment serving the model returns ModelPlacement(status='placed')."""
    _seed_llamacpp_deployment(
        db, name="dep-run", model_name="llama-3.2-3b-instruct", status="running"
    )

    placement = await fleet_manager.place_model("llama-3.2-3b-instruct", {})

    assert placement == ModelPlacement(
        model="llama-3.2-3b-instruct", node_id="dep-run", status="placed"
    )


async def test_place_model_pending_deployment_returns_pulling(fleet_manager, db):
    """A non-running deployment serving the model returns ModelPlacement(status='pulling')."""
    _seed_llamacpp_deployment(db, name="dep-pending", model_name="mistral-7b", status="pending")

    placement = await fleet_manager.place_model("mistral-7b", {})

    assert placement.status == "pulling"


async def test_place_model_no_deployment_raises(fleet_manager):
    """No deployment serving the model raises a descriptive RuntimeError."""
    with pytest.raises(RuntimeError, match="No llama.cpp deployment serving model 'ghost-model'"):
        await fleet_manager.place_model("ghost-model", {})


async def test_endpoints_for_returns_endpoint_for_matching_model(fleet_manager, db):
    """endpoints_for() returns an Endpoint for a deployment serving the model with a URL set."""
    _seed_llamacpp_deployment(
        db,
        name="dep-1",
        model_name="llama-3.2-3b-instruct",
        endpoint_url="http://a:8080",
        status="running",
    )

    endpoints = await fleet_manager.endpoints_for("llama-3.2-3b-instruct")

    assert endpoints == [
        Endpoint(
            url="http://a:8080",
            node_id="dep-1",
            loaded_models=["llama-3.2-3b-instruct"],
            healthy=True,
        )
    ]


async def test_endpoints_for_skips_deployment_without_endpoint_url(fleet_manager, db):
    """A deployment serving the model but with no endpoint_url is excluded from the results."""
    _seed_llamacpp_deployment(
        db, name="dep-1", model_name="llama-3.2-3b-instruct", endpoint_url=None
    )

    endpoints = await fleet_manager.endpoints_for("llama-3.2-3b-instruct")

    assert endpoints == []


async def test_endpoints_for_empty_when_model_nowhere_served(fleet_manager, db):
    """No deployment serves the requested model -> empty endpoint list."""
    _seed_llamacpp_deployment(db, name="dep-1", model_name="other-model")

    endpoints = await fleet_manager.endpoints_for("llama-3.2-3b-instruct")

    assert endpoints == []
