"""Abstract interfaces for PenguinCode client-server architecture.

These interfaces allow the same codebase to work in:
- Local mode: Direct in-process execution (current behavior)
- Standalone mode: gRPC server on localhost
- Remote mode: gRPC server on remote host with JWT auth
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolResult:
    """Result from tool execution."""

    success: bool
    data: str = ""
    error: str = ""


class IToolExecutor(ABC):
    """Interface for executing tools.

    In local mode, tools execute directly in-process.
    In client-server mode, tools execute on the client side
    and results are sent back to the server via gRPC.
    """

    @abstractmethod
    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: int = 30,
    ) -> ToolResult:
        """Execute a tool with given arguments.

        Args:
            tool_name: Name of the tool (read, write, bash, etc.)
            arguments: Tool-specific arguments
            timeout: Execution timeout in seconds

        Returns:
            ToolResult with success status and data/error
        """
        pass

    @abstractmethod
    def get_available_tools(self) -> list[str]:
        """Get list of available tools.

        Returns:
            List of tool names this executor can handle
        """
        pass


class IChatService(ABC):
    """Interface for chat service.

    Abstracts the chat functionality to work both:
    - Locally: Direct ChatAgent execution
    - Remote: gRPC client to remote server
    """

    @abstractmethod
    async def create_session(
        self,
        project_dir: str,
        available_tools: list[str],
    ) -> str:
        """Create a new chat session.

        Args:
            project_dir: Project directory path
            available_tools: Tools available on the client

        Returns:
            Session ID
        """
        pass

    @abstractmethod
    async def chat(
        self,
        session_id: str,
        message: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a chat message and receive streaming responses.

        Args:
            session_id: Session identifier
            message: User message

        Yields:
            Response chunks which can be:
            - {"type": "text", "content": str, "is_final": bool}
            - {"type": "tool_request", "request_id": str, "tool": str, "args": dict}
            - {"type": "agent_spawn", "agent_type": str, "task": str}
            - {"type": "agent_result", "agent_type": str, "success": bool, "output": str}
            - {"type": "status", "status": str, "message": str}
            - {"type": "error", "code": str, "message": str, "recoverable": bool}
        """
        pass

    @abstractmethod
    async def submit_tool_result(
        self,
        session_id: str,
        request_id: str,
        result: ToolResult,
    ) -> None:
        """Submit a tool execution result.

        Args:
            session_id: Session identifier
            request_id: Tool request identifier
            result: Tool execution result
        """
        pass

    @abstractmethod
    async def get_history(
        self,
        session_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get conversation history.

        Args:
            session_id: Session identifier
            limit: Maximum messages to return

        Returns:
            List of message dicts with role, content, timestamp
        """
        pass

    @abstractmethod
    async def close_session(self, session_id: str) -> bool:
        """Close a chat session.

        Args:
            session_id: Session identifier

        Returns:
            True if closed successfully
        """
        pass


class IAuthService(ABC):
    """Interface for authentication service.

    Used in remote mode for JWT-based authentication.
    """

    @abstractmethod
    async def authenticate(
        self,
        api_key: str,
        client_id: str,
    ) -> str | None:
        """Authenticate with API key and get access token.

        Args:
            api_key: API key for authentication
            client_id: Client identifier

        Returns:
            JWT access token if successful, None otherwise
        """
        pass

    @abstractmethod
    async def refresh_token(self, refresh_token: str) -> str | None:
        """Refresh an access token.

        Args:
            refresh_token: Refresh token

        Returns:
            New access token if successful, None otherwise
        """
        pass

    @abstractmethod
    async def validate_token(self, access_token: str) -> bool:
        """Validate an access token.

        Args:
            access_token: Token to validate

        Returns:
            True if valid, False otherwise
        """
        pass

    @abstractmethod
    def get_token(self) -> str | None:
        """Get current access token.

        Returns:
            Current access token or None
        """
        pass
