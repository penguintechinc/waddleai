"""Scope support on the metadata-only memory backend (mem0 client).

This backend has no schema: metadata['scope'] IS the scope, and an absent
key means personal (covers all legacy entries with zero backfill). Reads use
a two-query merge (personal + org) ranked by relevance.

The ChromaDB metadata-only backend was removed (PYSEC-2026-311, pre-auth
code injection in chromadb's server component with no fixed release) --
its removal's fail-fast contract is covered by
test_chromadb_backend_removed_fails_fast in
tests/unit/test_memory_integration.py, rather than exercising a real
ChromaDB store here.
# regression: same-org personal-memory isolation under org scope feature
"""

from datetime import datetime
from unittest.mock import Mock, patch

from shared.utils.memory_integration import MemoryEntry


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
    """An org-scope entry is written to mem0 under the synthetic "org-{id}" user, not the author."""
    store = _mem0_store()
    await store.store_memory(_entry(5, "team runbook", scope_type="org"))
    _, kwargs = store.client.add.call_args
    assert kwargs["user_id"] == "org-3"
    assert kwargs["metadata"]["scope"] == "org"
    assert kwargs["metadata"]["author_user_id"] == 5


async def test_mem0_personal_entry_stored_under_real_user():
    """A user-scope entry is written to mem0 under the real numeric user id."""
    store = _mem0_store()
    await store.store_memory(_entry(5, "my note", scope_type="user"))
    _, kwargs = store.client.add.call_args
    assert kwargs["user_id"] == "5"
    assert kwargs["metadata"]["scope"] == "user"


async def test_mem0_search_all_queries_both_buckets_and_merges():
    """scope="all" queries both the personal and synthetic org buckets, merged by relevance."""
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
    results = await store.search_memories(
        "q", user_id=5, organization_id=3, min_relevance=0.0, scope="all"
    )
    assert [m.content for m in results] == ["team runbook", "my note"]  # relevance-ranked
    assert store.client.search.call_count == 2
    assert results[0].scope_type == "org"
    assert results[0].author_user_id == 9


async def test_mem0_search_user_scope_single_query_excludes_org():
    """scope="user" issues a single search against the real user id, never the org bucket."""
    store = _mem0_store()
    store.client.search.return_value = []
    await store.search_memories("q", user_id=5, organization_id=3, scope="user")
    assert store.client.search.call_count == 1
    _, kwargs = store.client.search.call_args
    assert kwargs["user_id"] == "5"
