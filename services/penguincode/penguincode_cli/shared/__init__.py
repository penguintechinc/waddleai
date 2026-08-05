"""Shared interfaces and types for PenguinCode client-server architecture."""

from .interfaces import IAuthService, IChatService, IToolExecutor, ToolResult
from .types import AgentStatus, ChatMessage, ServerMode, SessionInfo

__all__ = [
    # Interfaces
    "IChatService",
    "IToolExecutor",
    "IAuthService",
    "ToolResult",
    # Types
    "SessionInfo",
    "ChatMessage",
    "AgentStatus",
    "ServerMode",
]
