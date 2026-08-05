"""
Integration tests for mem0/Qdrant memory services.

These tests run against a live Qdrant instance at QDRANT_BASE_URL
(default: http://localhost:6333). All tests are skipped when Qdrant
is not running.

The tests are idempotent: any collections or points created are cleaned
up at the end of each test.
"""

import uuid
from typing import Any, Dict, List

import httpx
import pytest

pytestmark = pytest.mark.integration

_TEST_COLLECTION = "waddleai_integration_test"


def _skip_if_unavailable(qdrant_available: bool) -> None:
    if not qdrant_available:
        pytest.skip("Qdrant not running – set QDRANT_BASE_URL or start Qdrant")


def _delete_collection_if_exists(base_url: str, name: str) -> None:
    """Best-effort cleanup of a test collection."""
    try:
        httpx.delete(f"{base_url}/collections/{name}", timeout=5.0)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Qdrant health & collections
# ---------------------------------------------------------------------------


def test_qdrant_health_check(
    qdrant_available: bool,
    qdrant_base_url: str,
) -> None:
    """GET /healthz should return 200 when Qdrant is running."""
    _skip_if_unavailable(qdrant_available)

    response = httpx.get(f"{qdrant_base_url}/healthz", timeout=5.0)
    assert response.status_code == 200


def test_qdrant_collections_list(
    qdrant_available: bool,
    qdrant_base_url: str,
) -> None:
    """GET /collections should return a collections payload."""
    _skip_if_unavailable(qdrant_available)

    response = httpx.get(f"{qdrant_base_url}/collections", timeout=5.0)
    assert response.status_code == 200
    data = response.json()
    assert "result" in data
    assert "collections" in data["result"]
    assert isinstance(data["result"]["collections"], list)


def test_qdrant_collection_create_and_delete(
    qdrant_available: bool,
    qdrant_base_url: str,
) -> None:
    """Create a test collection then delete it; both operations must succeed."""
    _skip_if_unavailable(qdrant_available)

    collection_name = f"{_TEST_COLLECTION}_{uuid.uuid4().hex[:8]}"
    try:
        # Create
        create_response = httpx.put(
            f"{qdrant_base_url}/collections/{collection_name}",
            json={"vectors": {"size": 4, "distance": "Cosine"}},
            timeout=10.0,
        )
        assert create_response.status_code in (200, 201)
        assert create_response.json()["result"] is True
    finally:
        # Always clean up
        _delete_collection_if_exists(qdrant_base_url, collection_name)


def test_qdrant_insert_point_and_search(
    qdrant_available: bool,
    qdrant_base_url: str,
) -> None:
    """Insert a vector point then perform a nearest-neighbour search."""
    _skip_if_unavailable(qdrant_available)

    collection_name = f"{_TEST_COLLECTION}_{uuid.uuid4().hex[:8]}"
    try:
        # Create a tiny 4-dim collection
        httpx.put(
            f"{qdrant_base_url}/collections/{collection_name}",
            json={"vectors": {"size": 4, "distance": "Cosine"}},
            timeout=10.0,
        )

        point_id = 1
        vector = [0.1, 0.2, 0.3, 0.4]

        # Upsert a point
        upsert_resp = httpx.put(
            f"{qdrant_base_url}/collections/{collection_name}/points",
            json={
                "points": [
                    {
                        "id": point_id,
                        "vector": vector,
                        "payload": {"text": "integration test memory"},
                    }
                ]
            },
            timeout=10.0,
        )
        assert upsert_resp.status_code == 200

        # Search
        search_resp = httpx.post(
            f"{qdrant_base_url}/collections/{collection_name}/points/search",
            json={"vector": vector, "limit": 1, "with_payload": True},
            timeout=10.0,
        )
        assert search_resp.status_code == 200
        results: List[Dict[str, Any]] = search_resp.json()["result"]
        assert len(results) == 1
        assert results[0]["id"] == point_id
        assert results[0]["payload"]["text"] == "integration test memory"

    finally:
        _delete_collection_if_exists(qdrant_base_url, collection_name)


def test_qdrant_retrieve_point_by_id(
    qdrant_available: bool,
    qdrant_base_url: str,
) -> None:
    """After upserting a point, retrieve it by ID and verify payload."""
    _skip_if_unavailable(qdrant_available)

    collection_name = f"{_TEST_COLLECTION}_{uuid.uuid4().hex[:8]}"
    try:
        httpx.put(
            f"{qdrant_base_url}/collections/{collection_name}",
            json={"vectors": {"size": 4, "distance": "Dot"}},
            timeout=10.0,
        )
        httpx.put(
            f"{qdrant_base_url}/collections/{collection_name}/points",
            json={"points": [{"id": 42, "vector": [1.0, 0.0, 0.0, 0.0], "payload": {"tag": "retrieve-test"}}]},
            timeout=10.0,
        )

        get_resp = httpx.post(
            f"{qdrant_base_url}/collections/{collection_name}/points",
            json={"ids": [42], "with_payload": True},
            timeout=10.0,
        )
        assert get_resp.status_code == 200
        points = get_resp.json()["result"]
        assert len(points) == 1
        assert points[0]["payload"]["tag"] == "retrieve-test"

    finally:
        _delete_collection_if_exists(qdrant_base_url, collection_name)


# ---------------------------------------------------------------------------
# WaddleAI memory_integration module
# ---------------------------------------------------------------------------


def test_memory_integration_module_importable() -> None:
    """shared.utils.memory_integration should import without errors."""
    from shared.utils.memory_integration import (  # type: ignore[import]
        ChromaDBMemoryStore,
        ConversationContext,
        MemoryEntry,
        MemoryStore,
    )

    assert MemoryEntry is not None
    assert MemoryStore is not None
    assert ChromaDBMemoryStore is not None
    assert ConversationContext is not None


def test_memory_entry_dataclass_construction() -> None:
    """MemoryEntry dataclass should be constructable with required fields."""
    from datetime import datetime

    from shared.utils.memory_integration import MemoryEntry  # type: ignore[import]

    entry = MemoryEntry(
        id="test-id-1",
        user_id=1,
        organization_id=1,
        session_id="sess-abc",
        content="The user prefers dark mode.",
        metadata={"source": "chat"},
        embedding=None,
        created_at=datetime.utcnow(),
    )

    assert entry.id == "test-id-1"
    assert entry.user_id == 1
    assert entry.content == "The user prefers dark mode."
    assert entry.relevance_score == 0.0  # default


def test_mem0_memory_store_import_and_has_mem0_flag() -> None:
    """Mem0MemoryStore should be importable and expose HAS_MEM0 flag."""
    from shared.utils.memory_integration import HAS_MEM0, Mem0MemoryStore  # type: ignore[import]

    assert isinstance(HAS_MEM0, bool)
    # The class itself is always importable; instantiation fails if not installed
    assert Mem0MemoryStore is not None


def test_mem0_memory_store_raises_when_package_missing() -> None:
    """Mem0MemoryStore.__init__ raises ImportError if mem0ai is not installed."""
    from shared.utils.memory_integration import HAS_MEM0, Mem0MemoryStore  # type: ignore[import]

    if HAS_MEM0:
        pytest.skip("mem0ai is installed – ImportError path not reachable")

    with pytest.raises(ImportError, match="mem0ai"):
        Mem0MemoryStore(api_key="test")


def test_qdrant_delete_nonexistent_collection_is_graceful(
    qdrant_available: bool,
    qdrant_base_url: str,
) -> None:
    """Deleting a collection that does not exist should not raise an exception."""
    _skip_if_unavailable(qdrant_available)

    fake_name = f"nonexistent_{uuid.uuid4().hex}"
    response = httpx.delete(
        f"{qdrant_base_url}/collections/{fake_name}",
        timeout=5.0,
    )
    # Qdrant returns 200 with result=false for unknown collections, or 404
    assert response.status_code in (200, 404)
