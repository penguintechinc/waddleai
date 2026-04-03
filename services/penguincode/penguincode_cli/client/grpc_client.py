"""gRPC client for connecting to PenguinCode server."""

import asyncio
import logging
from collections.abc import AsyncIterator

import grpc

from penguincode_cli.config.settings import ClientConfig, ServerConfig
from penguincode_cli.proto import (
    AuthRequest,
    AuthServiceStub,
    ChatRequest,
    ChatServiceStub,
    ClientCapabilities,
    CloseSessionRequest,
    CreateSessionRequest,
    GetHistoryRequest,
    HealthCheckRequest,
    HealthServiceStub,
    ToolCallbackServiceStub,
    ToolResponse,
)
from penguincode_cli.shared.interfaces import IChatService, ToolResult

from .auth import TokenManager

logger = logging.getLogger(__name__)


class GRPCClient(IChatService):
    """gRPC client that implements IChatService interface.

    Connects to a remote PenguinCode server and handles:
    - Authentication with JWT tokens
    - Session management
    - Streaming chat responses
    - Tool callback handling
    """

    def __init__(
        self,
        server_config: ServerConfig,
        client_config: ClientConfig,
        token_manager: TokenManager | None = None,
    ):
        self.server_config = server_config
        self.client_config = client_config
        self.token_manager = token_manager or TokenManager(client_config.token_path)

        self._channel: grpc.aio.Channel | None = None
        self._auth_stub: AuthServiceStub | None = None
        self._chat_stub: ChatServiceStub | None = None
        self._tool_stub: ToolCallbackServiceStub | None = None
        self._health_stub: HealthServiceStub | None = None

        self._current_session_id: str | None = None
        self._tool_callback_task: asyncio.Task | None = None

    async def connect(self) -> bool:
        """Connect to the gRPC server.

        Returns True if connection successful.
        """
        try:
            # Build server address
            address = f"{self.server_config.host}:{self.server_config.port}"

            # Create channel with or without TLS
            if self.server_config.tls_enabled:
                # TODO: Load TLS credentials
                credentials = grpc.ssl_channel_credentials()
                self._channel = grpc.aio.secure_channel(address, credentials)
            else:
                self._channel = grpc.aio.insecure_channel(address)

            # Create stubs
            self._auth_stub = AuthServiceStub(self._channel)
            self._chat_stub = ChatServiceStub(self._channel)
            self._tool_stub = ToolCallbackServiceStub(self._channel)
            self._health_stub = HealthServiceStub(self._channel)

            # Test connection with health check
            response = await self._health_stub.Check(HealthCheckRequest())
            logger.info(f"Connected to server version {response.version}")

            return True

        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            return False

    async def disconnect(self) -> None:
        """Disconnect from the server."""
        if self._tool_callback_task:
            self._tool_callback_task.cancel()
            try:
                await self._tool_callback_task
            except asyncio.CancelledError:
                pass

        if self._channel:
            await self._channel.close()
            self._channel = None

        logger.info("Disconnected from server")

    async def authenticate(self, api_key: str, client_id: str = "") -> bool:
        """Authenticate with the server.

        Args:
            api_key: API key for authentication
            client_id: Optional client identifier

        Returns True if authentication successful.
        """
        if not self._auth_stub:
            raise RuntimeError("Not connected to server")

        try:
            response = await self._auth_stub.Authenticate(
                AuthRequest(api_key=api_key, client_id=client_id)
            )

            # Store token
            self.token_manager.store_token(
                response.access_token,
                response.refresh_token,
                response.expires_in,
            )

            logger.info("Authentication successful")
            return True

        except grpc.RpcError as e:
            logger.error(f"Authentication failed: {e.details()}")
            return False

    def _get_auth_metadata(self) -> list[tuple]:
        """Get authentication metadata for requests."""
        token = self.token_manager.get_token()
        if token:
            return [("authorization", f"Bearer {token}")]
        return []

    async def create_session(
        self,
        project_dir: str,
        available_tools: list[str],
    ) -> str:
        """Create a new chat session."""
        if not self._chat_stub:
            raise RuntimeError("Not connected to server")

        response = await self._chat_stub.CreateSession(
            CreateSessionRequest(
                project_dir=project_dir,
                capabilities=ClientCapabilities(
                    available_tools=available_tools,
                    platform="linux",  # TODO: Detect platform
                ),
            ),
            metadata=self._get_auth_metadata(),
        )

        self._current_session_id = response.session_id
        logger.info(f"Created session {response.session_id}")

        # Start tool callback handler
        await self._start_tool_callback_handler()

        return response.session_id

    async def chat(
        self,
        session_id: str,
        message: str,
    ) -> AsyncIterator[dict[str, any]]:
        """Send a chat message and receive streaming responses."""
        if not self._chat_stub:
            raise RuntimeError("Not connected to server")

        try:
            metadata = self._get_auth_metadata()
            metadata.append(("session-id", session_id))

            async for response in self._chat_stub.Chat(
                ChatRequest(session_id=session_id, message=message),
                metadata=metadata,
            ):
                # Convert protobuf response to dict
                which_one = response.WhichOneof("response_type")

                if which_one == "text":
                    yield {
                        "type": "text",
                        "content": response.text.content,
                        "is_final": response.text.is_final,
                    }
                elif which_one == "tool_request":
                    yield {
                        "type": "tool_request",
                        "request_id": response.tool_request.request_id,
                        "tool": response.tool_request.tool_name,
                        "args": dict(response.tool_request.arguments),
                    }
                elif which_one == "agent_spawn":
                    yield {
                        "type": "agent_spawn",
                        "agent_type": response.agent_spawn.agent_type,
                        "task": response.agent_spawn.task,
                    }
                elif which_one == "agent_result":
                    yield {
                        "type": "agent_result",
                        "agent_type": response.agent_result.agent_type,
                        "success": response.agent_result.success,
                        "output": response.agent_result.output,
                    }
                elif which_one == "status":
                    yield {
                        "type": "status",
                        "status": response.status.status,
                        "message": response.status.message,
                    }
                elif which_one == "error":
                    yield {
                        "type": "error",
                        "code": response.error.code,
                        "message": response.error.message,
                        "recoverable": response.error.recoverable,
                    }

        except grpc.RpcError as e:
            yield {
                "type": "error",
                "code": "RPC_ERROR",
                "message": str(e.details()),
                "recoverable": True,
            }

    async def submit_tool_result(
        self,
        session_id: str,
        request_id: str,
        result: ToolResult,
    ) -> None:
        """Submit a tool execution result."""
        if self._tool_response_queue:
            await self._tool_response_queue.put(
                ToolResponse(
                    request_id=request_id,
                    success=result.success,
                    data=result.data,
                    error=result.error,
                )
            )

    async def get_history(
        self,
        session_id: str,
        limit: int = 50,
    ) -> list[dict[str, any]]:
        """Get conversation history."""
        if not self._chat_stub:
            raise RuntimeError("Not connected to server")

        response = await self._chat_stub.GetHistory(
            GetHistoryRequest(session_id=session_id, limit=limit),
            metadata=self._get_auth_metadata(),
        )

        return [
            {
                "role": msg.role,
                "content": msg.content,
                "timestamp": msg.timestamp,
            }
            for msg in response.messages
        ]

    async def close_session(self, session_id: str) -> bool:
        """Close a chat session."""
        if not self._chat_stub:
            raise RuntimeError("Not connected to server")

        response = await self._chat_stub.CloseSession(
            CloseSessionRequest(session_id=session_id),
            metadata=self._get_auth_metadata(),
        )

        if response.success:
            if self._tool_callback_task:
                self._tool_callback_task.cancel()
            self._current_session_id = None

        return response.success

    async def _start_tool_callback_handler(self) -> None:
        """Start the tool callback handler for bidirectional streaming."""
        if not self._tool_stub or not self._current_session_id:
            return

        self._tool_response_queue = asyncio.Queue()
        self._tool_callback_task = asyncio.create_task(
            self._tool_callback_loop()
        )

    async def _tool_callback_loop(self) -> None:
        """Handle tool callback requests from server."""
        if not self._tool_stub:
            return

        async def response_generator():
            while True:
                response = await self._tool_response_queue.get()
                yield response

        try:
            metadata = self._get_auth_metadata()
            metadata.append(("session-id", self._current_session_id))

            async for request in self._tool_stub.ExecuteTools(
                response_generator(),
                metadata=metadata,
            ):
                # Emit event for tool execution
                # The REPL will handle actual execution
                logger.debug(f"Tool request: {request.tool_name}")
                # This would be connected to the tool executor

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Tool callback error: {e}")
