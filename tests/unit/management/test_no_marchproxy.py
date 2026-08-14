"""
Guards against reintroduction of the deleted MarchProxy/AILB coupling.

Task 13 (Sec 5.6 deletion inventory) removes services/management's
``ailb.py``, ``ailb_memory.py``, and ``marchproxy_config.py``, the AILB
webhook ingest routes, the ``MARCHPROXY_AILB_*`` env surface, and the
vendored ``marchproxy`` proto stubs under
``proxy/apps/proxy_server/grpc_proto/``. This test asserts those stay gone.

Out of scope (deliberately left alone -- see task report for why):
``services/management/app/services/provider_sync.py``, ``.../grpc/client.py``,
and ``.../api/v1/ollama_models.py`` + ``keys.py``/``providers.py``/
``quotas.py``/``usage_tracker.py``. Those still consume the live
AILB-route-sync client and ``virtual_keys.ailb_*``/``marchproxy_ailb_sync``/
``ailb_usage_events`` columns and tables that Task 14 (migration 007) drops;
deleting their dependencies now would break working functionality before the
schema that redefines it lands.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "module_name",
    [
        "services.management.app.api.v1.ailb",
        "services.management.app.api.v1.ailb_memory",
        "services.management.app.services.marchproxy_config",
    ],
)
def test_deleted_module_is_unimportable(module_name: str) -> None:
    """The deleted AILB modules must not be importable."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


def test_deleted_module_files_absent() -> None:
    """The deleted AILB module files must not exist on disk."""
    base = REPO_ROOT / "services" / "management" / "app"
    assert not (base / "api" / "v1" / "ailb.py").exists()
    assert not (base / "api" / "v1" / "ailb_memory.py").exists()
    assert not (base / "services" / "marchproxy_config.py").exists()


def test_proxy_side_vendored_marchproxy_proto_removed() -> None:
    """The proxy's vendored marchproxy proto stubs (superseded by
    proto/waddleai/v1 in Task 12) must be gone."""
    proxy_marchproxy_dir = (
        REPO_ROOT / "proxy" / "apps" / "proxy_server" / "grpc_proto" / "marchproxy"
    )
    assert not proxy_marchproxy_dir.exists()


def test_api_v1_registry_drops_ailb_imports_and_adds_memory_config() -> None:
    """api/v1/__init__.py no longer imports ailb/ailb_memory, and registers
    the re-homed memory_config module instead."""
    init_path = REPO_ROOT / "services" / "management" / "app" / "api" / "v1" / "__init__.py"
    text = init_path.read_text()
    assert "ailb_memory" not in text
    assert "\n    ailb,\n" not in text
    assert "memory_config" in text


def test_webhooks_module_has_no_ailb_ingest_routes() -> None:
    """webhooks.py keeps the generic verify_webhook_signature() helper but
    drops the AILB usage/health/batch ingest routes and their AILB-table
    writers."""
    webhooks_path = REPO_ROOT / "services" / "management" / "app" / "api" / "v1" / "webhooks.py"
    text = webhooks_path.read_text()
    assert "/webhooks/ailb/usage" not in text
    assert "/webhooks/ailb/health" not in text
    assert "/webhooks/ailb/batch" not in text
    assert "ailb_usage_events" not in text
    assert "def verify_webhook_signature(" in text


def test_config_has_no_marchproxy_ailb_env() -> None:
    """config.py no longer defines the MARCHPROXY_AILB_* env surface."""
    config_path = REPO_ROOT / "services" / "management" / "app" / "config.py"
    text = config_path.read_text()
    assert "MARCHPROXY_AILB" not in text


def test_helm_management_deployment_has_no_marchproxy_ailb_env() -> None:
    """The management Deployment template no longer injects
    MARCHPROXY_AILB_* env vars."""
    helm_path = REPO_ROOT / "k8s" / "helm" / "waddleai" / "templates" / "management-deployment.yaml"
    text = helm_path.read_text()
    assert "MARCHPROXY_AILB" not in text


def test_grpc_server_no_longer_imports_vendored_marchproxy_proto() -> None:
    """grpc_server.py must be rewired to the in-repo proto/waddleai/v1
    package (Task 12), not the deleted grpc_proto.marchproxy stubs."""
    grpc_server_path = REPO_ROOT / "proxy" / "apps" / "proxy_server" / "grpc_server.py"
    text = grpc_server_path.read_text()
    assert "grpc_proto.marchproxy" not in text
    assert "grpc_proto.waddleai" in text
