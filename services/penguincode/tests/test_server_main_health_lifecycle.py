"""Tests for `PenguinCodeServer.start()`/`stop()` wiring the standard grpc.health.v1.Health servicer.

Runs the real `start()`/`stop()` methods with `grpc.aio.server` mocked (same
harness shape as `test_server_main_grpc_hardening.py`), and asserts on
`server.grpc_health`'s underlying servicer state directly -- proving the
overall ("") entry goes NOT_SERVING -> SERVING across startup and back to
NOT_SERVING once shutdown drain begins, and that the kill-switch leaves
`grpc_health.registered` False.

# regression: penguincode-ops-audit (Helm PR #264 nativeGrpcProbe had no server-side grpc.health.v1.Health registration)
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from penguincode_cli.config.settings import Settings
from penguincode_cli.server.grpc_health import KNOWLEDGE_SERVICE, OVERALL_SERVICE
from penguincode_cli.server.main import PenguinCodeServer

_SERVING_LABEL = 1  # health_pb2.HealthCheckResponse.SERVING
_NOT_SERVING_LABEL = 2  # health_pb2.HealthCheckResponse.NOT_SERVING


class _FakeConfigStore:
    async def open(self) -> None:
        pass

    async def seed_defaults(self) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _clean_flag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PENGUINCODE_FLAG_DISABLE_GRPC_TRACING",
        "PENGUINCODE_FLAG_DISABLE_GRPC_CONCURRENCY_LIMITS",
        "PENGUINCODE_FLAG_DISABLE_GRPC_MESSAGE_LIMITS",
        "PENGUINCODE_FLAG_DISABLE_GRPC_HEALTH_SERVICE",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def grpc_aio_server_mock() -> Iterator[MagicMock]:
    """Patch `grpc.aio.server` to a mock -- registration calls land on it harmlessly."""
    fake_server = MagicMock()
    fake_server.start = AsyncMock()
    fake_server.stop = AsyncMock()
    fake_server.add_insecure_port = MagicMock(return_value=50999)

    with patch("penguincode_cli.server.main.grpc.aio.server", return_value=fake_server) as mocked:
        mocked.return_value = fake_server
        yield mocked


async def _started_server(monkeypatch: pytest.MonkeyPatch) -> PenguinCodeServer:
    async def _fake_hypercorn_serve(*args: Any, **kwargs: Any) -> None:
        shutdown_trigger = kwargs.get("shutdown_trigger")
        if shutdown_trigger is not None:
            await shutdown_trigger()

    monkeypatch.setattr("penguincode_cli.server.main.ConfigStore", lambda: _FakeConfigStore())
    with patch("hypercorn.asyncio.serve", _fake_hypercorn_serve):
        settings = Settings()
        server = PenguinCodeServer(settings, host="localhost", port=0, rest_port=0)
        await server.start()
    return server


class TestHealthRegistrationDefault:
    @pytest.mark.asyncio
    async def test_health_servicer_is_registered_by_default(
        self, monkeypatch: pytest.MonkeyPatch, grpc_aio_server_mock: MagicMock
    ) -> None:
        server = await _started_server(monkeypatch)
        try:
            assert server.grpc_health.registered is True
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_kill_switch_leaves_health_servicer_unregistered(
        self, monkeypatch: pytest.MonkeyPatch, grpc_aio_server_mock: MagicMock
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_GRPC_HEALTH_SERVICE", "true")
        server = await _started_server(monkeypatch)
        try:
            assert server.grpc_health.registered is False
        finally:
            await server.stop()


class TestHealthLifecycleTransitions:
    @pytest.mark.asyncio
    async def test_overall_is_serving_once_startup_completes(
        self, monkeypatch: pytest.MonkeyPatch, grpc_aio_server_mock: MagicMock
    ) -> None:
        server = await _started_server(monkeypatch)
        try:
            status = server.grpc_health._servicer._server_status
            assert status[OVERALL_SERVICE] == _SERVING_LABEL
            assert status[KNOWLEDGE_SERVICE] == _SERVING_LABEL
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_overall_goes_not_serving_once_shutdown_drain_begins(
        self, monkeypatch: pytest.MonkeyPatch, grpc_aio_server_mock: MagicMock
    ) -> None:
        server = await _started_server(monkeypatch)
        status = server.grpc_health._servicer._server_status
        assert status[OVERALL_SERVICE] == _SERVING_LABEL

        await server.stop()

        assert status[OVERALL_SERVICE] == _NOT_SERVING_LABEL
        assert status[KNOWLEDGE_SERVICE] == _NOT_SERVING_LABEL
