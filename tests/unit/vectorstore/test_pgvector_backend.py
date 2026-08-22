"""Unit tests for ``shared.vectorstore.pgvector_backend`` (PgvectorVectorStore + table defs).

Complements ``tests/conformance/test_vectorstore_conformance.py`` (which
exercises the shared ``VectorStoreBackend`` contract across both backends)
with pgvector-specific coverage that isn't part of that shared contract:
``define_local_vectorstore_tables``, the ``_get_collection_row`` not-found
branch, search filter matching/exclusion, and the ``health()`` failure
branch. Uses ``tests.conformance._fake_dal.FakeDAL`` -- the existing
PyDAL-calling-convention fake already reused by
``tests/unit/vectorstore/test_factory.py`` and ``test_registry.py`` --
rather than a second hand-rolled fake.
"""

from __future__ import annotations

import orjson
import pytest

from shared.vectorstore.base import (
    CollectionSpec,
    VectorCollectionMismatchError,
    VectorPoint,
)
from shared.vectorstore.pgvector_backend import (
    PgvectorVectorStore,
    define_local_vectorstore_tables,
)
from tests.conformance._fake_dal import FakeDAL

_SPEC = CollectionSpec(name="coll-a", dimensions=3, embedder_id="test:embedder-a")


@pytest.fixture
def backend() -> PgvectorVectorStore:
    """A fresh ``PgvectorVectorStore`` bound to an empty ``FakeDAL``."""
    return PgvectorVectorStore(db=FakeDAL())


# --- define_local_vectorstore_tables ---------------------------------------


class _RealishDB:
    """Minimal stand-in for a real penguin-dal ``db`` handle's schema surface.

    ``FakeDAL`` auto-vivifies table access and has no ``.tables``/
    ``.define_table`` concept (real schema definition never applies to it),
    so ``define_local_vectorstore_tables`` -- which is only ever called
    against a real handle -- needs its own minimal fake.
    """

    def __init__(self, existing_tables: set[str] | None = None) -> None:
        self.tables: set[str] = set(existing_tables or ())
        self.define_table_calls: list[tuple[str, tuple, dict]] = []

    def define_table(self, name: str, *fields: object, **kwargs: object) -> None:
        self.define_table_calls.append((name, fields, kwargs))
        self.tables.add(name)


def test_define_local_vectorstore_tables_creates_both_when_absent() -> None:
    """Both tables are defined, migrate=False, when neither exists yet."""
    db = _RealishDB()
    define_local_vectorstore_tables(db)

    names = [call[0] for call in db.define_table_calls]
    assert names == ["local_vector_collections", "local_vector_points"]
    for _, fields, kwargs in db.define_table_calls:
        assert kwargs == {"migrate": False}
        assert len(fields) == 5


def test_define_local_vectorstore_tables_field_names_match_schema() -> None:
    """The collections table's Field objects carry the expected column names."""
    db = _RealishDB()
    define_local_vectorstore_tables(db)

    coll_fields = db.define_table_calls[0][1]
    assert [f.name for f in coll_fields] == [
        "name",
        "dimensions",
        "embedder_id",
        "distance",
        "created_at",
    ]

    point_fields = db.define_table_calls[1][1]
    assert [f.name for f in point_fields] == [
        "collection_id",
        "external_id",
        "vector_json",
        "payload_json",
        "created_at",
    ]


def test_define_local_vectorstore_tables_skips_when_both_already_present() -> None:
    """Already-defined tables are left alone -- no redefinition call at all."""
    db = _RealishDB(existing_tables={"local_vector_collections", "local_vector_points"})
    define_local_vectorstore_tables(db)
    assert db.define_table_calls == []


def test_define_local_vectorstore_tables_defines_only_the_missing_one() -> None:
    """Each table's presence is checked independently -- one existing, one missing."""
    db = _RealishDB(existing_tables={"local_vector_collections"})
    define_local_vectorstore_tables(db)
    names = [call[0] for call in db.define_table_calls]
    assert names == ["local_vector_points"]


# --- ensure_collection -------------------------------------------------------


async def test_ensure_collection_inserts_row_when_absent(backend: PgvectorVectorStore) -> None:
    """A brand-new collection name gets inserted with its spec fields."""
    await backend.ensure_collection(_SPEC)

    row = backend.db._tables["local_vector_collections"][0]
    assert row.name == _SPEC.name
    assert row.dimensions == _SPEC.dimensions
    assert row.embedder_id == _SPEC.embedder_id
    assert row.distance == "cosine"


async def test_ensure_collection_matching_spec_is_a_noop(backend: PgvectorVectorStore) -> None:
    """Reopening with the identical spec does not raise and does not duplicate the row."""
    await backend.ensure_collection(_SPEC)
    await backend.ensure_collection(_SPEC)

    rows = backend.db._tables["local_vector_collections"]
    assert len(rows) == 1


async def test_ensure_collection_dimension_mismatch_raises(backend: PgvectorVectorStore) -> None:
    """A different ``dimensions`` on the same name is refused with a descriptive message."""
    await backend.ensure_collection(_SPEC)
    mismatched = CollectionSpec(name=_SPEC.name, dimensions=99, embedder_id=_SPEC.embedder_id)

    with pytest.raises(VectorCollectionMismatchError, match="dimensions=99"):
        await backend.ensure_collection(mismatched)


async def test_ensure_collection_embedder_mismatch_raises(backend: PgvectorVectorStore) -> None:
    """A different ``embedder_id`` on the same name+dims is refused too."""
    await backend.ensure_collection(_SPEC)
    mismatched = CollectionSpec(
        name=_SPEC.name, dimensions=_SPEC.dimensions, embedder_id="other:embedder"
    )

    with pytest.raises(VectorCollectionMismatchError, match="other:embedder"):
        await backend.ensure_collection(mismatched)


# --- _get_collection_row (exercised via upsert/search/delete) --------------


async def test_upsert_unknown_collection_raises_value_error(backend: PgvectorVectorStore) -> None:
    """Upserting into a collection that was never created raises ValueError, not a KeyError."""
    with pytest.raises(ValueError, match="not found; call ensure_collection first"):
        await backend.upsert("ghost", [VectorPoint(id="p1", vector=[1.0], payload={})])


async def test_search_unknown_collection_raises_value_error(backend: PgvectorVectorStore) -> None:
    """Searching an unknown collection raises ValueError."""
    with pytest.raises(ValueError, match="not found"):
        await backend.search("ghost", query_vector=[1.0])


async def test_delete_unknown_collection_raises_value_error(backend: PgvectorVectorStore) -> None:
    """Deleting from an unknown collection raises ValueError."""
    with pytest.raises(ValueError, match="not found"):
        await backend.delete("ghost", ["p1"])


# --- upsert ------------------------------------------------------------------


async def test_upsert_wrong_dimension_point_raises_before_writing_any(
    backend: PgvectorVectorStore,
) -> None:
    """One bad-dimension point in a batch fails the whole batch before any row is written."""
    await backend.ensure_collection(_SPEC)
    points = [
        VectorPoint(id="ok", vector=[1.0, 0.0, 0.0], payload={}),
        VectorPoint(id="bad", vector=[1.0, 0.0], payload={}),
    ]

    with pytest.raises(VectorCollectionMismatchError, match="'bad'"):
        await backend.upsert(_SPEC.name, points)

    assert backend.db._tables.get("local_vector_points", []) == []


async def test_upsert_serializes_vector_and_payload_as_json(backend: PgvectorVectorStore) -> None:
    """Inserted points store the vector/payload as orjson-round-trippable text columns."""
    await backend.ensure_collection(_SPEC)
    await backend.upsert(
        _SPEC.name, [VectorPoint(id="p1", vector=[1.0, 2.0, 3.0], payload={"k": "v"})]
    )

    row = backend.db._tables["local_vector_points"][0]
    assert orjson.loads(row.vector_json) == [1.0, 2.0, 3.0]
    assert orjson.loads(row.payload_json) == {"k": "v"}
    assert row.external_id == "p1"


async def test_upsert_updates_existing_point_without_duplicating(
    backend: PgvectorVectorStore,
) -> None:
    """Re-upserting the same external id updates the row in place."""
    await backend.ensure_collection(_SPEC)
    await backend.upsert(_SPEC.name, [VectorPoint(id="p1", vector=[1.0, 0.0, 0.0], payload={})])
    await backend.upsert(_SPEC.name, [VectorPoint(id="p1", vector=[0.0, 1.0, 0.0], payload={})])

    rows = backend.db._tables["local_vector_points"]
    assert len(rows) == 1
    assert orjson.loads(rows[0].vector_json) == [0.0, 1.0, 0.0]


# --- search ------------------------------------------------------------------


async def test_search_wrong_dimension_query_raises(backend: PgvectorVectorStore) -> None:
    """A query vector of the wrong length is refused."""
    await backend.ensure_collection(_SPEC)
    with pytest.raises(VectorCollectionMismatchError, match="Query vector"):
        await backend.search(_SPEC.name, query_vector=[1.0, 0.0])


async def test_search_empty_collection_returns_empty_list(backend: PgvectorVectorStore) -> None:
    """A collection with no points searches to an empty result list, not an error."""
    await backend.ensure_collection(_SPEC)
    results = await backend.search(_SPEC.name, query_vector=[1.0, 0.0, 0.0])
    assert results == []


async def test_search_filters_excludes_non_matching_payload(backend: PgvectorVectorStore) -> None:
    """A filter that a point's payload doesn't satisfy excludes it from results."""
    await backend.ensure_collection(_SPEC)
    await backend.upsert(
        _SPEC.name,
        [
            VectorPoint(id="a", vector=[1.0, 0.0, 0.0], payload={"tenant": "acme"}),
            VectorPoint(id="b", vector=[1.0, 0.0, 0.0], payload={"tenant": "other"}),
        ],
    )

    results = await backend.search(
        _SPEC.name, query_vector=[1.0, 0.0, 0.0], filters={"tenant": "acme"}
    )

    assert [r.id for r in results] == ["a"]


async def test_search_filters_matching_payload_is_included(backend: PgvectorVectorStore) -> None:
    """A filter every point satisfies includes all of them."""
    await backend.ensure_collection(_SPEC)
    await backend.upsert(
        _SPEC.name,
        [VectorPoint(id="a", vector=[1.0, 0.0, 0.0], payload={"tenant": "acme", "kind": "doc"})],
    )

    results = await backend.search(
        _SPEC.name, query_vector=[1.0, 0.0, 0.0], filters={"tenant": "acme"}
    )

    assert [r.id for r in results] == ["a"]


async def test_search_min_score_excludes_weak_matches(backend: PgvectorVectorStore) -> None:
    """Points scoring below ``min_score`` are dropped from the results."""
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


async def test_search_top_k_truncates_results(backend: PgvectorVectorStore) -> None:
    """``top_k`` caps the number of results even when more points match."""
    await backend.ensure_collection(_SPEC)
    await backend.upsert(
        _SPEC.name,
        [
            VectorPoint(id="a", vector=[1.0, 0.0, 0.0], payload={}),
            VectorPoint(id="b", vector=[0.9, 0.1, 0.0], payload={}),
            VectorPoint(id="c", vector=[0.8, 0.2, 0.0], payload={}),
        ],
    )

    results = await backend.search(_SPEC.name, query_vector=[1.0, 0.0, 0.0], top_k=1)

    assert len(results) == 1
    assert results[0].id == "a"


# --- delete / delete_collection ----------------------------------------------


async def test_delete_removes_only_specified_ids(backend: PgvectorVectorStore) -> None:
    """Deleting a subset of ids leaves the rest of the collection untouched."""
    await backend.ensure_collection(_SPEC)
    await backend.upsert(
        _SPEC.name,
        [
            VectorPoint(id="keep", vector=[1.0, 0.0, 0.0], payload={}),
            VectorPoint(id="drop", vector=[1.0, 0.0, 0.0], payload={}),
        ],
    )

    await backend.delete(_SPEC.name, ["drop"])

    remaining = [row.external_id for row in backend.db._tables["local_vector_points"]]
    assert remaining == ["keep"]


async def test_delete_collection_removes_row_and_its_points(backend: PgvectorVectorStore) -> None:
    """Deleting a collection removes both its row and every point row referencing it."""
    await backend.ensure_collection(_SPEC)
    await backend.upsert(_SPEC.name, [VectorPoint(id="a", vector=[1.0, 0.0, 0.0], payload={})])

    await backend.delete_collection(_SPEC.name)

    assert backend.db._tables["local_vector_collections"] == []
    assert backend.db._tables["local_vector_points"] == []


async def test_delete_collection_noop_when_absent(backend: PgvectorVectorStore) -> None:
    """Deleting a collection that was never created is a no-op, not an error."""
    await backend.delete_collection("never-existed")  # must not raise
    assert backend.db._tables.get("local_vector_collections", []) == []


# --- health --------------------------------------------------------------------


async def test_health_reports_healthy_on_success(backend: PgvectorVectorStore) -> None:
    """A reachable backend reports healthy=True with an empty error detail."""
    health = await backend.health()
    assert health.healthy is True
    assert health.backend == "pgvector"
    assert health.detail == {}


class _IdField:
    """Stand-in for ``table.id`` that satisfies the ``> 0`` comparison in health()."""

    def __gt__(self, other: object) -> None:
        return None


class _ExplodingTable:
    """Stand-in for ``db.local_vector_collections`` exposing only ``.id``."""

    id = _IdField()


class _ExplodingDB:
    """A db stand-in whose query call always raises, exercising health()'s except branch."""

    local_vector_collections = _ExplodingTable()

    def __call__(self, query: object) -> None:
        raise RuntimeError("connection refused")


async def test_health_reports_unhealthy_with_error_detail_on_exception() -> None:
    """A DB error is caught and reported, never raised out of health()."""
    backend = PgvectorVectorStore(db=_ExplodingDB())

    health = await backend.health()

    assert health.healthy is False
    assert health.backend == "pgvector"
    assert health.detail == {"error": "connection refused"}
