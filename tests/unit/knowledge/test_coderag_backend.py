"""Tests for shared.knowledge.coderag_backend.PgCodeSearchBackend.

SQL-scoping is a **security** property (§9.7): every query's WHERE clause
must filter by org_id (and repo_id/branch_ref when the caller specifies
them), not rely solely on the post-fetch Python filter in
shared.knowledge.scoping. These tests capture the SQL string + params
through a fake DAL (same technique as
tests/unit/test_memory_scope_pgvector.py) and assert the WHERE clause
literally contains the scoping filters -- proving the SQL itself is scoped.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from shared.knowledge.code_search import SearchResult
from shared.knowledge.coderag_backend import (
    CodeKnowledgeSourceAdapter,
    PgCodeSearchBackend,
    build_code_knowledge_sources,
)
from shared.knowledge.scoping import ScopedRecord, ScopeKey, ScopeType, TrustTier


class FakeDB:
    """Captures executesql calls; returns queued results, one batch per call in order."""

    def __init__(self, results: list[Any] | None = None) -> None:
        """Queue up the result batches executesql should return, one per call, in order."""
        self.calls: list[tuple[str, tuple]] = []
        self._results = results or []

    def executesql(self, sql: str, params: Any = None) -> Any:
        """Record the SQL/params call and pop the next queued result batch."""
        self.calls.append((sql, tuple(params) if params else ()))
        return self._results.pop(0) if self._results else []


@pytest.mark.asyncio
async def test_vector_search_where_clause_scopes_by_org_and_repo() -> None:
    """vector_search's SQL WHERE clause filters by org_id and repo_id, not just Python."""
    db = FakeDB(
        results=[
            [(42,)],  # repo-name -> id resolution
            [("chunk-9",), ("chunk-3",)],  # vector_search rows
        ]
    )
    backend = PgCodeSearchBackend(db)
    scope = ScopeKey(org="7", repo="waddleai", branch="main")

    chunk_ids = await backend.vector_search([0.1] * 768, scope, top_k=5)

    assert chunk_ids == ["chunk-9", "chunk-3"]
    resolve_sql, resolve_params = db.calls[0]
    assert "code_repos" in resolve_sql
    assert resolve_params == (7, "waddleai")
    search_sql, search_params = db.calls[1]
    assert "r.org_id = %s" in search_sql
    assert "c.repo_id = %s" in search_sql
    assert "c.branch_ref = %s" in search_sql
    assert search_params[0:3] == (7, 42, "main")


@pytest.mark.asyncio
async def test_vector_search_unknown_repo_returns_empty_never_org_wide() -> None:
    """A typo'd repo name must return empty, never silently search the whole org."""
    db = FakeDB(results=[[]])  # repo-name resolution finds nothing
    backend = PgCodeSearchBackend(db)
    scope = ScopeKey(org="7", repo="typo-repo", branch="main")

    chunk_ids = await backend.vector_search([0.1] * 768, scope, top_k=5)

    assert chunk_ids == []
    assert len(db.calls) == 1  # the search query was never issued at all


@pytest.mark.asyncio
async def test_vector_search_no_repo_scopes_to_org_only() -> None:
    """caller.repo=None searches every repo in the org -- the org filter is still mandatory."""
    db = FakeDB(results=[[("chunk-1",)]])
    backend = PgCodeSearchBackend(db)
    scope = ScopeKey(org="7", branch="main")

    chunk_ids = await backend.vector_search([0.1] * 768, scope, top_k=5)

    assert chunk_ids == ["chunk-1"]
    search_sql, search_params = db.calls[0]
    assert "r.org_id = %s" in search_sql
    assert "c.repo_id = %s" not in search_sql
    assert search_params[0] == 7


@pytest.mark.asyncio
async def test_fts_search_uses_plainto_tsquery_and_scopes_by_org() -> None:
    """fts_search ranks by ts_rank over the generated content_tsv column, org-scoped."""
    db = FakeDB(results=[[("chunk-5",)]])
    backend = PgCodeSearchBackend(db)
    scope = ScopeKey(org="7")

    chunk_ids = await backend.fts_search("handle_request", scope, top_k=5)

    assert chunk_ids == ["chunk-5"]
    sql, params = db.calls[0]
    assert "plainto_tsquery" in sql
    assert "ts_rank" in sql
    assert "r.org_id = %s" in sql
    assert params[0] == 7
    assert "handle_request" in params


@pytest.mark.asyncio
async def test_symbol_exact_builds_a_scoped_record() -> None:
    """symbol_exact resolves the full record, scoped by org/repo, org_id/name from the join."""
    created = datetime(2026, 8, 31, 12, 0, 0)
    db = FakeDB(
        results=[
            [(42,)],  # repo resolution
            [
                (
                    "9",
                    "billing.py",
                    "calculate_total",
                    "function",
                    "def calculate_total(): ...",
                    "main",
                    "repo",
                    "waddleai",
                    "derived",
                    "active",
                    created,
                    7,
                    "waddleai",
                )
            ],
        ]
    )
    backend = PgCodeSearchBackend(db)
    scope = ScopeKey(org="7", repo="waddleai", branch="main")

    record = await backend.symbol_exact("calculate_total", scope)

    assert record is not None
    assert record.id == "9"
    assert record.symbol == "calculate_total"
    assert record.org == "7"
    assert record.repo == "waddleai"
    assert record.trust_tier == TrustTier.DERIVED
    sql, params = db.calls[1]
    assert "c.symbol = %s" in sql
    assert "r.org_id = %s" in sql


@pytest.mark.asyncio
async def test_symbol_exact_no_match_returns_none() -> None:
    """No matching symbol resolves to None, not an empty record."""
    db = FakeDB(results=[[(42,)], []])
    backend = PgCodeSearchBackend(db)
    scope = ScopeKey(org="7", repo="waddleai")

    record = await backend.symbol_exact("does_not_exist", scope)

    assert record is None


@pytest.mark.asyncio
async def test_fetch_records_scopes_by_org_and_repo_with_in_clause() -> None:
    """fetch_records resolves multiple ids in one scoped IN (...) query."""
    created = datetime(2026, 8, 31, 12, 0, 0)
    db = FakeDB(
        results=[
            [(42,)],  # repo resolution
            [
                (
                    "1",
                    "a.py",
                    "alpha",
                    "function",
                    "def alpha(): ...",
                    "main",
                    "repo",
                    "waddleai",
                    "derived",
                    "active",
                    created,
                    7,
                    "waddleai",
                ),
                (
                    "2",
                    "b.py",
                    "beta",
                    "function",
                    "def beta(): ...",
                    "main",
                    "repo",
                    "waddleai",
                    "derived",
                    "active",
                    created,
                    7,
                    "waddleai",
                ),
            ],
        ]
    )
    backend = PgCodeSearchBackend(db)
    scope = ScopeKey(org="7", repo="waddleai", branch="main")

    records = await backend.fetch_records(["1", "2"], scope)

    assert set(records) == {"1", "2"}
    assert records["1"].symbol == "alpha"
    assert records["2"].symbol == "beta"
    sql, params = db.calls[1]
    assert "IN (%s, %s)" in sql
    assert "r.org_id = %s" in sql
    assert params[-2:] == (1, 2)


@pytest.mark.asyncio
async def test_fetch_records_empty_ids_never_queries() -> None:
    """An empty chunk_ids list short-circuits without issuing a query."""
    db = FakeDB()
    backend = PgCodeSearchBackend(db)
    scope = ScopeKey(org="7")

    records = await backend.fetch_records([], scope)

    assert records == {}
    assert db.calls == []


def _search_result(record_id: str) -> SearchResult:
    """Build a minimal SearchResult wrapping a repo-scoped ScopedRecord."""
    record = ScopedRecord(
        id=record_id,
        content="def handler(): ...",
        scope_type=ScopeType.REPO,
        scope_ref="waddleai",
        trust_tier=TrustTier.DERIVED,
        author_user_id=None,
        org="7",
        repo="waddleai",
        branch="main",
    )
    return SearchResult(
        chunk_id=record_id,
        path="handler.py",
        symbol="handler",
        kind="function",
        content="def handler(): ...",
        score=0.9,
        record=record,
    )


@pytest.mark.asyncio
async def test_code_knowledge_source_adapter_delegates_to_search_code(monkeypatch) -> None:
    """CodeKnowledgeSourceAdapter.search() returns the underlying records, unwrapped."""
    adapter = CodeKnowledgeSourceAdapter(db=FakeDB())

    async def _fake_search_code(query, caller, backend, top_k, *, embed_db=None):
        return [_search_result("chunk-1")]

    monkeypatch.setattr("shared.knowledge.coderag_backend.retriever_search_code", _fake_search_code)

    caller = ScopeKey(org="7", repo="waddleai", branch="main")
    records = await adapter.search("handler", caller, top_k=5)

    assert len(records) == 1
    assert records[0].id == "chunk-1"


def test_build_code_knowledge_sources_returns_code_key_only() -> None:
    """build_code_knowledge_sources() wires only 'code' -- docs/uploaded/memory land separately."""
    sources = build_code_knowledge_sources(FakeDB())

    assert set(sources) == {"code"}
    assert isinstance(sources["code"], CodeKnowledgeSourceAdapter)
