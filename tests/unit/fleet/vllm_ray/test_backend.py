"""Tests for VllmRayBackend: lifecycle, Serve status reconcile, inventory, link auth."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.fleet.base import BackendType, ManagementScope, ProvisionSpec
from shared.fleet.enums import DeploymentStatus as S
from shared.fleet.enums import GpuVendor, InventoryState
from shared.fleet.idle_teardown import IdleTeardownController
from shared.fleet.vllm_ray import backend as backend_mod
from shared.fleet.vllm_ray.backend import (
    ENV_CONFIG_FILE,
    LINK_AUTH_REFUSAL,
    LinkAuthRefused,
    VllmRayBackend,
)
from shared.fleet.vllm_ray.ray_client import RayClientError
from tests.conformance._fake_dal import FakeDAL
from tests.conformance._fake_ray import FakeRayServeClient

CFG: dict[str, Any] = {
    "head_url": "http://head:8265",
    "serve_base_url": "http://serve:8000",
    "org_id": 7,
    "nccl": {},
}
APP = "vllm-devstral-small"


def _naive_now() -> datetime:
    """Helper: naive now."""
    return datetime.now(UTC).replace(tzinfo=None)


def _insert_dep(db: FakeDAL, **over: Any) -> Any:
    """Helper: insert dep."""
    vals: dict[str, Any] = {
        "public_id": "pid-1",
        "org_id": 7,
        "fleet_backend_id": 1,
        "name": "devstral",
        "slug": "devstral-small",
        "served_model_name": "devstral-small",
        "vendor": int(GpuVendor.NVIDIA),
        "gpu_class": "nvidia-a100-80g",
        "quantization": 0,
        "dtype": "bfloat16",
        "tensor_parallel": 1,
        "pipeline_parallel": 1,
        "replicas_min": 0,
        "replicas_max": 1,
        "max_model_len": 8192,
        "status": int(S.PENDING),
        "serve_app_name": APP,
        "created_at": _naive_now() - timedelta(seconds=30),
    }
    vals.update(over)
    row_id = db.vllm_deployments.insert(**vals)
    return db(db.vllm_deployments.id == row_id).select().first()


def _events(db: FakeDAL, dep: Any) -> list[Any]:
    """Helper: events."""
    return list(db(db.vllm_deployment_events.deployment_id == dep.id).select())


def _row(db: FakeDAL, dep: Any) -> Any:
    """Helper: row."""
    return db(db.vllm_deployments.id == dep.id).select().first()


@pytest.fixture
def db() -> FakeDAL:
    """Helper: db."""
    return FakeDAL()


@pytest.fixture
def client() -> FakeRayServeClient:
    """Helper: client."""
    return FakeRayServeClient()


@pytest.fixture
def canary() -> AsyncMock:
    """Helper: canary."""
    return AsyncMock(return_value=True)


@pytest.fixture(autouse=True)
def _weights_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Helper: weights present."""
    monkeypatch.setattr(VllmRayBackend, "_weights_present", staticmethod(lambda path: True))


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Helper: no env."""
    monkeypatch.delenv(ENV_CONFIG_FILE, raising=False)


def _make(db: FakeDAL, client: Any, canary: Any, cfg: dict[str, Any] | None = None) -> Any:
    """Helper: make."""
    b = VllmRayBackend(db, cfg if cfg is not None else dict(CFG), None, client, canary=canary)
    b.fleet_backend_id = 1
    return b


@pytest.fixture
def backend(db: FakeDAL, client: FakeRayServeClient, canary: AsyncMock) -> VllmRayBackend:
    """Helper: backend."""
    return _make(db, client, canary)


@pytest.fixture
def dep(db: FakeDAL) -> Any:
    """Helper: dep."""
    return _insert_dep(db)


def test_class_attributes() -> None:
    """Check: class attributes."""
    assert VllmRayBackend.type == BackendType.VLLM_RAY
    assert VllmRayBackend.management_scope == ManagementScope.FULL_LIFECYCLE


def test_registered_with_registry() -> None:
    """Check: registered with registry."""
    from shared.fleet.registry import _REGISTRY

    assert _REGISTRY[BackendType.VLLM_RAY] is VllmRayBackend


# --- AM9 defaults file -------------------------------------------------------------


def test_init_loads_configmap_defaults_row_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db: FakeDAL, client: Any
) -> None:
    """Check: init loads configmap defaults row wins."""
    f = tmp_path / "cfg.json"
    f.write_text(
        json.dumps(
            {
                "head_url": "http://default:1",
                "tco": {"kwh_usd": 0.12, "other": 1},
                "worker_groups": [{"gpu_class": "a"}],
            }
        )
    )
    monkeypatch.setenv(ENV_CONFIG_FILE, str(f))
    row_cfg = {**CFG, "tco": {"kwh_usd": 0.5}}
    b = VllmRayBackend(db, row_cfg, None, client)
    assert b.config["head_url"] == "http://head:8265"
    assert b.config["tco"] == {"kwh_usd": 0.5}  # replaced whole, never deep-merged
    assert b.config["worker_groups"] == [{"gpu_class": "a"}]  # untouched default survives
    f.write_text(json.dumps({"tco": {"kwh_usd": 0.12}}))
    b2 = VllmRayBackend(db, dict(CFG), None, client)
    assert b2.config["tco"]["kwh_usd"] == 0.12


def test_init_env_unset_uses_row_only(db: FakeDAL, client: Any) -> None:
    """Check: init env unset uses row only."""
    b = VllmRayBackend(db, dict(CFG), None, client)
    assert set(b.config) - {"model_store_path", "class_map", "kv_headroom_pct"} == set(CFG)


@pytest.mark.parametrize("content", ["not json", "[1, 2]", None])
def test_init_unreadable_defaults_file_raises_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db: FakeDAL, client: Any, content: str | None
) -> None:
    """Check: init unreadable defaults file raises runtime error."""
    f = tmp_path / "cfg.json"
    if content is not None:
        f.write_text(content)
    monkeypatch.setenv(ENV_CONFIG_FILE, str(f))
    with pytest.raises(RuntimeError, match="vLLM Ray config file unreadable"):
        VllmRayBackend(db, dict(CFG), None, client)


@pytest.mark.parametrize("missing", ["head_url", "serve_base_url"])
def test_init_requires_head_and_serve_url(db: FakeDAL, client: Any, missing: str) -> None:
    """Check: init requires head and serve url."""
    cfg = {k: v for k, v in CFG.items() if k != missing}
    with pytest.raises(ValueError, match=missing):
        VllmRayBackend(db, cfg, None, client)


def test_config_defaults_applied(backend: VllmRayBackend) -> None:
    """Check: config defaults applied."""
    assert backend.config["model_store_path"] == "/models"
    assert backend.config["kv_headroom_pct"] == 20
    assert backend.config["class_map"] == {}


def test_lazy_http_client_uses_ray_token(db: FakeDAL, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check: lazy http client uses ray token."""
    b = VllmRayBackend(db, dict(CFG), {"ray_token": "tok-abc"}, None)
    c = b._client()
    assert type(c).__name__ == "HttpRayServeClient"
    assert "tok-abc" not in repr(c)
    assert b._client() is c


def test_credentials_string_is_token(db: FakeDAL) -> None:
    """Check: credentials string is token."""
    b = VllmRayBackend(db, dict(CFG), "plain-token", None)  # type: ignore[arg-type]
    assert type(b._client()).__name__ == "HttpRayServeClient"


def test_credentials_json_string(db: FakeDAL) -> None:
    """Check: credentials json string."""
    b = VllmRayBackend(db, dict(CFG), '{"ray_token": "t"}', None)  # type: ignore[arg-type]
    assert type(b._client()).__name__ == "HttpRayServeClient"


# --- deploy and link auth ----------------------------------------------------------


async def test_deploy_puts_full_desired_set_and_sets_starting(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL, dep: Any
) -> None:
    """Check: deploy puts full desired set and sets starting."""
    await backend.deploy(dep)
    row = _row(db, dep)
    assert row.status == int(S.STARTING)
    assert APP in client.apps
    assert row.endpoint_url == "http://serve:8000/d/devstral-small/v1"
    assert _events(db, dep)[-1].state == int(S.STARTING)


async def test_deploy_includes_other_deployments_and_skips_unready(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL, dep: Any
) -> None:
    """Check: deploy includes other deployments and skips unready."""
    _insert_dep(db, slug="other", serve_app_name="vllm-other", status=int(S.SERVING))
    _insert_dep(db, slug="pend", serve_app_name="vllm-pend", status=int(S.PENDING))
    _insert_dep(db, slug="foreign", serve_app_name="vllm-foreign", fleet_backend_id=2)
    await backend.deploy(dep)
    assert set(client.apps) == {APP, "vllm-other"}


async def test_deploy_missing_weights_marks_failed(
    backend: VllmRayBackend,
    client: FakeRayServeClient,
    db: FakeDAL,
    dep: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check: deploy missing weights marks failed."""
    monkeypatch.setattr(VllmRayBackend, "_weights_present", staticmethod(lambda path: False))
    with pytest.raises(RuntimeError, match="weights not in model store"):
        await backend.deploy(dep)
    assert _row(db, dep).status == int(S.FAILED)
    assert client.puts == []


async def test_deploy_failure_marks_failed_with_detail(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL, dep: Any
) -> None:
    """Check: deploy failure marks failed with detail."""
    client.fail_next_put = RayClientError("HTTP 500: boom")
    with pytest.raises(RuntimeError, match="Ray Serve rejected the deployment") as ei:
        await backend.deploy(dep)
    assert "boom" not in str(ei.value)
    row = _row(db, dep)
    assert row.status == int(S.FAILED)
    assert "HTTP 500" in row.status_detail


async def test_deploy_refuses_multi_node_without_link_auth(
    db: FakeDAL, client: FakeRayServeClient, canary: AsyncMock
) -> None:
    """Check: deploy refuses multi node without link auth."""
    cfg = {**CFG, "link_auth": {"mutual_auth": False, "internal_tls": False}}
    b = _make(db, client, canary, cfg)
    d = _insert_dep(db, pipeline_parallel=2, tensor_parallel=1)
    with pytest.raises(LinkAuthRefused) as ei:
        await b.deploy(d)
    assert str(ei.value) == LINK_AUTH_REFUSAL
    assert LINK_AUTH_REFUSAL == (
        "enable Cilium mutual auth (SPIRE) or Ray internal TLS to deploy across nodes"
    )
    row = _row(db, d)
    assert row.status == int(S.FAILED)
    assert row.status_detail == LINK_AUTH_REFUSAL
    assert client.puts == []


@pytest.mark.parametrize(
    "link_auth",
    [
        {"mutual_auth": True, "internal_tls": False},
        {"mutual_auth": False, "internal_tls": True},
    ],
)
async def test_deploy_multi_node_allowed_with_either_link_auth(
    db: FakeDAL, client: FakeRayServeClient, canary: AsyncMock, link_auth: dict[str, bool]
) -> None:
    """Check: deploy multi node allowed with either link auth."""
    b = _make(db, client, canary, {**CFG, "link_auth": link_auth})
    d = _insert_dep(db, pipeline_parallel=2, tensor_parallel=1)
    await b.deploy(d)
    assert len(client.puts) == 1


async def test_deploy_single_node_needs_no_link_auth(
    db: FakeDAL, client: FakeRayServeClient, canary: AsyncMock, dep: Any
) -> None:
    """Check: deploy single node needs no link auth."""
    b = _make(
        db, client, canary, {**CFG, "link_auth": {"mutual_auth": False, "internal_tls": False}}
    )
    await b.deploy(dep)
    assert len(client.puts) == 1


async def test_link_auth_defaults_true(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL
) -> None:
    """Check: link auth defaults true."""
    d = _insert_dep(db, pipeline_parallel=2)
    await backend.deploy(d)
    assert len(client.puts) == 1


async def test_place_model_applies_same_link_auth_refusal(
    db: FakeDAL, client: FakeRayServeClient, canary: AsyncMock
) -> None:
    """Check: place model applies same link auth refusal."""
    b = _make(
        db, client, canary, {**CFG, "link_auth": {"mutual_auth": False, "internal_tls": False}}
    )
    _insert_dep(db, pipeline_parallel=2, status=int(S.SERVING))
    with pytest.raises(LinkAuthRefused):
        await b.place_model("devstral-small", {"lazy": True})
    assert client.puts == []


# --- reconcile ---------------------------------------------------------------------


def _script(client: FakeRayServeClient, status: str, message: str = "") -> None:
    """Helper: script."""
    client.statuses[APP] = {"status": status, "message": message}


async def test_reconcile_running_warms_then_canary_serves(
    backend: VllmRayBackend, client: FakeRayServeClient, canary: AsyncMock, db: FakeDAL
) -> None:
    """Check: reconcile running warms then canary serves."""
    d = _insert_dep(
        db, status=int(S.STARTING), endpoint_url="http://serve:8000/d/devstral-small/v1"
    )
    _script(client, "RUNNING")
    assert await backend.reconcile(d) == int(S.SERVING)
    assert _row(db, d).status == int(S.SERVING)
    canary.assert_awaited_once_with("http://serve:8000/d/devstral-small/v1", "devstral-small")
    states = [e.state for e in _events(db, d)]
    assert states == [int(S.WARMING), int(S.SERVING)]


async def test_reconcile_canary_failure_stays_warming(
    backend: VllmRayBackend, client: FakeRayServeClient, canary: AsyncMock, db: FakeDAL
) -> None:
    """Check: reconcile canary failure stays warming."""
    canary.return_value = False
    d = _insert_dep(db, status=int(S.STARTING), endpoint_url="http://u/v1")
    _script(client, "RUNNING")
    assert await backend.reconcile(d) == int(S.WARMING)
    assert "canary" in _row(db, d).status_detail
    assert await backend.reconcile(_row(db, d)) == int(S.WARMING)
    assert [e.state for e in _events(db, d)] == [int(S.WARMING)]


async def test_reconcile_warming_canary_pass_serves(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL
) -> None:
    """Check: reconcile warming canary pass serves."""
    d = _insert_dep(db, status=int(S.WARMING), endpoint_url="http://u/v1")
    _script(client, "RUNNING")
    assert await backend.reconcile(d) == int(S.SERVING)


async def test_reconcile_running_serving_is_noop(
    backend: VllmRayBackend, client: FakeRayServeClient, canary: AsyncMock, db: FakeDAL
) -> None:
    """Check: reconcile running serving is noop."""
    d = _insert_dep(db, status=int(S.SERVING), endpoint_url="http://u/v1")
    _script(client, "RUNNING")
    assert await backend.reconcile(d) == int(S.SERVING)
    canary.assert_not_awaited()
    assert _events(db, d) == []


async def test_reconcile_degraded_recovers_via_canary(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL
) -> None:
    """Check: reconcile degraded recovers via canary."""
    d = _insert_dep(db, status=int(S.DEGRADED), endpoint_url="http://u/v1")
    _script(client, "RUNNING")
    assert await backend.reconcile(d) == int(S.SERVING)
    assert _events(db, d)[-1].message == "recovered"


async def test_reconcile_degraded_canary_failure_stays_degraded(
    backend: VllmRayBackend, client: FakeRayServeClient, canary: AsyncMock, db: FakeDAL
) -> None:
    """Check: reconcile degraded canary failure stays degraded."""
    canary.return_value = False
    d = _insert_dep(db, status=int(S.DEGRADED), endpoint_url="http://u/v1")
    _script(client, "RUNNING")
    assert await backend.reconcile(d) == int(S.DEGRADED)


@pytest.mark.parametrize("serve_status", ["DEPLOYING", "NOT_STARTED"])
async def test_reconcile_deploying_maps_to_starting(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL, serve_status: str
) -> None:
    """Check: reconcile deploying maps to starting."""
    d = _insert_dep(db, status=int(S.WARMING))
    _script(client, serve_status)
    assert await backend.reconcile(d) == int(S.STARTING)


@pytest.mark.parametrize("cur", [S.SERVING, S.DEGRADED, S.STOPPED])
async def test_reconcile_deploying_leaves_live_states(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL, cur: S
) -> None:
    """Check: reconcile deploying leaves live states."""
    d = _insert_dep(db, status=int(cur))
    _script(client, "DEPLOYING")
    assert await backend.reconcile(d) == int(cur)
    assert _events(db, d) == []


async def test_reconcile_deploy_failed_after_serving_is_degraded_with_message(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL
) -> None:
    """Check: reconcile deploy failed after serving is degraded with message."""
    d = _insert_dep(db, status=int(S.SERVING), endpoint_url="http://u/v1")
    _script(client, "DEPLOY_FAILED", "CUDA out of memory...")
    assert await backend.reconcile(d) == int(S.DEGRADED)
    row = _row(db, d)
    assert row.status_detail == "CUDA out of memory..."
    ev = _events(db, d)[-1]
    assert ev.state == int(S.DEGRADED)
    assert ev.message == "CUDA out of memory..."


async def test_reconcile_deploy_failed_otherwise_is_failed(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL
) -> None:
    """Check: reconcile deploy failed otherwise is failed."""
    d = _insert_dep(db, status=int(S.STARTING))
    _script(client, "DEPLOY_FAILED", "bad config")
    assert await backend.reconcile(d) == int(S.FAILED)
    assert _row(db, d).status_detail == "bad config"


async def test_reconcile_unhealthy_serving_is_degraded(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL
) -> None:
    """Check: reconcile unhealthy serving is degraded."""
    d = _insert_dep(db, status=int(S.SERVING))
    _script(client, "UNHEALTHY", "replica died")
    assert await backend.reconcile(d) == int(S.DEGRADED)


async def test_reconcile_unknown_serve_status_is_unchanged(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL
) -> None:
    """Check: reconcile unknown serve status is unchanged."""
    d = _insert_dep(db, status=int(S.SERVING))
    _script(client, "DELETING")
    assert await backend.reconcile(d) == int(S.SERVING)


async def test_degraded_endpoint_is_not_offered(backend: VllmRayBackend, db: FakeDAL) -> None:
    """Check: degraded endpoint is not offered."""
    _insert_dep(db, status=int(S.DEGRADED), endpoint_url="http://u/v1")
    assert await backend.endpoints_for("devstral-small") == []


async def test_serving_endpoint_is_offered(backend: VllmRayBackend, db: FakeDAL) -> None:
    """Check: serving endpoint is offered."""
    _insert_dep(db, status=int(S.SERVING), endpoint_url="http://u/v1")
    _insert_dep(
        db,
        slug="x",
        serve_app_name="vllm-x",
        served_model_name="x",
        status=int(S.SERVING),
        endpoint_url="http://x/v1",
    )
    _insert_dep(db, slug="y", serve_app_name="vllm-y", status=int(S.SERVING))  # no url
    eps = await backend.endpoints_for("devstral-small")
    assert len(eps) == 1
    assert eps[0].healthy is True
    assert eps[0].url == "http://u/v1"
    assert eps[0].node_id == APP
    assert eps[0].loaded_models == ["devstral-small"]


async def test_endpoints_ignore_other_backends(backend: VllmRayBackend, db: FakeDAL) -> None:
    """Check: endpoints ignore other backends."""
    _insert_dep(db, status=int(S.SERVING), endpoint_url="http://u/v1", fleet_backend_id=2)
    assert await backend.endpoints_for("devstral-small") == []


async def test_reconcile_not_found_when_not_stopped_is_failed(
    backend: VllmRayBackend, db: FakeDAL
) -> None:
    """Check: reconcile not found when not stopped is failed."""
    d = _insert_dep(db, status=int(S.STARTING))
    assert await backend.reconcile(d) == int(S.FAILED)
    assert _row(db, d).status_detail == "Serve application missing"


@pytest.mark.parametrize("cur", [S.PENDING, S.PULLING])
async def test_reconcile_not_found_before_submit_is_unchanged(
    backend: VllmRayBackend, db: FakeDAL, cur: S
) -> None:
    """Check: reconcile not found before submit is unchanged."""
    d = _insert_dep(db, status=int(cur))
    assert await backend.reconcile(d) == int(cur)


async def test_reconcile_stopped_not_found_is_unchanged(
    backend: VllmRayBackend, db: FakeDAL
) -> None:
    """Check: reconcile stopped not found is unchanged."""
    d = _insert_dep(db, status=int(S.STOPPED))
    assert await backend.reconcile(d) == int(S.STOPPED)
    assert _events(db, d) == []


async def test_reconcile_appends_event_only_on_change(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL
) -> None:
    """Check: reconcile appends event only on change."""
    d = _insert_dep(db, status=int(S.STARTING), endpoint_url="http://u/v1")
    _script(client, "RUNNING")
    await backend.reconcile(d)
    await backend.reconcile(_row(db, d))
    assert len(_events(db, d)) == 2  # WARMING + SERVING once; second pass is a no-op


async def test_observes_model_load_histogram_on_serving(
    backend: VllmRayBackend,
    client: FakeRayServeClient,
    db: FakeDAL,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check: observes model load histogram on serving."""
    obs = MagicMock()
    monkeypatch.setattr(backend_mod, "_observe_model_load_seconds", obs)
    d = _insert_dep(db, status=int(S.STARTING), endpoint_url="http://u/v1")
    _script(client, "RUNNING")
    await backend.reconcile(d)
    await backend.reconcile(_row(db, d))
    obs.assert_called_once()
    assert obs.call_args.args[0] >= 29


def test_observe_model_load_seconds_records_on_meter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check: observe model load seconds records on meter."""
    hist = MagicMock()
    meter = MagicMock()
    meter.create_histogram.return_value = hist
    monkeypatch.setattr(backend_mod, "_load_histogram", None)
    import shared.observability.metrics as m

    monkeypatch.setattr(m, "get_meter", lambda: meter)
    backend_mod._observe_model_load_seconds(12.5)
    backend_mod._observe_model_load_seconds(1.0)
    assert meter.create_histogram.call_args.args[0] == "waddleai_vllm_model_load_seconds"
    assert hist.record.call_count == 2


# --- delete, stop, scale, place ----------------------------------------------------


async def test_delete_removes_serve_app_and_endpoint_immediately(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL
) -> None:
    """Check: delete removes serve app and endpoint immediately."""
    d = _insert_dep(db, status=int(S.SERVING), endpoint_url="http://u/v1")
    await backend.deploy(_row(db, d))
    await backend.delete(_row(db, d))
    assert APP not in client.apps
    row = _row(db, d)
    assert row.endpoint_url is None
    assert row.status == int(S.STOPPED)
    assert await backend.endpoints_for("devstral-small") == []
    assert _events(db, d)[-1].message == "deleted"


async def test_delete_keeps_other_deployments_in_put(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL
) -> None:
    """Check: delete keeps other deployments in put."""
    d = _insert_dep(db, status=int(S.SERVING), endpoint_url="http://u/v1")
    _insert_dep(
        db, slug="o", serve_app_name="vllm-o", status=int(S.SERVING), endpoint_url="http://o/v1"
    )
    await backend.delete(d)
    assert set(client.apps) == {"vllm-o"}


async def test_delete_ray_failure_raises_plain_error(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL
) -> None:
    """Check: delete ray failure raises plain error."""
    d = _insert_dep(db, status=int(S.SERVING), endpoint_url="http://u/v1")
    client.fail_next_put = RayClientError("HTTP 500: secret-body")
    with pytest.raises(RuntimeError, match="Ray Serve rejected the request") as ei:
        await backend.delete(d)
    assert "secret-body" not in str(ei.value)


async def test_stop_keeps_app_with_min_replicas_zero(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL
) -> None:
    """Check: stop keeps app with min replicas zero."""
    d = _insert_dep(db, status=int(S.SERVING), replicas_min=2, endpoint_url="http://u/v1")
    await backend.stop(d)
    assert _row(db, d).status == int(S.STOPPED)
    cfg = client.apps[APP]["args"]["llm_config"]["deployment_config"]["autoscaling_config"]
    assert cfg["min_replicas"] == 0
    assert _events(db, d)[-1].state == int(S.STOPPED)


async def test_scale_updates_replicas_and_reputs(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL
) -> None:
    """Check: scale updates replicas and reputs."""
    d = _insert_dep(db, status=int(S.SERVING), endpoint_url="http://u/v1")
    await backend.scale(d, 1, 4)
    row = _row(db, d)
    assert (row.replicas_min, row.replicas_max) == (1, 4)
    cfg = client.puts[-1]["applications"][0]["args"]["llm_config"]["deployment_config"]
    assert cfg["autoscaling_config"]["max_replicas"] == 4


@pytest.mark.parametrize("bounds", [(-1, 2), (3, 2), (0, 0)])
async def test_scale_rejects_bad_bounds(
    backend: VllmRayBackend, db: FakeDAL, bounds: tuple[int, int]
) -> None:
    """Check: scale rejects bad bounds."""
    d = _insert_dep(db, status=int(S.SERVING))
    with pytest.raises(ValueError):
        await backend.scale(d, *bounds)


async def test_place_model_lazy_without_serving_deployment_raises(backend: VllmRayBackend) -> None:
    """Check: place model lazy without serving deployment raises."""
    with pytest.raises(RuntimeError, match="Deploy it from the GPU Fleet page"):
        await backend.place_model("devstral-small", {"lazy": True})


async def test_place_model_returns_placed(backend: VllmRayBackend, db: FakeDAL) -> None:
    """Check: place model returns placed."""
    _insert_dep(db, status=int(S.SERVING), endpoint_url="http://u/v1")
    p = await backend.place_model("devstral-small", {})
    assert (p.model, p.node_id, p.status) == ("devstral-small", APP, "placed")


# --- provision ---------------------------------------------------------------------


async def test_provision_deploys_listed_deployments(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL, dep: Any
) -> None:
    """Check: provision deploys listed deployments."""
    spec = ProvisionSpec("n", ["m"], "x", {"deployment_public_ids": ["pid-1"]})
    assert await backend.provision(spec) == []
    assert APP in client.apps


async def test_provision_unknown_deployment_raises(backend: VllmRayBackend) -> None:
    """Check: provision unknown deployment raises."""
    spec = ProvisionSpec("n", ["m"], "x", {"deployment_public_ids": ["nope"]})
    with pytest.raises(ValueError, match="unknown deployment"):
        await backend.provision(spec)


async def test_provision_without_ids_raises(backend: VllmRayBackend) -> None:
    """Check: provision without ids raises."""
    with pytest.raises(ValueError, match="deployment_public_ids"):
        await backend.provision(ProvisionSpec("n", [], "x", {}))


async def test_deprovision_unsupported(backend: VllmRayBackend) -> None:
    """Check: deprovision unsupported."""
    with pytest.raises(PermissionError, match="deprovision is unsupported"):
        await backend.deprovision("n/c")


# --- nodes, health -----------------------------------------------------------------


def _gpu(db: FakeDAL, **over: Any) -> None:
    """Helper: gpu."""
    vals: dict[str, Any] = {
        "fleet_backend_id": 1,
        "node_uid": "u1",
        "node_name": "n1",
        "gpu_index": 0,
        "vendor": int(GpuVendor.NVIDIA),
        "gpu_class": "nvidia-a100-80g",
        "vram_total_mb": 81920,
        "vram_used_mb": 1000,
        "schedulable": True,
        "state": int(InventoryState.WARM),
        "last_seen": _naive_now(),
    }
    vals.update(over)
    db.gpu_inventory.insert(**vals)


async def test_list_nodes_groups_by_node_and_class(backend: VllmRayBackend, db: FakeDAL) -> None:
    """Check: list nodes groups by node and class."""
    _gpu(db, gpu_index=0)
    _gpu(db, gpu_index=1, vram_used_mb=2000)
    _gpu(db, gpu_index=2, gpu_class="nvidia-l4-24g", vram_total_mb=24576, vram_used_mb=0)
    _gpu(db, node_uid="u9", fleet_backend_id=2)
    _insert_dep(db, status=int(S.SERVING), endpoint_url="http://u/v1")
    nodes = await backend.list_nodes()
    assert len(nodes) == 2
    a100 = next(n for n in nodes if n.gpu_class == "nvidia-a100-80g")
    assert a100.node_uid == "u1"
    assert a100.node_id == "n1/nvidia-a100-80g"
    assert a100.vram_total_mb == 2 * 81920
    assert a100.vram_free_mb == (81920 - 1000) + (81920 - 2000)
    assert a100.healthy is True
    assert a100.kind == "k8s"
    assert a100.gpu_vendor == int(GpuVendor.NVIDIA)
    assert a100.loaded_models == ["devstral-small"]
    l4 = next(n for n in nodes if n.gpu_class == "nvidia-l4-24g")
    assert l4.node_uid == "u1"
    assert l4.loaded_models == []


async def test_list_nodes_unschedulable_excluded_from_free(
    backend: VllmRayBackend, db: FakeDAL
) -> None:
    """Check: list nodes unschedulable excluded from free."""
    _gpu(db, gpu_index=0)
    _gpu(db, gpu_index=1, schedulable=False)
    (n,) = await backend.list_nodes()
    assert n.vram_free_mb == 81920 - 1000


async def test_list_nodes_stale_rows_unhealthy(backend: VllmRayBackend, db: FakeDAL) -> None:
    """Check: list nodes stale rows unhealthy."""
    _gpu(db, last_seen=_naive_now() - timedelta(seconds=400))
    _gpu(db, node_uid="u2", node_name="n2", state=int(InventoryState.COLD))
    nodes = await backend.list_nodes()
    assert [n.healthy for n in nodes] == [False, False]
    assert {n.node_id for n in nodes} == {"n1/nvidia-a100-80g", "n2/nvidia-a100-80g"}


async def test_list_nodes_unclassified_gpu(backend: VllmRayBackend, db: FakeDAL) -> None:
    """Check: list nodes unclassified gpu."""
    _gpu(db, gpu_class=None, last_seen=None)
    (n,) = await backend.list_nodes()
    assert n.node_id == "n1/unclassified"
    assert n.healthy is False


async def test_health_counts_deployments(backend: VllmRayBackend, db: FakeDAL) -> None:
    """Check: health counts deployments."""
    _gpu(db)
    _insert_dep(db, status=int(S.SERVING), endpoint_url="http://u/v1")
    _insert_dep(db, slug="b", serve_app_name="b", status=int(S.SERVING))
    h = await backend.health()
    assert h.healthy is True
    assert h.backend_id == 1
    assert h.node_count == 1
    assert h.detail == {"deployments": {"SERVING": 2}}


async def test_health_unhealthy_without_warm_nodes(backend: VllmRayBackend) -> None:
    """Check: health unhealthy without warm nodes."""
    h = await backend.health()
    assert h.healthy is False
    assert h.node_count == 0


async def test_health_reports_unreachable_ray_without_raising(
    backend: VllmRayBackend, client: FakeRayServeClient, db: FakeDAL
) -> None:
    """Check: health reports unreachable ray without raising."""
    _gpu(db)
    client.fail_next_get = RayClientError("ray GET timeout")
    h = await backend.health()
    assert h.healthy is False
    assert h.detail == {"error": "ray head unreachable"}


# --- inventory ---------------------------------------------------------------------


def _payload(uid: str = "u1", name: str = "n1") -> dict[str, Any]:
    """Helper: payload."""
    raw = "0, NVIDIA A100-SXM4-80GB, 81920, 1000, 00000000:00:04.0"
    return {"node_uid": uid, "node_name": name, "vendor": "nvidia", "raw": raw}


async def test_collect_inventory_upserts(
    backend: VllmRayBackend, client: FakeRayServeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check: collect inventory upserts."""
    up = AsyncMock()
    monkeypatch.setattr(backend_mod, "upsert_inventory", up)
    client.inventory_payloads = [_payload()]
    recs = await backend.collect_inventory()
    assert len(recs) == 1
    up.assert_awaited_once()
    assert up.await_args is not None
    args, kwargs = up.await_args
    assert args[1:3] == (1, 7)
    assert args[3] == recs
    assert kwargs["skip_node_uids"] == frozenset()


async def test_collect_inventory_empty_payloads_returns_empty_and_does_not_sweep(
    backend: VllmRayBackend,
    client: FakeRayServeClient,
    db: FakeDAL,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Check: collect inventory empty payloads returns empty and does not sweep."""
    _gpu(db, node_uid="u1")
    _gpu(db, node_uid="u2", node_name="n2")
    up = AsyncMock()
    monkeypatch.setattr(backend_mod, "upsert_inventory", up)
    client.inventory_payloads = []
    with caplog.at_level(logging.WARNING):
        assert await backend.collect_inventory() == []
    assert "no GPU inventory" in caplog.text
    # incomplete + empty: every known node is skipped so only the age sweep can act
    assert up.await_args is not None
    assert up.await_args.kwargs["skip_node_uids"] == frozenset({"u1", "u2"})


async def test_collect_inventory_passes_ray_node_list_as_authoritative(
    backend: VllmRayBackend, client: FakeRayServeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trusted Ray node list reaches upsert_inventory and collect drops off-list payloads."""
    up = AsyncMock()
    monkeypatch.setattr(backend_mod, "upsert_inventory", up)
    client.inventory_payloads = [_payload("u1"), _payload("rogue", "n-rogue")]
    client.alive_node_ids = ["u1"]
    recs = await backend.collect_inventory()
    assert [r.node_uid for r in recs] == ["u1"]
    assert up.await_args is not None
    assert up.await_args.kwargs["authoritative_node_ids"] == ["u1"]


async def test_collect_inventory_node_list_failure_marks_cycle_incomplete(
    backend: VllmRayBackend,
    client: FakeRayServeClient,
    db: FakeDAL,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RayClientError on the list: log, authoritative=None, every known node skipped (no sweep)."""
    _gpu(db, node_uid="u9", node_name="n9")
    up = AsyncMock()
    monkeypatch.setattr(backend_mod, "upsert_inventory", up)
    client.inventory_payloads = [_payload("u1")]
    client.fail_next_nodes = RayClientError("nodes down")
    with caplog.at_level(logging.WARNING):
        recs = await backend.collect_inventory()
    assert len(recs) == 1
    assert "node list unavailable" in caplog.text
    assert up.await_args is not None
    assert up.await_args.kwargs["authoritative_node_ids"] is None
    assert "u9" in up.await_args.kwargs["skip_node_uids"]


async def test_collect_inventory_requires_org_id(db: FakeDAL, client: Any) -> None:
    """Check: collect inventory requires org id."""
    cfg = {k: v for k, v in CFG.items() if k != "org_id"}
    b = VllmRayBackend(db, cfg, None, client)
    with pytest.raises(ValueError, match="org_id"):
        await b.collect_inventory()


async def test_collect_inventory_complete_empty_still_upserts(
    backend: VllmRayBackend, client: FakeRayServeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check: collect inventory complete empty still upserts."""
    up = AsyncMock()
    monkeypatch.setattr(backend_mod, "upsert_inventory", up)
    client.inventory_payloads = [
        {"node_uid": "u1", "node_name": "n1", "vendor": "nvidia", "raw": "", "allocatable_gpus": 0}
    ]
    assert await backend.collect_inventory() == []
    assert up.await_args is not None
    assert up.await_args.kwargs["skip_node_uids"] == frozenset()


# --- security / teardown -----------------------------------------------------------


async def test_no_token_in_logs(
    db: FakeDAL, canary: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Check: no token in logs."""
    client = FakeRayServeClient()
    b = VllmRayBackend(db, dict(CFG), {"ray_token": "SECRET123"}, client, canary=canary)
    b.fleet_backend_id = 1
    d = _insert_dep(db)
    client.fail_next_put = RayClientError("boom")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError):
            await b.deploy(d)
        await b.health()
    assert "SECRET123" not in caplog.text
    assert "SECRET123" not in repr(b)


async def test_idle_teardown_sweep_never_deprovisions_vllm_ray_nodes(
    backend: VllmRayBackend, db: FakeDAL, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check: idle teardown sweep never deprovisions vllm ray nodes."""
    _gpu(db)
    backend.deprovision = AsyncMock()  # type: ignore[method-assign]
    clock = [1000.0]
    ctl = IdleTeardownController(db, None, AsyncMock(), idle_seconds=10, clock=lambda: clock[0])
    await ctl.sweep([backend])
    clock[0] += 10_000
    stopped = await ctl.sweep([backend])
    assert stopped == []
    backend.deprovision.assert_not_awaited()


async def test_idle_teardown_ensure_deployed_never_provisions_vllm_ray(
    backend: VllmRayBackend,
) -> None:
    """Check: idle teardown ensure deployed never provisions vllm ray."""
    backend.provision = AsyncMock()  # type: ignore[method-assign]
    ctl = IdleTeardownController(None, None, AsyncMock())
    assert await ctl.ensure_deployed(backend, "devstral-small") is False
    backend.provision.assert_not_awaited()


# --- default canary ----------------------------------------------------------------


class _Resp:
    """Test double: Resp."""

    def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
        """Helper: init  ."""
        self.status_code = status
        self._body = body or {}

    def json(self) -> dict[str, Any]:
        """Helper: json."""
        return self._body


class _FakeHttp:
    """Test double: FakeHttp."""

    def __init__(self, models: _Resp, chat: _Resp | Exception) -> None:
        """Helper: init  ."""
        self.models, self.chat = models, chat
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __aenter__(self) -> _FakeHttp:
        """Helper: aenter  ."""
        return self

    async def __aexit__(self, *a: Any) -> None:
        """Helper: aexit  ."""
        return None

    async def get(self, url: str, **kw: Any) -> _Resp:
        """Helper: get."""
        self.calls.append(("GET", url, kw))
        return self.models

    async def post(self, url: str, **kw: Any) -> _Resp:
        """Helper: post."""
        self.calls.append(("POST", url, kw))
        if isinstance(self.chat, Exception):
            raise self.chat
        return self.chat


def _patch_http(monkeypatch: pytest.MonkeyPatch, fake: _FakeHttp) -> None:
    """Helper: patch http."""
    monkeypatch.setattr(backend_mod.httpx, "AsyncClient", lambda **kw: fake)


async def test_default_canary_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check: default canary pass."""
    fake = _FakeHttp(_Resp(200, {"data": [{"id": "m"}]}), _Resp(200))
    _patch_http(monkeypatch, fake)
    assert await backend_mod._default_canary("http://u/v1", "m") is True
    assert fake.calls[0][1] == "http://u/v1/models"
    assert fake.calls[1][1] == "http://u/v1/chat/completions"
    assert fake.calls[1][2]["json"]["max_tokens"] == 4


@pytest.mark.parametrize(
    "models,chat",
    [
        (_Resp(500), _Resp(200)),
        (_Resp(200, {"data": [{"id": "other"}]}), _Resp(200)),
        (_Resp(200, {"data": [{"id": "m"}]}), _Resp(503)),
        (_Resp(200, {"data": [{"id": "m"}]}), backend_mod.httpx.ConnectError("x")),
    ],
)
async def test_default_canary_failures(
    monkeypatch: pytest.MonkeyPatch, models: _Resp, chat: _Resp | Exception
) -> None:
    """Check: default canary failures."""
    _patch_http(monkeypatch, _FakeHttp(models, chat))
    assert await backend_mod._default_canary("http://u/v1", "m") is False


async def test_default_canary_used_when_none_injected(
    db: FakeDAL, client: FakeRayServeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check: default canary used when none injected."""
    b = VllmRayBackend(db, dict(CFG), None, client)
    assert b._canary is backend_mod._default_canary
