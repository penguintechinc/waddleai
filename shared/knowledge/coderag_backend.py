"""The real production CodeSearchBackend (§9.1 core-completion): pgvector + Postgres FTS.

Implements shared.knowledge.code_search.CodeSearchBackend against
PostgreSQL's pgvector (`<=>` cosine distance, `code_chunks_emb_idx`
ivfflat index) and native FTS (`content_tsv` generated tsvector column,
`code_chunks_fts_idx` GIN index, both from migration 012). Every query's
WHERE clause is scoped to the caller's (org, repo, branch) directly in SQL
-- not just via the post-fetch shared.knowledge.scoping.filter_visible
Python filter, which stays as defense-in-depth only.

Raw parameterized SQL via db.executesql() throughout (never the PyDAL query
builder, which has no vector/tsvector operator support) -- the same style
already proven in shared/utils/memory_integration.py::PgvectorMemoryStore.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from shared.knowledge.code_search import CodeChunkRecord
from shared.knowledge.retriever import KnowledgeSourceBackend
from shared.knowledge.retriever import search_code as retriever_search_code
from shared.knowledge.scoping import ScopedRecord, ScopeKey, ScopeType, TrustTier

_RECORD_COLUMNS = (
    "c.id, c.path, c.symbol, c.kind, c.content, c.branch_ref, "
    "c.scope_type, c.scope_ref, c.trust_tier, c.status, c.created_at, "
    "r.org_id, r.name"
)


def _build_scope_where(
    org_id: int, repo_id: int | None, branch_ref: str | None
) -> tuple[str, list[int | str]]:
    """Build the mandatory org filter plus optional repo/branch filters for a code_chunks query.

    org_id is always present; repo_id/branch_ref are added only when the
    caller's scope specifies them, mirroring scoping.is_visible()'s
    permissive-when-unspecified semantics so the SQL-level filter and the
    Python defense-in-depth filter never disagree.
    """
    clauses = ["r.org_id = %s"]
    params: list[int | str] = [org_id]
    if repo_id is not None:
        clauses.append("c.repo_id = %s")
        params.append(repo_id)
    if branch_ref is not None:
        clauses.append("c.branch_ref = %s")
        params.append(branch_ref)
    return " AND ".join(clauses), params


def _row_to_record(row: tuple[Any, ...]) -> CodeChunkRecord:
    """Build a CodeChunkRecord from one _RECORD_COLUMNS row."""
    (
        chunk_id,
        path,
        symbol,
        kind,
        content,
        branch_ref,
        scope_type,
        scope_ref,
        trust_tier,
        status,
        created_at,
        org_id,
        repo_name,
    ) = row
    return CodeChunkRecord(
        id=str(chunk_id),
        content=content,
        scope_type=ScopeType(scope_type),
        scope_ref=scope_ref,
        trust_tier=TrustTier(trust_tier),
        author_user_id=None,  # code_chunks has no author column -- AST-derived, not user-authored
        org=str(org_id),
        repo=repo_name,
        branch=branch_ref,
        status=status,
        created_at=created_at,
        path=path,
        symbol=symbol,
        kind=kind,
    )


class PgCodeSearchBackend:
    """Real CodeSearchBackend: pgvector cosine + Postgres FTS, org/repo/branch-scoped in SQL."""

    def __init__(self, db: object) -> None:
        """Bind to a penguin-dal handle exposing ``executesql`` (management or proxy's ``db``)."""
        self.db = db

    async def _resolve_scope(self, scope: ScopeKey) -> tuple[int, int | None, bool]:
        """Resolve (org_id, repo_id, repo_requested_but_missing) from a ScopeKey.

        repo_requested_but_missing is True only when scope.repo was given
        but no such repo exists in this org -- callers must short-circuit to
        empty results in that case, never fall back to an org-wide search.
        """
        org_id = int(scope.org)
        if scope.repo is None:
            return org_id, None, False
        rows = await asyncio.to_thread(
            self.db.executesql,
            "SELECT id FROM code_repos WHERE org_id = %s AND name = %s LIMIT 1",  # nosec B608 -- fixed literal, values bound via executesql params
            [org_id, scope.repo],
        )
        if not rows:
            return org_id, None, True
        return org_id, int(rows[0][0]), False

    async def vector_search(
        self, query_embedding: list[float], scope: ScopeKey, top_k: int
    ) -> list[str]:
        """Return chunk_ids ranked by pgvector cosine similarity, scoped to org/repo/branch."""
        org_id, repo_id, missing = await self._resolve_scope(scope)
        if missing:
            return []
        where_sql, where_params = _build_scope_where(org_id, repo_id, scope.branch)
        embedding_str = "[" + ",".join(str(f) for f in query_embedding) + "]"
        sql = (
            "SELECT c.id FROM code_chunks c "  # nosec B608 # noqa: S608 -- fixed literal fragments, values bound via executesql params
            "JOIN code_repos r ON r.id = c.repo_id "
            f"WHERE {where_sql} AND c.status = 'active' AND c.embedding IS NOT NULL "
            "ORDER BY c.embedding <=> %s::vector LIMIT %s"
        )
        params = [*where_params, embedding_str, top_k]
        rows = await asyncio.to_thread(self.db.executesql, sql, params)
        return [str(row[0]) for row in rows]

    async def fts_search(self, query_text: str, scope: ScopeKey, top_k: int) -> list[str]:
        """Return chunk_ids ranked by Postgres ts_rank over content_tsv, org/repo/branch-scoped."""
        org_id, repo_id, missing = await self._resolve_scope(scope)
        if missing:
            return []
        where_sql, where_params = _build_scope_where(org_id, repo_id, scope.branch)
        sql = (
            "SELECT c.id FROM code_chunks c "  # nosec B608 # noqa: S608 -- fixed literal fragments, values bound via executesql params
            "JOIN code_repos r ON r.id = c.repo_id "
            f"WHERE {where_sql} AND c.status = 'active' "
            "AND c.content_tsv @@ plainto_tsquery('english', %s) "
            "ORDER BY ts_rank(c.content_tsv, plainto_tsquery('english', %s)) DESC LIMIT %s"
        )
        params = [*where_params, query_text, query_text, top_k]
        rows = await asyncio.to_thread(self.db.executesql, sql, params)
        return [str(row[0]) for row in rows]

    async def symbol_exact(self, query_text: str, scope: ScopeKey) -> CodeChunkRecord | None:
        """Return the record whose symbol exactly matches query_text, scoped to org/repo/branch."""
        org_id, repo_id, missing = await self._resolve_scope(scope)
        if missing:
            return None
        where_sql, where_params = _build_scope_where(org_id, repo_id, scope.branch)
        sql = (
            f"SELECT {_RECORD_COLUMNS} FROM code_chunks c "  # nosec B608 # noqa: S608 -- fixed literal fragments, values bound via executesql params
            "JOIN code_repos r ON r.id = c.repo_id "
            f"WHERE {where_sql} AND c.status = 'active' AND c.symbol = %s LIMIT 1"
        )
        params = [*where_params, query_text]
        rows = await asyncio.to_thread(self.db.executesql, sql, params)
        return _row_to_record(rows[0]) if rows else None

    async def fetch_records(
        self, chunk_ids: list[str], scope: ScopeKey
    ) -> dict[str, CodeChunkRecord]:
        """Resolve chunk_ids to full records in one scoped IN (...) query."""
        if not chunk_ids:
            return {}
        org_id, repo_id, missing = await self._resolve_scope(scope)
        if missing:
            return {}
        where_sql, where_params = _build_scope_where(org_id, repo_id, scope.branch)
        placeholders = ", ".join(["%s"] * len(chunk_ids))
        sql = (
            f"SELECT {_RECORD_COLUMNS} FROM code_chunks c "  # nosec B608 # noqa: S608 -- placeholder count matches chunk_ids length, all bound
            "JOIN code_repos r ON r.id = c.repo_id "
            f"WHERE {where_sql} AND c.id IN ({placeholders})"
        )
        params = [*where_params, *[int(cid) for cid in chunk_ids]]
        rows = await asyncio.to_thread(self.db.executesql, sql, params)
        return {str(row[0]): _row_to_record(row) for row in rows}


@dataclass(slots=True)
class CodeKnowledgeSourceAdapter:
    """Adapts PgCodeSearchBackend to KnowledgeSourceBackend for KnowledgeRetriever's "code" source.

    Used by the proxy's KnowledgeInjectStage (auto-inject path for plain,
    non-MCP-capable clients) -- the MCP pull-path tools use
    shared.mcp.knowledge_adapter.CodeRagKnowledgeService instead, which
    wraps the same PgCodeSearchBackend for the KnowledgeService Protocol.
    """

    db: object
    backend: PgCodeSearchBackend = field(init=False)

    def __post_init__(self) -> None:
        """Bind the underlying PgCodeSearchBackend to this adapter's db handle."""
        self.backend = PgCodeSearchBackend(self.db)

    async def search(self, query: str, caller: ScopeKey, top_k: int) -> list[ScopedRecord]:
        """Hybrid CodeRAG search, unwrapped to the ScopedRecord list KnowledgeRetriever expects."""
        results = await retriever_search_code(query, caller, self.backend, top_k, embed_db=self.db)
        return [r.record for r in results]


def build_code_knowledge_sources(db: object) -> dict[str, KnowledgeSourceBackend]:
    """Real KnowledgeRetriever.sources wiring for CodeRAG (§9.1) -- replaces sources={}."""
    return {"code": CodeKnowledgeSourceAdapter(db)}


__all__ = [
    "PgCodeSearchBackend",
    "CodeKnowledgeSourceAdapter",
    "build_code_knowledge_sources",
]
