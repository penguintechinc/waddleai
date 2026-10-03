"""Tests for `KnowledgeClient._call`'s O8 retry/re-auth wiring on top of
`grpc_client.retry_with_backoff`.

Covers: a transient UNAVAILABLE retried and then succeeding, giving up after
`retry_max` and translating to `KnowledgeServerUnavailableError`, UNAUTHENTICATED never
retried (single attempt) while invalidating the cached WaddleAI token so the next call
re-acquires, and PERMISSION_DENIED never retried without invalidating the token (a scope
problem, not a stale-credential one).

# regression: ops-audit O8 (CLI resilience -- retry/backoff + re-auth for KnowledgeClient)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import grpc
import pytest
from grpc.aio import Metadata

from penguincode_cli.client.knowledge_client import (
    KnowledgeAuthError,
    KnowledgeClient,
    KnowledgeServerUnavailableError,
)
from penguincode_cli.client.waddleai_auth import WaddleAITokenProvider
from penguincode_cli.config.settings import ClientConfig, ServerConfig

_AUTH_METADATA = [("authorization", "Bearer test-jwt")]


class _FakeStub:
    def __init__(self) -> None:
        self.Query = AsyncMock()


def _rpc_error(code: grpc.StatusCode, details: str = "boom") -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(code, Metadata(), Metadata(), details=details)


def _client(
    monkeypatch: pytest.MonkeyPatch, stub: _FakeStub, *, retry_max: int = 3
) -> tuple[KnowledgeClient, AsyncMock]:
    monkeypatch.setattr(
        "penguincode_cli.client.knowledge_client.KnowledgeServiceStub", lambda channel: stub
    )
    token_provider = AsyncMock(spec=WaddleAITokenProvider)
    token_provider.get_auth_metadata = AsyncMock(return_value=_AUTH_METADATA)
    server_config = ServerConfig(host="pc-server.internal", port=50051)
    client = KnowledgeClient(
        server_config,
        token_provider=token_provider,
        channel=object(),
        client_config=ClientConfig(retry_max=retry_max, retry_base_ms=1, retry_max_ms=5),
    )
    return client, token_provider


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "penguincode_cli.client.grpc_client.asyncio.sleep", AsyncMock(return_value=None)
    )


class TestUnavailableRetry:
    async def test_succeeds_after_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.Query.side_effect = [
            _rpc_error(grpc.StatusCode.UNAVAILABLE),
            _rpc_error(grpc.StatusCode.DEADLINE_EXCEEDED),
            object(),  # QueryResponse-shaped stand-in, adapted by the caller, not here
        ]
        client, _ = _client(monkeypatch, stub, retry_max=3)

        # Call `_call` directly -- the adaptation layer above it is irrelevant to this test.
        result = await client._call(stub.Query, object())
        assert stub.Query.await_count == 3
        assert result is not None

    async def test_exhausts_retries_then_raises_server_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.Query.side_effect = _rpc_error(grpc.StatusCode.UNAVAILABLE, "down")
        client, _ = _client(monkeypatch, stub, retry_max=2)

        with pytest.raises(KnowledgeServerUnavailableError, match="unreachable"):
            await client._call(stub.Query, object())
        # 1 initial + 2 retries = 3 total attempts
        assert stub.Query.await_count == 3

    async def test_total_attempts_bounded_by_retry_max_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.Query.side_effect = _rpc_error(grpc.StatusCode.UNAVAILABLE)
        client, _ = _client(monkeypatch, stub, retry_max=0)

        with pytest.raises(KnowledgeServerUnavailableError):
            await client._call(stub.Query, object())
        assert stub.Query.await_count == 1


class TestAuthErrorsNeverRetried:
    async def test_unauthenticated_single_attempt_invalidates_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.Query.side_effect = _rpc_error(grpc.StatusCode.UNAUTHENTICATED, "expired")
        client, token_provider = _client(monkeypatch, stub, retry_max=5)

        with pytest.raises(KnowledgeAuthError, match="expired"):
            await client._call(stub.Query, object())

        assert stub.Query.await_count == 1  # never retried, even with retry_max=5
        token_provider.invalidate_cache.assert_called_once()

    async def test_permission_denied_single_attempt_does_not_invalidate_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.Query.side_effect = _rpc_error(grpc.StatusCode.PERMISSION_DENIED, "no scope")
        client, token_provider = _client(monkeypatch, stub, retry_max=5)

        with pytest.raises(KnowledgeAuthError, match="no scope"):
            await client._call(stub.Query, object())

        assert stub.Query.await_count == 1
        token_provider.invalidate_cache.assert_not_called()


class TestUnauthenticatedWithBareTokenProvider:
    """regression: gh-275 CI -- a REAL (non-mock) token-provider subclass with no
    `self._store` (same shape as `tests/integration/conftest.py`'s `StaticTokenProvider`:
    overrides `get_access_token`/`get_auth_metadata`, never calls
    `WaddleAITokenProvider.__init__`) must surface `KnowledgeAuthError` on
    `UNAUTHENTICATED`, never a raw `AttributeError` from `invalidate_cache()` reaching
    into `self._store`.
    """

    async def test_unauthenticated_with_bare_provider_raises_auth_error_not_attribute_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _BareTokenProvider(WaddleAITokenProvider):
            def __init__(self) -> None:
                pass  # deliberately never calls super().__init__() -- no self._store

            async def get_auth_metadata(self) -> list[tuple[str, str]]:
                return _AUTH_METADATA

        stub = _FakeStub()
        stub.Query.side_effect = _rpc_error(grpc.StatusCode.UNAUTHENTICATED, "expired")
        monkeypatch.setattr(
            "penguincode_cli.client.knowledge_client.KnowledgeServiceStub", lambda channel: stub
        )
        server_config = ServerConfig(host="pc-server.internal", port=50051)
        client = KnowledgeClient(
            server_config,
            token_provider=_BareTokenProvider(),
            channel=object(),
            client_config=ClientConfig(retry_max=5, retry_base_ms=1, retry_max_ms=5),
        )

        with pytest.raises(KnowledgeAuthError, match="expired"):
            await client._call(stub.Query, object())

        assert stub.Query.await_count == 1  # never retried
