"""Tests for `server.grpc_health.GrpcHealthManager` (standard grpc.health.v1.Health wiring).

Covers: the kill-switch gating registration, the startup NOT_SERVING seed
followed by the real-status transition once `mark_started()` runs,
per-service dependency derivation (knowledge/lessons tracking the shared db
pool's open/closed state), the shutdown-drain permanent NOT_SERVING flip,
the emitted OTel transition counter, and a real local insecure-channel
`Check`/`Watch` round trip against the standard `grpc_health.v1.health_pb2_grpc`
stub -- proving the servicer is wire-compatible with an actual gRPC health
client, not just the Python object directly.

# regression: penguincode-ops-audit (Helm PR #264 nativeGrpcProbe had no server-side grpc.health.v1.Health registration)
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.util._once import Once

from penguincode_cli.db import pool as db_pool
from penguincode_cli.observability import otel
from penguincode_cli.server.grpc_health import (
    CHAT_SERVICE,
    KNOWLEDGE_SERVICE,
    LESSONS_SERVICE,
    OVERALL_SERVICE,
    GrpcHealthManager,
)

_SERVING = health_pb2.HealthCheckResponse.SERVING
_NOT_SERVING = health_pb2.HealthCheckResponse.NOT_SERVING


@pytest.fixture(autouse=True)
def _clean_flag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PENGUINCODE_FLAG_DISABLE_GRPC_HEALTH_SERVICE", raising=False)


@pytest.fixture(autouse=True)
def _reset_shared_pool() -> Iterator[None]:
    """Every test starts and ends with no shared db pool open -- no cross-test leakage."""
    db_pool.close_pool()
    yield
    db_pool.close_pool()


@pytest.fixture(autouse=True)
def _reset_otel_module_state() -> Iterator[None]:
    otel.reset_for_testing()
    yield
    otel.reset_for_testing()


@pytest.fixture
def in_memory_metric_reader() -> Iterator[InMemoryMetricReader]:
    """Install a real in-memory MeterProvider so counter emission is provable."""
    prev_provider = otel_metrics._internal._METER_PROVIDER
    prev_once = otel_metrics._internal._METER_PROVIDER_SET_ONCE

    reader = InMemoryMetricReader()
    otel_metrics._internal._METER_PROVIDER = None
    otel_metrics._internal._METER_PROVIDER_SET_ONCE = Once()
    otel_metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))

    try:
        yield reader
    finally:
        otel_metrics._internal._METER_PROVIDER = prev_provider
        otel_metrics._internal._METER_PROVIDER_SET_ONCE = prev_once


def _counter_points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    out: list[Any] = []
    for rm in getattr(data, "resource_metrics", []) or []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    out.extend(m.data.data_points)
    return out


def _open_fake_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mark the shared pool as open without a real Postgres connection.

    `is_pool_open()` only checks the module-level `_shared_pool is not None`
    sentinel, so a `MagicMock` standing in for the real `ConnectionPool` is
    sufficient -- no network/DB dependency for these tests. Must support
    `.close()` (a plain `object()` does not) since the `_reset_shared_pool`
    autouse fixture calls `db_pool.close_pool()` at teardown regardless.
    """
    monkeypatch.setattr(db_pool, "_shared_pool", MagicMock())


async def _registered_manager() -> GrpcHealthManager:
    manager = GrpcHealthManager()
    manager.register(grpc.aio.server())
    return manager


class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_register_installs_the_servicer_by_default(self) -> None:
        manager = GrpcHealthManager()
        manager.register(grpc.aio.server())
        assert manager.registered is True

    @pytest.mark.asyncio
    async def test_kill_switch_skips_registration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_GRPC_HEALTH_SERVICE", "true")
        manager = GrpcHealthManager()
        manager.register(grpc.aio.server())
        assert manager.registered is False

    @pytest.mark.asyncio
    async def test_kill_switch_makes_every_mark_call_a_no_op(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_GRPC_HEALTH_SERVICE", "true")
        manager = GrpcHealthManager()
        manager.register(grpc.aio.server())

        # None of these may raise, and the underlying servicer's own default
        # ("" -> SERVING from HealthServicer.__init__) must stay untouched.
        await manager.mark_starting()
        await manager.mark_started()
        await manager.mark_draining()
        assert manager._servicer._server_status[""] == _SERVING


class TestStartupLifecycle:
    @pytest.mark.asyncio
    async def test_mark_starting_seeds_every_tracked_service_not_serving(self) -> None:
        manager = await _registered_manager()
        await manager.mark_starting()

        status = manager._servicer._server_status
        for service in (OVERALL_SERVICE, KNOWLEDGE_SERVICE, LESSONS_SERVICE, CHAT_SERVICE):
            assert status[service] == _NOT_SERVING

    @pytest.mark.asyncio
    async def test_mark_started_with_pool_open_sets_everything_serving(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _open_fake_pool(monkeypatch)
        manager = await _registered_manager()
        await manager.mark_starting()
        await manager.mark_started()

        status = manager._servicer._server_status
        assert status[KNOWLEDGE_SERVICE] == _SERVING
        assert status[LESSONS_SERVICE] == _SERVING
        assert status[CHAT_SERVICE] == _SERVING
        assert status[OVERALL_SERVICE] == _SERVING

    @pytest.mark.asyncio
    async def test_mark_started_with_pool_closed_knowledge_and_lessons_not_serving(self) -> None:
        """db.pool never opened (e.g. startup failed before open_pool) -> dependents NOT_SERVING."""
        manager = await _registered_manager()
        await manager.mark_starting()
        await manager.mark_started()

        status = manager._servicer._server_status
        assert status[KNOWLEDGE_SERVICE] == _NOT_SERVING
        assert status[LESSONS_SERVICE] == _NOT_SERVING
        # Chat has no tracked dependency here, so it still comes up.
        assert status[CHAT_SERVICE] == _SERVING
        # Overall is NOT_SERVING because not every per-service entry is SERVING.
        assert status[OVERALL_SERVICE] == _NOT_SERVING


class TestRefreshDependencyHealth:
    @pytest.mark.asyncio
    async def test_refresh_follows_pool_closing_after_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _open_fake_pool(monkeypatch)
        manager = await _registered_manager()
        await manager.mark_starting()
        await manager.mark_started()
        assert manager._servicer._server_status[KNOWLEDGE_SERVICE] == _SERVING

        # Pool goes down mid-run (e.g. close_pool() called, or the pool was
        # never reopened after a crash) -- a fresh refresh must reflect it.
        monkeypatch.setattr(db_pool, "_shared_pool", None)
        await manager.refresh_dependency_health()

        status = manager._servicer._server_status
        assert status[KNOWLEDGE_SERVICE] == _NOT_SERVING
        assert status[LESSONS_SERVICE] == _NOT_SERVING
        assert status[OVERALL_SERVICE] == _NOT_SERVING


class TestShutdownDrain:
    @pytest.mark.asyncio
    async def test_mark_draining_sets_every_service_not_serving(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _open_fake_pool(monkeypatch)
        manager = await _registered_manager()
        await manager.mark_starting()
        await manager.mark_started()
        assert manager._servicer._server_status[OVERALL_SERVICE] == _SERVING

        await manager.mark_draining()

        status = manager._servicer._server_status
        for service in (OVERALL_SERVICE, KNOWLEDGE_SERVICE, LESSONS_SERVICE, CHAT_SERVICE):
            assert status[service] == _NOT_SERVING

    @pytest.mark.asyncio
    async def test_mark_draining_permanently_ignores_further_set_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`enter_graceful_shutdown()` makes later `set()` calls no-ops -- draining is one-way."""
        _open_fake_pool(monkeypatch)
        manager = await _registered_manager()
        await manager.mark_starting()
        await manager.mark_started()
        await manager.mark_draining()

        # A spurious refresh after drain must not resurrect any service.
        await manager.refresh_dependency_health()

        status = manager._servicer._server_status
        for service in (OVERALL_SERVICE, KNOWLEDGE_SERVICE, LESSONS_SERVICE, CHAT_SERVICE):
            assert status[service] == _NOT_SERVING


class TestTelemetry:
    @pytest.mark.asyncio
    async def test_transitions_emit_bounded_counter_points(
        self, monkeypatch: pytest.MonkeyPatch, in_memory_metric_reader: InMemoryMetricReader
    ) -> None:
        _open_fake_pool(monkeypatch)
        manager = await _registered_manager()
        await manager.mark_starting()
        await manager.mark_started()

        points = _counter_points(in_memory_metric_reader, "penguincode.health.status_transitions")
        assert len(points) > 0
        labels = {(p.attributes["service"], p.attributes["status"]) for p in points}
        assert ("overall", "not_serving") in labels
        assert ("overall", "serving") in labels
        assert ("knowledge", "serving") in labels


#: Hard ceiling for the two real-socket tests below -- a genuine hang (vs. a
#: slow-but-working call) must fail loudly rather than stall the whole suite
#: indefinitely, especially on a heavily loaded shared dev box.
_REAL_CHANNEL_TIMEOUT_SECONDS = 15.0


class TestRealChannelCheckAndWatch:
    """Proves wire-compatibility with a real `grpc_health.v1` client, not just the Python object."""

    @pytest.mark.asyncio
    async def test_check_and_watch_over_a_real_insecure_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _open_fake_pool(monkeypatch)
        server = grpc.aio.server()
        manager = GrpcHealthManager()
        manager.register(server)
        port = server.add_insecure_port("127.0.0.1:0")
        await manager.mark_starting()
        await manager.mark_started()
        await server.start()

        async def _run() -> None:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                stub = health_pb2_grpc.HealthStub(channel)

                overall = await stub.Check(health_pb2.HealthCheckRequest(service=""))
                assert overall.status == _SERVING

                knowledge = await stub.Check(
                    health_pb2.HealthCheckRequest(service=KNOWLEDGE_SERVICE)
                )
                assert knowledge.status == _SERVING

                watch_call = stub.Watch(health_pb2.HealthCheckRequest(service=KNOWLEDGE_SERVICE))
                first_update = await watch_call.read()
                assert first_update.status == _SERVING
                watch_call.cancel()

        try:
            await asyncio.wait_for(_run(), timeout=_REAL_CHANNEL_TIMEOUT_SECONDS)
        finally:
            await asyncio.wait_for(server.stop(0), timeout=5.0)

    @pytest.mark.asyncio
    async def test_check_unknown_service_returns_not_found(self) -> None:
        server = grpc.aio.server()
        manager = GrpcHealthManager()
        manager.register(server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()

        async def _run() -> None:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                stub = health_pb2_grpc.HealthStub(channel)
                with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                    await stub.Check(health_pb2.HealthCheckRequest(service="not.a.real.Service"))
                assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND

        try:
            await asyncio.wait_for(_run(), timeout=_REAL_CHANNEL_TIMEOUT_SECONDS)
        finally:
            await asyncio.wait_for(server.stop(0), timeout=5.0)
