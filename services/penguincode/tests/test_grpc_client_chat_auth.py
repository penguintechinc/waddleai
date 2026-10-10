"""Tests for `client/grpc_client.py`'s Chat RS256 bearer-token plumbing.

`server/interceptors.py` now gates every `ChatService` RPC with the same RS256/
`ScopeContext` check `KnowledgeService`/`LessonsService` already enforce (tenancy-gap
fix) -- the CLI's legacy HS256 token (`_get_auth_metadata`/`TokenManager`) is no longer
accepted there by default, so `create_session`/`chat`/`get_history`/`close_session` must
attach a `WaddleAITokenProvider`-acquired bearer token instead (`_get_chat_auth_metadata`),
exactly like `client.knowledge_client.KnowledgeClient` already does for `KnowledgeService`.
`ToolCallbackService` (unaffected by this fix) must keep using the legacy token.

# regression: penguincode-chat-rs256-scope (tenancy gap, PR #262 follow-up)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from penguincode_cli.client.auth import TokenManager
from penguincode_cli.client.grpc_client import GRPCClient
from penguincode_cli.client.waddleai_auth import WaddleAIAuthError
from penguincode_cli.config.settings import ClientConfig, ServerConfig
from penguincode_cli.proto import (
    ChatResponse,
    CloseSessionResponse,
    CreateSessionResponse,
    GetHistoryResponse,
    ServerInfo,
    TextChunk,
)


class _FakeTokenProvider:
    """Stand-in for `WaddleAITokenProvider` -- never touches the network."""

    def __init__(self, token: str = "a-waddleai-rs256-jwt") -> None:
        self.token = token
        self.calls = 0

    async def get_auth_metadata(self) -> list[tuple[str, str]]:
        self.calls += 1
        return [("authorization", f"Bearer {self.token}")]


class _RaisingTokenProvider:
    """Stand-in that always fails acquisition, as a real `WaddleAITokenProvider` would
    when no credential/issuer is configured and the dev fallback is refused."""

    async def get_auth_metadata(self) -> list[tuple[str, str]]:
        raise WaddleAIAuthError("no credential configured")


async def _empty_async_iter() -> Any:
    return
    yield  # pragma: no cover -- makes this an async generator


def _client(token_provider: Any) -> GRPCClient:
    client = GRPCClient(
        ServerConfig(),
        ClientConfig(),
        token_manager=TokenManager("/tmp/does-not-matter-token-path"),
        waddleai_token_provider=token_provider,
    )
    client._auth_stub = AsyncMock()
    client._chat_stub = AsyncMock()
    client._tool_stub = AsyncMock()
    client._health_stub = AsyncMock()
    # `ExecuteTools` (grpc.aio bidi-streaming) is directly async-iterable, not
    # awaited first -- an `AsyncMock`'s call returns an unawaited coroutine
    # instead, which `async for` rejects (see `_tool_callback_loop`). Every
    # test here that triggers `create_session`'s background tool-callback
    # task needs a real async-iterable in its place.
    client._tool_stub.ExecuteTools = MagicMock(side_effect=lambda *a, **k: _empty_async_iter())
    return client


class _OneChunkAsyncIterator:
    """Minimal async iterator yielding one `ChatResponse`, standing in for the real
    server-streaming `Chat` call."""

    def __init__(self, response: ChatResponse) -> None:
        self._response = response
        self._yielded = False

    def __aiter__(self) -> _OneChunkAsyncIterator:
        return self

    async def __anext__(self) -> ChatResponse:
        if self._yielded:
            raise StopAsyncIteration
        self._yielded = True
        return self._response


class TestCreateSessionUsesWaddleAIToken:
    @pytest.mark.asyncio
    async def test_attaches_waddleai_bearer_metadata(self) -> None:
        provider = _FakeTokenProvider(token="tok-123")
        client = _client(provider)
        client._chat_stub.CreateSession = AsyncMock(
            return_value=CreateSessionResponse(
                session_id="s1", server_info=ServerInfo(version="0.1.0")
            )
        )

        await client.create_session("/tmp/proj", [])

        assert provider.calls == 1
        _, kwargs = client._chat_stub.CreateSession.call_args
        assert kwargs["metadata"] == [("authorization", "Bearer tok-123")]

    @pytest.mark.asyncio
    async def test_never_uses_the_legacy_hs256_token_manager(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with a legacy HS256 token cached, ChatService calls must not send it."""
        provider = _FakeTokenProvider()
        client = _client(provider)
        client.token_manager.get_token = lambda: "legacy-hs256-token"  # type: ignore[method-assign]
        client._chat_stub.CreateSession = AsyncMock(
            return_value=CreateSessionResponse(
                session_id="s1", server_info=ServerInfo(version="0.1.0")
            )
        )

        await client.create_session("/tmp/proj", [])

        _, kwargs = client._chat_stub.CreateSession.call_args
        assert kwargs["metadata"] == [("authorization", "Bearer a-waddleai-rs256-jwt")]


class TestChatUsesWaddleAIToken:
    @pytest.mark.asyncio
    async def test_attaches_waddleai_bearer_metadata_and_session_id(self) -> None:
        provider = _FakeTokenProvider(token="tok-456")
        client = _client(provider)
        # `Chat` (grpc.aio server-streaming) is directly async-iterable, not
        # awaited first -- a `MagicMock`, not `AsyncMock`, mirrors that shape.
        client._chat_stub.Chat = MagicMock(
            return_value=_OneChunkAsyncIterator(
                ChatResponse(text=TextChunk(content="hi", is_final=True))
            )
        )

        responses = [resp async for resp in client.chat("s1", "hello")]

        assert responses[0]["type"] == "text"
        _, kwargs = client._chat_stub.Chat.call_args
        assert ("authorization", "Bearer tok-456") in kwargs["metadata"]
        assert ("session-id", "s1") in kwargs["metadata"]

    @pytest.mark.asyncio
    async def test_token_acquisition_failure_yields_recoverable_false_auth_error(self) -> None:
        client = _client(_RaisingTokenProvider())

        responses = [resp async for resp in client.chat("s1", "hello")]

        assert len(responses) == 1
        assert responses[0] == {
            "type": "error",
            "code": "AUTH_ERROR",
            "message": "no credential configured",
            "recoverable": False,
        }
        client._chat_stub.Chat.assert_not_called()


class TestGetHistoryAndCloseSessionUseWaddleAIToken:
    @pytest.mark.asyncio
    async def test_get_history_attaches_waddleai_bearer_metadata(self) -> None:
        provider = _FakeTokenProvider(token="tok-789")
        client = _client(provider)
        client._chat_stub.GetHistory = AsyncMock(return_value=GetHistoryResponse(messages=[]))

        await client.get_history("s1")

        _, kwargs = client._chat_stub.GetHistory.call_args
        assert kwargs["metadata"] == [("authorization", "Bearer tok-789")]

    @pytest.mark.asyncio
    async def test_close_session_attaches_waddleai_bearer_metadata(self) -> None:
        provider = _FakeTokenProvider(token="tok-abc")
        client = _client(provider)
        client._chat_stub.CloseSession = AsyncMock(return_value=CloseSessionResponse(success=True))

        await client.close_session("s1")

        _, kwargs = client._chat_stub.CloseSession.call_args
        assert kwargs["metadata"] == [("authorization", "Bearer tok-abc")]


class TestToolCallbackServiceUnaffected:
    @pytest.mark.asyncio
    async def test_tool_callback_loop_still_uses_the_legacy_token_manager(self) -> None:
        """ChatService moved to RS256; ToolCallbackService is untouched by this fix."""
        provider = _FakeTokenProvider()
        client = _client(provider)
        client.token_manager.get_token = lambda: "legacy-hs256-token"  # type: ignore[method-assign]
        client._current_session_id = "s1"

        captured: dict[str, Any] = {}

        def _fake_execute_tools(request_iter: Any, metadata: Any) -> Any:
            # `grpc.aio`'s real bidi-streaming call object is directly
            # async-iterable (no `await` before `async for`) -- an `async def`
            # here would instead return an unawaited coroutine, which is not.
            captured["metadata"] = metadata

            async def _empty() -> Any:
                return
                yield  # pragma: no cover -- makes this an async generator

            return _empty()

        client._tool_stub.ExecuteTools = _fake_execute_tools
        import asyncio as _asyncio

        client._tool_response_queue = _asyncio.Queue()
        await client._tool_callback_loop()

        assert ("authorization", "Bearer legacy-hs256-token") in captured["metadata"]
        assert provider.calls == 0
