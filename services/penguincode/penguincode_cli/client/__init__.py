"""PenguinCode Client for gRPC communication.

Provides the client-side implementation for client-server mode.
Handles tool execution locally and communicates with remote server.
"""

from .auth import TokenManager
from .grpc_client import GRPCClient
from .tool_executor import LocalToolExecutor

__all__ = ["GRPCClient", "LocalToolExecutor", "TokenManager"]
