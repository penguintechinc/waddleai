"""Shared types for PenguinCode client-server architecture."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ServerMode(Enum):
    """Server operation mode."""

    LOCAL = "local"  # In-process execution (default, current behavior)
    STANDALONE = "standalone"  # gRPC server on localhost
    REMOTE = "remote"  # gRPC server on remote host with auth


@dataclass
class SessionInfo:
    """Information about a chat session."""

    session_id: str
    project_dir: str
    created_at: datetime = field(default_factory=datetime.now)
    server_version: str = ""
    available_models: list[str] = field(default_factory=list)
    ollama_connected: bool = False


@dataclass
class ChatMessage:
    """A chat message in conversation history."""

    role: str  # "user", "assistant", "system"
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentStatus:
    """Status of agents in the system."""

    active_agents: int = 0
    available_slots: int = 5
    max_concurrent: int = 5
    queued_tasks: int = 0


@dataclass
class ToolRequest:
    """Request to execute a tool on the client."""

    request_id: str
    session_id: str
    tool_name: str
    arguments: dict[str, Any]
    timeout_seconds: int = 30


@dataclass
class ConnectionInfo:
    """Information about server connection."""

    mode: ServerMode
    host: str = "localhost"
    port: int = 50051
    tls_enabled: bool = False
    authenticated: bool = False
    token_expires_at: datetime | None = None
