"""Tests for the vector-store backend registry/factory (shared.vectorstore.registry)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from shared.vectorstore import registry as vs_registry
from shared.vectorstore.base import VectorStoreBackendType
from shared.vectorstore.pgvector_backend import PgvectorVectorStore
from shared.vectorstore.qdrant_backend import QdrantVectorStore
from tests.conformance._fake_dal import FakeDAL


@pytest.fixture(autouse=True)
def _clean_registry():
    """Snapshot/restore ``_REGISTRY`` so tests don't leak across each other."""
    original = dict(vs_registry._REGISTRY)
    yield
    vs_registry._REGISTRY.clear()
    vs_registry._REGISTRY.update(original)


def test_build_vector_store_returns_pgvector() -> None:
    """PGVECTOR resolves to PgvectorVectorStore, constructed with the given db."""
    backend = vs_registry.build_vector_store(VectorStoreBackendType.PGVECTOR, db=FakeDAL())
    assert isinstance(backend, PgvectorVectorStore)


def test_build_vector_store_returns_qdrant() -> None:
    """QDRANT resolves to QdrantVectorStore, constructed with the given url.

    Patches AsyncQdrantClient so this stays a hermetic unit test -- the real
    client's constructor performs a live compatibility-check network call,
    which belongs in tests/integration/test_vectorstore_local_profile.py,
    not here.
    """
    with patch("shared.vectorstore.qdrant_backend.AsyncQdrantClient"):
        backend = vs_registry.build_vector_store(
            VectorStoreBackendType.QDRANT, url="http://localhost:6333"
        )
    assert isinstance(backend, QdrantVectorStore)


def test_build_vector_store_unregistered_type_raises_value_error() -> None:
    """A valid type with no registered class raises ValueError, not a crash."""
    vs_registry._REGISTRY.pop(VectorStoreBackendType.QDRANT, None)

    with pytest.raises(ValueError, match="No vector store backend registered"):
        vs_registry.build_vector_store(VectorStoreBackendType.QDRANT, url="http://x:6333")


def test_register_decorator_populates_registry() -> None:
    """The @register decorator stores the class under its VectorStoreBackendType."""
    from shared.vectorstore.base import VectorStoreBackend

    @vs_registry.register(VectorStoreBackendType.PGVECTOR)
    class _Stub(VectorStoreBackend):
        backend_name = "stub"

        async def ensure_collection(self, spec):
            return None

        async def upsert(self, collection, points):
            return None

        async def search(self, collection, query_vector, top_k=10, min_score=0.0, filters=None):
            return []

        async def delete(self, collection, ids):
            return None

        async def delete_collection(self, collection):
            return None

        async def health(self):
            return None

    assert vs_registry._REGISTRY[VectorStoreBackendType.PGVECTOR] is _Stub


def test_ensure_imported_swallows_missing_optional_module(caplog) -> None:
    """A backend module that fails to import degrades gracefully, not a crash."""
    vs_registry._REGISTRY.pop(VectorStoreBackendType.QDRANT, None)
    original_module = vs_registry._MODULE_MAP[VectorStoreBackendType.QDRANT]
    vs_registry._MODULE_MAP[VectorStoreBackendType.QDRANT] = "shared.vectorstore._does_not_exist"
    try:
        with caplog.at_level("WARNING"):
            vs_registry._ensure_imported(VectorStoreBackendType.QDRANT)
        assert VectorStoreBackendType.QDRANT not in vs_registry._REGISTRY
    finally:
        vs_registry._MODULE_MAP[VectorStoreBackendType.QDRANT] = original_module
