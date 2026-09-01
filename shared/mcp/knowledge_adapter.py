"""The real MCP KnowledgeService adapter for CodeRAG (§9.1/§11.1 core-completion).

Replaces NotWiredKnowledgeService.search_code/.get_symbol with real
PgCodeSearchBackend-backed implementations, wired into the /mcp
search_code/get_symbol tools via McpServiceFactory
(proxy/apps/proxy_server/mcp_mount.py). Subclasses NotWiredKnowledgeService
so search_docs/fetch_docs keep raising ServiceUnavailableError honestly --
docs-cache is a separate subsystem this plan does not touch.
"""

from __future__ import annotations

from typing import Any

from shared.knowledge.coderag_backend import PgCodeSearchBackend
from shared.knowledge.retriever import search_code as retriever_search_code
from shared.knowledge.scoping import ScopeKey
from shared.mcp.stub_adapters import NotWiredKnowledgeService


class CodeRagKnowledgeService(NotWiredKnowledgeService):
    """Real search_code/get_symbol, backed by PgCodeSearchBackend; docs stay not-wired."""

    def __init__(self, db: object) -> None:
        """Bind to a penguin-dal handle, constructing the underlying search backend."""
        self.db = db
        self.backend = PgCodeSearchBackend(db)

    async def search_code(
        self, *, org_id: int, query: str, repo: str | None, branch: str | None
    ) -> list[dict[str, Any]]:
        """Hybrid CodeRAG search over an org's indexed repos, serialized for the MCP tool."""
        caller = ScopeKey(org=str(org_id), repo=repo, branch=branch)
        results = await retriever_search_code(
            query, caller, self.backend, top_k=10, embed_db=self.db
        )
        return [
            {
                "chunk_id": r.chunk_id,
                "path": r.path,
                "symbol": r.symbol,
                "kind": r.kind,
                "content": r.content,
                "score": r.score,
            }
            for r in results
        ]

    async def get_symbol(
        self, *, org_id: int, symbol: str, repo: str | None
    ) -> dict[str, Any] | None:
        """Symbol-exact chunk lookup, serialized; None if the symbol isn't indexed."""
        caller = ScopeKey(org=str(org_id), repo=repo)
        record = await self.backend.symbol_exact(symbol, caller)
        if record is None:
            return None
        return {
            "path": record.path,
            "symbol": record.symbol,
            "kind": record.kind,
            "content": record.content,
        }


__all__ = ["CodeRagKnowledgeService"]
