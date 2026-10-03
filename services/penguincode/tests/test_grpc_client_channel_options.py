"""Tests for `GRPCClient.connect()`'s channel-level message size options (O6, server hardening).

The client reads `ServerConfig.grpc_max_message_bytes` directly, so it
shares the exact same env-driven default as the server it talks to (see
`ServerConfig`'s docstring in `config/settings.py`) -- these tests prove the
channel is constructed with matching `grpc.max_receive_message_length` /
`grpc.max_send_message_length` options, for both the insecure and TLS paths.

# regression: gRPC server hardening (O6 -- client/server message-size contract)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from penguincode_cli.client.grpc_client import GRPCClient
from penguincode_cli.config.settings import ClientConfig, ServerConfig


def _fake_channel() -> MagicMock:
    channel = MagicMock()
    channel.close = AsyncMock()
    return channel


@pytest.mark.asyncio
async def test_insecure_channel_gets_message_size_options() -> None:
    server_config = ServerConfig(host="localhost", port=50051, grpc_max_message_bytes=1234)
    client = GRPCClient(server_config, ClientConfig())

    with (
        patch("grpc.aio.insecure_channel", return_value=_fake_channel()) as mocked_channel,
        patch("penguincode_cli.client.grpc_client.HealthServiceStub") as mocked_health_stub,
        patch("penguincode_cli.client.grpc_client.AuthServiceStub"),
        patch("penguincode_cli.client.grpc_client.ChatServiceStub"),
        patch("penguincode_cli.client.grpc_client.ToolCallbackServiceStub"),
    ):
        mocked_health_stub.return_value.Check = AsyncMock(return_value=MagicMock(version="1.2.3"))
        connected = await client.connect()

    assert connected is True
    _, kwargs = mocked_channel.call_args
    options = dict(kwargs["options"])
    assert options["grpc.max_receive_message_length"] == 1234
    assert options["grpc.max_send_message_length"] == 1234


@pytest.mark.asyncio
async def test_secure_channel_gets_message_size_options() -> None:
    server_config = ServerConfig(
        host="localhost", port=50051, tls_enabled=True, grpc_max_message_bytes=777
    )
    client = GRPCClient(server_config, ClientConfig())

    with (
        patch("grpc.ssl_channel_credentials", return_value=MagicMock()),
        patch("grpc.aio.secure_channel", return_value=_fake_channel()) as mocked_channel,
        patch("penguincode_cli.client.grpc_client.HealthServiceStub") as mocked_health_stub,
        patch("penguincode_cli.client.grpc_client.AuthServiceStub"),
        patch("penguincode_cli.client.grpc_client.ChatServiceStub"),
        patch("penguincode_cli.client.grpc_client.ToolCallbackServiceStub"),
    ):
        mocked_health_stub.return_value.Check = AsyncMock(return_value=MagicMock(version="1.2.3"))
        connected = await client.connect()

    assert connected is True
    args, kwargs = mocked_channel.call_args
    options = dict(kwargs["options"])
    assert options["grpc.max_receive_message_length"] == 777
    assert options["grpc.max_send_message_length"] == 777
