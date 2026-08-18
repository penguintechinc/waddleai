"""Proxy memory & context-efficiency layers (§6A).

Scratchpad, summarization, embedding/retrieval caches, and tool-schema/
system-prompt dedup. Every layer routes reads/writes through
shared.memory.provenance (filter_on_write / recall) and is gated by
shared.memory.config (resolve_proxy_memory_config), which is itself gated
by the ``waddleai.proxy_memory`` feature flag, fail-safe OFF.
"""

from shared.memory.config import (
    ALL_DISABLED,
    PROXY_MEMORY_FLAG,
    ProxyMemoryConfig,
    resolve_proxy_memory_config,
)
from shared.memory.provenance import (
    ProvenanceTag,
    WriteVerdict,
    filter_on_write,
    recall,
)

__all__ = [
    "ALL_DISABLED",
    "PROXY_MEMORY_FLAG",
    "ProxyMemoryConfig",
    "resolve_proxy_memory_config",
    "ProvenanceTag",
    "WriteVerdict",
    "filter_on_write",
    "recall",
]
