"""Generated gRPC code for PenguinCode client-server communication."""

from .penguincode_pb2 import (
    AgentResult,
    AgentSpawn,
    # Auth messages
    AuthRequest,
    AuthResponse,
    ChatRequest,
    ChatResponse,
    ClientCapabilities,
    CloseSessionRequest,
    CloseSessionResponse,
    # Chat messages
    CreateSessionRequest,
    CreateSessionResponse,
    Error,
    GetHistoryRequest,
    GetHistoryResponse,
    # Health messages
    HealthCheckRequest,
    HealthCheckResponse,
    HistoryMessage,
    RefreshRequest,
    ServerInfo,
    StatusUpdate,
    TextChunk,
    # Tool messages
    ToolRequest,
    ToolResponse,
    ValidateRequest,
    ValidateResponse,
)
from .penguincode_pb2_grpc import (
    # Service servicers (server-side)
    AuthServiceServicer,
    # Service stubs (client-side)
    AuthServiceStub,
    ChatServiceServicer,
    ChatServiceStub,
    HealthServiceServicer,
    HealthServiceStub,
    ToolCallbackServiceServicer,
    ToolCallbackServiceStub,
    # Server registration functions
    add_AuthServiceServicer_to_server,
    add_ChatServiceServicer_to_server,
    add_HealthServiceServicer_to_server,
    add_ToolCallbackServiceServicer_to_server,
)

__all__ = [
    # Auth
    "AuthRequest",
    "AuthResponse",
    "RefreshRequest",
    "ValidateRequest",
    "ValidateResponse",
    # Chat
    "CreateSessionRequest",
    "CreateSessionResponse",
    "ClientCapabilities",
    "ServerInfo",
    "ChatRequest",
    "ChatResponse",
    "TextChunk",
    "AgentSpawn",
    "AgentResult",
    "StatusUpdate",
    "Error",
    "GetHistoryRequest",
    "GetHistoryResponse",
    "HistoryMessage",
    "CloseSessionRequest",
    "CloseSessionResponse",
    # Tools
    "ToolRequest",
    "ToolResponse",
    # Health
    "HealthCheckRequest",
    "HealthCheckResponse",
    # Stubs
    "AuthServiceStub",
    "ChatServiceStub",
    "ToolCallbackServiceStub",
    "HealthServiceStub",
    # Servicers
    "AuthServiceServicer",
    "ChatServiceServicer",
    "ToolCallbackServiceServicer",
    "HealthServiceServicer",
    # Registration
    "add_AuthServiceServicer_to_server",
    "add_ChatServiceServicer_to_server",
    "add_ToolCallbackServiceServicer_to_server",
    "add_HealthServiceServicer_to_server",
]
