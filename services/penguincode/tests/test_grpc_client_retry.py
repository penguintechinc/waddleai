"""Tests for `client/grpc_client.py`'s `retry_with_backoff` (O8 CLI resilience) and its
use in `GRPCClient.connect()`'s health check.

Covers: success-after-N-retries, giving up after `max_retries`, never retrying
UNAUTHENTICATED/PERMISSION_DENIED, a non-retryable status code propagating on the first
attempt, the `penguincode.disable-client-retry` kill-switch collapsing to a single
attempt, and a bounded total backoff time (every sleep capped at `max_delay_ms`).

# regression: ops-audit O8 (CLI resilience -- retry/backoff for gRPC/knowledge clients)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import pytest
from grpc.aio import Metadata

from penguincode_cli.client.grpc_client import GRPCClient, retry_with_backoff
from penguincode_cli.config.settings import ClientConfig, ServerConfig


def _rpc_error(code: grpc.StatusCode, details: str = "boom") -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(code, Metadata(), Metadata(), details=details)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here asserts on retry *counts*, not real wall-clock delay -- patch
    `asyncio.sleep` so the whole suite runs in milliseconds regardless of backoff config.
    """
    monkeypatch.setattr(
        "penguincode_cli.client.grpc_client.asyncio.sleep", AsyncMock(return_value=None)
    )


@pytest.fixture(autouse=True)
def _retry_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No PostHog configured, no env override -> the kill-switch defaults OFF (retry ON)."""
    monkeypatch.delenv("PENGUINCODE_FLAG_DISABLE_CLIENT_RETRY", raising=False)
    monkeypatch.delenv("POSTHOG_KEY", raising=False)


class TestRetryWithBackoff:
    async def test_succeeds_on_first_attempt_no_retry(self) -> None:
        call = AsyncMock(return_value="ok")
        result = await retry_with_backoff(call, max_retries=3, base_delay_ms=10, max_delay_ms=100)
        assert result == "ok"
        assert call.await_count == 1

    async def test_succeeds_after_two_retries(self) -> None:
        call = AsyncMock(
            side_effect=[
                _rpc_error(grpc.StatusCode.UNAVAILABLE),
                _rpc_error(grpc.StatusCode.DEADLINE_EXCEEDED),
                "ok",
            ]
        )
        result = await retry_with_backoff(call, max_retries=3, base_delay_ms=10, max_delay_ms=100)
        assert result == "ok"
        assert call.await_count == 3

    async def test_gives_up_after_max_retries(self) -> None:
        err = _rpc_error(grpc.StatusCode.UNAVAILABLE)
        call = AsyncMock(side_effect=err)
        with pytest.raises(grpc.aio.AioRpcError):
            await retry_with_backoff(call, max_retries=2, base_delay_ms=10, max_delay_ms=100)
        # 1 initial attempt + 2 retries = 3 total calls
        assert call.await_count == 3

    async def test_never_retries_unauthenticated(self) -> None:
        call = AsyncMock(side_effect=_rpc_error(grpc.StatusCode.UNAUTHENTICATED))
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await retry_with_backoff(call, max_retries=5, base_delay_ms=10, max_delay_ms=100)
        assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED
        assert call.await_count == 1

    async def test_never_retries_permission_denied(self) -> None:
        call = AsyncMock(side_effect=_rpc_error(grpc.StatusCode.PERMISSION_DENIED))
        with pytest.raises(grpc.aio.AioRpcError):
            await retry_with_backoff(call, max_retries=5, base_delay_ms=10, max_delay_ms=100)
        assert call.await_count == 1

    async def test_non_retryable_status_propagates_immediately(self) -> None:
        call = AsyncMock(side_effect=_rpc_error(grpc.StatusCode.RESOURCE_EXHAUSTED))
        with pytest.raises(grpc.aio.AioRpcError):
            await retry_with_backoff(call, max_retries=5, base_delay_ms=10, max_delay_ms=100)
        assert call.await_count == 1

    async def test_kill_switch_collapses_to_single_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`penguincode.disable-client-retry=true` -> legacy single-attempt behavior."""
        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_CLIENT_RETRY", "true")
        call = AsyncMock(side_effect=_rpc_error(grpc.StatusCode.UNAVAILABLE))
        with pytest.raises(grpc.aio.AioRpcError):
            await retry_with_backoff(call, max_retries=5, base_delay_ms=10, max_delay_ms=100)
        assert call.await_count == 1

    async def test_total_backoff_time_is_bounded_by_max_delay_ms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every sleep call must be <= max_delay_ms/1000 seconds, even with a huge base."""
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("penguincode_cli.client.grpc_client.asyncio.sleep", sleep_mock)
        call = AsyncMock(side_effect=_rpc_error(grpc.StatusCode.UNAVAILABLE))

        with pytest.raises(grpc.aio.AioRpcError):
            await retry_with_backoff(call, max_retries=4, base_delay_ms=10_000, max_delay_ms=50)

        assert sleep_mock.await_count == 4
        for call_args in sleep_mock.await_args_list:
            (delay,) = call_args.args
            assert 0 <= delay <= 0.05  # max_delay_ms=50 -> 0.05s cap


class TestConnectRetry:
    """`GRPCClient.connect()`'s health-check call retries on a transient outage."""

    async def test_connect_retries_health_check_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server_config = ServerConfig(host="localhost", port=50051)
        client_config = ClientConfig(retry_max=3, retry_base_ms=1, retry_max_ms=5)
        client = GRPCClient(server_config, client_config)

        channel = MagicMock()
        channel.close = AsyncMock()

        health_check = AsyncMock(
            side_effect=[
                _rpc_error(grpc.StatusCode.UNAVAILABLE),
                MagicMock(version="9.9.9"),
            ]
        )

        with (
            patch("grpc.aio.insecure_channel", return_value=channel),
            patch("penguincode_cli.client.grpc_client.HealthServiceStub") as mocked_health_stub,
            patch("penguincode_cli.client.grpc_client.AuthServiceStub"),
            patch("penguincode_cli.client.grpc_client.ChatServiceStub"),
            patch("penguincode_cli.client.grpc_client.ToolCallbackServiceStub"),
        ):
            mocked_health_stub.return_value.Check = health_check
            connected = await client.connect()

        assert connected is True
        assert health_check.await_count == 2

    async def test_connect_returns_false_after_exhausting_retries(self) -> None:
        server_config = ServerConfig(host="localhost", port=50051)
        client_config = ClientConfig(retry_max=1, retry_base_ms=1, retry_max_ms=5)
        client = GRPCClient(server_config, client_config)

        channel = MagicMock()
        channel.close = AsyncMock()
        health_check = AsyncMock(side_effect=_rpc_error(grpc.StatusCode.UNAVAILABLE))

        with (
            patch("grpc.aio.insecure_channel", return_value=channel),
            patch("penguincode_cli.client.grpc_client.HealthServiceStub") as mocked_health_stub,
            patch("penguincode_cli.client.grpc_client.AuthServiceStub"),
            patch("penguincode_cli.client.grpc_client.ChatServiceStub"),
            patch("penguincode_cli.client.grpc_client.ToolCallbackServiceStub"),
        ):
            mocked_health_stub.return_value.Check = health_check
            connected = await client.connect()

        assert connected is False
        # 1 initial attempt + 1 retry (retry_max=1) = 2 total calls
        assert health_check.await_count == 2
