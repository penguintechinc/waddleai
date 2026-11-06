"""Conformance fixtures: keep fleet backends hermetic (no developer kubeconfig leakage)."""

import pytest


@pytest.fixture(autouse=True)
def _no_k8s_node_lookup(monkeypatch):
    """Report no AI nodes to the GPU co-tenancy guard instead of querying a real cluster.

    The production guard fails closed when nodes cannot be listed, so without this the
    K8s-mode provisioning cases pass only on machines that happen to have a kubeconfig.
    """
    base = "services.management.app.services"
    monkeypatch.setattr(f"{base}.ollama_manager.default_list_nodes", lambda _sel, core=None: [])
    monkeypatch.setattr(f"{base}.llamacpp_manager._list_candidate_nodes", lambda _sel: [])
