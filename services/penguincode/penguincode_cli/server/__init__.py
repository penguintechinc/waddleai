"""PenguinCode gRPC Server.

Provides the server-side implementation for client-server mode.
Handles agent orchestration, Ollama communication, and tool callbacks.
"""

from .main import PenguinCodeServer, serve

__all__ = ["serve", "PenguinCodeServer"]
