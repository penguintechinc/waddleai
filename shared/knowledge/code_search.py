"""CodeRAG hybrid search (§9.1 + §9.7): pgvector + FTS, branch-scoped.

Hybrid pgvector cosine + Postgres FTS (``tsvector``) fused by reciprocal-rank
fusion (RRF); an exact ``symbol`` match short-circuits and ranks first.
Retrieval is filtered to the caller's active ``(org, repo, branch)`` context
via ``shared.knowledge.scoping`` before anything is returned -- branch and
org isolation are **security** properties: a caller on ``feature/A`` must
never receive ``feature/B``'s in-flight chunks, and org A must never see
org B's code, even when semantically closer.

The actual pgvector/FTS SQL lives behind the :class:`CodeSearchBackend`
protocol so the RRF math and isolation filtering are testable against a
stubbed candidate set, independent of a live Postgres connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from shared.knowledge.embed import embed_cached
from shared.knowledge.scoping import ScopedRecord, ScopeKey, filter_visible


@dataclass(slots=True)
class CodeChunkRecord(ScopedRecord):
    """A ScopedRecord specialized for code_chunks, adding path/symbol/kind."""

    path: str = ""
    symbol: str | None = None
    kind: str = "chunk"


@dataclass(slots=True, frozen=True)
class SearchResult:
    """One ranked CodeRAG hit, ready for provenance-headed injection."""

    chunk_id: str
    path: str
    symbol: str | None
    kind: str
    content: str
    score: float
    record: CodeChunkRecord
    """The underlying record, for filter_for_inject()/provenance headers."""


class CodeSearchBackend(Protocol):
    """The DB-facing seam ``search_code`` calls through.

    Swap for a real pgvector+FTS implementation in production, a stub in
    tests.
    """

    async def vector_search(self, query_embedding: list[float], top_k: int) -> list[str]:
        """Return chunk_ids ranked by cosine similarity, best first."""
        ...

    async def fts_search(self, query_text: str, top_k: int) -> list[str]:
        """Return chunk_ids ranked by Postgres ``ts_rank``, best first."""
        ...

    async def symbol_exact(self, query_text: str, scope: ScopeKey) -> CodeChunkRecord | None:
        """Return a record whose symbol exactly matches ``query_text``, if any."""
        ...

    async def fetch_records(self, chunk_ids: list[str]) -> dict[str, CodeChunkRecord]:
        """Resolve chunk_ids to their full CodeChunkRecord (content, scope, status)."""
        ...


def reciprocal_rank_fusion(
    ranked_lists: list[list[str]], k: int = 60
) -> dict[str, float]:
    """Fuse multiple rankings of the same id-space via reciprocal-rank fusion.

    ``score(d) = sum(1 / (k + rank_i(d)))`` over every ranking ``d`` appears
    in (1-indexed rank). Standard RRF constant ``k=60``. An id absent from a
    given list contributes 0 for that list.
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for position, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + position)
    return scores


async def search_code(
    query: str,
    caller: ScopeKey,
    backend: CodeSearchBackend,
    top_k: int = 10,
    *,
    embed_db: object | None = None,
) -> list[SearchResult]:
    """Hybrid CodeRAG search: symbol-exact short-circuit, else RRF-fused vector+FTS.

    Args:
        query: Free-text or symbol query.
        caller: The caller's active (org, repo, branch, session) context --
            results are filtered to what this scope may read (§9.7).
        backend: DB-facing search implementation (real or stubbed).
        top_k: Maximum results to return.
        embed_db: penguin-dal handle passed through to ``embed_cached`` for
            embedding-cache dedup; ``None`` disables caching.

    Returns:
        Ranked, scope-filtered, ``status='active'``-only results.
    """
    exact = await backend.symbol_exact(query, caller)

    query_embedding = await embed_cached(query, db=embed_db)
    vector_ranked = await backend.vector_search(query_embedding, top_k=top_k * 2)
    fts_ranked = await backend.fts_search(query, top_k=top_k * 2)

    fused_scores = reciprocal_rank_fusion([vector_ranked, fts_ranked])
    candidate_ids = [cid for cid in fused_scores if not exact or cid != exact.id]

    records = await backend.fetch_records(candidate_ids)

    results: list[SearchResult] = []
    if exact is not None:
        results.append(_to_result(exact, score=float("inf")))

    scored_candidates = [
        (cid, record, fused_scores[cid])
        for cid, record in records.items()
        if record.status == "active"
    ]
    visible = filter_visible([r for _cid, r, _s in scored_candidates], caller)
    visible_ids = {r.id for r in visible}

    for _cid, record, score in sorted(scored_candidates, key=lambda t: t[2], reverse=True):
        if record.id not in visible_ids:
            continue
        results.append(_to_result(record, score=score))

    return results[:top_k]


def _to_result(record: CodeChunkRecord, score: float) -> SearchResult:
    return SearchResult(
        chunk_id=record.id,
        path=record.path,
        symbol=record.symbol,
        kind=record.kind,
        content=record.content,
        score=score,
        record=record,
    )


__all__ = ["SearchResult", "CodeSearchBackend", "reciprocal_rank_fusion", "search_code"]
