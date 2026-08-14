"""Tests for shared.knowledge.retriever: unified ranked retrieval (§9.5/§9.6)."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from shared.knowledge.retriever import KnowledgeRetriever, memory_search, search_docs
from shared.knowledge.scoping import ScopedRecord, ScopeKey, ScopeType, TrustTier


def _record(**overrides: object) -> ScopedRecord:
    defaults: dict[str, object] = dict(
        id="rec-1",
        content="the build command is make build",
        scope_type=ScopeType.REPO,
        scope_ref="repo-1",
        trust_tier=TrustTier.DERIVED,
        author_user_id=None,
        org="org-a",
        repo="repo-1",
        relevance=0.8,
    )
    defaults.update(overrides)
    return ScopedRecord(**defaults)  # type: ignore[arg-type]


class _StubSource:
    """A KnowledgeSourceBackend stub returning a fixed candidate list."""

    def __init__(self, records: list[ScopedRecord]) -> None:
        self._records = records
        self.calls: list[tuple[str, ScopeKey, int]] = []

    async def search(self, query: str, caller: ScopeKey, top_k: int) -> list[ScopedRecord]:
        self.calls.append((query, caller, top_k))
        return self._records


def _clean_scanner() -> Mock:
    scanner = Mock()
    scanner.scan_prompt = Mock(return_value=([], "unchanged"))
    scanner.should_block = Mock(return_value=False)
    return scanner


def _allow_content_filter():
    from unittest.mock import AsyncMock

    from shared.security.content_filter import FilterResult

    content_filter = Mock()

    async def _filter_input(text: str, **kwargs: object):
        return FilterResult(
            allowed=True, action="allow", violations=[], filtered_text=text, auditor_used=False
        )

    content_filter.filter_input = AsyncMock(side_effect=_filter_input)
    return content_filter


class TestRetrieveAcrossSources:
    """(a) KnowledgeRetriever.retrieve() ranks across sources, injection-re-filtered + tagged."""

    @pytest.mark.asyncio
    async def test_merges_and_ranks_across_two_sources(self) -> None:
        """Records from two sources are merged into one ranked, provenance-tagged result set."""
        code_record = _record(id="code-1", relevance=0.6, trust_tier=TrustTier.DERIVED)
        doc_record = _record(
            id="doc-1", relevance=0.9, trust_tier=TrustTier.VERIFIED, scope_type=ScopeType.ORG
        )
        retriever = KnowledgeRetriever(
            sources={"code": _StubSource([code_record]), "docs": _StubSource([doc_record])},
            scanner=_clean_scanner(),
            content_filter=_allow_content_filter(),
            org_id=1,
        )
        caller = ScopeKey(org="org-a", repo="repo-1")

        blocks = await retriever.retrieve("build command", caller)

        assert {b.record_id for b in blocks} == {"code-1", "doc-1"}
        # doc-1 (higher relevance*trust) ranks first.
        assert blocks[0].record_id == "doc-1"
        assert all(b.text.startswith("> [") for b in blocks)

    @pytest.mark.asyncio
    async def test_only_queries_requested_sources(self) -> None:
        """Passing sources=["code"] never queries the docs source."""
        code_source = _StubSource([_record(id="code-1")])
        docs_source = _StubSource([_record(id="doc-1")])
        retriever = KnowledgeRetriever(
            sources={"code": code_source, "docs": docs_source},
            scanner=_clean_scanner(),
            content_filter=_allow_content_filter(),
        )
        caller = ScopeKey(org="org-a", repo="repo-1")

        blocks = await retriever.retrieve("query", caller, sources=["code"])

        assert {b.record_id for b in blocks} == {"code-1"}
        assert docs_source.calls == []

    @pytest.mark.asyncio
    async def test_scope_isolation_applied_before_ranking(self) -> None:
        """A record outside the caller's org is filtered out even from a stub source."""
        own_org = _record(id="own", org="org-a")
        other_org = _record(id="other", org="org-b")
        retriever = KnowledgeRetriever(
            sources={"code": _StubSource([own_org, other_org])},
            scanner=_clean_scanner(),
            content_filter=_allow_content_filter(),
        )
        caller = ScopeKey(org="org-a", repo="repo-1")

        blocks = await retriever.retrieve("query", caller)

        assert {b.record_id for b in blocks} == {"own"}

    @pytest.mark.asyncio
    async def test_respects_top_k(self) -> None:
        """No more than top_k blocks are returned even with more candidates."""
        records = [_record(id=f"r{i}", relevance=0.1 * i) for i in range(10)]
        retriever = KnowledgeRetriever(
            sources={"code": _StubSource(records)},
            scanner=_clean_scanner(),
            content_filter=_allow_content_filter(),
        )
        caller = ScopeKey(org="org-a", repo="repo-1")

        blocks = await retriever.retrieve("query", caller, top_k=3)

        assert len(blocks) == 3


class TestPullPathServiceFunctions:
    """(f) pull-path search_code/search_docs/memory_search share the injection-safe contract."""

    @pytest.mark.asyncio
    async def test_search_docs_ranks_and_scope_filters(self) -> None:
        """search_docs() ranks candidates and applies scope isolation."""
        visible = _record(id="visible", org="org-a")
        hidden = _record(id="hidden", org="org-b")
        backend = _StubSource([visible, hidden])
        caller = ScopeKey(org="org-a")

        results = await search_docs("python asyncio", caller, backend)

        assert {r.id for r in results} == {"visible"}

    @pytest.mark.asyncio
    async def test_memory_search_ranks_and_scope_filters(self) -> None:
        """memory_search() ranks candidates and applies scope isolation."""
        own_session = _record(
            id="mine", scope_type=ScopeType.SESSION, scope_ref="s1", org="org-a"
        )
        other_session = _record(
            id="theirs", scope_type=ScopeType.SESSION, scope_ref="s2", org="org-a"
        )
        backend = _StubSource([own_session, other_session])
        caller = ScopeKey(org="org-a", session="s1")

        results = await memory_search("preferences", caller, backend)

        assert {r.id for r in results} == {"mine"}
