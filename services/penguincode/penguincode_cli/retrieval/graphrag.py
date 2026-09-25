"""GraphRAG hybrid retrieval (T14): scoped vector top-k + scoped graph expansion.

``retrieve()`` is the read-side entry point layered on top of T6's
``PgVectorStore`` and T10's ``GraphStore``: embed the query -> scoped vector
top-k across the configured vector tables -> derive candidate graph seed
keys from the hits -> for each of the three graph kinds (``code`` /
``knowledge`` / ``memory``) whose PostHog flag is independently on, expand a
scoped subgraph from those seeds -> merge + rank -> assemble one bounded
augmented-context string. Every read is issued through ``PgVectorStore`` /
``GraphStore`` with the caller's ``ScopeContext`` passed through unchanged --
this module never queries Postgres directly, so the tenant/team/user
boundary those stores already enforce (re-applied at every graph traversal
hop, per ``stores/graph.py``) is never bypassed here.

**Flag gating, three independent layers** (spec S6.4, S10):

- ``penguincode.rag`` OFF -> no embedding call, no vector query, no graph
  expansion at all: an empty :class:`RetrievalResult` is returned before any
  other work happens.
- Each of ``penguincode.code-graph`` / ``penguincode.knowledge-graph`` /
  ``penguincode.memory-graph`` independently gates whether *that*
  ``graph_kind`` is expanded. A flag being off removes only that kind's
  contribution -- it is simply absent from :attr:`RetrievalResult.subgraphs`
  -- and never disables the vector half or the other two graph kinds.
- Flag-store outage: ``flags.client.is_enabled`` already degrades to a
  last-known cached value or OFF and never raises (T4) -- this module adds
  no additional handling on top of that; a flag store outage during
  retrieval simply behaves like the flag being off.

**Seed-key derivation is metadata-driven, not id-guessed.** A vector row
becomes a GraphRAG expansion seed by carrying the corresponding graph node's
``key`` in its metadata under ``"node_key"`` (a single string) and/or
``"node_keys"`` (a list of strings) -- the integration seam future indexers
and extractors (T7 docs-RAG, a future T13 memory-write hook) stamp when they
also write a graph node describing the same content. The hit's own
vector-row ``id`` is included too as a zero-cost extra candidate, for
producers that happen to reuse the same identifier as both the vector row id
and a graph node key. A candidate that matches no node in a given
``graph_kind`` simply contributes nothing to that kind's subgraph --
``GraphStore.subgraph`` never errors on an unmatched seed key.

**Graceful degradation everywhere.** An Ollama embedding failure degrades
the whole call to an empty result (no vector hits means no derivable seeds,
so graph expansion would be moot anyway). A single vector table's query
failure, or a single graph kind's expansion failure, degrades only that
piece -- the other tables/kinds still contribute -- mirroring
``docs_rag/indexer.py``'s ``search()`` and ``graphs/knowledge.py``'s
Ollama-failure handling. ``retrieve()`` never raises into its caller.

**Observability.** The composite operation is wrapped in a trace span
(``graphrag.retrieve``) via ``observability.otel.store_span`` carrying only
bounded, non-sensitive attributes (counts, depth, table count) -- never query
text or document content. Each underlying vector/graph call is already
individually instrumented (span + latency histogram + event counter) inside
``PgVectorStore.query`` / ``GraphStore.subgraph`` themselves (T6/T10), so no
additional per-call metric plumbing is duplicated here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import GraphConfig, PostgresGraphStoreConfig
from penguincode_cli.flags.client import (
    CODE_GRAPH_FLAG,
    KNOWLEDGE_GRAPH_FLAG,
    MEMORY_GRAPH_FLAG,
    RAG_FLAG,
    is_enabled,
)
from penguincode_cli.observability.otel import store_span
from penguincode_cli.stores.graph import GraphStore, Subgraph, create_graph_store
from penguincode_cli.stores.vector import PgVectorStore, TableName, VectorHit, VectorStore

logger = logging.getLogger(__name__)

#: Injectable embedding function signature -- mirrors ``docs_rag/indexer.py``'s
#: ``EmbedFn`` seam. Production default hits Ollama's ``/api/embeddings``
#: directly (see ``_get_embedding``); tests supply a deterministic fake.
EmbedFn = Callable[[str], Awaitable[list[float]]]

#: The three graph kinds and the flag gating each, in a fixed evaluation
#: order. Kept local (not imported from ``stores.graph.VALID_GRAPH_KINDS``)
#: because the flag *mapping* -- not just the set of valid kinds -- is what
#: this module owns.
_GRAPH_FLAGS: dict[str, str] = {
    "code": CODE_GRAPH_FLAG,
    "knowledge": KNOWLEDGE_GRAPH_FLAG,
    "memory": MEMORY_GRAPH_FLAG,
}

_DEFAULT_VECTOR_TABLES: tuple[TableName, ...] = ("docs_vectors", "memory_vectors")
_DEFAULT_OLLAMA_URL = "http://localhost:11434"
_DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"
_DEFAULT_MAX_CONTEXT_CHARS = 4000


@dataclass(slots=True, frozen=True)
class RetrievalResult:
    """Hybrid retrieval output: vector hits + per-graph-kind expansions + assembled context.

    ``subgraphs`` only contains an entry for a graph kind whose flag was on
    for this call -- a flag-off kind is absent from the mapping entirely
    (never present with an empty ``Subgraph``), so a caller can distinguish
    "this graph is off" from "this graph is on but found nothing".
    """

    vector_hits: list[VectorHit]
    subgraphs: dict[str, Subgraph]
    context: str


async def _get_embedding(
    query: str,
    *,
    embed_fn: EmbedFn | None,
    ollama_base_url: str,
    embedding_model: str,
) -> list[float]:
    """Embed ``query`` via the injected test double, or Ollama's ``/api/embeddings``.

    Raises on failure (connection error, non-2xx, missing/empty embedding
    field) -- the caller (``retrieve``) is responsible for catching and
    degrading, matching ``docs_rag/indexer.py``'s ``_get_embedding``/``search``
    split.
    """
    if embed_fn is not None:
        return await embed_fn(query)

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{ollama_base_url}/api/embeddings",
            json={"model": embedding_model, "prompt": query},
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        embedding = data.get("embedding")
        if not embedding:
            raise RuntimeError("ollama embeddings response missing a non-empty 'embedding' field")
        return [float(x) for x in embedding]


async def _query_table(
    store: VectorStore, ctx: ScopeContext, embedding: list[float], n: int, table: str
) -> list[VectorHit]:
    """Query one vector table, degrading to no hits (logged) on failure.

    ``VectorStore.query`` is a synchronous psycopg call -- run off the event
    loop via ``asyncio.to_thread`` rather than blocking it.
    """
    try:
        return await asyncio.to_thread(store.query, ctx, embedding, n=n)
    except Exception as exc:  # noqa: BLE001 -- one table's outage degrades, others still contribute
        logger.warning("graphrag.retrieve: vector query failed for table=%s: %s", table, exc)
        return []


async def _expand_kind(
    store: GraphStore, ctx: ScopeContext, kind: str, seed_keys: list[str], depth: int
) -> Subgraph:
    """Expand one graph kind's subgraph, degrading to an empty ``Subgraph`` (logged) on failure.

    ``GraphStore.subgraph`` is a synchronous psycopg call -- run off the
    event loop via ``asyncio.to_thread`` rather than blocking it.
    """
    try:
        return await asyncio.to_thread(store.subgraph, ctx, kind, seed_keys, depth=depth)
    except Exception as exc:  # noqa: BLE001 -- one kind's outage degrades, others still contribute
        logger.warning("graphrag.retrieve: graph expansion failed for kind=%s: %s", kind, exc)
        return Subgraph(nodes=[], edges=[])


def _seed_keys_from_hits(hits: list[VectorHit]) -> list[str]:
    """Derive candidate graph-node seed keys from vector hits (deduped, order-preserving).

    See the module docstring's "Seed-key derivation" section for the
    ``node_key`` / ``node_keys`` metadata contract and the ``hit.id``
    fallback.
    """
    seen: dict[str, None] = {}
    for hit in hits:
        candidates: list[str] = [hit.id]
        node_key = hit.metadata.get("node_key")
        if isinstance(node_key, str) and node_key:
            candidates.append(node_key)
        node_keys = hit.metadata.get("node_keys")
        if isinstance(node_keys, list):
            candidates.extend(key for key in node_keys if isinstance(key, str) and key)
        for key in candidates:
            seen.setdefault(key, None)
    return list(seen.keys())


def _assemble_context(
    vector_hits: list[VectorHit], subgraphs: dict[str, Subgraph], *, max_chars: int
) -> str:
    """Render vector hits + graph expansions into one bounded augmented-context string.

    Vector hits are rendered in the order given (the caller has already
    sorted them by score); every graph kind present in ``subgraphs`` (i.e.
    its flag was on) contributes its nodes and edges as short factual lines.
    The whole result is truncated to ``max_chars`` -- a defensive cap on
    prompt size, not a precise token budget -- rather than raising or
    dropping a whole section silently.
    """
    lines: list[str] = []
    for hit in vector_hits:
        lines.append(f"[vector score={hit.score:.3f}] {hit.document}")
    for kind, subgraph in subgraphs.items():
        for node in subgraph.nodes:
            lines.append(f"[graph:{kind}] node {node.node_type}:{node.key}")
        for edge in subgraph.edges:
            lines.append(
                f"[graph:{kind}] {edge.src_type}:{edge.src_key} "
                f"--{edge.rel_type}--> {edge.dst_type}:{edge.dst_key}"
            )
    context = "\n".join(lines)
    return context[:max_chars]


async def retrieve(
    ctx: ScopeContext,
    query: str,
    *,
    n_vector: int = 8,
    graph_depth: int = 1,
    vector_tables: tuple[TableName, ...] = _DEFAULT_VECTOR_TABLES,
    vector_stores: Mapping[str, VectorStore] | None = None,
    graph_store: GraphStore | None = None,
    embed_fn: EmbedFn | None = None,
    ollama_base_url: str = _DEFAULT_OLLAMA_URL,
    embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
    dsn: str | None = None,
    max_context_chars: int = _DEFAULT_MAX_CONTEXT_CHARS,
) -> RetrievalResult:
    """Hybrid GraphRAG retrieval: scoped vector top-k + scoped, flag-gated graph expansion.

    Args:
        ctx: Caller's validated scope -- the hard tenant/team/user boundary
            enforced by every store call this function makes.
        query: Free-text query to embed and search for.
        n_vector: Max vector hits to return, after merging all
            ``vector_tables`` and ranking by score descending.
        graph_depth: Traversal depth passed to ``GraphStore.subgraph`` for
            every enabled graph kind.
        vector_tables: Which ``PgVectorStore``-backed tables to search.
        vector_stores: Test/production seam -- a table -> ``VectorStore``
            mapping. Defaults to one ``PgVectorStore`` per table in
            ``vector_tables``, built from ``dsn`` (or ``PGVECTOR_URL``).
        graph_store: Test/production seam -- defaults to
            ``create_graph_store`` against the same DSN.
        embed_fn: Test seam bypassing the live Ollama HTTP call.
        ollama_base_url: Ollama base URL for the default embedding call.
        embedding_model: Ollama embedding model (768-dim ``nomic-embed-text``
            everywhere per the platform plan -- do not change casually).
        dsn: Shared-Postgres DSN for default store construction; falls back
            to the ``PGVECTOR_URL`` env var, matching ``PGVectorStoreConfig``
            / ``PostgresGraphStoreConfig``.
        max_context_chars: Bound on the assembled ``context`` string length.

    Returns:
        A :class:`RetrievalResult`. Never raises -- every failure mode
        (flag off, embedding failure, a table/kind outage) degrades to an
        empty or partial result instead.
    """
    empty = RetrievalResult(vector_hits=[], subgraphs={}, context="")

    if not is_enabled(RAG_FLAG, ctx):
        logger.debug("graphrag.retrieve skipped: %s is off", RAG_FLAG)
        return empty

    with store_span(
        "graphrag.retrieve",
        n_vector=n_vector,
        graph_depth=graph_depth,
        table_count=len(vector_tables),
    ):
        try:
            embedding = await _get_embedding(
                query,
                embed_fn=embed_fn,
                ollama_base_url=ollama_base_url,
                embedding_model=embedding_model,
            )
        except Exception as exc:  # noqa: BLE001 -- Ollama outage degrades to empty retrieval
            logger.warning("graphrag.retrieve: query embedding failed: %s", exc)
            return empty

        resolved_dsn = dsn if dsn is not None else os.environ.get("PGVECTOR_URL", "")
        stores: Mapping[str, VectorStore] = vector_stores or {
            table: PgVectorStore(resolved_dsn, table=table) for table in vector_tables
        }

        table_results = await asyncio.gather(
            *(
                _query_table(stores[table], ctx, embedding, n_vector, table)
                for table in vector_tables
                if table in stores
            )
        )
        all_hits: list[VectorHit] = [hit for hits in table_results for hit in hits]
        all_hits.sort(key=lambda hit: hit.score, reverse=True)
        vector_hits = all_hits[:n_vector]

        seed_keys = _seed_keys_from_hits(vector_hits)
        graph = graph_store or create_graph_store(
            GraphConfig(postgres=PostgresGraphStoreConfig(url=resolved_dsn))
        )

        enabled_kinds = [kind for kind, flag in _GRAPH_FLAGS.items() if is_enabled(flag, ctx)]
        kind_results = await asyncio.gather(
            *(
                _expand_kind(graph, ctx, kind, seed_keys, graph_depth)
                for kind in enabled_kinds
            )
        )
        subgraphs = dict(zip(enabled_kinds, kind_results, strict=True))

        context = _assemble_context(vector_hits, subgraphs, max_chars=max_context_chars)
        logger.info(
            "graphrag.retrieve: %d vector hit(s), %d graph(s) expanded, %d char(s) context",
            len(vector_hits),
            len(subgraphs),
            len(context),
        )
        return RetrievalResult(vector_hits=vector_hits, subgraphs=subgraphs, context=context)


__all__ = ["EmbedFn", "RetrievalResult", "retrieve"]
