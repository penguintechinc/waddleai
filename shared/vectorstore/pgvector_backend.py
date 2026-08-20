"""Pgvector-backed ``VectorStoreBackend`` — the default, cluster-path implementation.

Storage is two tables (``local_vector_collections``, ``local_vector_points``,
migration 015): vectors are JSON-serialized text, not a native pgvector
``vector(n)`` column, and ranking is cosine similarity computed in Python —
deliberately, the same tradeoff ``shared.cache.semantic.SemanticCache``
already makes, so this module is byte-identical on SQLite (tests, via the
fleet-conformance-suite's ``FakeDAL``) and PostgreSQL (production) without a
dialect branch. A per-collection fixed vector width would otherwise force a
real ``vector(n)`` column per collection (schema-per-collection, or a
runtime ``ALTER TABLE`` — both ruled out by the "no runtime DDL, no
per-collection migrations" constraint this generic interface exists to
satisfy).

Queries go through penguin-dal's PyDAL query API (``db.table.insert``,
``db(query).select()``) exclusively — no raw SQL — so this class works
unmodified against the real ``penguin_dal.DAL`` in production and against
the lightweight ``FakeDAL`` test double used by the fleet conformance suite
(``tests/conformance/_fake_dal.py``), reused here rather than duplicated.

This module is entirely new and net-new-optional: it does not touch, wrap,
or alter ``shared.utils.memory_integration.PgvectorMemoryStore``,
``shared.utils.rag_integration.PgvectorRAGStore``, or
``shared.cache.semantic.SemanticCache`` in any way. Those remain the
production runtime path for memory/RAG/semantic-cache today, completely
unchanged.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import orjson

from shared.vectorstore.base import (
    CollectionSpec,
    VectorCollectionMismatchError,
    VectorPoint,
    VectorSearchResult,
    VectorStoreBackend,
    VectorStoreBackendType,
    VectorStoreHealth,
    cosine_similarity,
)
from shared.vectorstore.registry import register


def define_local_vectorstore_tables(db: Any) -> None:
    """Register the PyDAL field definitions for a real (non-fake) ``db`` handle.

    Alembic migration 015 is the schema authority (``CREATE TABLE``); this
    only teaches PyDAL's query builder the column set so
    ``PgvectorVectorStore`` can issue ``db.local_vector_collections`` /
    ``db.local_vector_points`` queries against it — ``migrate=False``
    throughout, matching the house rule that PyDAL/penguin-dal never
    auto-migrates schema at runtime. Call once at service startup for a real
    ``db``; the conformance suite's ``FakeDAL`` auto-vivifies table access
    on attribute lookup and needs no explicit definition, so tests never
    call this.
    """
    from penguin_dal import Field  # noqa: PLC0415 -- optional runtime-only import path

    if "local_vector_collections" not in db.tables:
        db.define_table(
            "local_vector_collections",
            Field("name", unique=True, notnull=True),
            Field("dimensions", "integer", notnull=True),
            Field("embedder_id", "string", notnull=True),
            Field("distance", "string", default="cosine"),
            Field("created_at", "datetime", default=datetime.utcnow),
            migrate=False,
        )
    if "local_vector_points" not in db.tables:
        db.define_table(
            "local_vector_points",
            Field("collection_id", "reference local_vector_collections", notnull=True),
            Field("external_id", "string", notnull=True),
            Field("vector_json", "text", notnull=True),
            Field("payload_json", "text", default="{}"),
            Field("created_at", "datetime", default=datetime.utcnow),
            migrate=False,
        )


@register(VectorStoreBackendType.PGVECTOR)
class PgvectorVectorStore(VectorStoreBackend):
    """The default vector-store backend — penguin-dal against Postgres (or SQLite in tests)."""

    backend_name = "pgvector"

    def __init__(self, db: Any) -> None:
        """Bind to ``db``.

        ``db``: a penguin-dal/PyDAL handle (real, or ``FakeDAL`` in tests)
        with ``local_vector_collections``/``local_vector_points`` already
        reachable (via ``define_local_vectorstore_tables`` for a real
        handle).
        """
        self.db = db

    async def ensure_collection(self, spec: CollectionSpec) -> None:
        """See ``VectorStoreBackend.ensure_collection``."""
        await asyncio.to_thread(self._ensure_collection_sync, spec)

    def _ensure_collection_sync(self, spec: CollectionSpec) -> None:
        table = self.db.local_vector_collections
        existing = self.db(table.name == spec.name).select().first()
        if existing is None:
            table.insert(
                name=spec.name,
                dimensions=spec.dimensions,
                embedder_id=spec.embedder_id,
                distance=spec.distance,
                created_at=datetime.utcnow(),
            )
            self.db.commit()
            return
        if existing.dimensions != spec.dimensions or existing.embedder_id != spec.embedder_id:
            raise VectorCollectionMismatchError(
                f"Collection {spec.name!r} already exists with "
                f"dimensions={existing.dimensions} embedder_id={existing.embedder_id!r}; "
                f"refusing mismatched dimensions={spec.dimensions} "
                f"embedder_id={spec.embedder_id!r}"
            )

    def _get_collection_row(self, collection: str) -> Any:
        table = self.db.local_vector_collections
        row = self.db(table.name == collection).select().first()
        if row is None:
            raise ValueError(
                f"Vector collection {collection!r} not found; call ensure_collection first"
            )
        return row

    async def upsert(self, collection: str, points: list[VectorPoint]) -> None:
        """See ``VectorStoreBackend.upsert``."""
        await asyncio.to_thread(self._upsert_sync, collection, points)

    def _upsert_sync(self, collection: str, points: list[VectorPoint]) -> None:
        coll = self._get_collection_row(collection)
        for point in points:
            if len(point.vector) != coll.dimensions:
                raise VectorCollectionMismatchError(
                    f"Point {point.id!r} has {len(point.vector)} dims; "
                    f"collection {collection!r} expects {coll.dimensions}"
                )

        points_table = self.db.local_vector_points
        for point in points:
            existing = (
                self.db(
                    (points_table.collection_id == coll.id) & (points_table.external_id == point.id)
                )
                .select()
                .first()
            )
            values = {
                "vector_json": orjson.dumps(point.vector).decode(),
                "payload_json": orjson.dumps(point.payload).decode(),
            }
            if existing is None:
                points_table.insert(
                    collection_id=coll.id,
                    external_id=point.id,
                    created_at=datetime.utcnow(),
                    **values,
                )
            else:
                self.db(points_table.id == existing.id).update(**values)
        self.db.commit()

    async def search(
        self,
        collection: str,
        query_vector: list[float],
        top_k: int = 10,
        min_score: float = 0.0,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorSearchResult]:
        """See ``VectorStoreBackend.search``."""
        return await asyncio.to_thread(
            self._search_sync, collection, query_vector, top_k, min_score, filters
        )

    def _search_sync(
        self,
        collection: str,
        query_vector: list[float],
        top_k: int,
        min_score: float,
        filters: dict[str, Any] | None,
    ) -> list[VectorSearchResult]:
        coll = self._get_collection_row(collection)
        if len(query_vector) != coll.dimensions:
            raise VectorCollectionMismatchError(
                f"Query vector has {len(query_vector)} dims; "
                f"collection {collection!r} expects {coll.dimensions}"
            )

        points_table = self.db.local_vector_points
        rows = self.db(points_table.collection_id == coll.id).select()

        scored: list[VectorSearchResult] = []
        for row in rows:
            payload = orjson.loads(row.payload_json)
            if filters and not all(payload.get(k) == v for k, v in filters.items()):
                continue
            vector = orjson.loads(row.vector_json)
            score = cosine_similarity(query_vector, vector)
            if score < min_score:
                continue
            scored.append(VectorSearchResult(id=row.external_id, score=score, payload=payload))

        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]

    async def delete(self, collection: str, ids: list[str]) -> None:
        """See ``VectorStoreBackend.delete``."""
        await asyncio.to_thread(self._delete_sync, collection, ids)

    def _delete_sync(self, collection: str, ids: list[str]) -> None:
        coll = self._get_collection_row(collection)
        points_table = self.db.local_vector_points
        self.db(
            (points_table.collection_id == coll.id) & (points_table.external_id.belongs(ids))
        ).delete()
        self.db.commit()

    async def delete_collection(self, collection: str) -> None:
        """See ``VectorStoreBackend.delete_collection``."""
        await asyncio.to_thread(self._delete_collection_sync, collection)

    def _delete_collection_sync(self, collection: str) -> None:
        table = self.db.local_vector_collections
        row = self.db(table.name == collection).select().first()
        if row is None:
            return
        points_table = self.db.local_vector_points
        self.db(points_table.collection_id == row.id).delete()
        self.db(table.id == row.id).delete()
        self.db.commit()

    async def health(self) -> VectorStoreHealth:
        """See ``VectorStoreBackend.health``."""
        try:
            await asyncio.to_thread(
                lambda: self.db(self.db.local_vector_collections.id > 0).select()
            )
            return VectorStoreHealth(healthy=True, backend=self.backend_name, detail={})
        except Exception as exc:  # noqa: BLE001 -- health probe reports, never raises
            return VectorStoreHealth(
                healthy=False, backend=self.backend_name, detail={"error": str(exc)}
            )
