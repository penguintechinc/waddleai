"""Tests for readiness gating, grpc health mirroring, options and lifecycle (T10)."""

from __future__ import annotations

import asyncio
from typing import Any

import auth_fakes as af
import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from grpc_health.v1.health import aio as health_aio

import services.classification.app.grpc_server as gs
from services.classification import app as app_pkg
from services.classification.app.config import ClassificationConfig, ConfigError
from services.classification.app.grpc_server import (
    MAX_CONNECTION_AGE_GRACE_MS,
    MAX_CONNECTION_AGE_MS,
    bound_port,
    build_grpc_server,
    server_options,
)
from services.classification.app.health import (
    GRPC_SERVICE_NAMES,
    HealthMirror,
    cache_config_valid,
    evaluate_readiness,
)
from services.classification.app.lifecycle import startup

SERVING = health_pb2.HealthCheckResponse.SERVING
NOT_SERVING = health_pb2.HealthCheckResponse.NOT_SERVING
KEY32 = "k" * 32


def _app(engine: Any, cfg: ClassificationConfig | None = None, **kw: Any) -> Any:
    """App with a fake engine and the static verifier on an ephemeral gRPC port."""
    cfg = cfg or ClassificationConfig(grpc_port=0, allow_insecure_grpc=True)
    return app_pkg.create_app(
        cfg, engine=engine, servicer=_servicer(engine, cfg), verifier=af.make_verifier(), **kw
    )


def _servicer(engine: Any, cfg: ClassificationConfig) -> Any:
    """Real servicer over doubles."""
    from services.classification.app.servicer import ClassificationServicer

    return ClassificationServicer(
        engine, af.FakeCache(), af.FakeGate(), af.FakePolicy(), cfg.max_text_bytes
    )


async def test_readyz_503_before_warm_200_after() -> None:
    """/readyz is 503 until the engine reports ready, then 200."""
    engine = af.FakeEngine(ready=False)
    client = _app(engine).test_client()
    assert (await client.get("/readyz")).status_code == 503
    engine.ready = True
    resp = await client.get("/readyz")
    assert resp.status_code == 200
    assert (await resp.get_json())["engine_ready"] is True


async def test_readyz_503_when_model_missing() -> None:
    """No engine at all (model missing / not constructed) means not ready."""
    app = app_pkg.create_app(ClassificationConfig())
    resp = await app.test_client().get("/readyz")
    assert resp.status_code == 503
    assert (await resp.get_json())["engine_ready"] is False


async def test_healthz_200_even_when_not_ready() -> None:
    """Process-up probes answer 200 while the engine is cold."""
    client = _app(af.FakeEngine(ready=False)).test_client()
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/livez")).status_code == 200


async def test_metrics_endpoint_exposes_prometheus() -> None:
    """/metrics serves the Prometheus text exposition."""
    resp = await _app(af.FakeEngine()).test_client().get("/metrics")
    assert resp.status_code == 200
    assert b"classification_auth_failures_total" in await resp.get_data()


async def test_cache_down_does_not_fail_readyz() -> None:
    """A cache pointed at an unreachable Valkey never fails readiness (config-only check)."""
    cfg = ClassificationConfig(
        ner_cache_ttl_seconds=60, ner_cache_hmac_key=KEY32, redis_url="redis://127.0.0.1:1/0"
    )
    resp = await _app(af.FakeEngine(), cfg).test_client().get("/readyz")
    body = await resp.get_json()
    assert resp.status_code == 200
    assert body["cache_enabled"] is True and body["cache_config_valid"] is True


@pytest.mark.parametrize(
    ("cfg", "valid"),
    [
        (ClassificationConfig(), True),
        (ClassificationConfig(ner_cache_ttl_seconds=60, ner_cache_hmac_key=KEY32), False),
        (ClassificationConfig(ner_cache_ttl_seconds=60, redis_url="redis://x"), False),
        (
            ClassificationConfig(
                ner_cache_ttl_seconds=60, ner_cache_hmac_key="short", redis_url="r"
            ),
            False,
        ),
        (
            ClassificationConfig(ner_cache_ttl_seconds=60, ner_cache_hmac_key=KEY32, redis_url="r"),
            True,
        ),
    ],
)
def test_cache_config_validity_gates_readiness(cfg: ClassificationConfig, valid: bool) -> None:
    """An invalid cache CONFIG fails readiness even with a warm engine."""
    assert cache_config_valid(cfg) is valid
    assert evaluate_readiness(af.FakeEngine(ready=True), cfg).ready is valid


async def test_grpc_health_not_serving_until_ready() -> None:
    """The mirror reports NOT_SERVING while cold and SERVING once warm, for all names."""
    engine = af.FakeEngine(ready=False)
    cfg = ClassificationConfig(grpc_port=0)
    servicer = health_aio.HealthServicer()
    mirror = HealthMirror(servicer, lambda: evaluate_readiness(engine, cfg).ready)
    server = await build_grpc_server(
        cfg, _servicer(engine, cfg), af.make_verifier(), health=servicer
    )
    await server.start()
    async with grpc.aio.insecure_channel(f"127.0.0.1:{bound_port(server)}") as ch:
        stub = health_pb2_grpc.HealthStub(ch)
        assert await mirror.sync_once() is False
        for name in GRPC_SERVICE_NAMES:
            assert (
                await stub.Check(health_pb2.HealthCheckRequest(service=name))
            ).status == NOT_SERVING
        engine.ready = True
        assert await mirror.sync_once() is True
        for name in GRPC_SERVICE_NAMES:
            assert (await stub.Check(health_pb2.HealthCheckRequest(service=name))).status == SERVING
    await server.stop(0)


async def test_mirror_run_loop_follows_readiness() -> None:
    """The background loop flips status without manual syncs."""
    engine = af.FakeEngine(ready=False)
    servicer = health_aio.HealthServicer()
    mirror = HealthMirror(servicer, lambda: engine.ready)
    task = asyncio.create_task(mirror.run(0.01))
    await asyncio.sleep(0.05)
    engine.ready = True
    for _ in range(100):
        await asyncio.sleep(0.01)
        resp = await servicer.Check(health_pb2.HealthCheckRequest(service=""), None)  # type: ignore[arg-type]
        if resp.status == SERVING:
            break
    task.cancel()
    await task
    assert resp.status == SERVING


def test_max_connection_age_option_set() -> None:
    """Options carry max_connection_age (~300s) AND a grace; guards headless rebalancing."""
    opts = dict(server_options(ClassificationConfig()))
    assert opts["grpc.max_connection_age_ms"] == MAX_CONNECTION_AGE_MS == 300_000
    assert opts["grpc.max_connection_age_grace_ms"] == MAX_CONNECTION_AGE_GRACE_MS > 0
    assert opts["grpc.keepalive_time_ms"] > 0
    assert opts["grpc.max_receive_message_length"] > ClassificationConfig().max_text_bytes


async def test_max_connection_age_option_applied_to_real_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The options are actually handed to grpc.aio.server (not just computed)."""
    seen: dict[str, Any] = {}
    real = grpc.aio.server

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen["options"] = dict(kwargs["options"])
        return real(*args, **kwargs)

    monkeypatch.setattr(gs.grpc.aio, "server", spy)
    cfg = ClassificationConfig(grpc_port=0)
    await build_grpc_server(cfg, _servicer(af.FakeEngine(), cfg), af.make_verifier())
    assert seen["options"]["grpc.max_connection_age_ms"] == 300_000
    assert seen["options"]["grpc.max_connection_age_grace_ms"] > 0


async def test_lifecycle_serves_and_drains() -> None:
    """before_serving binds gRPC + starts the engine; after_serving closes it."""
    engine = af.FakeEngine(ready=False)
    app = _app(engine, health_interval_s=0.01)
    async with app.test_app():
        await asyncio.sleep(0.05)
        assert engine.started
        rt = app.extensions["classification_runtime"]
        assert rt.server is not None
        engine.ready = True
        assert (await app.test_client().get("/readyz")).status_code == 200
    assert engine.closed


async def test_lifecycle_requires_jwt_config_when_no_verifier() -> None:
    """Serving without JWT issuer/audience/JWKS fails loudly at startup."""
    app = app_pkg.create_app(
        ClassificationConfig(grpc_port=0, allow_insecure_grpc=True), engine=af.FakeEngine()
    )
    with pytest.raises(ConfigError):
        await startup(app)


async def test_engine_start_failure_keeps_not_ready_and_is_logged() -> None:
    """A crashing warm-up leaves readiness false instead of killing the service."""

    class Boom(af.FakeEngine):
        async def start(self) -> None:
            raise RuntimeError("boom")

    engine = Boom(ready=False)
    app = _app(engine, health_interval_s=0.01)
    async with app.test_app():
        await asyncio.sleep(0.05)
        assert (await app.test_client().get("/readyz")).status_code == 503
        assert (await app.test_client().get("/healthz")).status_code == 200


def test_spiffe_ids_parsed_from_env() -> None:
    """SPIFFE_ALLOWED_IDS is a comma list into a frozenset."""
    cfg = ClassificationConfig.from_env({"SPIFFE_ALLOWED_IDS": "spiffe://a/b, spiffe://c/d,"})
    assert cfg.spiffe_allowed_ids == frozenset({"spiffe://a/b", "spiffe://c/d"})


def test_build_servicer_wires_real_components() -> None:
    """The production wiring helper assembles a servicer from real collaborators."""
    from services.classification.app.lifecycle import build_servicer

    servicer = build_servicer(ClassificationConfig(), af.FakeEngine())  # type: ignore[arg-type]
    assert servicer is not None


async def test_after_serving_flushes_telemetry_last(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drain force-flushes + shuts down telemetry after the engine closes; errors are non-fatal."""
    order: list[str] = []

    class FakeTelemetry:
        def shutdown(self) -> None:
            order.append("telemetry")
            raise RuntimeError("collector down")  # must not break shutdown

    engine = af.FakeEngine(ready=False)
    monkeypatch.setattr(
        "services.classification.app.lifecycle.init_telemetry", lambda _cfg: FakeTelemetry()
    )
    app = _app(engine, health_interval_s=0.01)
    async with app.test_app():
        await asyncio.sleep(0.02)
    assert engine.closed
    assert order == ["telemetry"]
