"""Tests for shared.knowledge.code_search: hybrid RRF search, branch/org isolation.

Branch isolation and org isolation are security properties -- see the
class docstrings for (c) and (e).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from shared.knowledge.code_search import (
    CodeChunkRecord,
    reciprocal_rank_fusion,
    search_code,
)
from shared.knowledge.scoping import ScopeKey, ScopeType, TrustTier


@pytest.fixture(autouse=True)
def _stub_query_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_code() embeds the query text -- stub it out so tests never hit a real backend."""

    async def _fake_embed_cached(content: str, db: object = None, **kwargs: object) -> list[float]:
        return [0.1] * 768

    monkeypatch.setattr("shared.knowledge.code_search.embed_cached", _fake_embed_cached)


def _chunk(**overrides: object) -> CodeChunkRecord:
    defaults: dict[str, object] = dict(
        id="chunk-1",
        content="def handler(): ...",
        scope_type=ScopeType.REPO,
        scope_ref="repo-1",
        trust_tier=TrustTier.DERIVED,
        author_user_id=None,
        org="org-a",
        repo="repo-1",
        branch="main",
        path="handler.py",
        symbol="handler",
        kind="function",
    )
    defaults.update(overrides)
    return CodeChunkRecord(**defaults)  # type: ignore[arg-type]


class _StubBackend:
    """A CodeSearchBackend stub with a fixed candidate set for RRF testing."""

    def __init__(
        self,
        vector_ranked: list[str],
        fts_ranked: list[str],
        records: dict[str, CodeChunkRecord],
        exact: CodeChunkRecord | None = None,
    ) -> None:
        self.vector_ranked = vector_ranked
        self.fts_ranked = fts_ranked
        self.records = records
        self.exact = exact
        self.vector_search = AsyncMock(return_value=vector_ranked)
        self.fts_search = AsyncMock(return_value=fts_ranked)
        self.symbol_exact = AsyncMock(return_value=exact)

    async def fetch_records(
        self, chunk_ids: list[str], scope: ScopeKey
    ) -> dict[str, CodeChunkRecord]:
        return {cid: self.records[cid] for cid in chunk_ids if cid in self.records}


class TestSymbolExactShortCircuit:
    """(a) An exact symbol match short-circuits and ranks first."""

    @pytest.mark.asyncio
    async def test_exact_match_is_first_result(self) -> None:
        """A symbol-exact hit always ranks ahead of RRF-fused candidates."""
        exact = _chunk(id="exact-1", symbol="handle_request")
        other = _chunk(id="other-1", symbol="unrelated")
        backend = _StubBackend(
            vector_ranked=["other-1"],
            fts_ranked=["other-1"],
            records={"other-1": other},
            exact=exact,
        )
        caller = ScopeKey(org="org-a", repo="repo-1", branch="main")

        results = await search_code("handle_request", caller, backend, embed_db=None)

        assert results[0].chunk_id == "exact-1"
        assert results[0].score == float("inf")


class TestReciprocalRankFusion:
    """(b) Hybrid fusion: vector and FTS disagreement resolved by RRF math."""

    def test_rrf_math_on_stubbed_candidate_set(self) -> None:
        """Verify the exact RRF formula: score = sum(1/(k+rank))."""
        vector_ranked = ["a", "b", "c"]
        fts_ranked = ["b", "a", "c"]

        scores = reciprocal_rank_fusion([vector_ranked, fts_ranked], k=60)

        expected_a = 1 / (60 + 1) + 1 / (60 + 2)
        expected_b = 1 / (60 + 2) + 1 / (60 + 1)
        expected_c = 1 / (60 + 3) + 1 / (60 + 3)
        assert scores["a"] == pytest.approx(expected_a)
        assert scores["b"] == pytest.approx(expected_b)
        assert scores["c"] == pytest.approx(expected_c)
        # a and b tie (same two positions, swapped) -- both outrank c.
        assert scores["a"] == pytest.approx(scores["b"])
        assert scores["a"] > scores["c"]

    def test_absent_from_one_list_still_contributes_from_the_other(self) -> None:
        """A doc found only by vector search still scores (not zeroed by FTS absence)."""
        scores = reciprocal_rank_fusion([["only-vector"], []], k=60)
        assert scores["only-vector"] == pytest.approx(1 / 61)

    @pytest.mark.asyncio
    async def test_disagreeing_rankings_produce_fused_order(self) -> None:
        """When vector and FTS disagree, results follow RRF-fused order, not either alone."""
        doc_a = _chunk(id="a", symbol="alpha")
        doc_b = _chunk(id="b", symbol="beta")
        doc_c = _chunk(id="c", symbol="gamma")
        backend = _StubBackend(
            vector_ranked=["a", "b", "c"],
            fts_ranked=["b", "c", "a"],
            records={"a": doc_a, "b": doc_b, "c": doc_c},
        )
        caller = ScopeKey(org="org-a", repo="repo-1", branch="main")

        results = await search_code("query", caller, backend, embed_db=None)

        # b: vector rank2 + fts rank1 -- highest fused score of the three
        # (beats a's rank1+rank3 and c's rank3+rank2 combinations).
        assert results[0].chunk_id == "b"


class TestBranchIsolation:
    """(c) Branch isolation (security): feature/A never receives feature/B's in-flight chunks."""

    @pytest.mark.asyncio
    async def test_other_branch_chunk_excluded_even_if_top_ranked(self) -> None:
        """A semantically-closer chunk on a different branch is dropped, not returned."""
        own_branch_chunk = _chunk(id="own", branch="feature/A", content="feature A code")
        other_branch_chunk = _chunk(id="other", branch="feature/B", content="feature B code")
        backend = _StubBackend(
            vector_ranked=["other", "own"],  # other-branch chunk ranks HIGHER
            fts_ranked=["other", "own"],
            records={"own": own_branch_chunk, "other": other_branch_chunk},
        )
        caller = ScopeKey(org="org-a", repo="repo-1", branch="feature/A")

        results = await search_code("code", caller, backend, embed_db=None)

        chunk_ids = {r.chunk_id for r in results}
        assert chunk_ids == {"own"}


class TestSymbolPrecision:
    """(d) Symbol-retrieval precision on a small labeled query set."""

    @pytest.mark.asyncio
    async def test_precise_symbol_query_returns_the_correct_definition(self) -> None:
        """Querying for an exact symbol name resolves to that symbol's chunk, not a near-miss."""
        target = _chunk(id="target", symbol="calculate_total", path="billing.py")
        decoy = _chunk(id="decoy", symbol="calculate_totals_v2", path="legacy.py")
        backend = _StubBackend(
            vector_ranked=["decoy", "target"],
            fts_ranked=["decoy", "target"],
            records={"target": target, "decoy": decoy},
            exact=target,
        )
        caller = ScopeKey(org="org-a", repo="repo-1", branch="main")

        results = await search_code("calculate_total", caller, backend, embed_db=None)

        assert results[0].chunk_id == "target"
        assert results[0].symbol == "calculate_total"


class TestOrgIsolation:
    """(e) Org isolation (security): org A's query never returns org B's chunks."""

    @pytest.mark.asyncio
    async def test_other_org_chunk_never_returned(self) -> None:
        """A chunk belonging to a different org is excluded regardless of ranking."""
        org_a_chunk = _chunk(id="a-chunk", org="org-a", repo="repo-1", branch="main")
        org_b_chunk = _chunk(id="b-chunk", org="org-b", repo="repo-1", branch="main")
        backend = _StubBackend(
            vector_ranked=["b-chunk", "a-chunk"],
            fts_ranked=["b-chunk", "a-chunk"],
            records={"a-chunk": org_a_chunk, "b-chunk": org_b_chunk},
        )
        caller = ScopeKey(org="org-a", repo="repo-1", branch="main")

        results = await search_code("query", caller, backend, embed_db=None)

        assert {r.chunk_id for r in results} == {"a-chunk"}


class TestRepoIsolation:
    """(f) Repo isolation (security): repo-1's caller never receives repo-2's chunks, same org."""

    @pytest.mark.asyncio
    async def test_other_repo_chunk_excluded_even_if_top_ranked(self) -> None:
        """A semantically-closer chunk from a different repo in the same org is dropped."""
        own_repo_chunk = _chunk(id="own", repo="repo-1", content="repo 1 code")
        other_repo_chunk = _chunk(id="other", repo="repo-2", content="repo 2 code")
        backend = _StubBackend(
            vector_ranked=["other", "own"],  # other-repo chunk ranks HIGHER
            fts_ranked=["other", "own"],
            records={"own": own_repo_chunk, "other": other_repo_chunk},
        )
        caller = ScopeKey(org="org-a", repo="repo-1", branch="main")

        results = await search_code("code", caller, backend, embed_db=None)

        chunk_ids = {r.chunk_id for r in results}
        assert chunk_ids == {"own"}


class TestBackendReceivesCallerScope:
    """(g) The orchestration threads the caller's scope into every backend call.

    A real SQL backend can only push org/repo/branch into its WHERE clause if
    search_code() actually hands it the scope -- this proves the plumbing,
    independent of any specific backend implementation.
    """

    @pytest.mark.asyncio
    async def test_every_backend_call_receives_the_caller_scope(self) -> None:
        """vector_search, fts_search, and fetch_records each receive (..., caller, ...).

        vector_search/fts_search are AsyncMock (set up by _StubBackend), so their
        received args are asserted via call_args directly; fetch_records is a plain
        async method, so it's spied the same way it was before this test grew to
        cover all three calls.
        """
        chunk = _chunk(id="a", symbol="alpha")
        backend = _StubBackend(
            vector_ranked=["a"],
            fts_ranked=["a"],
            records={"a": chunk},
        )
        caller = ScopeKey(org="org-a", repo="repo-1", branch="main")

        received_scopes: list[object] = []
        original_fetch_records = backend.fetch_records

        async def _spy_fetch_records(chunk_ids, scope):
            received_scopes.append(scope)
            return await original_fetch_records(chunk_ids, scope)

        backend.fetch_records = _spy_fetch_records

        await search_code("query", caller, backend, embed_db=None)

        assert received_scopes == [caller]
        assert backend.vector_search.call_args.args[1] == caller
        assert backend.fts_search.call_args.args[1] == caller


class TestActiveStatusOnly:
    """Reads honor status='active' only -- quarantined/superseded chunks are excluded."""

    @pytest.mark.asyncio
    async def test_quarantined_chunk_excluded(self) -> None:
        """A quarantined chunk never appears in search results."""
        active_chunk = _chunk(id="active", status="active")
        quarantined_chunk = _chunk(id="quarantined", status="quarantined")
        backend = _StubBackend(
            vector_ranked=["quarantined", "active"],
            fts_ranked=["quarantined", "active"],
            records={"active": active_chunk, "quarantined": quarantined_chunk},
        )
        caller = ScopeKey(org="org-a", repo="repo-1", branch="main")

        results = await search_code("query", caller, backend, embed_db=None)

        assert {r.chunk_id for r in results} == {"active"}
