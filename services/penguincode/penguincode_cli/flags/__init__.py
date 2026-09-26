"""PostHog-backed feature flags for the standalone PenguinCode service.

Exposes the four knowledge-platform flag keys and the graceful-degradation
client used to gate them -- see :mod:`penguincode_cli.flags.client`.
"""

from .client import (
    CODE_GRAPH_FLAG,
    KNOWLEDGE_GRAPH_FLAG,
    MEMORY_GRAPH_FLAG,
    RAG_FLAG,
    FlagClient,
    ScopeContextLike,
    is_enabled,
)

__all__ = [
    "RAG_FLAG",
    "CODE_GRAPH_FLAG",
    "KNOWLEDGE_GRAPH_FLAG",
    "MEMORY_GRAPH_FLAG",
    "FlagClient",
    "ScopeContextLike",
    "is_enabled",
]
