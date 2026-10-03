"""Tests for `PenguinCodeServer.start()`'s gRPC server construction (O1/O6/O9 hardening).

Mocks `grpc.aio.server`/`ConfigStore`/hypercorn's `serve` so the real
`start()` method runs end-to-end without any real socket/DB/HTTP I/O, and
asserts on the exact kwargs it builds -- worker pool size, the
`maximum_concurrent_rpcs` cap, the message-length `options`, and the
tracing interceptor's presence -- including each opt-out kill-switch
reverting to the pre-hardening behavior.

# regression: gRPC server hardening (O1 -- tracing, O6 -- message limits, O9 -- concurrency cap)
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from penguincode_cli.config.settings import Settings
from penguincode_cli.server.interceptors import TracingInterceptor
from penguincode_cli.server.main import PenguinCodeServer


class _FakeConfigStore:
    async def open(self) -> None:
        pass

    async def seed_defaults(self) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _clean_grpc_flag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with none of the kill-switch env overrides set."""
    for name in (
        "PENGUINCODE_FLAG_DISABLE_GRPC_TRACING",
        "PENGUINCODE_FLAG_DISABLE_GRPC_CONCURRENCY_LIMITS",
        "PENGUINCODE_FLAG_DISABLE_GRPC_MESSAGE_LIMITS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def grpc_aio_server_mock() -> Iterator[MagicMock]:
    """Patch `grpc.aio.server` to a mock capturing its call kwargs."""
    fake_server = MagicMock()
    fake_server.start = AsyncMock()
    fake_server.stop = AsyncMock()
    fake_server.add_insecure_port = MagicMock(return_value=50999)

    with patch("penguincode_cli.server.main.grpc.aio.server", return_value=fake_server) as mocked:
        mocked.return_value = fake_server
        yield mocked


async def _run_start_and_capture(
    monkeypatch: pytest.MonkeyPatch, grpc_aio_server_mock: MagicMock
) -> PenguinCodeServer:
    """Build a `PenguinCodeServer`, run `start()` with I/O short-circuited, then stop it."""

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


class TestWorkerPoolAndConcurrencyCap:
    @pytest.mark.asyncio
    async def test_defaults_apply_worker_count_and_concurrency_cap(
        self, monkeypatch: pytest.MonkeyPatch, grpc_aio_server_mock: MagicMock
    ) -> None:
        await _run_start_and_capture(monkeypatch, grpc_aio_server_mock)

        _, kwargs = grpc_aio_server_mock.call_args
        assert kwargs["maximum_concurrent_rpcs"] == 40  # 10 workers * 4 default multiplier
        executor = grpc_aio_server_mock.call_args[0][0]
        assert executor._max_workers == 10

    @pytest.mark.asyncio
    async def test_concurrency_kill_switch_omits_the_cap(
        self, monkeypatch: pytest.MonkeyPatch, grpc_aio_server_mock: MagicMock
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_GRPC_CONCURRENCY_LIMITS", "true")
        await _run_start_and_capture(monkeypatch, grpc_aio_server_mock)

        _, kwargs = grpc_aio_server_mock.call_args
        assert "maximum_concurrent_rpcs" not in kwargs


class TestMessageLimits:
    @pytest.mark.asyncio
    async def test_defaults_set_four_mebibyte_message_options(
        self, monkeypatch: pytest.MonkeyPatch, grpc_aio_server_mock: MagicMock
    ) -> None:
        await _run_start_and_capture(monkeypatch, grpc_aio_server_mock)

        _, kwargs = grpc_aio_server_mock.call_args
        options = dict(kwargs["options"])
        assert options["grpc.max_receive_message_length"] == 4 * 1024 * 1024
        assert options["grpc.max_send_message_length"] == 4 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_message_limits_kill_switch_yields_empty_options(
        self, monkeypatch: pytest.MonkeyPatch, grpc_aio_server_mock: MagicMock
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_GRPC_MESSAGE_LIMITS", "true")
        await _run_start_and_capture(monkeypatch, grpc_aio_server_mock)

        _, kwargs = grpc_aio_server_mock.call_args
        assert kwargs["options"] == []


class TestTracingInterceptorWiring:
    @pytest.mark.asyncio
    async def test_tracing_interceptor_is_outermost_by_default(
        self, monkeypatch: pytest.MonkeyPatch, grpc_aio_server_mock: MagicMock
    ) -> None:
        await _run_start_and_capture(monkeypatch, grpc_aio_server_mock)

        _, kwargs = grpc_aio_server_mock.call_args
        interceptors = kwargs["interceptors"]
        assert isinstance(interceptors[0], TracingInterceptor)

    @pytest.mark.asyncio
    async def test_tracing_kill_switch_omits_the_interceptor(
        self, monkeypatch: pytest.MonkeyPatch, grpc_aio_server_mock: MagicMock
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_GRPC_TRACING", "true")
        await _run_start_and_capture(monkeypatch, grpc_aio_server_mock)

        _, kwargs = grpc_aio_server_mock.call_args
        interceptors = kwargs["interceptors"]
        assert not any(isinstance(i, TracingInterceptor) for i in interceptors)
