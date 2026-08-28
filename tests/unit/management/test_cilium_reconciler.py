"""Unit tests for the Cilium capability detection + reconciler orchestration.

Covers services/management/app/services/cilium_policy.py. All Kubernetes
client interaction is mocked -- these tests never touch a real
cluster. Capability detection and reconcile() must never raise regardless of
what the mocked k8s client does.
"""

from unittest.mock import MagicMock

import pytest

from services.management.app.services import cilium_policy as cp


class _FakeCRD:
    def __init__(self, name):
        self.metadata = MagicMock(name="metadata")
        self.metadata.name = name


class _FakeCRDList:
    def __init__(self, names):
        self.items = [_FakeCRD(n) for n in names]


class _FakeApiext:
    def __init__(self, crds):
        self._crds = crds

    def list_custom_resource_definition(self):
        return _FakeCRDList(self._crds)


TOPOLOGY = {
    "namespace": "waddleai",
    "gateway_name": "shared",
    "gateway_namespace": "gateway",
    "aiproxy_port": 8080,
    "postgres_port": 5432,
    "valkey_port": 6379,
    "fleet_ports": [8080, 11434],
    "fleet_component_key": "app.kubernetes.io/component",
    "fleet_components": ["ollama", "llamacpp"],
    "selectors": {
        "gateway": {"app.kubernetes.io/name": "cilium-gateway"},
        "aiproxy": {"app.kubernetes.io/name": "waddleai", "app.kubernetes.io/component": "proxy"},
        "management": {
            "app.kubernetes.io/name": "waddleai",
            "app.kubernetes.io/component": "management",
        },
        "postgres": {
            "app.kubernetes.io/name": "waddleai",
            "app.kubernetes.io/component": "postgres",
        },
        "valkey": {"app.kubernetes.io/name": "waddleai", "app.kubernetes.io/component": "valkey"},
    },
}

BOTH_CRDS = ["ciliumnetworkpolicies.cilium.io", "ciliumenvoyconfigs.cilium.io"]


# ---------------------------------------------------------------------------
# cilium_capabilities()
# ---------------------------------------------------------------------------


class TestCiliumCapabilities:
    """Tests for cilium_capabilities()."""

    def test_both_crds_present(self, monkeypatch):
        """Both CRDs installed yields network_policy, envoy_config, and available all True."""
        monkeypatch.setattr(cp, "get_k8s_apiext_client", lambda: _FakeApiext(BOTH_CRDS))
        caps = cp.cilium_capabilities()
        assert caps == {"network_policy": True, "envoy_config": True, "available": True}

    def test_only_network_policy_crd_present(self, monkeypatch):
        """Only the CNP CRD installed yields envoy_config=False, available=True."""
        monkeypatch.setattr(
            cp, "get_k8s_apiext_client", lambda: _FakeApiext(["ciliumnetworkpolicies.cilium.io"])
        )
        caps = cp.cilium_capabilities()
        assert caps == {"network_policy": True, "envoy_config": False, "available": True}

    def test_neither_crd_present(self, monkeypatch):
        """No Cilium CRDs installed yields every capability False."""
        monkeypatch.setattr(cp, "get_k8s_apiext_client", lambda: _FakeApiext([]))
        caps = cp.cilium_capabilities()
        assert caps == {"network_policy": False, "envoy_config": False, "available": False}

    def test_capabilities_absent_when_crds_missing(self, monkeypatch):
        """An empty CRD list on the cluster is treated the same as no Cilium at all."""
        monkeypatch.setattr(cp, "get_k8s_apiext_client", lambda: _FakeApiext([]))
        caps = cp.cilium_capabilities()
        assert caps == {"network_policy": False, "envoy_config": False, "available": False}

    def test_client_construction_failure_degrades_gracefully(self, monkeypatch):
        """A client-construction error (e.g. no kubeconfig) degrades to all-False, not a raise."""

        def _boom():
            raise RuntimeError("no kubeconfig found")

        monkeypatch.setattr(cp, "get_k8s_apiext_client", _boom)
        caps = cp.cilium_capabilities()
        assert caps == {"network_policy": False, "envoy_config": False, "available": False}

    def test_api_exception_degrades_gracefully(self, monkeypatch):
        """A 403/API error while listing CRDs degrades to available=False, not a raise."""

        class _Forbidden:
            def list_custom_resource_definition(self):
                raise Exception("403 Forbidden")

        monkeypatch.setattr(cp, "get_k8s_apiext_client", lambda: _Forbidden())
        caps = cp.cilium_capabilities()
        assert caps["available"] is False


# ---------------------------------------------------------------------------
# is_native_rate_limit_enabled()
# ---------------------------------------------------------------------------


class TestNativeRateLimitFlag:
    """Tests for is_native_rate_limit_enabled()."""

    def test_flag_helper_import_failure_fails_safe_off(self, monkeypatch):
        """An ImportError on the feature-flag helper fails safe to OFF, not a raise."""
        import builtins

        real_import = builtins.__import__

        def _blocked_import(name, *args, **kwargs):
            if name == "shared.utils.feature_flags":
                raise ImportError("simulated missing module")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked_import)
        assert cp.is_native_rate_limit_enabled() is False


# ---------------------------------------------------------------------------
# CiliumPolicyReconciler.reconcile()
# ---------------------------------------------------------------------------


def _mock_db(orgs=None):
    """A minimal mock mimicking penguin-dal's db(query).select() chain.

    MagicMock does not auto-configure ordering comparisons (`>`, `<`, ...),
    so `db.organizations.id > 0` needs an explicit __gt__ stub -- mirrors why
    tests/unit/management/route_conftest.py hand-rolls _DBField/_DBQuery.
    """
    db = MagicMock()
    db.organizations = MagicMock()
    db.organizations.id.__gt__ = MagicMock(return_value="query")
    # No rpm_limit column reflected by default (spec §13.1 migration 007
    # owns that column; not yet present on this branch).
    del db.organizations.rpm_limit
    db.return_value.select.return_value = orgs or []
    return db


class TestReconcile:
    """Tests for CiliumPolicyReconciler.reconcile()."""

    @pytest.fixture(autouse=True)
    def _default_flag_and_caps(self, monkeypatch):
        """By default: flag ON, both CRDs present -- individual tests override."""
        monkeypatch.setattr(cp, "is_native_rate_limit_enabled", lambda: True)
        monkeypatch.setattr(
            cp,
            "cilium_capabilities",
            lambda: {"network_policy": True, "envoy_config": True, "available": True},
        )

    def test_flag_off_makes_zero_crd_calls(self, monkeypatch):
        """§14.2 flag-off proof: zero CustomObjectsApi calls when the flag is off."""
        monkeypatch.setattr(cp, "is_native_rate_limit_enabled", lambda: False)
        mock_client_factory = MagicMock()
        monkeypatch.setattr(cp, "get_k8s_custom_objects_client", mock_client_factory)

        reconciler = cp.CiliumPolicyReconciler(_mock_db(), topology=TOPOLOGY)
        status = reconciler.reconcile()

        assert status.skipped is True
        assert status.reason == "flag_off"
        assert status.applied == []
        mock_client_factory.assert_not_called()

    def test_crds_absent_makes_zero_crd_calls(self, monkeypatch):
        """CRDs absent skips the reconcile before any CustomObjectsApi call is made."""
        monkeypatch.setattr(
            cp,
            "cilium_capabilities",
            lambda: {"network_policy": False, "envoy_config": False, "available": False},
        )
        mock_client_factory = MagicMock()
        monkeypatch.setattr(cp, "get_k8s_custom_objects_client", mock_client_factory)

        reconciler = cp.CiliumPolicyReconciler(_mock_db(), topology=TOPOLOGY)
        status = reconciler.reconcile()

        assert status.skipped is True
        assert status.reason == "crds_absent"
        mock_client_factory.assert_not_called()

    def test_create_path_when_object_absent(self, monkeypatch):
        """A 404 read triggers create_* for every rendered object, never replace_*."""
        from kubernetes.client.rest import ApiException

        client = MagicMock()
        client.get_namespaced_custom_object.side_effect = ApiException(status=404)
        client.get_cluster_custom_object.side_effect = ApiException(status=404)
        monkeypatch.setattr(cp, "get_k8s_custom_objects_client", lambda: client)

        reconciler = cp.CiliumPolicyReconciler(_mock_db(), topology=TOPOLOGY)
        status = reconciler.reconcile()

        assert status.skipped is False
        assert status.degraded is False
        assert "waddleai-org-ratelimit" in status.applied
        assert "waddleai-default-deny" in status.applied
        assert client.create_namespaced_custom_object.called
        assert client.create_cluster_custom_object.called
        assert not client.replace_namespaced_custom_object.called

    def test_replace_path_when_object_exists(self, monkeypatch):
        """An existing object triggers replace_* with its resourceVersion, no duplicate create."""
        client = MagicMock()
        client.get_namespaced_custom_object.return_value = {"metadata": {"resourceVersion": "42"}}
        client.get_cluster_custom_object.return_value = {"metadata": {"resourceVersion": "7"}}
        monkeypatch.setattr(cp, "get_k8s_custom_objects_client", lambda: client)

        reconciler = cp.CiliumPolicyReconciler(_mock_db(), topology=TOPOLOGY)
        status = reconciler.reconcile()

        assert status.degraded is False
        assert client.replace_namespaced_custom_object.called
        assert client.replace_cluster_custom_object.called
        assert not client.create_namespaced_custom_object.called
        # No duplicate create on the replace path.
        assert not client.create_cluster_custom_object.called

    def test_partial_capability_only_cnp_applies_cnp_skips_cec(self, monkeypatch):
        """CNP CRD present but not CEC applies only CNP/CCNP objects, skips the CEC entirely."""
        monkeypatch.setattr(
            cp,
            "cilium_capabilities",
            lambda: {"network_policy": True, "envoy_config": False, "available": True},
        )
        from kubernetes.client.rest import ApiException

        client = MagicMock()
        client.get_namespaced_custom_object.side_effect = ApiException(status=404)
        client.get_cluster_custom_object.side_effect = ApiException(status=404)
        monkeypatch.setattr(cp, "get_k8s_custom_objects_client", lambda: client)

        reconciler = cp.CiliumPolicyReconciler(_mock_db(), topology=TOPOLOGY)
        status = reconciler.reconcile()

        assert "waddleai-default-deny" in status.applied
        assert "waddleai-org-ratelimit" not in status.applied
        # Only CNP/CCNP pluralities were ever touched.
        called_plurals = {c.args[3] for c in client.create_namespaced_custom_object.call_args_list}
        assert called_plurals <= {cp.CNP_PLURAL}

    def test_api_exception_mid_upsert_degrades_without_raising(self, monkeypatch):
        """A create failure on one object sets degraded but does not stop later upserts."""
        from kubernetes.client.rest import ApiException

        client = MagicMock()
        client.get_namespaced_custom_object.side_effect = ApiException(status=404)
        client.get_cluster_custom_object.side_effect = ApiException(status=404)
        # First namespaced create fails (CEC), remaining CNPs must still be attempted.
        client.create_namespaced_custom_object.side_effect = [
            ApiException(status=500),
            *[None] * 20,
        ]
        monkeypatch.setattr(cp, "get_k8s_custom_objects_client", lambda: client)

        reconciler = cp.CiliumPolicyReconciler(_mock_db(), topology=TOPOLOGY)
        status = reconciler.reconcile()

        assert status.degraded is True
        # Reconcile did not raise, and continued past the failure -- more than
        # one namespaced create was attempted (CEC failed, CNPs proceeded).
        assert client.create_namespaced_custom_object.call_count > 1

    def test_client_construction_failure_is_skipped_and_degraded(self, monkeypatch):
        """A CustomObjectsApi construction failure skips with reason client_unavailable."""

        def _boom():
            raise RuntimeError("kubeconfig missing")

        monkeypatch.setattr(cp, "get_k8s_custom_objects_client", _boom)

        reconciler = cp.CiliumPolicyReconciler(_mock_db(), topology=TOPOLOGY)
        status = reconciler.reconcile()

        assert status.skipped is True
        assert status.reason == "client_unavailable"
        assert status.degraded is True

    def test_reconcile_never_raises_on_db_load_failure(self, monkeypatch):
        """A DB error while loading orgs is swallowed into status.degraded, not a raise."""
        from kubernetes.client.rest import ApiException

        client = MagicMock()
        client.get_namespaced_custom_object.side_effect = ApiException(status=404)
        client.get_cluster_custom_object.side_effect = ApiException(status=404)
        monkeypatch.setattr(cp, "get_k8s_custom_objects_client", lambda: client)

        bad_db = MagicMock()
        bad_db.organizations = MagicMock()
        bad_db.organizations.id.__gt__ = MagicMock(return_value="query")
        del bad_db.organizations.rpm_limit
        bad_db.side_effect = RuntimeError("db connection lost")

        reconciler = cp.CiliumPolicyReconciler(bad_db, topology=TOPOLOGY)
        status = reconciler.reconcile()  # must not raise

        assert status.degraded is True

    def test_reconcile_updates_last_status(self, monkeypatch):
        """A completed reconcile is retrievable afterward via get_last_status()."""
        from kubernetes.client.rest import ApiException

        client = MagicMock()
        client.get_namespaced_custom_object.side_effect = ApiException(status=404)
        client.get_cluster_custom_object.side_effect = ApiException(status=404)
        monkeypatch.setattr(cp, "get_k8s_custom_objects_client", lambda: client)

        reconciler = cp.CiliumPolicyReconciler(_mock_db(), topology=TOPOLOGY)
        status = reconciler.reconcile()

        assert cp.get_last_status() is status


class TestLoadOrgs:
    """Tests for CiliumPolicyReconciler._load_orgs()."""

    def test_load_orgs_defaults_rpm_limit_none_when_column_absent(self):
        """When organizations.rpm_limit isn't reflected, every org loads with rpm_limit=None."""
        org_row = MagicMock()
        org_row.id = 1
        org_row.name = "acme"
        org_row.enabled = True
        del org_row.rpm_limit  # column not yet present (migration 007 not landed here)

        db = MagicMock()
        db.organizations = MagicMock()
        db.organizations.id.__gt__ = MagicMock(return_value="query")
        del db.organizations.rpm_limit
        db.return_value.select.return_value = [org_row]

        reconciler = cp.CiliumPolicyReconciler(db, topology=TOPOLOGY)
        orgs = reconciler._load_orgs()

        assert orgs == [(1, "acme", None, True)]

    def test_load_orgs_picks_up_rpm_limit_once_column_exists(self):
        """Forward-compat: once migration 007 adds organizations.rpm_limit elsewhere.

        This loader must start reading it with no code changes here.
        """
        org_row = MagicMock()
        org_row.id = 1
        org_row.name = "acme"
        org_row.enabled = True
        org_row.rpm_limit = 600

        db = MagicMock()
        db.organizations = MagicMock()
        db.organizations.id.__gt__ = MagicMock(return_value="query")
        db.organizations.rpm_limit = MagicMock()  # column IS reflected
        db.return_value.select.return_value = [org_row]

        reconciler = cp.CiliumPolicyReconciler(db, topology=TOPOLOGY)
        orgs = reconciler._load_orgs()

        assert orgs == [(1, "acme", 600, True)]


class TestK8sClientLoaders:
    """Exercise the real loader bodies (in-cluster -> kubeconfig fallback)."""

    def test_apiext_client_falls_back_to_kubeconfig(self, monkeypatch):
        """When in-cluster config load fails, the client falls back to kubeconfig."""
        import kubernetes

        monkeypatch.setattr(
            kubernetes.config,
            "load_incluster_config",
            MagicMock(side_effect=Exception("not in cluster")),
        )
        monkeypatch.setattr(kubernetes.config, "load_kube_config", MagicMock())
        fake_api = MagicMock()
        monkeypatch.setattr(
            kubernetes.client, "ApiextensionsV1Api", MagicMock(return_value=fake_api)
        )

        result = cp.get_k8s_apiext_client()

        assert result is fake_api
        kubernetes.config.load_kube_config.assert_called_once()

    def test_custom_objects_client_falls_back_to_kubeconfig(self, monkeypatch):
        """When in-cluster config load fails, the client falls back to kubeconfig."""
        import kubernetes

        monkeypatch.setattr(
            kubernetes.config,
            "load_incluster_config",
            MagicMock(side_effect=Exception("not in cluster")),
        )
        monkeypatch.setattr(kubernetes.config, "load_kube_config", MagicMock())
        fake_api = MagicMock()
        monkeypatch.setattr(kubernetes.client, "CustomObjectsApi", MagicMock(return_value=fake_api))

        result = cp.get_k8s_custom_objects_client()

        assert result is fake_api
        kubernetes.config.load_kube_config.assert_called_once()


class TestIsNativeRateLimitEnabledSuccessPath:
    """Tests for is_native_rate_limit_enabled()'s success path."""

    def test_delegates_to_feature_flag_helper(self, monkeypatch):
        """The real function returns whatever the feature-flag helper resolves."""
        import shared.utils.feature_flags as ff

        monkeypatch.setattr(ff, "is_feature_enabled", lambda *a, **k: True)
        assert cp.is_native_rate_limit_enabled() is True


class TestTopologyResolution:
    """Tests for CiliumPolicyReconciler._topology()."""

    def test_no_override_no_env_returns_default(self, monkeypatch):
        """With no constructor override and no env var, DEFAULT_TOPOLOGY is used."""
        monkeypatch.delenv("CILIUM_TOPOLOGY", raising=False)
        reconciler = cp.CiliumPolicyReconciler(_mock_db())
        assert reconciler._topology() == cp.DEFAULT_TOPOLOGY

    def test_invalid_json_env_falls_back_to_default(self, monkeypatch):
        """Malformed JSON in CILIUM_TOPOLOGY logs a warning and falls back to the default."""
        monkeypatch.setenv("CILIUM_TOPOLOGY", "{not-valid-json")
        reconciler = cp.CiliumPolicyReconciler(_mock_db())
        assert reconciler._topology() == cp.DEFAULT_TOPOLOGY

    def test_valid_json_env_is_parsed(self, monkeypatch):
        """Valid JSON in CILIUM_TOPOLOGY is parsed and returned as-is."""
        monkeypatch.setenv("CILIUM_TOPOLOGY", '{"namespace": "custom-ns"}')
        reconciler = cp.CiliumPolicyReconciler(_mock_db())
        assert reconciler._topology() == {"namespace": "custom-ns"}


class TestUpsertReadFailures:
    """Tests for CiliumPolicyReconciler._upsert()'s pre-write read failure handling."""

    def test_non_404_api_exception_on_read_degrades_and_skips(self, monkeypatch):
        """A non-404 ApiException on the pre-upsert read degrades and skips that object."""
        from kubernetes.client.rest import ApiException

        monkeypatch.setattr(cp, "is_native_rate_limit_enabled", lambda: True)
        monkeypatch.setattr(
            cp,
            "cilium_capabilities",
            lambda: {"network_policy": True, "envoy_config": False, "available": True},
        )
        client = MagicMock()
        client.get_namespaced_custom_object.side_effect = ApiException(status=500)
        client.get_cluster_custom_object.side_effect = ApiException(status=500)
        monkeypatch.setattr(cp, "get_k8s_custom_objects_client", lambda: client)

        reconciler = cp.CiliumPolicyReconciler(_mock_db(), topology=TOPOLOGY)
        status = reconciler.reconcile()

        assert status.degraded is True
        assert status.applied == []
        assert not client.create_namespaced_custom_object.called
        assert not client.replace_namespaced_custom_object.called

    def test_generic_exception_on_read_degrades_and_skips(self, monkeypatch):
        """A non-ApiException error (e.g. a network error) on read degrades and skips."""
        monkeypatch.setattr(cp, "is_native_rate_limit_enabled", lambda: True)
        monkeypatch.setattr(
            cp,
            "cilium_capabilities",
            lambda: {"network_policy": True, "envoy_config": False, "available": True},
        )
        client = MagicMock()
        client.get_namespaced_custom_object.side_effect = ConnectionError("cluster unreachable")
        client.get_cluster_custom_object.side_effect = ConnectionError("cluster unreachable")
        monkeypatch.setattr(cp, "get_k8s_custom_objects_client", lambda: client)

        reconciler = cp.CiliumPolicyReconciler(_mock_db(), topology=TOPOLOGY)
        status = reconciler.reconcile()

        assert status.degraded is True
        assert status.applied == []
