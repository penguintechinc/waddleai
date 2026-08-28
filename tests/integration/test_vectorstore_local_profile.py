"""Integration tests for the local-only profile's vector-store seam.

Runs against live Qdrant/Ollama at QDRANT_BASE_URL/OLLAMA_BASE_URL (default:
http://localhost:6333 / http://localhost:11434 — see tests/integration/
conftest.py), skipped when either is unreachable. Mirrors
test_mem0_integration.py's / test_ollama_integration.py's established
skip-if-unavailable + uuid-suffixed-collection + always-cleanup pattern.

Every collection this file creates is prefixed ``_TEST_COLLECTION`` and
uuid-suffixed, and deleted in a ``finally`` block — this suite must never
read, write, or delete the pre-existing ``claude_memories``/
``mem0migrations`` collections a live Qdrant instance may already hold.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from shared.vectorstore.base import CollectionSpec, VectorCollectionMismatchError, VectorPoint
from shared.vectorstore.factory import LocalProfileConfig, create_vector_store_backend
from shared.vectorstore.pgvector_backend import PgvectorVectorStore
from shared.vectorstore.qdrant_backend import QdrantVectorStore
from tests.conformance._fake_dal import FakeDAL

pytestmark = pytest.mark.integration

_TEST_COLLECTION = "waddleai_local_profile_test"


def _skip_if_unavailable(available: bool, what: str) -> None:
    if not available:
        pytest.skip(f"{what} not running – see tests/integration/conftest.py")


def _unique_collection() -> str:
    return f"{_TEST_COLLECTION}_{uuid.uuid4().hex[:8]}"


async def _embed_via_ollama(ollama_base_url: str, text: str) -> list[float]:
    """Real nomic-embed-text embedding via the live Ollama instance (768-dim)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{ollama_base_url}/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": text},
        )
        response.raise_for_status()
        return response.json()["embedding"]


async def test_qdrant_vector_store_round_trip(qdrant_available: bool, qdrant_base_url: str) -> None:
    """ensure_collection -> upsert -> search -> delete -> delete_collection, for real."""
    _skip_if_unavailable(qdrant_available, "Qdrant")

    backend = QdrantVectorStore(url=qdrant_base_url)
    collection = _unique_collection()
    spec = CollectionSpec(name=collection, dimensions=4, embedder_id="test:integration")

    try:
        await backend.ensure_collection(spec)
        await backend.ensure_collection(spec)  # idempotent

        await backend.upsert(
            collection,
            [
                VectorPoint(id="a", vector=[1.0, 0.0, 0.0, 0.0], payload={"label": "a"}),
                VectorPoint(id="b", vector=[0.0, 1.0, 0.0, 0.0], payload={"label": "b"}),
            ],
        )

        results = await backend.search(collection, query_vector=[1.0, 0.0, 0.0, 0.0], top_k=1)
        assert len(results) == 1
        assert results[0].id == "a"
        assert results[0].payload["label"] == "a"

        await backend.delete(collection, ["a"])
        results_after_delete = await backend.search(
            collection, query_vector=[1.0, 0.0, 0.0, 0.0], top_k=10
        )
        assert [r.id for r in results_after_delete] == ["b"]
    finally:
        await backend.delete_collection(collection)


async def test_qdrant_vector_store_refuses_real_nomic_vs_minilm_dimension_mismatch(
    qdrant_available: bool, qdrant_base_url: str, ollama_available: bool, ollama_base_url: str
) -> None:
    """A real 768-dim nomic-embed-text vector refused against a 384-dim collection.

    The concrete trap: a real embedding from one path must never land in a
    collection opened for the other path's embedder -- refused, not
    accepted.
    """
    _skip_if_unavailable(qdrant_available, "Qdrant")
    _skip_if_unavailable(ollama_available, "Ollama")

    backend = QdrantVectorStore(url=qdrant_base_url)
    collection = _unique_collection()
    minilm_spec = CollectionSpec(
        name=collection,
        dimensions=384,
        embedder_id="sentence-transformers:all-MiniLM-L6-v2",
    )

    try:
        await backend.ensure_collection(minilm_spec)

        real_nomic_vector = await _embed_via_ollama(ollama_base_url, "local-only profile test")
        assert len(real_nomic_vector) == 768  # sanity: nomic-embed-text really is 768-dim

        with pytest.raises(VectorCollectionMismatchError):
            await backend.upsert(
                collection, [VectorPoint(id="mismatched", vector=real_nomic_vector, payload={})]
            )

        # Reopening the same collection under the nomic spec is refused too --
        # the collection is already committed to 384/MiniLM.
        nomic_spec = CollectionSpec(
            name=collection, dimensions=768, embedder_id="ollama:nomic-embed-text"
        )
        with pytest.raises(VectorCollectionMismatchError):
            await backend.ensure_collection(nomic_spec)
    finally:
        await backend.delete_collection(collection)


async def test_create_vector_store_backend_local_profile_succeeds_against_live_stack(
    qdrant_available: bool, qdrant_base_url: str, ollama_available: bool, ollama_base_url: str
) -> None:
    """The factory's reachability probes pass for real when both services are up."""
    _skip_if_unavailable(qdrant_available, "Qdrant")
    _skip_if_unavailable(ollama_available, "Ollama")

    backend = await create_vector_store_backend(
        db=FakeDAL(),
        feature_flag_enabled=True,
        config=LocalProfileConfig(qdrant_url=qdrant_base_url, ollama_host=ollama_base_url),
    )

    assert isinstance(backend, QdrantVectorStore)


async def test_create_vector_store_backend_flag_off_ignores_live_qdrant(
    qdrant_available: bool, qdrant_base_url: str
) -> None:
    """Flag off returns pgvector even when a real, reachable Qdrant sits right there.

    Belt-and-suspenders: a live Qdrant being incidentally reachable in this
    environment must never be enough on its own to activate the profile.
    """
    _skip_if_unavailable(qdrant_available, "Qdrant")

    backend = await create_vector_store_backend(
        db=FakeDAL(),
        feature_flag_enabled=False,
        config=LocalProfileConfig(qdrant_url=qdrant_base_url),
    )

    assert isinstance(backend, PgvectorVectorStore)
