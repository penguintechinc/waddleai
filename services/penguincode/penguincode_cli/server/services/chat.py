"""Chat service implementation wrapping ChatAgent.

**O4-a (security audit, High) fix.** This servicer used to keep
`sessions: dict[str, SessionState]` as in-process state while
CreateSession/Chat/GetHistory/CloseSession are four independent gRPC RPCs
and prod runs `replicas=3` -- a session created on one pod 404d on every
other pod, and a rolling deploy silently dropped every in-flight session.
State now lives in `sessions.store.SessionStore` (shared Postgres by
default, see `db/migrations/0007_chat_sessions.sql`), so any pod can serve
any RPC for any session: `Chat`/`GetHistory`/`CloseSession` load the
persisted row at the start of the call, and `Chat` persists the turn's
updated conversation back before returning. A live `ChatAgent` (and its
`OllamaClient`) is never itself persisted -- it is cheaply rebuilt from the
row's `project_dir` + serialized message history on every `Chat`/
`GetHistory` call instead (see `_restore_agent_state`/`_state_from_agent`).

Scope: a session is visible to its own tenant AND its owning user only --
see `sessions.store`'s module docstring for the full scope/TTL/kill-switch
contract, and `_scope_for_request` below for how that scope is derived
(the real WaddleAI `ScopeContext` when present, or a synthesized
single-tenant scope for penguincode's legacy standalone client-server
mode).
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import grpc
import jwt

from penguincode_cli.agents import ChatAgent
from penguincode_cli.auth.middleware import current_scope_context, extract_token_from_grpc_metadata
from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import Settings
from penguincode_cli.ollama import Message, OllamaClient, ToolCall
from penguincode_cli.proto import (
    ChatRequest,
    ChatResponse,
    ChatServiceServicer,
    CloseSessionRequest,
    CloseSessionResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    Error,
    GetHistoryRequest,
    GetHistoryResponse,
    HistoryMessage,
    ServerInfo,
    StatusUpdate,
    TextChunk,
)
from penguincode_cli.sessions.store import (
    LEGACY_TENANT_ID,
    SessionSweeper,
    create_session_store,
)
from penguincode_cli.sessions.store import SessionStore as SessionStoreProtocol

logger = logging.getLogger(__name__)


def _legacy_user_id(context: grpc.aio.ServicerContext) -> str:
    """Best-effort `sub` extraction from penguincode's legacy HS256 token.

    Only called when no WaddleAI `ScopeContext` is present (local
    standalone client-server mode, gated upstream by
    `server.interceptors.JWTValidationInterceptor`). That interceptor has
    already verified the token's signature before this RPC ever runs, so
    decoding it again here *without* re-verifying reads already-trusted
    data -- it is not a new trust boundary, just a convenience read.
    """
    token = extract_token_from_grpc_metadata(list(context.invocation_metadata() or []))
    if not token:
        return "anonymous"
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return "anonymous"
    return str(claims.get("sub") or "anonymous")


def _scope_for_request(context: grpc.aio.ServicerContext) -> ScopeContext:
    """The caller's `ScopeContext` for session scoping.

    Prefers the real, tenant-bounded `ScopeContext` set by
    `WaddleAIAuthInterceptor` (multi-tenant/SaaS deployments). Falls back
    to a synthesized single-tenant scope for penguincode's legacy
    standalone client-server mode (local HS256 auth, no WaddleAI JWT) --
    that mode has no tenant concept at all today, so every legacy session
    lives under one fixed pseudo-tenant (`LEGACY_TENANT_ID`), scoped only
    by the authenticated caller's `sub`.
    """
    ctx = current_scope_context()
    if ctx is not None:
        return ctx
    return ScopeContext(
        tenant_id=LEGACY_TENANT_ID,
        org_id=None,
        team_ids=(),
        user_id=_legacy_user_id(context),
        scopes=(),
    )


def _message_to_dict(message: Message) -> dict[str, Any]:
    """`ollama.types.Message` -> the JSON-safe shape persisted in `chat_sessions.state`."""
    return {
        "role": message.role,
        "content": message.content,
        "images": message.images,
        "tool_calls": [tc.function for tc in message.tool_calls] if message.tool_calls else None,
    }


def _message_from_dict(data: dict[str, Any]) -> Message:
    """Inverse of `_message_to_dict`."""
    tool_calls = data.get("tool_calls")
    return Message(
        role=data.get("role", "user"),
        content=data.get("content", ""),
        images=data.get("images"),
        tool_calls=[ToolCall(function=f) for f in tool_calls] if tool_calls else None,
    )


def _state_from_agent(chat_agent: ChatAgent) -> dict[str, Any]:
    """Serialize a `ChatAgent`'s conversation into `chat_sessions.state`'s shape."""
    return {
        "messages": [_message_to_dict(m) for m in chat_agent.conversation_history],
        "conversation_summary": chat_agent.conversation_summary,
    }


def _restore_agent_state(chat_agent: ChatAgent, state: dict[str, Any]) -> None:
    """Replay a persisted `state` dict onto a freshly constructed `ChatAgent`."""
    chat_agent.conversation_history = [_message_from_dict(m) for m in state.get("messages", [])]
    chat_agent.conversation_summary = state.get("conversation_summary", "")


class ChatServiceImpl(ChatServiceServicer):
    """Chat service that wraps ChatAgent for gRPC.

    Session state is delegated entirely to a `sessions.store.SessionStore`
    (see module docstring) -- this class holds no per-session dict of its
    own. `enable_sweeper`/`session_store` are keyword-only test seams,
    mirroring `LessonsServiceImpl`'s own constructor-injection convention.
    """

    VERSION = "0.1.0"

    def __init__(
        self,
        settings: Settings,
        *,
        session_store: SessionStoreProtocol | None = None,
        enable_sweeper: bool = True,
    ):
        self.settings = settings
        self._session_store_override = session_store
        self._enable_sweeper = enable_sweeper
        self._sweeper_task: asyncio.Task[None] | None = None
        self._sweeper_lock = asyncio.Lock()
        self._ollama_client: OllamaClient | None = None

    def _session_store(self) -> SessionStoreProtocol:
        """The active `SessionStore` -- the injected override (tests), or the process default."""
        if self._session_store_override is not None:
            return self._session_store_override
        return create_session_store(self.settings)

    async def _ensure_sweeper_started(self) -> None:
        """Lazily start this pod's background `SessionSweeper`, exactly once.

        Started on first RPC rather than in `__init__` because constructing
        a `ChatServiceImpl` must not require a running event loop (several
        call sites, including tests, build it synchronously) --
        `asyncio.create_task` does. Not wired into any graceful-shutdown
        hook today (`server/main.py`'s `PenguinCodeServer.stop()` has no
        callback into service implementations); this is a lightweight
        periodic DB sweep, not state that needs a clean handoff, so it is
        acceptable for it to simply end with the process.
        """
        if self._sweeper_task is not None:
            return
        async with self._sweeper_lock:
            if self._sweeper_task is not None:
                return
            sweeper = SessionSweeper(
                self._session_store(),
                interval_seconds=self.settings.sessions.sweep_interval_seconds,
                batch_size=self.settings.sessions.sweep_batch_size,
            )
            self._sweeper_task = asyncio.create_task(sweeper.run_forever())

    async def active_session_count(self) -> int:
        """Current non-expired session count, for `HealthServiceImpl`'s `active_sessions` field."""
        return await asyncio.to_thread(self._session_store().count_active)

    async def _get_ollama_client(self) -> OllamaClient:
        """Get or create Ollama client."""
        if self._ollama_client is None:
            self._ollama_client = OllamaClient(
                base_url=self.settings.ollama.api_url,
                timeout=self.settings.ollama.timeout,
            )
            await self._ollama_client.__aenter__()
        return self._ollama_client

    async def _check_ollama_connection(self) -> bool:
        """Check if Ollama is connected."""
        try:
            client = await self._get_ollama_client()
            # Try to list models as a health check
            await client.list_models()
            return True
        except Exception:
            return False

    async def _get_available_models(self) -> list[str]:
        """Get list of available Ollama models."""
        try:
            client = await self._get_ollama_client()
            models = await client.list_models()
            return [m.name for m in models]
        except Exception:
            return []

    async def CreateSession(
        self,
        request: CreateSessionRequest,
        context: grpc.aio.ServicerContext,
    ) -> CreateSessionResponse:
        """Create a new chat session, persisted to the shared `SessionStore`."""
        if self._enable_sweeper:
            await self._ensure_sweeper_started()

        ctx = _scope_for_request(context)
        session_id = str(uuid.uuid4())
        client_tools = list(request.capabilities.available_tools) if request.capabilities else []

        try:
            initial_state: dict[str, Any] = {"messages": [], "conversation_summary": ""}
            await asyncio.to_thread(
                self._session_store().create,
                ctx,
                session_id,
                request.project_dir,
                client_tools,
                initial_state,
                ttl_seconds=self.settings.sessions.ttl_seconds,
            )

            logger.info(f"Created session {session_id} for {request.project_dir}")

            # Get server info
            ollama_connected = await self._check_ollama_connection()
            available_models = await self._get_available_models()

            return CreateSessionResponse(
                session_id=session_id,
                server_info=ServerInfo(
                    version=self.VERSION,
                    available_models=available_models,
                    ollama_connected=ollama_connected,
                ),
            )

        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            await context.abort(
                grpc.StatusCode.INTERNAL,
                f"Failed to create session: {str(e)}",
            )

    async def Chat(
        self,
        request: ChatRequest,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[ChatResponse]:
        """Handle a chat message and stream responses.

        Loads the session's persisted state at the start of the call,
        rebuilds a `ChatAgent` from it, processes the turn, and persists
        the updated conversation back -- all three steps scoped to the
        caller's `ScopeContext` (tenant+user). The DB round trips bracket
        `ChatAgent.process`'s LLM call; no connection is held open across
        it. If the process crashes mid-turn (after the DB read, before the
        DB write), the response is still handed back to the caller once,
        but that turn's state never persists -- the session reverts to its
        last successfully-saved turn on the next call, never left
        half-written.
        """
        ctx = _scope_for_request(context)

        try:
            record = await asyncio.to_thread(self._session_store().get, ctx, request.session_id)
        except Exception as e:
            logger.error(f"Chat error loading session {request.session_id}: {e}")
            yield ChatResponse(error=Error(code="CHAT_ERROR", message=str(e), recoverable=True))
            return

        if record is None:
            yield ChatResponse(
                error=Error(
                    code="SESSION_NOT_FOUND",
                    message=f"Session {request.session_id} not found",
                    recoverable=False,
                )
            )
            return

        try:
            # Send status update
            yield ChatResponse(
                status=StatusUpdate(
                    status="processing",
                    message="Processing your request...",
                )
            )

            ollama_client = await self._get_ollama_client()
            chat_agent = ChatAgent(
                ollama_client=ollama_client,
                settings=self.settings,
                project_dir=record.project_dir,
                session_id=request.session_id,
            )
            _restore_agent_state(chat_agent, record.state)

            start_time = time.time()
            response = await chat_agent.process(request.message)
            int((time.time() - start_time) * 1000)

            try:
                await asyncio.to_thread(
                    self._session_store().update_state,
                    ctx,
                    request.session_id,
                    _state_from_agent(chat_agent),
                    ttl_seconds=self.settings.sessions.ttl_seconds,
                )
            except LookupError:
                # Session expired/closed during this turn's processing -- the
                # response below is still handed back once (see docstring),
                # but there is no longer a row to persist it into.
                logger.warning(
                    "session %s vanished before its turn's state could persist",
                    request.session_id,
                )

            # Yield the response
            yield ChatResponse(
                text=TextChunk(
                    content=response,
                    is_final=True,
                )
            )

        except Exception as e:
            logger.error(f"Chat error in session {request.session_id}: {e}")
            yield ChatResponse(
                error=Error(
                    code="CHAT_ERROR",
                    message=str(e),
                    recoverable=True,
                )
            )

    async def GetHistory(
        self,
        request: GetHistoryRequest,
        context: grpc.aio.ServicerContext,
    ) -> GetHistoryResponse:
        """Get conversation history for a session, from the shared `SessionStore`."""
        ctx = _scope_for_request(context)
        record = await asyncio.to_thread(self._session_store().get, ctx, request.session_id)
        if record is None:
            await context.abort(
                grpc.StatusCode.NOT_FOUND,
                f"Session {request.session_id} not found",
            )
        assert record is not None  # context.abort() always raises; unreachable otherwise

        limit = request.limit or 50
        persisted_messages = record.state.get("messages", [])

        messages = [
            HistoryMessage(
                role=msg.get("role", ""),
                content=msg.get("content", ""),
                timestamp="",  # TODO: Add timestamps to Message
            )
            for msg in persisted_messages[-limit:]
        ]

        return GetHistoryResponse(messages=messages)

    async def CloseSession(
        self,
        request: CloseSessionRequest,
        context: grpc.aio.ServicerContext,
    ) -> CloseSessionResponse:
        """Close a chat session, deleting it from the shared `SessionStore`."""
        ctx = _scope_for_request(context)
        deleted = await asyncio.to_thread(self._session_store().delete, ctx, request.session_id)

        if deleted:
            logger.info(f"Closed session {request.session_id}")
        return CloseSessionResponse(success=deleted)
