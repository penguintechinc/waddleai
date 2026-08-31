"""Tests for shared.mcp.knowledge_adapter.CodeRagKnowledgeService.

search_code/get_symbol are real (wired to PgCodeSearchBackend); search_docs/
fetch_docs stay ServiceUnavailableError (inherited from NotWiredKnowledgeService)
-- docs-cache is a separate subsystem this plan does not touch.
"""

from __future__ import annotations

import pytest

from shared.knowledge.code_search import CodeChunkRecord
from shared.knowledge.scoping import ScopeKey, ScopeType, TrustTier
from shared.mcp.knowledge_adapter import CodeRagKnowledgeService
from shared.mcp.tools import ServiceUnavailableError


def _record(**overrides: object) -> CodeChunkRecord:
    defaults: dict[str, object] = dict(
        id="9",
        content="def calculate_total(): ...",
        scope_type=ScopeType.REPO,
        scope_ref="waddleai",
        trust_tier=TrustTier.DERIVED,
        author_user_id=None,
        org="7",
        repo="waddleai",
        branch="main",
        path="billing.py",
        symbol="calculate_total",
        kind="function",
    )
    defaults.update(overrides)
    return CodeChunkRecord(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_search_code_forwards_exact_scope_key_and_returns_serialized_results(
    monkeypatch,
) -> None:
    """search_code builds the caller's exact ScopeKey and serializes SearchResults."""
    service = CodeRagKnowledgeService(db=object())
    captured: dict[str, object] = {}

    async def _fake_search_code(query, caller, backend, top_k, *, embed_db=None):
        captured["query"] = query
        captured["caller"] = caller
        captured["backend"] = backend
        captured["top_k"] = top_k
        from shared.knowledge.code_search import SearchResult

        return [
            SearchResult(
                chunk_id="9",
                path="billing.py",
                symbol="calculate_total",
                kind="function",
                content="def calculate_total(): ...",
                score=0.95,
                record=_record(),
            )
        ]

    monkeypatch.setattr("shared.mcp.knowledge_adapter.retriever_search_code", _fake_search_code)

    results = await service.search_code(
        org_id=7, query="calculate_total", repo="waddleai", branch="main"
    )

    # Strong scope-forwarding assertion: the exact ScopeKey (not just "was
    # called") must reach the backend, org/repo/branch mapped 1:1 with no
    # cross-field bleed (e.g. repo landing in branch, or org left unset).
    assert captured["caller"] == ScopeKey(org="7", repo="waddleai", branch="main")
    assert captured["query"] == "calculate_total"
    assert captured["backend"] is service.backend

    assert results == [
        {
            "chunk_id": "9",
            "path": "billing.py",
            "symbol": "calculate_total",
            "kind": "function",
            "content": "def calculate_total(): ...",
            "score": 0.95,
        }
    ]


@pytest.mark.asyncio
async def test_search_code_forwards_scope_key_with_no_repo_or_branch(monkeypatch) -> None:
    """An org-wide search (no repo/branch given) forwards a ScopeKey with those fields unset."""
    service = CodeRagKnowledgeService(db=object())
    captured: dict[str, object] = {}

    async def _fake_search_code(query, caller, backend, top_k, *, embed_db=None):
        captured["caller"] = caller
        return []

    monkeypatch.setattr("shared.mcp.knowledge_adapter.retriever_search_code", _fake_search_code)

    await service.search_code(org_id=42, query="foo", repo=None, branch=None)

    assert captured["caller"] == ScopeKey(org="42", repo=None, branch=None)


@pytest.mark.asyncio
async def test_get_symbol_found_forwards_exact_scope_key(monkeypatch) -> None:
    """get_symbol resolves a symbol-exact hit to a plain dict, scoped by the caller's ScopeKey."""
    service = CodeRagKnowledgeService(db=object())
    captured: dict[str, object] = {}

    async def _fake_symbol_exact(self, query_text, scope):
        captured["query_text"] = query_text
        captured["scope"] = scope
        return _record()

    monkeypatch.setattr(
        "shared.knowledge.coderag_backend.PgCodeSearchBackend.symbol_exact", _fake_symbol_exact
    )

    result = await service.get_symbol(org_id=7, symbol="calculate_total", repo="waddleai")

    assert captured["scope"] == ScopeKey(org="7", repo="waddleai")
    assert captured["query_text"] == "calculate_total"
    assert result == {
        "path": "billing.py",
        "symbol": "calculate_total",
        "kind": "function",
        "content": "def calculate_total(): ...",
    }


@pytest.mark.asyncio
async def test_get_symbol_not_found(monkeypatch) -> None:
    """get_symbol returns None, not an error, when the symbol isn't indexed."""
    service = CodeRagKnowledgeService(db=object())

    async def _fake_symbol_exact(self, query_text, scope):
        return None

    monkeypatch.setattr(
        "shared.knowledge.coderag_backend.PgCodeSearchBackend.symbol_exact", _fake_symbol_exact
    )

    result = await service.get_symbol(org_id=7, symbol="missing", repo="waddleai")

    assert result is None


@pytest.mark.asyncio
async def test_search_docs_and_fetch_docs_remain_not_wired() -> None:
    """docs-cache is a separate subsystem -- CodeRagKnowledgeService does not implement it."""
    service = CodeRagKnowledgeService(db=object())

    with pytest.raises(ServiceUnavailableError):
        await service.search_docs(query="q", ecosystem=None)
    with pytest.raises(ServiceUnavailableError):
        await service.fetch_docs(ecosystem="python", package="requests", version=None)
