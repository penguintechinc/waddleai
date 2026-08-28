"""``VectorStoreBackend`` ABC and its value types (spec §17.1).

Every vector-store backend — pgvector (default) and Qdrant (local-only
profile) — implements this five-method interface so callers work against
one collection/point/search shape regardless of which store is behind it.
Modeled directly on ``shared.fleet.base.InferenceFleetBackend``: a pure
contract module, no behavior, concrete backends do the work.

Collections carry an explicit ``dimensions`` and ``embedder_id`` at creation
time. Both backends refuse (``VectorCollectionMismatchError``) rather than
silently accept a collection re-opened with a different dimensionality or a
different embedder — the failure mode this guards against is real: WaddleAI
runs two independent embedding paths at different dimensions (``nomic-embed-
text``, 768-dim, via Ollama; ``all-MiniLM-L6-v2``, 384-dim, in-process
SentenceTransformer) and pointing one at the other's collection is a
dimension mismatch that, left unchecked, fails confusingly at query time
instead of being refused up front.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class VectorStoreBackendType(StrEnum):
    """Vector-store backend types recognized by the registry (§17.1)."""

    PGVECTOR = "pgvector"
    QDRANT = "qdrant"


@dataclass(slots=True, frozen=True)
class CollectionSpec:
    """Desired-state spec for a vector collection.

    ``embedder_id`` is an opaque identifier for the embedding model that
    produced (and must keep producing) vectors for this collection, e.g.
    ``"ollama:nomic-embed-text"`` or ``"sentence-transformers:all-MiniLM-L6-v2"``.
    It is compared verbatim on every ``ensure_collection`` call — a
    same-dimension-but-different-model collision is refused just like a
    dimension mismatch, since two models at the same output width are not
    interchangeable.
    """

    name: str
    dimensions: int
    embedder_id: str
    distance: str = "cosine"


@dataclass(slots=True, frozen=True)
class VectorPoint:
    """A single point to upsert: an external id, its vector, and a payload."""

    id: str
    vector: list[float]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class VectorSearchResult:
    """One ranked hit from ``search`` — the external id, score, and payload."""

    id: str
    score: float
    payload: dict[str, Any]


@dataclass(slots=True, frozen=True)
class VectorStoreHealth:
    """Health snapshot returned by ``health()``. Never raises — always returned."""

    healthy: bool
    backend: str
    detail: dict[str, Any] = field(default_factory=dict)


class VectorCollectionMismatchError(ValueError):
    """Raised when a collection is reopened with different dimensions/embedder.

    Fail-fast, fail-clear: raised at ``ensure_collection``/``upsert``/
    ``search`` time (whichever the caller reaches first) rather than left to
    surface as a confusing distance-computation error deep in a query.
    """


class VectorStoreBackend(abc.ABC):
    """Pluggable interface every vector-store backend implements.

    Concrete subclasses set the ``backend_name`` class attribute and
    implement all five abstract methods. Construction is backend-specific
    (connection handles, URLs, credentials) — the registry factory
    (``shared.vectorstore.registry``) is the uniform construction seam for
    callers that select a backend by type rather than importing a concrete
    class directly.
    """

    backend_name: str

    @abc.abstractmethod
    async def ensure_collection(self, spec: CollectionSpec) -> None:
        """Create the collection if absent; refuse a dimensions/embedder mismatch if present."""
        raise NotImplementedError

    @abc.abstractmethod
    async def upsert(self, collection: str, points: list[VectorPoint]) -> None:
        """Insert or overwrite ``points`` by their external id.

        Raises ``VectorCollectionMismatchError`` if any point's vector length
        disagrees with the collection's registered dimensions, and
        ``ValueError`` if the collection does not exist yet.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def search(
        self,
        collection: str,
        query_vector: list[float],
        top_k: int = 10,
        min_score: float = 0.0,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorSearchResult]:
        """Return the ``top_k`` nearest points to ``query_vector``, best first.

        Raises ``VectorCollectionMismatchError`` if ``query_vector``'s length
        disagrees with the collection's registered dimensions.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def delete(self, collection: str, ids: list[str]) -> None:
        """Delete points by external id. Unknown ids are a no-op, not an error."""
        raise NotImplementedError

    @abc.abstractmethod
    async def delete_collection(self, collection: str) -> None:
        """Delete a collection and all its points. No-op if it doesn't exist."""
        raise NotImplementedError

    @abc.abstractmethod
    async def health(self) -> VectorStoreHealth:
        """Return a health snapshot for this backend. Must never raise."""
        raise NotImplementedError


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [-1, 1]; 0.0 for empty/mismatched-length/zero vectors.

    Shared by backends that rank in Python (pgvector's JSON-stored vectors,
    and any conformance-suite fake) — mirrors ``shared.cache.semantic``'s
    private helper of the same shape, promoted here since two backends need
    it.
    """
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(dot / (norm_a * norm_b))


__all__ = [
    "CollectionSpec",
    "VectorCollectionMismatchError",
    "VectorPoint",
    "VectorSearchResult",
    "VectorStoreBackend",
    "VectorStoreBackendType",
    "VectorStoreHealth",
    "cosine_similarity",
]
