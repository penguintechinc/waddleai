"""Chat service implementation wrapping ChatAgent."""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator

import grpc

from penguincode_cli.agents import ChatAgent
from penguincode_cli.config.settings import Settings
from penguincode_cli.ollama import OllamaClient
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

logger = logging.getLogger(__name__)


class SessionState:
    """State for an active chat session."""

    def __init__(
        self,
        session_id: str,
        project_dir: str,
        chat_agent: ChatAgent,
        client_tools: list[str],
    ):
        self.session_id = session_id
        self.project_dir = project_dir
        self.chat_agent = chat_agent
        self.client_tools = client_tools
        self.created_at = time.time()
        self.last_activity = time.time()

        # Tool callback management
        self.pending_tool_requests: dict[str, asyncio.Future] = {}

    def update_activity(self):
        self.last_activity = time.time()


class ChatServiceImpl(ChatServiceServicer):
    """Chat service that wraps ChatAgent for gRPC.

    Manages multiple sessions and handles streaming responses.
    """

    VERSION = "0.1.0"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.sessions: dict[str, SessionState] = {}
        self._ollama_client: OllamaClient | None = None
        self._lock = asyncio.Lock()

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
        """Create a new chat session."""
        session_id = str(uuid.uuid4())

        # Get client capabilities
        client_tools = list(request.capabilities.available_tools) if request.capabilities else []

        # Create Ollama client and ChatAgent
        try:
            ollama_client = await self._get_ollama_client()

            chat_agent = ChatAgent(
                ollama_client=ollama_client,
                settings=self.settings,
                project_dir=request.project_dir,
                session_id=session_id,
            )

            # Store session
            session = SessionState(
                session_id=session_id,
                project_dir=request.project_dir,
                chat_agent=chat_agent,
                client_tools=client_tools,
            )

            async with self._lock:
                self.sessions[session_id] = session

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
        """Handle a chat message and stream responses."""
        # Get session
        session = self.sessions.get(request.session_id)
        if not session:
            yield ChatResponse(
                error=Error(
                    code="SESSION_NOT_FOUND",
                    message=f"Session {request.session_id} not found",
                    recoverable=False,
                )
            )
            return

        session.update_activity()

        try:
            # Send status update
            yield ChatResponse(
                status=StatusUpdate(
                    status="processing",
                    message="Processing your request...",
                )
            )

            # Process message through ChatAgent
            # Note: In full implementation, we'd hook into agent spawning
            # to yield AgentSpawn/AgentResult messages
            start_time = time.time()
            response = await session.chat_agent.process(request.message)
            int((time.time() - start_time) * 1000)

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
        """Get conversation history for a session."""
        session = self.sessions.get(request.session_id)
        if not session:
            await context.abort(
                grpc.StatusCode.NOT_FOUND,
                f"Session {request.session_id} not found",
            )

        # Get history from ChatAgent
        history = session.chat_agent.conversation_history
        limit = request.limit or 50

        messages = []
        for msg in history[-limit:]:
            messages.append(
                HistoryMessage(
                    role=msg.role,
                    content=msg.content,
                    timestamp="",  # TODO: Add timestamps to Message
                )
            )

        return GetHistoryResponse(messages=messages)

    async def CloseSession(
        self,
        request: CloseSessionRequest,
        context: grpc.aio.ServicerContext,
    ) -> CloseSessionResponse:
        """Close a chat session."""
        async with self._lock:
            session = self.sessions.pop(request.session_id, None)

        if session:
            logger.info(f"Closed session {request.session_id}")
            return CloseSessionResponse(success=True)
        else:
            return CloseSessionResponse(success=False)

    async def cleanup_stale_sessions(self, max_age_seconds: int = 3600) -> int:
        """Clean up sessions that have been inactive too long.

        Returns number of sessions cleaned up.
        """
        now = time.time()
        stale_sessions = []

        async with self._lock:
            for session_id, session in self.sessions.items():
                if now - session.last_activity > max_age_seconds:
                    stale_sessions.append(session_id)

            for session_id in stale_sessions:
                del self.sessions[session_id]
                logger.info(f"Cleaned up stale session {session_id}")

        return len(stale_sessions)
