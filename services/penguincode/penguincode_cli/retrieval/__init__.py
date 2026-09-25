"""Hybrid GraphRAG retrieval for penguincode (T14).

Exposes ``retrieve()`` and ``RetrievalResult`` as the single entry point
later callers (CLI/REPL, an MCP tool, docs-RAG-augmented chat) use to fetch
scoped hybrid vector+graph context; see
:mod:`penguincode_cli.retrieval.graphrag` for the full flow and flag-gating
contract.
"""

from .graphrag import EmbedFn, RetrievalResult, retrieve

__all__ = ["EmbedFn", "RetrievalResult", "retrieve"]
