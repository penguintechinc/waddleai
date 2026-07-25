"""Scope support on the metadata-only memory backends (ChromaDB, mem0 client).

These backends have no schema: metadata['scope'] IS the scope, and an absent
key means personal (covers all legacy entries with zero backfill). Reads use
a two-query merge (personal + org) ranked by relevance.

Includes a real-ChromaDB (ephemeral, fake encoder) visibility test — the
core isolation regression: personal memories are invisible to another user
in the same org; org memories are visible.
# regression: same-org personal-memory isolation under org scope feature
"""

from datetime import datetime
from typing import List
from unittest.mock import Mock, patch

import pytest

from shared.utils.memory_integration import ChromaDBMemoryStore, MemoryEntry


def _entry(user_id: int, content: str, scope_type: str = "user", entry_id: str = "") -> MemoryEntry:
    return MemoryEntry(
        id=entry_id or f"m-{user_id}-{content[:8]}",
        user_id=user_id,
        organization_id=3,
        session_id="",
        content=content,
        metadata={"role": "user"},
        embedding=None,
        created_at=datetime.utcnow(),
        scope_type=scope_type,
        author_user_id=user_id,
    )


class FakeEncoder:
    """Deterministic 'embeddings' — identical vectors so every stored doc
    is a perfect match and visibility is decided purely by the where filters."""

    def encode(self, text: str, convert_to_tensor: bool = False) -> List[float]:
        return [1.0, 0.0, 0.0]


@pytest.fixture
def chroma_store(tmp_path):
    with patch("shared.utils.memory_integration.SentenceTransformer"):
        store = ChromaDBMemoryStore(
            persist_directory=str(tmp_path / "chroma"),
            collection_name="scope_test",
        )
    store.encoder = FakeEncoder()
    return store


# --- ChromaDB: real store/search visibility --------------------------------


async def test_chroma_store_writes_scope_and_author_metadata(chroma_store):
    await chroma_store.initialize()
    assert await chroma_store.store_memory(_entry(5, "org runbook", scope_type="org"))
    got = chroma_store.collection.get(include=["metadatas"])
    meta = got["metadatas"][0]
    assert meta["scope"] == "org"
    assert meta["author_user_id"] == 5


async def test_chroma_personal_invisible_to_other_user_org_visible(chroma_store):
    """User 5 stores one personal + one org memory. User 6 (same org) must
    see the org memory and must NOT see the personal one."""
    await chroma_store.initialize()
    await chroma_store.store_memory(_entry(5, "my private note", scope_type="user"))
    await chroma_store.store_memory(_entry(5, "team deploy runbook", scope_type="org"))

    merged = await chroma_store.search_memories(
        "anything", user_id=6, organization_id=3, min_relevance=0.0, scope="all"
    )
    contents = [m.content for m in merged]
    assert "team deploy runbook" in contents
    assert "my private note" not in contents
    org_entry = next(m for m in merged if m.content == "team deploy runbook")
    assert org_entry.scope_type == "org"
    assert org_entry.author_user_id == 5


async def test_chroma_owner_merged_view_no_duplicates(chroma_store):
    """The author matches both the personal and org buckets — the org row
    must not appear twice in the merged view."""
    await chroma_store.initialize()
    await chroma_store.store_memory(_entry(5, "my private note", scope_type="user"))
    await chroma_store.store_memory(_entry(5, "team deploy runbook", scope_type="org"))

    merged = await chroma_store.search_memories(
        "anything", user_id=5, organization_id=3, min_relevance=0.0, scope="all"
    )
    contents = sorted(m.content for m in merged)
    assert contents == ["my private note", "team deploy runbook"]


async def test_chroma_scope_user_excludes_org_rows(chroma_store):
    await chroma_store.initialize()
    await chroma_store.store_memory(_entry(5, "my private note", scope_type="user"))
    await chroma_store.store_memory(_entry(5, "team deploy runbook", scope_type="org"))

    personal = await chroma_store.search_memories(
        "anything", user_id=5, organization_id=3, min_relevance=0.0, scope="user"
    )
    assert [m.content for m in personal] == ["my private note"]


async def test_chroma_legacy_entry_without_scope_key_is_personal(chroma_store):
    """Entries stored before this feature have no metadata['scope'] key —
    they must behave as personal."""
    await chroma_store.initialize()
    chroma_store.collection.add(
        ids=["legacy-1"],
        documents=["pre-feature memory"],
        metadatas=[
            {
                "user_id": 5,
                "organization_id": 3,
                "session_id": "",
                "created_at": datetime.utcnow().isoformat(),
            }
        ],
        embeddings=[[1.0, 0.0, 0.0]],
    )
    other = await chroma_store.search_memories("anything", user_id=6, organization_id=3, min_relevance=0.0, scope="all")
    assert "pre-feature memory" not in [m.content for m in other]

    owner = await chroma_store.search_memories("anything", user_id=5, organization_id=3, min_relevance=0.0, scope="all")
    got = next(m for m in owner if m.content == "pre-feature memory")
    assert got.scope_type == "user"


# --- Mem0MemoryStore: mocked client ----------------------------------------


def _mem0_store():
    from shared.utils.memory_integration import Mem0MemoryStore

    with patch("shared.utils.memory_integration.HAS_MEM0", True):
        store = Mem0MemoryStore.__new__(Mem0MemoryStore)
    store.api_key = None
    store.org_id = None
    store.config = {}
    store.client = Mock()
    return store


async def test_mem0_org_entry_stored_under_synthetic_org_user():
    store = _mem0_store()
    await store.store_memory(_entry(5, "team runbook", scope_type="org"))
    _, kwargs = store.client.add.call_args
    assert kwargs["user_id"] == "org-3"
    assert kwargs["metadata"]["scope"] == "org"
    assert kwargs["metadata"]["author_user_id"] == 5


async def test_mem0_personal_entry_stored_under_real_user():
    store = _mem0_store()
    await store.store_memory(_entry(5, "my note", scope_type="user"))
    _, kwargs = store.client.add.call_args
    assert kwargs["user_id"] == "5"
    assert kwargs["metadata"]["scope"] == "user"


async def test_mem0_search_all_queries_both_buckets_and_merges():
    store = _mem0_store()

    def fake_search(query, user_id, limit):
        if user_id == "5":
            return [
                {
                    "id": "p1",
                    "memory": "my note",
                    "score": 0.8,
                    "metadata": {
                        "organization_id": 3,
                        "session_id": "",
                        "created_at": datetime.utcnow().isoformat(),
                        "memory_id": "p1",
                        "scope": "user",
                        "author_user_id": 5,
                    },
                }
            ]
        if user_id == "org-3":
            return [
                {
                    "id": "o1",
                    "memory": "team runbook",
                    "score": 0.9,
                    "metadata": {
                        "organization_id": 3,
                        "session_id": "",
                        "created_at": datetime.utcnow().isoformat(),
                        "memory_id": "o1",
                        "scope": "org",
                        "author_user_id": 9,
                    },
                }
            ]
        return []

    store.client.search.side_effect = fake_search
    results = await store.search_memories("q", user_id=5, organization_id=3, min_relevance=0.0, scope="all")
    assert [m.content for m in results] == ["team runbook", "my note"]  # relevance-ranked
    assert store.client.search.call_count == 2
    assert results[0].scope_type == "org"
    assert results[0].author_user_id == 9


async def test_mem0_search_user_scope_single_query_excludes_org():
    store = _mem0_store()
    store.client.search.return_value = []
    await store.search_memories("q", user_id=5, organization_id=3, scope="user")
    assert store.client.search.call_count == 1
    _, kwargs = store.client.search.call_args
    assert kwargs["user_id"] == "5"
