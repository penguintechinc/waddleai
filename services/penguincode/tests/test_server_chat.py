"""Tests for `penguincode_cli.server.services.chat` -- the O4-a (High) cross-pod fix.

Mirrors `tests/test_server_lessons_service.py`'s style (`_FakeContext`/
`AbortCalledError`/real-`ScopeContext`-via-contextvar fixtures). `ChatAgent`
is replaced with a lightweight fake (`_FakeChatAgent`) that reproduces its
real `conversation_history`/`conversation_summary` contract without hitting
Ollama; `_check_ollama_connection`/`_get_available_models` are patched to
avoid a real network call from `CreateSession`.

Proves, per RPC:

- `CreateSession` persists a session scoped to the caller's `ScopeContext`.
- `Chat` on an unknown/wrong-scope session yields `SESSION_NOT_FOUND`, never
  an exception; a valid turn persists the updated conversation.
- `GetHistory`/`CloseSession` are scope-checked the same way.
- **The actual regression**: a session created via one `ChatServiceImpl`
  instance is usable from a second instance sharing the same store --
  standing in for two pods behind the same Service.
- The `penguincode.disable-shared-sessions` kill switch's in-memory
  fallback still serves a full create->chat->history->close cycle
  end-to-end (flag-off... er, flag-ON/legacy-fallback path).
- The legacy standalone (no-WaddleAI-JWT) `ScopeContext` fallback isolates
  sessions by the HS256 token's `sub`.

# regression: penguincode-shared-chat-sessions (O4-a High -- ChatServiceImpl)
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import jwt
import pytest

import penguincode_cli.auth.middleware as auth_middleware
import penguincode_cli.server.services.chat as chat_module
from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import Settings
from penguincode_cli.ollama import Message
from penguincode_cli.proto import (
    ChatRequest,
    ClientCapabilities,
    CloseSessionRequest,
    CreateSessionRequest,
    GetHistoryRequest,
)
from penguincode_cli.sessions.store import InMemorySessionStore, reset_for_testing


class AbortCalledError(Exception):
    """Raised by `_FakeContext.abort` -- mirrors real `grpc.aio` abort semantics."""


class _FakeContext:
    """Minimal `grpc.aio.ServicerContext` double: records the abort call and raises.

    `invocation_metadata` defaults to empty (the common case: a real
    `ScopeContext` is already installed via the `scope_ctx` fixture, so
    `_legacy_user_id` is never reached) -- legacy-mode tests pass their own
    metadata via the constructor.
    """

    def __init__(self, metadata: list[tuple[str, str]] | None = None) -> None:
        self.aborted_with: tuple[Any, str] | None = None
        self._metadata = metadata or []

    async def abort(self, code: Any, details: str) -> None:
        self.aborted_with = (code, details)
        raise AbortCalledError(details)

    def invocation_metadata(self) -> list[tuple[str, str]]:
        return self._metadata


class _FakeChatAgent:
    """Stand-in for `agents.chat.ChatAgent`: same `conversation_history`/
    `conversation_summary` contract, no Ollama/network involved."""

    def __init__(
        self, *, ollama_client: Any, settings: Any, project_dir: str, session_id: str
    ) -> None:
        self.project_dir = project_dir
        self.session_id = session_id
        self.conversation_history: list[Message] = []
        self.conversation_summary: str = ""

    async def process(self, user_message: str) -> str:
        self.conversation_history.append(Message(role="user", content=user_message))
        response = f"echo: {user_message}"
        self.conversation_history.append(Message(role="assistant", content=response))
        return response


def _ctx(tenant_id: str = "tenant-a", **overrides: Any) -> ScopeContext:
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "org_id": "org-a",
        "team_ids": ("team-a",),
        "user_id": "user-a",
        "scopes": (),
    }
    defaults.update(overrides)
    return ScopeContext(**defaults)


@pytest.fixture
def scope_ctx() -> Iterator[ScopeContext]:
    """Install a real `ScopeContext` into the auth contextvar for the test's duration."""
    ctx = _ctx()
    token = auth_middleware._current_scope.set(ctx)
    yield ctx
    auth_middleware._current_scope.reset(token)


@pytest.fixture(autouse=True)
def _no_leftover_scope() -> Iterator[None]:
    """Guarantee `current_scope_context()` is `None` by default in every test."""
    assert auth_middleware.current_scope_context() is None
    yield
    auth_middleware._current_scope.set(None)


@pytest.fixture(autouse=True)
def _fake_chat_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test uses `_FakeChatAgent` -- no real ChatAgent/Ollama involved."""
    monkeypatch.setattr(chat_module, "ChatAgent", _FakeChatAgent)


@pytest.fixture(autouse=True)
def _reset_session_store_singletons() -> Iterator[None]:
    """Tests that go through the real factory (kill-switch path) get a clean slate."""
    reset_for_testing()
    yield
    reset_for_testing()


def _make_service(
    *, store: InMemorySessionStore | None = None, settings: Settings | None = None
) -> chat_module.ChatServiceImpl:
    service = chat_module.ChatServiceImpl(
        settings or Settings(),
        session_store=store if store is not None else InMemorySessionStore(),
        enable_sweeper=False,
    )
    service._check_ollama_connection = AsyncMock(return_value=True)  # type: ignore[method-assign]
    service._get_available_models = AsyncMock(return_value=[])  # type: ignore[method-assign]
    return service


async def _chat_responses(
    service: chat_module.ChatServiceImpl, request: ChatRequest, context: Any
) -> list[Any]:
    return [resp async for resp in service.Chat(request, context)]


class TestCreateSession:
    @pytest.mark.asyncio
    async def test_persists_session_scoped_to_caller(self, scope_ctx: ScopeContext) -> None:
        store = InMemorySessionStore()
        service = _make_service(store=store)
        context = _FakeContext()

        response = await service.CreateSession(
            CreateSessionRequest(
                project_dir="/tmp/proj",
                capabilities=ClientCapabilities(available_tools=["read", "write"]),
            ),
            context,
        )

        assert response.session_id
        record = store.get(scope_ctx, response.session_id)
        assert record is not None
        assert record.project_dir == "/tmp/proj"
        assert record.client_tools == ("read", "write")


class TestChat:
    @pytest.mark.asyncio
    async def test_unknown_session_yields_session_not_found(self, scope_ctx: ScopeContext) -> None:
        service = _make_service()
        responses = await _chat_responses(
            service, ChatRequest(session_id="nope", message="hi"), _FakeContext()
        )
        assert len(responses) == 1
        assert responses[0].error.code == "SESSION_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_happy_path_persists_updated_conversation(self, scope_ctx: ScopeContext) -> None:
        store = InMemorySessionStore()
        service = _make_service(store=store)
        context = _FakeContext()

        created = await service.CreateSession(
            CreateSessionRequest(project_dir="/tmp/proj", capabilities=ClientCapabilities()),
            context,
        )

        responses = await _chat_responses(
            service, ChatRequest(session_id=created.session_id, message="hello"), context
        )

        kinds = [r.WhichOneof("response_type") for r in responses]
        assert "status" in kinds
        assert "text" in kinds
        text_response = next(r for r in responses if r.WhichOneof("response_type") == "text")
        assert text_response.text.content == "echo: hello"

        record = store.get(scope_ctx, created.session_id)
        assert record is not None
        assert record.state["messages"] == [
            {"role": "user", "content": "hello", "images": None, "tool_calls": None},
            {"role": "assistant", "content": "echo: hello", "images": None, "tool_calls": None},
        ]

    @pytest.mark.asyncio
    async def test_wrong_scope_cannot_chat_on_someone_elses_session(self) -> None:
        store = InMemorySessionStore()
        service = _make_service(store=store)

        owner_token = auth_middleware._current_scope.set(
            _ctx(tenant_id="tenant-a", user_id="user-a")
        )
        created = await service.CreateSession(
            CreateSessionRequest(project_dir="/tmp/proj", capabilities=ClientCapabilities()),
            _FakeContext(),
        )
        auth_middleware._current_scope.reset(owner_token)

        intruder_token = auth_middleware._current_scope.set(
            _ctx(tenant_id="tenant-b", user_id="user-b")
        )
        try:
            responses = await _chat_responses(
                service, ChatRequest(session_id=created.session_id, message="hi"), _FakeContext()
            )
        finally:
            auth_middleware._current_scope.reset(intruder_token)

        assert responses[0].error.code == "SESSION_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_two_servicer_instances_sharing_a_store_share_sessions(self) -> None:
        """The actual O4-a regression: a session created on `service_a` (pod A)
        must be usable through `service_b` (pod B) -- the whole point of
        moving state out of the per-process dict."""
        shared_store = InMemorySessionStore()
        service_a = _make_service(store=shared_store)
        service_b = _make_service(store=shared_store)

        token = auth_middleware._current_scope.set(_ctx())
        try:
            created = await service_a.CreateSession(
                CreateSessionRequest(project_dir="/tmp/proj", capabilities=ClientCapabilities()),
                _FakeContext(),
            )

            # regression: penguincode-shared-chat-sessions (O4-a High)
            responses = await _chat_responses(
                service_b,
                ChatRequest(session_id=created.session_id, message="from pod b"),
                _FakeContext(),
            )
            text_response = next(r for r in responses if r.WhichOneof("response_type") == "text")
            assert text_response.text.content == "echo: from pod b"

            history = await service_a.GetHistory(
                GetHistoryRequest(session_id=created.session_id, limit=10), _FakeContext()
            )
            assert [m.content for m in history.messages] == ["from pod b", "echo: from pod b"]
        finally:
            auth_middleware._current_scope.reset(token)


class TestGetHistory:
    @pytest.mark.asyncio
    async def test_unknown_session_aborts_not_found(self, scope_ctx: ScopeContext) -> None:
        service = _make_service()
        with pytest.raises(AbortCalledError):
            await service.GetHistory(GetHistoryRequest(session_id="nope", limit=10), _FakeContext())


class TestCloseSession:
    @pytest.mark.asyncio
    async def test_deletes_the_session(self, scope_ctx: ScopeContext) -> None:
        store = InMemorySessionStore()
        service = _make_service(store=store)
        context = _FakeContext()
        created = await service.CreateSession(
            CreateSessionRequest(project_dir="/tmp/proj", capabilities=ClientCapabilities()),
            context,
        )

        response = await service.CloseSession(
            CloseSessionRequest(session_id=created.session_id), context
        )
        assert response.success is True
        assert store.get(scope_ctx, created.session_id) is None

    @pytest.mark.asyncio
    async def test_close_then_chat_yields_session_not_found(self, scope_ctx: ScopeContext) -> None:
        service = _make_service()
        context = _FakeContext()
        created = await service.CreateSession(
            CreateSessionRequest(project_dir="/tmp/proj", capabilities=ClientCapabilities()),
            context,
        )
        await service.CloseSession(CloseSessionRequest(session_id=created.session_id), context)

        responses = await _chat_responses(
            service, ChatRequest(session_id=created.session_id, message="hi"), context
        )
        assert responses[0].error.code == "SESSION_NOT_FOUND"


class TestLegacyStandaloneScope:
    """No WaddleAI `ScopeContext` -- the fixed pseudo-tenant, scoped by the
    legacy HS256 token's `sub` (already-verified by the upstream
    interceptor; see `_legacy_user_id`'s docstring)."""

    @staticmethod
    def _legacy_context(sub: str) -> _FakeContext:
        token = jwt.encode(
            {"sub": sub}, "unused-test-secret-at-least-32-bytes-long", algorithm="HS256"
        )
        return _FakeContext(metadata=[("authorization", f"Bearer {token}")])

    @pytest.mark.asyncio
    async def test_same_sub_can_chat_on_its_own_session(self) -> None:
        store = InMemorySessionStore()
        service = _make_service(store=store)

        created = await service.CreateSession(
            CreateSessionRequest(project_dir="/tmp/proj", capabilities=ClientCapabilities()),
            self._legacy_context("alice"),
        )
        responses = await _chat_responses(
            service,
            ChatRequest(session_id=created.session_id, message="hi"),
            self._legacy_context("alice"),
        )
        assert responses[-1].WhichOneof("response_type") == "text"

    @pytest.mark.asyncio
    async def test_different_sub_cannot_see_the_session(self) -> None:
        store = InMemorySessionStore()
        service = _make_service(store=store)

        created = await service.CreateSession(
            CreateSessionRequest(project_dir="/tmp/proj", capabilities=ClientCapabilities()),
            self._legacy_context("alice"),
        )
        responses = await _chat_responses(
            service,
            ChatRequest(session_id=created.session_id, message="hi"),
            self._legacy_context("bob"),
        )
        assert responses[0].error.code == "SESSION_NOT_FOUND"


class TestKillSwitchFallback:
    """`penguincode.disable-shared-sessions` ON -- the in-memory legacy fallback
    still serves a full create->chat->history->close cycle correctly."""

    @pytest.mark.asyncio
    async def test_full_cycle_works_through_the_in_memory_fallback(
        self, scope_ctx: ScopeContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_SHARED_SESSIONS", "true")
        settings = Settings()
        service = chat_module.ChatServiceImpl(settings, enable_sweeper=False)
        service._check_ollama_connection = AsyncMock(return_value=True)  # type: ignore[method-assign]
        service._get_available_models = AsyncMock(return_value=[])  # type: ignore[method-assign]
        context = _FakeContext()

        created = await service.CreateSession(
            CreateSessionRequest(project_dir="/tmp/proj", capabilities=ClientCapabilities()),
            context,
        )
        responses = await _chat_responses(
            service, ChatRequest(session_id=created.session_id, message="hi"), context
        )
        assert responses[-1].WhichOneof("response_type") == "text"

        history = await service.GetHistory(
            GetHistoryRequest(session_id=created.session_id, limit=10), context
        )
        assert len(history.messages) == 2

        closed = await service.CloseSession(
            CloseSessionRequest(session_id=created.session_id), context
        )
        assert closed.success is True


class TestLegacyUserIdFallback:
    def test_no_token_in_metadata_returns_anonymous(self) -> None:
        assert chat_module._legacy_user_id(_FakeContext(metadata=[])) == "anonymous"

    def test_malformed_token_returns_anonymous(self) -> None:
        context = _FakeContext(metadata=[("authorization", "Bearer not-a-jwt")])
        assert chat_module._legacy_user_id(context) == "anonymous"


class TestMessageDictRoundTrip:
    def test_tool_calls_round_trip(self) -> None:
        from penguincode_cli.ollama import ToolCall

        message = Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(function={"name": "read_file", "arguments": {"path": "a.py"}})],
        )
        restored = chat_module._message_from_dict(chat_module._message_to_dict(message))
        assert restored.tool_calls is not None
        assert restored.tool_calls[0].function == {
            "name": "read_file",
            "arguments": {"path": "a.py"},
        }


class TestSweeperLifecycle:
    @pytest.mark.asyncio
    async def test_ensure_sweeper_started_creates_a_background_task_once(self) -> None:
        service = _make_service()
        service._enable_sweeper = True

        await service._ensure_sweeper_started()
        task = service._sweeper_task
        assert task is not None
        assert not task.done()

        # Calling again must not replace the already-running task.
        await service._ensure_sweeper_started()
        assert service._sweeper_task is task

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestOllamaHealthChecks:
    """Exercises `_check_ollama_connection`/`_get_available_models`'s real
    bodies (both tests mock the two methods away elsewhere in this file)."""

    @pytest.mark.asyncio
    async def test_check_ollama_connection_true_when_list_models_succeeds(self) -> None:
        service = _make_service()
        fake_client = AsyncMock()
        fake_client.list_models = AsyncMock(return_value=[])
        service._get_ollama_client = AsyncMock(return_value=fake_client)  # type: ignore[method-assign]
        assert await chat_module.ChatServiceImpl._check_ollama_connection(service) is True

    @pytest.mark.asyncio
    async def test_check_ollama_connection_false_when_it_raises(self) -> None:
        service = _make_service()
        service._get_ollama_client = AsyncMock(side_effect=RuntimeError("no ollama"))  # type: ignore[method-assign]
        assert await chat_module.ChatServiceImpl._check_ollama_connection(service) is False

    @pytest.mark.asyncio
    async def test_get_available_models_returns_names_on_success(self) -> None:
        service = _make_service()
        model = type("Model", (), {"name": "gemma4:12b-it-qat"})()
        fake_client = AsyncMock()
        fake_client.list_models = AsyncMock(return_value=[model])
        service._get_ollama_client = AsyncMock(return_value=fake_client)  # type: ignore[method-assign]
        assert await chat_module.ChatServiceImpl._get_available_models(service) == [
            "gemma4:12b-it-qat"
        ]

    @pytest.mark.asyncio
    async def test_get_available_models_returns_empty_on_failure(self) -> None:
        service = _make_service()
        service._get_ollama_client = AsyncMock(side_effect=RuntimeError("no ollama"))  # type: ignore[method-assign]
        assert await chat_module.ChatServiceImpl._get_available_models(service) == []


class _RaisingCreateStore(InMemorySessionStore):
    """Forces `CreateSession`'s `except Exception` branch."""

    def create(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("db unreachable")


class _LookupErrorOnUpdateStore(InMemorySessionStore):
    """Forces `Chat`'s "session vanished mid-turn" `LookupError` branch."""

    def update_state(self, *args: Any, **kwargs: Any) -> None:
        raise LookupError("session vanished")


class TestCreateSessionErrorPath:
    @pytest.mark.asyncio
    async def test_store_failure_aborts_internal(self, scope_ctx: ScopeContext) -> None:
        service = _make_service(store=_RaisingCreateStore())
        with pytest.raises(AbortCalledError):
            await service.CreateSession(
                CreateSessionRequest(project_dir="/tmp/proj", capabilities=ClientCapabilities()),
                _FakeContext(),
            )


class TestChatErrorPaths:
    @pytest.mark.asyncio
    async def test_update_state_lookup_error_still_yields_the_response(
        self, scope_ctx: ScopeContext
    ) -> None:
        store = _LookupErrorOnUpdateStore()
        service = _make_service(store=store)
        context = _FakeContext()
        created = await service.CreateSession(
            CreateSessionRequest(project_dir="/tmp/proj", capabilities=ClientCapabilities()),
            context,
        )

        responses = await _chat_responses(
            service, ChatRequest(session_id=created.session_id, message="hi"), context
        )
        text_response = next(r for r in responses if r.WhichOneof("response_type") == "text")
        assert text_response.text.content == "echo: hi"

    @pytest.mark.asyncio
    async def test_chat_agent_process_failure_yields_chat_error(
        self, scope_ctx: ScopeContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FailingChatAgent(_FakeChatAgent):
            async def process(self, user_message: str) -> str:
                raise RuntimeError("ollama exploded")

        monkeypatch.setattr(chat_module, "ChatAgent", _FailingChatAgent)
        service = _make_service()
        context = _FakeContext()
        created = await service.CreateSession(
            CreateSessionRequest(project_dir="/tmp/proj", capabilities=ClientCapabilities()),
            context,
        )

        responses = await _chat_responses(
            service, ChatRequest(session_id=created.session_id, message="hi"), context
        )
        assert responses[-1].error.code == "CHAT_ERROR"


class TestCloseSessionUnknown:
    @pytest.mark.asyncio
    async def test_unknown_session_returns_false_without_logging_closed(
        self, scope_ctx: ScopeContext
    ) -> None:
        service = _make_service()
        response = await service.CloseSession(
            CloseSessionRequest(session_id="nope"), _FakeContext()
        )
        assert response.success is False


class TestActiveSessionCount:
    @pytest.mark.asyncio
    async def test_delegates_to_the_store(self, scope_ctx: ScopeContext) -> None:
        store = InMemorySessionStore()
        store.create(scope_ctx, "s1", "/tmp/proj", [], {}, ttl_seconds=3600)
        service = _make_service(store=store)
        assert await service.active_session_count() == 1


class TestGetOllamaClientCaching:
    @pytest.mark.asyncio
    async def test_second_call_reuses_the_cached_client(self) -> None:
        service = _make_service()
        first = await chat_module.ChatServiceImpl._get_ollama_client(service)
        second = await chat_module.ChatServiceImpl._get_ollama_client(service)
        assert first is second


class TestCreateSessionStartsSweeperWhenEnabled:
    @pytest.mark.asyncio
    async def test_create_session_starts_the_sweeper_once_enabled(
        self, scope_ctx: ScopeContext
    ) -> None:
        service = _make_service()
        service._enable_sweeper = True
        context = _FakeContext()

        await service.CreateSession(
            CreateSessionRequest(project_dir="/tmp/proj", capabilities=ClientCapabilities()),
            context,
        )
        task = service._sweeper_task
        assert task is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestChatLoadFailure:
    @pytest.mark.asyncio
    async def test_store_get_raising_yields_chat_error(self, scope_ctx: ScopeContext) -> None:
        class _RaisingGetStore(InMemorySessionStore):
            def get(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("db unreachable")

        service = _make_service(store=_RaisingGetStore())
        responses = await _chat_responses(
            service, ChatRequest(session_id="whatever", message="hi"), _FakeContext()
        )
        assert responses[0].error.code == "CHAT_ERROR"
