"""Cross-backend conformance suite for ``VectorStoreBackend`` (spec §17.1).

Parametrized over both spec-mandated backends: pgvector (default, against
the fleet suite's ``FakeDAL``) and Qdrant (local-only profile, against
``FakeAsyncQdrantClient`` -- see ``_fake_qdrant.py``). This is the
hermetic, always-run unit-level conformance pass -- no network, no Docker,
safe for any CI runner. A genuine live-Qdrant/live-Ollama round trip
(including a real 768-dim nomic-embed-text vector refused against a
384-dim collection) lives in
``tests/integration/test_vectorstore_local_profile.py``, skipped
automatically when either service isn't reachable.
"""

from __future__ import annotations

import pytest

from shared.vectorstore.base import (
    CollectionSpec,
    VectorCollectionMismatchError,
    VectorPoint,
)
from shared.vectorstore.pgvector_backend import PgvectorVectorStore
from shared.vectorstore.qdrant_backend import QdrantVectorStore
from tests.conformance._fake_dal import FakeDAL
from tests.conformance._fake_qdrant import FakeAsyncQdrantClient


@pytest.fixture
def pgvector_backend() -> PgvectorVectorStore:
    """A fresh ``PgvectorVectorStore`` bound to an empty ``FakeDAL``."""
    return PgvectorVectorStore(db=FakeDAL())


@pytest.fixture
def qdrant_backend(monkeypatch) -> QdrantVectorStore:
    """A fresh ``QdrantVectorStore`` with ``AsyncQdrantClient`` swapped for the in-memory fake."""
    fake_client = FakeAsyncQdrantClient()
    monkeypatch.setattr(
        "shared.vectorstore.qdrant_backend.AsyncQdrantClient",
        lambda *args, **kwargs: fake_client,
    )
    return QdrantVectorStore(url="http://fake-qdrant:6333")


# BACKENDS: (fixture_name, expected backend_name) -- both spec-mandated backends.
BACKENDS = [
    ("pgvector_backend", "pgvector"),
    ("qdrant_backend", "qdrant"),
]

_SPEC = CollectionSpec(name="conformance-coll", dimensions=3, embedder_id="test:embedder-a")


@pytest.mark.parametrize("backend_fixture,expected_name", BACKENDS)
def test_backend_name_matches(backend_fixture, expected_name, request) -> None:
    """Each backend's ``backend_name`` matches its registry key."""
    backend = request.getfixturevalue(backend_fixture)
    assert backend.backend_name == expected_name


@pytest.mark.parametrize("backend_fixture", ["pgvector_backend", "qdrant_backend"])
class TestVectorStoreConformance:
    """One shared test body run against every backend fixture (spec §17.1)."""

    async def test_ensure_collection_idempotent(self, backend_fixture, request) -> None:
        """Calling ``ensure_collection`` twice with an identical spec is a no-op, not an error."""
        backend = request.getfixturevalue(backend_fixture)
        await backend.ensure_collection(_SPEC)
        await backend.ensure_collection(_SPEC)  # must not raise

    async def test_ensure_collection_dimension_mismatch_refused(
        self, backend_fixture, request
    ) -> None:
        """Reopening a collection with a different dimensionality is refused, not accepted.

        This is the concrete failure mode WaddleAI's two embedding paths
        create: nomic-embed-text (768-dim) and all-MiniLM-L6-v2 (384-dim)
        must never share a collection.
        """
        backend = request.getfixturevalue(backend_fixture)
        await backend.ensure_collection(_SPEC)

        mismatched = CollectionSpec(
            name=_SPEC.name, dimensions=384, embedder_id=_SPEC.embedder_id
        )
        with pytest.raises(VectorCollectionMismatchError):
            await backend.ensure_collection(mismatched)

    async def test_ensure_collection_embedder_mismatch_refused(
        self, backend_fixture, request
    ) -> None:
        """Same dims, different embedder_id is refused too -- dims alone isn't a safe check."""
        backend = request.getfixturevalue(backend_fixture)
        await backend.ensure_collection(_SPEC)

        mismatched = CollectionSpec(
            name=_SPEC.name, dimensions=_SPEC.dimensions, embedder_id="test:embedder-b"
        )
        with pytest.raises(VectorCollectionMismatchError):
            await backend.ensure_collection(mismatched)

    async def test_upsert_wrong_dimension_point_refused(self, backend_fixture, request) -> None:
        """A point whose vector length disagrees with the collection is refused before writing."""
        backend = request.getfixturevalue(backend_fixture)
        await backend.ensure_collection(_SPEC)

        bad_point = VectorPoint(id="bad", vector=[0.1, 0.2], payload={})
        with pytest.raises(VectorCollectionMismatchError):
            await backend.upsert(_SPEC.name, [bad_point])

    async def test_search_wrong_dimension_query_refused(self, backend_fixture, request) -> None:
        """A query vector of the wrong length is refused rather than silently scored."""
        backend = request.getfixturevalue(backend_fixture)
        await backend.ensure_collection(_SPEC)
        await backend.upsert(
            _SPEC.name, [VectorPoint(id="a", vector=[1.0, 0.0, 0.0], payload={"k": "a"})]
        )

        with pytest.raises(VectorCollectionMismatchError):
            await backend.search(_SPEC.name, query_vector=[1.0, 0.0])

    async def test_upsert_and_search_returns_nearest_first(
        self, backend_fixture, request
    ) -> None:
        """Search ranks by cosine similarity, best match first, respecting top_k."""
        backend = request.getfixturevalue(backend_fixture)
        await backend.ensure_collection(_SPEC)
        await backend.upsert(
            _SPEC.name,
            [
                VectorPoint(id="close", vector=[0.9, 0.1, 0.0], payload={"label": "close"}),
                VectorPoint(id="exact", vector=[1.0, 0.0, 0.0], payload={"label": "exact"}),
                VectorPoint(
                    id="orthogonal", vector=[0.0, 1.0, 0.0], payload={"label": "orthogonal"}
                ),
            ],
        )

        results = await backend.search(_SPEC.name, query_vector=[1.0, 0.0, 0.0], top_k=2)

        assert [r.id for r in results] == ["exact", "close"]
        assert results[0].score == pytest.approx(1.0, abs=1e-6)
        assert results[0].payload["label"] == "exact"

    async def test_search_respects_min_score(self, backend_fixture, request) -> None:
        """A high min_score excludes weakly-related points."""
        backend = request.getfixturevalue(backend_fixture)
        await backend.ensure_collection(_SPEC)
        await backend.upsert(
            _SPEC.name,
            [
                VectorPoint(id="exact", vector=[1.0, 0.0, 0.0], payload={}),
                VectorPoint(id="orthogonal", vector=[0.0, 1.0, 0.0], payload={}),
            ],
        )

        results = await backend.search(_SPEC.name, query_vector=[1.0, 0.0, 0.0], min_score=0.5)

        assert [r.id for r in results] == ["exact"]

    async def test_upsert_overwrites_existing_point(self, backend_fixture, request) -> None:
        """Re-upserting the same external id updates it in place, not a duplicate."""
        backend = request.getfixturevalue(backend_fixture)
        await backend.ensure_collection(_SPEC)
        await backend.upsert(
            _SPEC.name, [VectorPoint(id="p1", vector=[1.0, 0.0, 0.0], payload={"v": 1})]
        )
        await backend.upsert(
            _SPEC.name, [VectorPoint(id="p1", vector=[0.0, 1.0, 0.0], payload={"v": 2})]
        )

        results = await backend.search(_SPEC.name, query_vector=[0.0, 1.0, 0.0], top_k=10)

        assert len(results) == 1
        assert results[0].id == "p1"
        assert results[0].payload["v"] == 2

    async def test_delete_removes_points(self, backend_fixture, request) -> None:
        """Deleted points no longer appear in search results."""
        backend = request.getfixturevalue(backend_fixture)
        await backend.ensure_collection(_SPEC)
        await backend.upsert(
            _SPEC.name,
            [
                VectorPoint(id="keep", vector=[1.0, 0.0, 0.0], payload={}),
                VectorPoint(id="drop", vector=[1.0, 0.0, 0.0], payload={}),
            ],
        )

        await backend.delete(_SPEC.name, ["drop"])
        results = await backend.search(_SPEC.name, query_vector=[1.0, 0.0, 0.0], top_k=10)

        assert [r.id for r in results] == ["keep"]

    async def test_delete_unknown_id_is_noop(self, backend_fixture, request) -> None:
        """Deleting an id that was never inserted does not raise."""
        backend = request.getfixturevalue(backend_fixture)
        await backend.ensure_collection(_SPEC)
        await backend.delete(_SPEC.name, ["never-existed"])  # must not raise

    async def test_delete_collection_allows_clean_recreate(
        self, backend_fixture, request
    ) -> None:
        """After delete_collection, a fresh ensure_collection with different dims succeeds."""
        backend = request.getfixturevalue(backend_fixture)
        await backend.ensure_collection(_SPEC)
        await backend.upsert(
            _SPEC.name, [VectorPoint(id="a", vector=[1.0, 0.0, 0.0], payload={})]
        )

        await backend.delete_collection(_SPEC.name)

        new_spec = CollectionSpec(name=_SPEC.name, dimensions=5, embedder_id="test:embedder-c")
        await backend.ensure_collection(new_spec)  # must not raise VectorCollectionMismatchError

    async def test_health_reports_healthy(self, backend_fixture, request) -> None:
        """A reachable backend reports healthy=True with its own backend_name."""
        backend = request.getfixturevalue(backend_fixture)
        health = await backend.health()

        assert health.healthy is True
        assert health.backend == backend.backend_name


@pytest.mark.skip(
    reason=(
        "Server-side HNSW ANN ranking behavior (as opposed to this suite's "
        "FakeAsyncQdrantClient brute-force cosine ranking, which validates the "
        "adapter's own logic but not Qdrant's real index) is exercised in "
        "tests/integration/test_vectorstore_local_profile.py against a live "
        "Qdrant instance, auto-skipped there when unreachable. Not duplicated "
        "here since this module is the hermetic, network-free conformance pass."
    )
)
class TestQdrantLiveContainer:
    """Placeholder — see tests/integration/test_vectorstore_local_profile.py instead."""

    async def test_against_live_qdrant(self) -> None:
        """Never runs — see class skip reason."""
        raise AssertionError("should never run — see skip reason")
