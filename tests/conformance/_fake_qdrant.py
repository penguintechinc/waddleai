"""In-memory stand-in for ``qdrant_client.AsyncQdrantClient``.

Used by the vector-store conformance suite. Mirrors the fleet suite's
``_fake_dal.py`` rationale: a live Qdrant
container isn't available in this sandbox, and mocking every call site with
a bare ``MagicMock`` can't express real create/upsert/search semantics
(cosine ranking, dimension bookkeeping, filters). This implements just
enough of the real async client's surface -- ``get_collections``,
``get_collection``, ``create_collection``, ``upsert``, ``retrieve``,
``query_points``, ``delete``, ``delete_collection`` -- against an in-memory
dict store, accepting the *real* ``qdrant_client.http.models`` request
objects ``QdrantVectorStore`` constructs, so the adapter's own logic (id
mapping, dimension checks, sentinel-point handling, filter application) runs
for real, unmocked.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from shared.vectorstore.base import cosine_similarity


class _FakeCollection:
    def __init__(self, size: int) -> None:
        self.size = size
        self.points: dict[str, dict[str, Any]] = {}  # point_id -> {"vector", "payload"}


class FakeAsyncQdrantClient:
    """In-memory double for the subset of ``AsyncQdrantClient`` this codebase calls."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Accepts and ignores any constructor args (url/api_key/timeout/...)."""
        self._collections: dict[str, _FakeCollection] = {}
        self.unreachable = False  # tests flip this to simulate a dead server

    def _check_reachable(self) -> None:
        if self.unreachable:
            raise ConnectionError("fake qdrant: connection refused")

    async def get_collections(self) -> SimpleNamespace:
        self._check_reachable()
        names = [SimpleNamespace(name=name) for name in self._collections]
        return SimpleNamespace(collections=names)

    async def get_collection(self, collection_name: str) -> SimpleNamespace:
        self._check_reachable()
        coll = self._collections[collection_name]
        vectors = SimpleNamespace(size=coll.size)
        params = SimpleNamespace(vectors=vectors)
        config = SimpleNamespace(params=params)
        return SimpleNamespace(config=config)

    async def create_collection(self, collection_name: str, vectors_config: Any) -> bool:
        self._check_reachable()
        self._collections[collection_name] = _FakeCollection(size=vectors_config.size)
        return True

    async def upsert(self, collection_name: str, points: list[Any]) -> SimpleNamespace:
        self._check_reachable()
        coll = self._collections[collection_name]
        for point in points:
            coll.points[str(point.id)] = {
                "vector": list(point.vector),
                "payload": dict(point.payload or {}),
            }
        return SimpleNamespace(status="completed")

    async def retrieve(
        self, collection_name: str, ids: list[Any], with_payload: bool = True
    ) -> list[SimpleNamespace]:
        self._check_reachable()
        coll = self._collections.get(collection_name)
        if coll is None:
            return []
        out = []
        for pid in ids:
            entry = coll.points.get(str(pid))
            if entry is not None:
                out.append(SimpleNamespace(id=str(pid), payload=entry["payload"]))
        return out

    async def query_points(
        self,
        collection_name: str,
        query: list[float],
        limit: int = 10,
        score_threshold: float | None = None,
        query_filter: Any = None,
    ) -> SimpleNamespace:
        self._check_reachable()
        coll = self._collections[collection_name]
        scored = []
        for pid, entry in coll.points.items():
            payload = entry["payload"]
            if query_filter is not None and not _matches_filter(payload, query_filter):
                continue
            score = cosine_similarity(query, entry["vector"])
            if score_threshold is not None and score < score_threshold:
                continue
            scored.append(SimpleNamespace(id=pid, score=score, payload=payload))
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return SimpleNamespace(points=scored[:limit])

    async def delete(self, collection_name: str, points_selector: Any) -> SimpleNamespace:
        self._check_reachable()
        coll = self._collections.get(collection_name)
        if coll is not None:
            for pid in points_selector.points:
                coll.points.pop(str(pid), None)
        return SimpleNamespace(status="completed")

    async def delete_collection(self, collection_name: str) -> bool:
        self._check_reachable()
        self._collections.pop(collection_name, None)
        return True


def _matches_filter(payload: dict[str, Any], query_filter: Any) -> bool:
    """Evaluate a ``qdrant_client.http.models.Filter(must=[FieldCondition(...)])``."""
    for condition in getattr(query_filter, "must", None) or []:
        if payload.get(condition.key) != condition.match.value:
            return False
    return True
