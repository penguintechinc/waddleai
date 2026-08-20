"""Qdrant-backed ``VectorStoreBackend`` — the local-only profile implementation.

Docker is the user's, not ours: this class only ever connects to an
already-running Qdrant instance at ``url``; nothing here starts, stops, or
configures a container (spec §17). Unreachability is surfaced via
``health()`` / a raised ``VectorCollectionMismatchError`` — never a silent
fallback to another backend.

Qdrant point ids must be an unsigned int or a UUID, but this interface's
``VectorPoint.id`` is an arbitrary caller string, so ids are mapped through
a deterministic ``uuid.uuid5`` and the original string is round-tripped via
a reserved ``_id`` payload key.

Qdrant has no native collection-level metadata field to persist
``embedder_id`` against (only vector config: size/distance), so it's
persisted via a reserved sentinel point (fixed id, all-zero vector matching
the collection's dimensions, ``__meta__`` payload marker) written at
collection-creation time. This makes the embedder-mismatch check survive a
process restart, matching the dimension check's persistence guarantee (the
dimension check itself needs no such trick — Qdrant reports the real
``vectors_config.size`` on every ``get_collection`` call, authoritatively).
"""

from __future__ import annotations

import uuid
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qmodels

from shared.vectorstore.base import (
    CollectionSpec,
    VectorCollectionMismatchError,
    VectorPoint,
    VectorSearchResult,
    VectorStoreBackend,
    VectorStoreBackendType,
    VectorStoreHealth,
)
from shared.vectorstore.registry import register

# Fixed, arbitrary namespace UUID for the id5 mapping below (RFC 4122 §4.3) —
# any constant works as long as it's stable across process restarts.
_ID_NAMESPACE = uuid.UUID("6f1b1e0a-6c3e-4c9b-9a8e-2f6a4d5e8b7a")
_META_POINT_ID = "00000000-0000-0000-0000-000000000000"


def _qdrant_point_id(external_id: str) -> str:
    """Map an arbitrary external id to a deterministic Qdrant-legal UUID string."""
    return str(uuid.uuid5(_ID_NAMESPACE, external_id))


def _vector_size(info: Any) -> int:
    """Extract the configured vector width from a ``CollectionInfo``.

    Only single (unnamed) vector collections are created by this backend
    (``ensure_collection`` always calls ``create_collection`` with a bare
    ``VectorParams``), so ``vectors`` is a ``VectorParams`` with a direct
    ``.size`` — never the named-vectors dict shape.
    """
    vectors = info.config.params.vectors
    if hasattr(vectors, "size"):
        return int(vectors.size)
    raise VectorCollectionMismatchError(
        "Expected an unnamed single-vector collection; found a named-vectors config "
        "(not created by this backend)"
    )


@register(VectorStoreBackendType.QDRANT)
class QdrantVectorStore(VectorStoreBackend):
    """The local-only profile's vector-store backend — a user-run Qdrant container."""

    backend_name = "qdrant"

    def __init__(self, url: str, api_key: str | None = None, timeout: float = 5.0) -> None:
        """``url``: e.g. ``http://localhost:6333``. Never starts/stops the container."""
        self.url = url
        self._client = AsyncQdrantClient(url=url, api_key=api_key, timeout=int(timeout))

    async def ensure_collection(self, spec: CollectionSpec) -> None:
        """See ``VectorStoreBackend.ensure_collection``."""
        existing = await self._client.get_collections()
        names = {c.name for c in existing.collections}

        if spec.name not in names:
            await self._client.create_collection(
                collection_name=spec.name,
                vectors_config=qmodels.VectorParams(
                    size=spec.dimensions, distance=qmodels.Distance.COSINE
                ),
            )
            await self._write_meta_point(spec)
            return

        info = await self._client.get_collection(spec.name)
        actual_size = _vector_size(info)
        if actual_size != spec.dimensions:
            raise VectorCollectionMismatchError(
                f"Qdrant collection {spec.name!r} has dimensions={actual_size}; "
                f"refusing mismatched dimensions={spec.dimensions}"
            )

        meta = await self._client.retrieve(spec.name, ids=[_META_POINT_ID])
        if meta:
            recorded_embedder = meta[0].payload.get("embedder_id") if meta[0].payload else None
            if recorded_embedder and recorded_embedder != spec.embedder_id:
                raise VectorCollectionMismatchError(
                    f"Qdrant collection {spec.name!r} was created with "
                    f"embedder_id={recorded_embedder!r}; refusing mismatched "
                    f"embedder_id={spec.embedder_id!r}"
                )
        else:
            # Dimensions already match; backfill metadata for a collection
            # created by a foreign writer (or an older version of this class).
            await self._write_meta_point(spec)

    async def _write_meta_point(self, spec: CollectionSpec) -> None:
        await self._client.upsert(
            collection_name=spec.name,
            points=[
                qmodels.PointStruct(
                    id=_META_POINT_ID,
                    vector=[0.0] * spec.dimensions,
                    payload={
                        "__meta__": True,
                        "embedder_id": spec.embedder_id,
                        "distance": spec.distance,
                    },
                )
            ],
        )

    async def _expected_dims(self, collection: str) -> int:
        info = await self._client.get_collection(collection)
        return _vector_size(info)

    async def upsert(self, collection: str, points: list[VectorPoint]) -> None:
        """See ``VectorStoreBackend.upsert``."""
        expected = await self._expected_dims(collection)
        for point in points:
            if len(point.vector) != expected:
                raise VectorCollectionMismatchError(
                    f"Point {point.id!r} has {len(point.vector)} dims; "
                    f"collection {collection!r} expects {expected}"
                )

        qpoints = [
            qmodels.PointStruct(
                id=_qdrant_point_id(point.id),
                vector=point.vector,
                payload={**point.payload, "_id": point.id},
            )
            for point in points
        ]
        await self._client.upsert(collection_name=collection, points=qpoints)

    async def search(
        self,
        collection: str,
        query_vector: list[float],
        top_k: int = 10,
        min_score: float = 0.0,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorSearchResult]:
        """See ``VectorStoreBackend.search``."""
        expected = await self._expected_dims(collection)
        if len(query_vector) != expected:
            raise VectorCollectionMismatchError(
                f"Query vector has {len(query_vector)} dims; "
                f"collection {collection!r} expects {expected}"
            )

        qfilter = None
        if filters:
            qfilter = qmodels.Filter(
                must=[
                    qmodels.FieldCondition(key=k, match=qmodels.MatchValue(value=v))
                    for k, v in filters.items()
                ]
            )

        response = await self._client.query_points(
            collection_name=collection,
            query=query_vector,
            limit=top_k + 1,  # +1 headroom in case the sentinel meta point scores in-range
            score_threshold=min_score if min_score > 0 else None,
            query_filter=qfilter,
        )

        results: list[VectorSearchResult] = []
        for hit in response.points:
            payload = hit.payload or {}
            if payload.get("__meta__"):
                continue
            results.append(
                VectorSearchResult(
                    id=payload.get("_id", str(hit.id)),
                    score=hit.score,
                    payload={k: v for k, v in payload.items() if k != "_id"},
                )
            )
        return results[:top_k]

    async def delete(self, collection: str, ids: list[str]) -> None:
        """See ``VectorStoreBackend.delete``."""
        await self._client.delete(
            collection_name=collection,
            points_selector=qmodels.PointIdsList(points=[_qdrant_point_id(i) for i in ids]),
        )

    async def delete_collection(self, collection: str) -> None:
        """See ``VectorStoreBackend.delete_collection``."""
        await self._client.delete_collection(collection)

    async def health(self) -> VectorStoreHealth:
        """See ``VectorStoreBackend.health``."""
        try:
            await self._client.get_collections()
            return VectorStoreHealth(
                healthy=True, backend=self.backend_name, detail={"url": self.url}
            )
        except Exception as exc:  # noqa: BLE001 -- health probe reports, never raises
            return VectorStoreHealth(
                healthy=False,
                backend=self.backend_name,
                detail={"url": self.url, "error": str(exc)},
            )
