"""
Guards against reintroduction of the deleted MarchProxy/AILB coupling.

Task 13 (Sec 5.6 deletion inventory) removes services/management's
``ailb.py``, ``ailb_memory.py``, and ``marchproxy_config.py``, the AILB
webhook ingest routes, the ``MARCHPROXY_AILB_*`` env surface, and the
vendored ``marchproxy`` proto stubs under
``proxy/apps/proxy_server/grpc_proto/``. This test asserts those stay gone.

Migration 007 follow-up repointed the remaining live consumers of the
dropped ``marchproxy_ailb_sync``/``ailb_usage_events``/``ailb_usage_records``
tables and ``virtual_keys.ailb_*`` columns: ``keys.py``, ``quotas.py``,
``providers.py``, ``services/usage_tracker.py``, ``services/provider_sync.py``
(the AILB provider-config/virtual-key sync methods were removed outright --
no successor bookkeeping table -- while its unrelated Ollama-model-route
sync to the still-live AILB gRPC module was left alone), and
``shared/agents/usage_tracker.py`` (rewritten onto ``token_usage`` via
penguin-dal, organization-scoped). ``.../grpc/client.py`` and
``.../api/v1/ollama_models.py`` are intentionally untouched -- they drive
Ollama model-route sync to the AILB gRPC module, a separate, still-live
integration that migration 007 does not touch.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# Files repointed off the migration-007-dropped tables/columns. No live code
# reference to any of these should remain -- explanatory prose in module
# docstrings/comments naming the old table/column (e.g. "marchproxy_ailb_sync
# had no live caller") is fine and expected; an actual `db.<table>` or
# `.<column>` access is not.
_REPOINTED_FILES = [
    "services/management/app/api/v1/keys.py",
    "services/management/app/api/v1/quotas.py",
    "services/management/app/api/v1/providers.py",
    "services/management/app/services/usage_tracker.py",
    "services/management/app/services/provider_sync.py",
    "shared/agents/usage_tracker.py",
]

# Actual code-access patterns for the dropped tables/columns, not bare
# substrings -- so an explanatory docstring mentioning the name by itself
# doesn't trip this guard, but `db.ailb_usage_events.insert(...)` would.
_DROPPED_ACCESS_PATTERNS = (
    re.compile(r"\bdb\.ailb_usage_records\b"),
    re.compile(r"\bdb\.ailb_usage_events\b"),
    re.compile(r"\bdb\.marchproxy_ailb_sync\b"),
    re.compile(r"\.ailb_key_id\b"),
    re.compile(r"\.ailb_sync_status\b"),
)


@pytest.mark.parametrize("relative_path", _REPOINTED_FILES)
def test_repointed_file_has_no_dropped_table_or_column_references(relative_path: str) -> None:
    """None of the migration-007-dropped tables/columns are accessed as code anymore."""
    text = (REPO_ROOT / relative_path).read_text()
    for pattern in _DROPPED_ACCESS_PATTERNS:
        match = pattern.search(text)
        assert match is None, f"{relative_path} still accesses {match.group(0) if match else ''!r}"


def test_keys_sync_endpoint_removed() -> None:
    """POST /keys/<id>/sync was a bookkeeping-only stub with no successor."""
    text = (REPO_ROOT / "services/management/app/api/v1/keys.py").read_text()
    assert '"/keys/<int:key_id>/sync"' not in text


def test_providers_sync_endpoints_removed() -> None:
    """POST .../sync and GET .../sync-status were bookkeeping-only stubs with no successor."""
    text = (REPO_ROOT / "services/management/app/api/v1/providers.py").read_text()
    assert '"/providers/<int:provider_id>/sync"' not in text
    assert '"/providers/<int:provider_id>/sync-status"' not in text


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
