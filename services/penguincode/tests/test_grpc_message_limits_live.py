"""Live gRPC test proving message-size limits (O6, server hardening) actually enforce.

Binds a real `grpc.aio.server` to an ephemeral loopback port with
`grpc.max_receive_message_length` set low, using the real
`AuthServiceServicer`/`AuthRequest` wire types (pure, no DB/network I/O) --
proves an over-limit request is rejected with `RESOURCE_EXHAUSTED` and an
under-limit request is not, i.e. the `options` tuples `server/main.py`
passes to `grpc.aio.server()` are the real, library-enforced mechanism, not
just plumbing that happens to be present.

# regression: gRPC server hardening (O6 -- message-limit enforcement)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from concurrent import futures

import grpc
import pytest

from penguincode_cli.config.settings import AuthConfig
from penguincode_cli.proto import AuthRequest, AuthServiceStub, add_AuthServiceServicer_to_server
from penguincode_cli.server.services.auth import AuthServiceImpl

_MESSAGE_LIMIT_BYTES = 1024


class _RunningAuthServer:
    def __init__(self, server: grpc.aio.Server, port: int) -> None:
        self.server = server
        self.port = port


@pytest.fixture
async def bounded_auth_server() -> AsyncIterator[_RunningAuthServer]:
    """A real gRPC server with a 1 KiB receive/send message limit."""
    server = grpc.aio.server(
        futures.ThreadPoolExecutor(max_workers=2),
        options=[
            ("grpc.max_receive_message_length", _MESSAGE_LIMIT_BYTES),
            ("grpc.max_send_message_length", _MESSAGE_LIMIT_BYTES),
        ],
    )
    add_AuthServiceServicer_to_server(
        AuthServiceImpl(AuthConfig(enabled=False, shared_key="test-shared-key")), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield _RunningAuthServer(server, port)
    finally:
        await server.stop(grace=None)


@pytest.mark.asyncio
async def test_oversized_request_is_rejected_with_resource_exhausted(
    bounded_auth_server: _RunningAuthServer,
) -> None:
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{bounded_auth_server.port}")
    try:
        stub = AuthServiceStub(channel)
        oversized_key = "x" * (_MESSAGE_LIMIT_BYTES * 4)

        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.Authenticate(AuthRequest(api_key=oversized_key, client_id="c"))

        assert exc_info.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_under_limit_request_is_not_rejected_for_size(
    bounded_auth_server: _RunningAuthServer,
) -> None:
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{bounded_auth_server.port}")
    try:
        stub = AuthServiceStub(channel)
        # Under the 1 KiB limit; the shared key is valid so this succeeds
        # outright -- proving the limit itself, not just "some code ran",
        # is what distinguishes the two tests.
        response = await stub.Authenticate(AuthRequest(api_key="test-shared-key", client_id="c"))
        assert response.access_token
    finally:
        await channel.close()
