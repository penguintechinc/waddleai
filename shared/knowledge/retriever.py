"""Unified ranked retrieval across memory/coderag/docs/uploaded (§9.5/§9.6).

``KnowledgeRetriever`` composes per-source backends (code, docs, uploaded
knowledge, conversation memory) behind one ``KnowledgeSourceBackend``
protocol, ranks the merged candidate set via ``scoping.rank``, and re-filters
every result through ``injection_safety.filter_for_inject`` before handing
back provenance-headed, injection-safe blocks. This is the shared engine
behind both the pull-path service functions below (the contract mcp-v2's
MCP tools -- ``search_code``, ``search_docs``, ``memory_search`` -- call)
and the proxy's ``KnowledgeInjectStage`` (auto-injection for plain clients).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from shared.knowledge.code_search import CodeSearchBackend
from shared.knowledge.code_search import search_code as _search_code_hybrid
from shared.knowledge.injection_safety import InjectableBlock, filter_for_inject
from shared.knowledge.scoping import ScopedRecord, ScopeKey, filter_visible, rank
from shared.security.content_filter import ContentFilter
from shared.security.prompt_security import PromptSecurityScanner

DEFAULT_TOKEN_BUDGET = 2000


class KnowledgeSourceBackend(Protocol):
    """One retrievable knowledge source (code, docs, uploaded, memory)."""

    async def search(self, query: str, caller: ScopeKey, top_k: int) -> list[ScopedRecord]:
        """Return candidate records for ``query``, unranked and unfiltered."""
        ...


@dataclass(slots=True)
class KnowledgeRetriever:
    """Composes enabled knowledge sources into one ranked, injection-safe result set."""

    sources: dict[str, KnowledgeSourceBackend]
    scanner: PromptSecurityScanner
    content_filter: ContentFilter
    org_id: int | None = field(default=None)

    async def retrieve(
        self,
        query: str,
        caller: ScopeKey,
        sources: list[str] | None = None,
        top_k: int = 10,
    ) -> list[InjectableBlock]:
        """Rank across every enabled source; each result injection-re-filtered + provenance-tagged.

        Args:
            query: Free-text query.
            caller: The caller's active scope (org/repo/branch/user/session).
            sources: Subset of ``self.sources`` keys to query; all of them
                when omitted (each caller-facing surface gates its own
                sources via their individual §9 feature flags before
                reaching here).
            top_k: Maximum blocks to return.

        """
        active_names = [name for name in (sources or self.sources) if name in self.sources]
        per_source = await asyncio.gather(
            *(self.sources[name].search(query, caller, top_k) for name in active_names)
        )
        candidates = [record for records in per_source for record in records]

        visible = filter_visible(candidates, caller)
        ranked = rank(visible)[:top_k]

        return await filter_for_inject(
            ranked, self.scanner, self.content_filter, org_id=self.org_id
        )


async def search_code(
    query: str,
    caller: ScopeKey,
    backend: CodeSearchBackend,
    top_k: int = 10,
    *,
    embed_db: object | None = None,
):
    """Pull-path service function: CodeRAG hybrid search (mcp-v2 ``search_code`` tool contract).

    Thin passthrough to :func:`shared.knowledge.code_search.search_code` --
    kept here so mcp-v2 imports one module (``shared.knowledge.retriever``)
    for all three pull-path tools.
    """
    return await _search_code_hybrid(query, caller, backend, top_k, embed_db=embed_db)


async def search_docs(
    query: str,
    caller: ScopeKey,
    backend: KnowledgeSourceBackend,
    top_k: int = 10,
) -> list[ScopedRecord]:
    """Pull-path service function: docs-cache search (mcp-v2 ``search_docs`` tool contract).

    Ranks + scope-filters candidates from a docs-cache-backed
    :class:`KnowledgeSourceBackend`; injection re-filtering happens at the
    :class:`KnowledgeRetriever` layer for the auto-inject path, and is the
    caller's responsibility for the raw pull-path (mcp-v2 applies it before
    handing results to the agent).
    """
    candidates = await backend.search(query, caller, top_k)
    return rank(filter_visible(candidates, caller))[:top_k]


async def memory_search(
    query: str,
    caller: ScopeKey,
    backend: KnowledgeSourceBackend,
    top_k: int = 10,
) -> list[ScopedRecord]:
    """Pull-path service function: memory search (mcp-v2 ``memory_search`` tool contract)."""
    candidates = await backend.search(query, caller, top_k)
    return rank(filter_visible(candidates, caller))[:top_k]


__all__ = [
    "DEFAULT_TOKEN_BUDGET",
    "KnowledgeSourceBackend",
    "KnowledgeRetriever",
    "search_code",
    "search_docs",
    "memory_search",
]
