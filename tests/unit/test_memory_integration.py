"""Unit tests for shared.utils.memory_integration.

Covers the three MemoryStore backends (Mem0MemoryStore, ChromaDBMemoryStore,
PgvectorMemoryStore), the WaddleAIMemoryManager orchestration layer,
create_memory_manager's backend dispatch, and ReadReplicaPool. Scope-specific
(personal vs org) behaviour already has dedicated coverage in
tests/unit/test_memory_scope_pgvector.py and
tests/unit/test_memory_scope_metadata_backends.py -- this file focuses on
init/lazy-init, error handling, boundary filters, and the manager/factory
layers that those files don't touch.

All backends are exercised through small hand-written fakes (FakeDB,
FakeEmbedder, FakeMem0Client, FakeChromaCollection, FakeChromaClient,
FakeMemoryStore) rather than spec-less Mock(), and no test ever calls a real
mem0/ChromaDB/Postgres backend or downloads a model.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from shared.utils.memory_integration import (
    ChromaDBMemoryStore,
    ConversationContext,
    Mem0MemoryStore,
    MemoryEntry,
    MemoryStore,
    PgvectorMemoryStore,
    ReadReplicaPool,
    WaddleAIMemoryManager,
    create_memory_manager,
)


def _memory_entry(
    *,
    entry_id: str = "e1",
    user_id: int = 5,
    organization_id: int = 3,
    session_id: str | None = "s1",
    content: str = "remember this",
    metadata: dict | None = None,
    created_at: datetime | None = None,
    relevance_score: float = 0.0,
    scope_type: str = "user",
    author_user_id: int = 0,
) -> MemoryEntry:
    """Build a MemoryEntry with sensible defaults, overridable per field."""
    return MemoryEntry(
        id=entry_id,
        user_id=user_id,
        organization_id=organization_id,
        session_id=session_id,
        content=content,
        metadata=metadata if metadata is not None else {},
        embedding=None,
        created_at=created_at or datetime.utcnow(),
        relevance_score=relevance_score,
        scope_type=scope_type,
        author_user_id=author_user_id,
    )


class TestMemoryEntryAndConversationContext:
    """Dataclass defaults for the two plain data structures."""

    def test_memory_entry_defaults_to_user_scope_and_zero_author(self):
        """A MemoryEntry built without scope_type/author_user_id defaults to personal scope."""
        entry = _memory_entry()
        assert entry.scope_type == "user"
        assert entry.author_user_id == 0

    def test_conversation_context_holds_all_fields(self):
        """ConversationContext stores every constructor argument unchanged."""
        ctx = ConversationContext(
            user_id=1,
            organization_id=2,
            session_id="s1",
            recent_messages=[{"role": "user", "content": "hi"}],
            relevant_memories=[_memory_entry()],
            conversation_summary="a summary",
        )
        assert ctx.user_id == 1
        assert ctx.conversation_summary == "a summary"
        assert len(ctx.relevant_memories) == 1


# ---------------------------------------------------------------------------
# Mem0MemoryStore
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FakeMem0Client:
    """Records mem0 client calls and returns configurable results/exceptions."""

    add_calls: list = field(default_factory=list)
    search_results: dict = field(default_factory=dict)
    search_calls: list = field(default_factory=list)
    get_all_result: list = field(default_factory=list)
    delete_calls: list = field(default_factory=list)
    add_raises: Exception | None = None
    search_raises: Exception | None = None
    get_all_raises: Exception | None = None
    delete_raises: Exception | None = None

    def add(self, content: str, user_id: str, metadata: dict) -> None:
        """Record the call args, or raise the configured exception."""
        if self.add_raises:
            raise self.add_raises
        self.add_calls.append((content, user_id, metadata))

    def search(self, query: str, user_id: str, limit: int) -> list:
        """Return the queued results for user_id, or raise the configured exception."""
        if self.search_raises:
            raise self.search_raises
        self.search_calls.append((query, user_id, limit))
        return self.search_results.get(user_id, [])

    def get_all(self, user_id: str) -> list:
        """Return the queued get_all result, or raise the configured exception."""
        if self.get_all_raises:
            raise self.get_all_raises
        return self.get_all_result

    def delete(self, memory_id: str) -> None:
        """Record the deleted id, or raise the configured exception."""
        if self.delete_raises:
            raise self.delete_raises
        self.delete_calls.append(memory_id)


def _mem0_store(client: FakeMem0Client | None = None) -> Mem0MemoryStore:
    """Build a Mem0MemoryStore bypassing __init__ (works regardless of mem0ai install state)."""
    with patch("shared.utils.memory_integration.HAS_MEM0", True):
        store = Mem0MemoryStore.__new__(Mem0MemoryStore)
    store.api_key = None
    store.org_id = None
    store.config = {}
    store.client = client if client is not None else FakeMem0Client()
    return store


def _mem0_result(
    memory_id: str,
    content: str,
    score: float,
    organization_id: int = 3,
    session_id: str = "",
    scope: str = "user",
    author_user_id: int = 5,
    user_id: int = 5,
) -> dict:
    return {
        "id": memory_id,
        "memory": content,
        "score": score,
        "metadata": {
            "organization_id": organization_id,
            "session_id": session_id,
            "created_at": datetime.utcnow().isoformat(),
            "memory_id": memory_id,
            "scope": scope,
            "author_user_id": author_user_id,
            "user_id": user_id,
        },
    }


class TestMem0MemoryStoreInit:
    """Constructor and initialize() behaviour."""

    def test_init_raises_without_mem0_installed(self):
        """Constructing Mem0MemoryStore when mem0ai isn't importable raises ImportError."""
        with patch("shared.utils.memory_integration.HAS_MEM0", False):
            with pytest.raises(ImportError, match="mem0ai package not installed"):
                Mem0MemoryStore()

    async def test_initialize_passes_api_key_and_org_id_to_client(self):
        """initialize() forwards api_key/org_id into the mem0 client constructor."""
        captured = {}

        class FakeMemoryClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        store = Mem0MemoryStore.__new__(Mem0MemoryStore)
        store.api_key = "key-123"
        store.org_id = "org-9"
        store.config = {}
        store.client = None

        with patch("shared.utils.memory_integration.MemoryClient", FakeMemoryClient):
            await store.initialize()

        assert captured == {"api_key": "key-123", "org_id": "org-9"}
        assert isinstance(store.client, FakeMemoryClient)

    async def test_initialize_omits_absent_credentials(self):
        """initialize() passes no kwargs when api_key/org_id are both unset."""

        class FakeMemoryClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        store = Mem0MemoryStore.__new__(Mem0MemoryStore)
        store.api_key = None
        store.org_id = None
        store.config = {}
        store.client = None

        with patch("shared.utils.memory_integration.MemoryClient", FakeMemoryClient):
            await store.initialize()

        assert store.client.kwargs == {}

    async def test_initialize_failure_propagates(self):
        """A client construction failure is logged and re-raised, not swallowed."""

        def boom(**kwargs):
            raise RuntimeError("connect failed")

        store = Mem0MemoryStore.__new__(Mem0MemoryStore)
        store.api_key = None
        store.org_id = None
        store.config = {}
        store.client = None

        with patch("shared.utils.memory_integration.MemoryClient", boom):
            with pytest.raises(RuntimeError, match="connect failed"):
                await store.initialize()


class TestMem0MemoryStoreStoreMemory:
    """store_memory: lazy init and error handling."""

    async def test_lazy_initializes_when_client_missing(self):
        """store_memory() calls initialize() first when self.client is still None."""
        store = _mem0_store()
        store.client = None
        called = {"initialize": False}

        async def fake_initialize():
            called["initialize"] = True
            store.client = FakeMem0Client()

        store.initialize = fake_initialize
        ok = await store.store_memory(_memory_entry())

        assert ok is True
        assert called["initialize"] is True

    async def test_exception_returns_false(self):
        """A client.add() failure is caught and reported as a failed store, not raised."""
        client = FakeMem0Client(add_raises=RuntimeError("boom"))
        store = _mem0_store(client)
        ok = await store.store_memory(_memory_entry())
        assert ok is False


class TestMem0MemoryStoreSearchMemories:
    """search_memories: per-result filtering and error handling."""

    async def test_filters_wrong_org_wrong_session_and_low_relevance(self):
        """Only the result matching org, session, and min_relevance survives."""
        client = FakeMem0Client(
            search_results={
                "5": [
                    _mem0_result(
                        "m1", "right org+session", 0.9, organization_id=3, session_id="s1"
                    ),
                    _mem0_result("m2", "wrong org", 0.9, organization_id=999, session_id="s1"),
                    _mem0_result("m3", "wrong session", 0.9, organization_id=3, session_id="other"),
                    _mem0_result(
                        "m4", "too low relevance", 0.1, organization_id=3, session_id="s1"
                    ),
                ]
            }
        )
        store = _mem0_store(client)
        results = await store.search_memories(
            "q", user_id=5, organization_id=3, session_id="s1", min_relevance=0.5, scope="user"
        )
        assert [m.id for m in results] == ["m1"]

    async def test_exception_returns_empty_list(self):
        """A client.search() failure is caught and returns [] rather than raising."""
        client = FakeMem0Client(search_raises=RuntimeError("boom"))
        store = _mem0_store(client)
        results = await store.search_memories("q", user_id=5, organization_id=3)
        assert results == []

    async def test_org_scope_alone_queries_only_the_synthetic_org_bucket(self):
        """scope='org' (no 'all') skips the personal-bucket query entirely."""
        client = FakeMem0Client(
            search_results={
                "org-3": [_mem0_result("o1", "team note", 0.9, organization_id=3, scope="org")]
            }
        )
        store = _mem0_store(client)
        results = await store.search_memories("q", user_id=5, organization_id=3, scope="org")
        assert [m.id for m in results] == ["o1"]
        assert client.search_calls == [("q", "org-3", 10)]


class TestMem0MemoryStoreGetRecentMemories:
    """get_recent_memories: time-window/org/session filtering, limit, and error handling."""

    async def test_filters_by_org_session_and_time_window(self):
        """Only the row matching org, session, and the recency cutoff is returned."""
        now = datetime.utcnow()
        old = now - timedelta(hours=48)
        client = FakeMem0Client(
            get_all_result=[
                {
                    "metadata": {
                        "organization_id": 3,
                        "session_id": "s1",
                        "created_at": now.isoformat(),
                        "memory_id": "m1",
                    },
                    "memory": "recent right",
                },
                {
                    "metadata": {
                        "organization_id": 999,
                        "session_id": "s1",
                        "created_at": now.isoformat(),
                        "memory_id": "m2",
                    },
                    "memory": "wrong org",
                },
                {
                    "metadata": {
                        "organization_id": 3,
                        "session_id": "other",
                        "created_at": now.isoformat(),
                        "memory_id": "m3",
                    },
                    "memory": "wrong session",
                },
                {
                    "metadata": {
                        "organization_id": 3,
                        "session_id": "s1",
                        "created_at": old.isoformat(),
                        "memory_id": "m4",
                    },
                    "memory": "too old",
                },
            ]
        )
        store = _mem0_store(client)
        results = await store.get_recent_memories(
            user_id=5, organization_id=3, session_id="s1", hours=24, limit=10
        )
        assert [m.content for m in results] == ["recent right"]

    async def test_respects_limit(self):
        """get_recent_memories stops appending once limit is reached."""
        now = datetime.utcnow()
        client = FakeMem0Client(
            get_all_result=[
                {
                    "metadata": {
                        "organization_id": 3,
                        "session_id": "",
                        "created_at": now.isoformat(),
                        "memory_id": f"m{i}",
                    },
                    "memory": f"memory {i}",
                }
                for i in range(5)
            ]
        )
        store = _mem0_store(client)
        results = await store.get_recent_memories(user_id=5, organization_id=3, limit=2)
        assert len(results) == 2

    async def test_exception_returns_empty_list(self):
        """A client.get_all() failure is caught and returns [] rather than raising."""
        client = FakeMem0Client(get_all_raises=RuntimeError("boom"))
        store = _mem0_store(client)
        results = await store.get_recent_memories(user_id=5, organization_id=3)
        assert results == []


class TestMem0MemoryStoreDeleteAndCleanup:
    """delete_memory and cleanup_old_memories."""

    async def test_delete_memory_success(self):
        """delete_memory forwards the id to client.delete and returns True."""
        client = FakeMem0Client()
        store = _mem0_store(client)
        ok = await store.delete_memory("m1")
        assert ok is True
        assert client.delete_calls == ["m1"]

    async def test_delete_memory_exception_returns_false(self):
        """A client.delete() failure is caught and returns False rather than raising."""
        client = FakeMem0Client(delete_raises=RuntimeError("boom"))
        store = _mem0_store(client)
        ok = await store.delete_memory("m1")
        assert ok is False

    async def test_cleanup_old_memories_is_a_no_op_placeholder(self):
        """cleanup_old_memories always returns 0 -- mem0 has no bulk-delete-by-date API."""
        store = _mem0_store()
        count = await store.cleanup_old_memories(days=90)
        assert count == 0

    async def test_cleanup_old_memories_exception_still_returns_zero(self):
        """Even if the lazy initialize() call fails, cleanup_old_memories returns 0."""
        store = _mem0_store()
        store.client = None

        async def raising_initialize():
            raise RuntimeError("boom")

        store.initialize = raising_initialize
        count = await store.cleanup_old_memories(days=90)
        assert count == 0


class TestMem0MemoryStoreLazyInitOnReadAndDelete:
    """search/get_recent/delete also lazy-initialize the client, not just store_memory."""

    async def test_search_memories_lazy_initializes(self):
        """search_memories() calls initialize() first when self.client is still None."""
        store = _mem0_store()
        store.client = None
        called = {"initialize": False}

        async def fake_initialize():
            called["initialize"] = True
            store.client = FakeMem0Client()

        store.initialize = fake_initialize
        results = await store.search_memories("q", user_id=5, organization_id=3)
        assert called["initialize"] is True
        assert results == []

    async def test_get_recent_memories_lazy_initializes(self):
        """get_recent_memories() calls initialize() first when self.client is still None."""
        store = _mem0_store()
        store.client = None
        called = {"initialize": False}

        async def fake_initialize():
            called["initialize"] = True
            store.client = FakeMem0Client()

        store.initialize = fake_initialize
        results = await store.get_recent_memories(user_id=5, organization_id=3)
        assert called["initialize"] is True
        assert results == []

    async def test_delete_memory_lazy_initializes(self):
        """delete_memory() calls initialize() first when self.client is still None."""
        store = _mem0_store()
        store.client = None
        called = {"initialize": False}

        async def fake_initialize():
            called["initialize"] = True
            store.client = FakeMem0Client()

        store.initialize = fake_initialize
        ok = await store.delete_memory("m1")
        assert called["initialize"] is True
        assert ok is True


# ---------------------------------------------------------------------------
# ChromaDBMemoryStore
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FakeChromaCollection:
    """Stand-in for a chromadb Collection: records calls, returns queued results."""

    add_calls: list = field(default_factory=list)
    query_result: dict | None = None
    get_result: dict | None = None
    delete_calls: list = field(default_factory=list)
    add_raises: Exception | None = None
    query_raises: Exception | None = None
    get_raises: Exception | None = None
    delete_raises: Exception | None = None
    last_query_kwargs: dict = field(default_factory=dict)
    last_get_kwargs: dict = field(default_factory=dict)

    def add(self, ids, documents, metadatas, embeddings=None) -> None:
        """Record the call args, or raise the configured exception."""
        if self.add_raises:
            raise self.add_raises
        self.add_calls.append(
            {"ids": ids, "documents": documents, "metadatas": metadatas, "embeddings": embeddings}
        )

    def query(self, **kwargs) -> dict:
        """Return the queued query result, or raise the configured exception."""
        if self.query_raises:
            raise self.query_raises
        self.last_query_kwargs = kwargs
        if self.query_result is not None:
            return self.query_result
        return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

    def get(self, **kwargs) -> dict:
        """Return the queued get result, or raise the configured exception."""
        if self.get_raises:
            raise self.get_raises
        self.last_get_kwargs = kwargs
        if self.get_result is not None:
            return self.get_result
        return {"ids": [], "documents": [], "metadatas": []}

    def delete(self, ids) -> None:
        """Record the deleted ids, or raise the configured exception."""
        if self.delete_raises:
            raise self.delete_raises
        self.delete_calls.append(ids)


@dataclass(slots=True)
class FakeChromaClient:
    """Stand-in for a chromadb PersistentClient: get/create collection dispatch."""

    existing_collection: Any = None
    created_collection: Any = None
    get_raises: bool = True

    def get_collection(self, name: str):
        """Return the pre-loaded collection, or raise when none/get_raises is set."""
        if self.get_raises or self.existing_collection is None:
            raise RuntimeError("collection not found")
        return self.existing_collection

    def create_collection(self, name: str, metadata: dict | None = None):
        """Return the collection to hand back for a freshly created one."""
        return self.created_collection


def _chroma_store(
    collection: FakeChromaCollection | None = None, encoder: Any = None
) -> ChromaDBMemoryStore:
    """Build a ChromaDBMemoryStore with a fake collection and no real model load."""
    with patch("shared.utils.memory_integration._sentence_transformer"):
        store = ChromaDBMemoryStore(persist_directory="/unused", collection_name="test")
    store.encoder = encoder
    store.collection = collection if collection is not None else FakeChromaCollection()
    return store


class TestChromaDBMemoryStoreInit:
    """Encoder construction and initialize()."""

    def test_encoder_init_failure_sets_encoder_none(self):
        """A broken sentence-transformer load leaves encoder=None instead of raising."""
        with patch(
            "shared.utils.memory_integration._sentence_transformer",
            side_effect=RuntimeError("no gpu"),
        ):
            store = ChromaDBMemoryStore(persist_directory="/unused")
        assert store.encoder is None

    async def test_initialize_loads_existing_collection(self):
        """initialize() reuses an existing collection via get_collection when present."""
        collection = FakeChromaCollection()
        fake_client = FakeChromaClient(existing_collection=collection, get_raises=False)
        with patch("shared.utils.memory_integration._sentence_transformer"):
            store = ChromaDBMemoryStore(persist_directory="/unused")
        with patch(
            "shared.utils.memory_integration.chromadb.PersistentClient", return_value=fake_client
        ):
            await store.initialize()
        assert store.collection is collection

    async def test_initialize_creates_collection_when_missing(self):
        """initialize() falls back to create_collection when get_collection raises."""
        collection = FakeChromaCollection()
        fake_client = FakeChromaClient(created_collection=collection)
        with patch("shared.utils.memory_integration._sentence_transformer"):
            store = ChromaDBMemoryStore(persist_directory="/unused")
        with patch(
            "shared.utils.memory_integration.chromadb.PersistentClient", return_value=fake_client
        ):
            await store.initialize()
        assert store.collection is collection

    async def test_initialize_failure_propagates(self):
        """A PersistentClient construction failure is logged and re-raised."""
        with patch("shared.utils.memory_integration._sentence_transformer"):
            store = ChromaDBMemoryStore(persist_directory="/unused")
        with patch(
            "shared.utils.memory_integration.chromadb.PersistentClient",
            side_effect=RuntimeError("disk full"),
        ):
            with pytest.raises(RuntimeError, match="disk full"):
                await store.initialize()


class TestChromaDBMemoryStoreGenerateEmbedding:
    """_generate_embedding boundary/error branches."""

    def test_returns_none_without_encoder(self):
        """No encoder configured means embeddings are never generated."""
        store = _chroma_store(encoder=None)
        assert store._generate_embedding("hello") is None

    def test_exception_returns_none(self):
        """An encoder that raises is caught and treated as 'no embedding available'."""

        class BrokenEncoder:
            def encode(self, text, convert_to_tensor=False):
                raise RuntimeError("model crashed")

        store = _chroma_store(encoder=BrokenEncoder())
        assert store._generate_embedding("hello") is None

    def test_converts_tensor_like_result_via_tolist(self):
        """A tensor-like encode() result is converted via .tolist()."""

        class TensorLike:
            def tolist(self):
                return [0.5, 0.6]

        class FakeEncoder:
            def encode(self, text, convert_to_tensor=False):
                return TensorLike()

        store = _chroma_store(encoder=FakeEncoder())
        assert store._generate_embedding("hello") == [0.5, 0.6]


class TestChromaDBMemoryStoreStoreMemory:
    """store_memory error handling."""

    async def test_exception_returns_false(self):
        """A collection.add() failure is caught and reported as a failed store."""
        collection = FakeChromaCollection(add_raises=RuntimeError("disk full"))
        store = _chroma_store(collection=collection, encoder=None)
        ok = await store.store_memory(_memory_entry())
        assert ok is False

    async def test_preexisting_embedding_is_not_regenerated(self):
        """An entry that already carries an embedding skips _generate_embedding entirely."""
        collection = FakeChromaCollection()

        class ExplodingEncoder:
            def encode(self, text, convert_to_tensor=False):
                raise AssertionError("should not be called when embedding is pre-populated")

        store = _chroma_store(collection=collection, encoder=ExplodingEncoder())
        entry = _memory_entry()
        entry.embedding = [0.9, 0.8, 0.7]
        ok = await store.store_memory(entry)
        assert ok is True
        assert collection.add_calls[0]["embeddings"] == [[0.9, 0.8, 0.7]]


class TestChromaDBMemoryStoreSearchMemories:
    """search_memories boundary/error branches not covered by the scope-isolation suite."""

    async def test_uses_query_texts_when_no_embedding_available(self):
        """No encoder means query_embedding is None, so search falls back to query_texts."""
        collection = FakeChromaCollection(
            query_result={
                "ids": [["m1"]],
                "documents": [["hello world"]],
                "metadatas": [
                    [
                        {
                            "user_id": 5,
                            "organization_id": 3,
                            "session_id": "s1",
                            "created_at": datetime.utcnow().isoformat(),
                            "scope": "user",
                            "author_user_id": 5,
                        }
                    ]
                ],
                "distances": [[0.1]],
            }
        )
        store = _chroma_store(collection=collection, encoder=None)
        entries = await store.search_memories(
            "hello", user_id=5, organization_id=3, min_relevance=0.5, scope="user"
        )
        assert "query_texts" in collection.last_query_kwargs
        assert "query_embeddings" not in collection.last_query_kwargs
        assert entries[0].content == "hello world"

    async def test_session_id_filter_uses_and_operator_for_multi_key_where(self):
        """A session_id filter combined with user_id/org_id wraps the where clause in $and."""
        collection = FakeChromaCollection(
            query_result={
                "ids": [["m1"]],
                "documents": [["x"]],
                "metadatas": [
                    [
                        {
                            "user_id": 5,
                            "organization_id": 3,
                            "session_id": "s9",
                            "created_at": datetime.utcnow().isoformat(),
                            "scope": "user",
                            "author_user_id": 5,
                        }
                    ]
                ],
                "distances": [[0.1]],
            }
        )
        store = _chroma_store(collection=collection, encoder=None)
        await store.search_memories(
            "q", user_id=5, organization_id=3, session_id="s9", min_relevance=0.0
        )
        where = collection.last_query_kwargs["where"]
        assert where == {"$and": [{"user_id": 5}, {"organization_id": 3}, {"session_id": "s9"}]}

    async def test_empty_results_returns_empty_list(self):
        """An empty documents batch converts to an empty entry list."""
        collection = FakeChromaCollection(
            query_result={"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        )
        store = _chroma_store(collection=collection, encoder=None)
        entries = await store.search_memories("q", user_id=5, organization_id=3)
        assert entries == []

    async def test_missing_documents_key_short_circuits(self):
        """A falsy 'documents' value short-circuits _to_entries before any indexing."""
        collection = FakeChromaCollection(query_result={"documents": None})
        store = _chroma_store(collection=collection, encoder=None)
        entries = await store.search_memories("q", user_id=5, organization_id=3)
        assert entries == []

    async def test_exception_returns_empty_list(self):
        """A collection.query() failure is caught and returns [] rather than raising."""
        collection = FakeChromaCollection(query_raises=RuntimeError("boom"))
        store = _chroma_store(collection=collection, encoder=None)
        entries = await store.search_memories("q", user_id=5, organization_id=3)
        assert entries == []

    async def test_low_relevance_result_is_filtered_out(self):
        """A result below min_relevance is excluded from the converted entries."""
        collection = FakeChromaCollection(
            query_result={
                "ids": [["m1"]],
                "documents": [["low match"]],
                "metadatas": [
                    [
                        {
                            "user_id": 5,
                            "organization_id": 3,
                            "session_id": "s1",
                            "created_at": datetime.utcnow().isoformat(),
                            "scope": "user",
                            "author_user_id": 5,
                        }
                    ]
                ],
                "distances": [[0.9]],  # relevance_score = 1 - 0.9 = 0.1
            }
        )
        store = _chroma_store(collection=collection, encoder=None)
        entries = await store.search_memories("q", user_id=5, organization_id=3, min_relevance=0.7)
        assert entries == []


class TestChromaDBMemoryStoreLazyInitOnReadAndWrite:
    """store/search/get_recent/delete/cleanup all lazy-initialize the collection."""

    async def test_store_memory_lazy_initializes(self):
        """store_memory() calls initialize() first when self.collection is still None."""
        store = _chroma_store(encoder=None)
        store.collection = None
        called = {"initialize": False}

        async def fake_initialize():
            called["initialize"] = True
            store.collection = FakeChromaCollection()

        store.initialize = fake_initialize
        ok = await store.store_memory(_memory_entry())
        assert called["initialize"] is True
        assert ok is True

    async def test_search_memories_lazy_initializes(self):
        """search_memories() calls initialize() first when self.collection is still None."""
        store = _chroma_store(encoder=None)
        store.collection = None
        called = {"initialize": False}

        async def fake_initialize():
            called["initialize"] = True
            store.collection = FakeChromaCollection()

        store.initialize = fake_initialize
        entries = await store.search_memories("q", user_id=5, organization_id=3)
        assert called["initialize"] is True
        assert entries == []

    async def test_get_recent_memories_lazy_initializes(self):
        """get_recent_memories() calls initialize() first when self.collection is still None."""
        store = _chroma_store(encoder=None)
        store.collection = None
        called = {"initialize": False}

        async def fake_initialize():
            called["initialize"] = True
            store.collection = FakeChromaCollection()

        store.initialize = fake_initialize
        entries = await store.get_recent_memories(user_id=5, organization_id=3)
        assert called["initialize"] is True
        assert entries == []

    async def test_delete_memory_lazy_initializes(self):
        """delete_memory() calls initialize() first when self.collection is still None."""
        store = _chroma_store(encoder=None)
        store.collection = None
        called = {"initialize": False}

        async def fake_initialize():
            called["initialize"] = True
            store.collection = FakeChromaCollection()

        store.initialize = fake_initialize
        ok = await store.delete_memory("m1")
        assert called["initialize"] is True
        assert ok is True

    async def test_cleanup_old_memories_lazy_initializes(self):
        """cleanup_old_memories() calls initialize() first when self.collection is still None."""
        store = _chroma_store(encoder=None)
        store.collection = None
        called = {"initialize": False}

        async def fake_initialize():
            called["initialize"] = True
            store.collection = FakeChromaCollection()

        store.initialize = fake_initialize
        count = await store.cleanup_old_memories(days=90)
        assert called["initialize"] is True
        assert count == 0


class TestChromaDBMemoryStoreGetRecentMemories:
    """get_recent_memories time-window filtering and error handling."""

    async def test_filters_by_time_window_and_uses_session_filter(self):
        """Only the row inside the recency cutoff is returned; session_id reaches the where."""
        now = datetime.utcnow()
        old = now - timedelta(hours=48)
        collection = FakeChromaCollection(
            get_result={
                "ids": ["m1", "m2"],
                "documents": ["recent", "old"],
                "metadatas": [
                    {
                        "user_id": 5,
                        "organization_id": 3,
                        "session_id": "s1",
                        "created_at": now.isoformat(),
                    },
                    {
                        "user_id": 5,
                        "organization_id": 3,
                        "session_id": "s1",
                        "created_at": old.isoformat(),
                    },
                ],
            }
        )
        store = _chroma_store(collection=collection, encoder=None)
        entries = await store.get_recent_memories(
            user_id=5, organization_id=3, session_id="s1", hours=24
        )
        assert [e.content for e in entries] == ["recent"]
        assert collection.last_get_kwargs["where"]["session_id"] == "s1"

    async def test_no_results_returns_empty_list(self):
        """An empty get() result converts to an empty entry list."""
        collection = FakeChromaCollection(get_result={"ids": [], "documents": [], "metadatas": []})
        store = _chroma_store(collection=collection, encoder=None)
        entries = await store.get_recent_memories(user_id=5, organization_id=3)
        assert entries == []

    async def test_exception_returns_empty_list(self):
        """A collection.get() failure is caught and returns [] rather than raising."""
        collection = FakeChromaCollection(get_raises=RuntimeError("boom"))
        store = _chroma_store(collection=collection, encoder=None)
        entries = await store.get_recent_memories(user_id=5, organization_id=3)
        assert entries == []


class TestChromaDBMemoryStoreDeleteAndCleanup:
    """delete_memory and cleanup_old_memories."""

    async def test_delete_memory_success(self):
        """delete_memory forwards the id to collection.delete and returns True."""
        collection = FakeChromaCollection()
        store = _chroma_store(collection=collection, encoder=None)
        ok = await store.delete_memory("m1")
        assert ok is True
        assert collection.delete_calls == [["m1"]]

    async def test_delete_memory_exception_returns_false(self):
        """A collection.delete() failure is caught and returns False rather than raising."""
        collection = FakeChromaCollection(delete_raises=RuntimeError("boom"))
        store = _chroma_store(collection=collection, encoder=None)
        ok = await store.delete_memory("m1")
        assert ok is False

    async def test_cleanup_deletes_old_entries_and_returns_count(self):
        """Entries older than the cutoff are deleted; the returned count matches."""
        now = datetime.utcnow()
        old = now - timedelta(days=100)
        collection = FakeChromaCollection(
            get_result={
                "ids": ["m1", "m2"],
                "metadatas": [{"created_at": old.isoformat()}, {"created_at": now.isoformat()}],
            }
        )
        store = _chroma_store(collection=collection, encoder=None)
        count = await store.cleanup_old_memories(days=90)
        assert count == 1
        assert collection.delete_calls == [["m1"]]

    async def test_cleanup_with_no_old_entries_skips_delete_call(self):
        """When nothing is older than the cutoff, delete() is never called."""
        now = datetime.utcnow()
        collection = FakeChromaCollection(
            get_result={"ids": ["m1"], "metadatas": [{"created_at": now.isoformat()}]}
        )
        store = _chroma_store(collection=collection, encoder=None)
        count = await store.cleanup_old_memories(days=90)
        assert count == 0
        assert collection.delete_calls == []

    async def test_cleanup_exception_returns_zero(self):
        """A collection.get() failure during cleanup is caught and returns 0."""
        collection = FakeChromaCollection(get_raises=RuntimeError("boom"))
        store = _chroma_store(collection=collection, encoder=None)
        count = await store.cleanup_old_memories(days=90)
        assert count == 0


# ---------------------------------------------------------------------------
# PgvectorMemoryStore (gaps not already covered by test_memory_scope_pgvector.py
# and tests/unit/memory/test_memory_integration_wiring.py)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FakeDB:
    """Captures executesql calls; returns queued result batches, or raises."""

    results: list = field(default_factory=list)
    raises: Exception | None = None
    calls: list = field(default_factory=list)

    def executesql(self, sql: str, params: Any = None) -> Any:
        """Record the SQL/params call and pop the next queued result, or raise."""
        if self.raises is not None:
            raise self.raises
        self.calls.append((sql, tuple(params) if params else ()))
        return self.results.pop(0) if self.results else []


@dataclass(slots=True)
class FakeEmbedder:
    """Stand-in embedding manager returning a fixed vector -- never calls a real model."""

    vector: list = field(default_factory=lambda: [0.1, 0.2, 0.3])

    def embed(self, text: str) -> list:
        """Return the fixed embedding vector regardless of input text."""
        return self.vector


def _pgvector_store(results: list | None = None, **kwargs) -> tuple[PgvectorMemoryStore, FakeDB]:
    db = FakeDB(results or [])
    return PgvectorMemoryStore(write_db=db, embedding_manager=FakeEmbedder(), **kwargs), db


class TestPgvectorMemoryStoreReadDb:
    """_read_db replica-vs-primary selection."""

    def test_uses_replica_pool_when_available(self):
        """_read_db returns a replica connection when the pool has entries."""
        write_db = object()
        pool = ReadReplicaPool(["replica-1"])
        store = PgvectorMemoryStore(
            write_db=write_db, embedding_manager=FakeEmbedder(), replica_pool=pool
        )
        assert store._read_db() == "replica-1"

    def test_falls_back_to_write_db_without_replica_pool(self):
        """With no replica_pool configured, reads use the primary write_db."""
        write_db = object()
        store = PgvectorMemoryStore(write_db=write_db, embedding_manager=FakeEmbedder())
        assert store._read_db() is write_db

    def test_falls_back_to_write_db_when_replica_pool_empty(self):
        """An empty replica pool falls back to the primary write_db."""
        write_db = object()
        store = PgvectorMemoryStore(
            write_db=write_db, embedding_manager=FakeEmbedder(), replica_pool=ReadReplicaPool([])
        )
        assert store._read_db() is write_db


class TestPgvectorMemoryStoreInitialize:
    """initialize() is a no-op that just flips the internal ready flag."""

    async def test_sets_internal_flag(self):
        """initialize() requires no I/O; it only marks the store as ready."""
        store, _ = _pgvector_store()
        assert store._initialized is False
        await store.initialize()
        assert store._initialized is True


class TestPgvectorMemoryStoreStoreMemory:
    """store_memory write-path error handling."""

    async def test_exception_returns_false(self):
        """A write failure is caught and returns False rather than raising."""
        db = FakeDB(raises=RuntimeError("boom"))
        store = PgvectorMemoryStore(write_db=db, embedding_manager=FakeEmbedder())
        ok = await store.store_memory(_memory_entry())
        assert ok is False


class TestPgvectorMemoryStoreSearchMemoriesExtra:
    """search_memories: retrieval-cache org key, session filter, malformed metadata, errors."""

    async def test_org_scope_uses_org_only_cache_store_key(self):
        """scope='org' partitions the retrieval cache by min_relevance only, not by user."""
        from unittest.mock import AsyncMock, MagicMock

        retrieval_cache = MagicMock()
        seen_stores = []

        async def fake_get_or_compute(org_id, store, query, top_k, compute):
            seen_stores.append(store)
            return await compute()

        retrieval_cache.get_or_compute = AsyncMock(side_effect=fake_get_or_compute)
        store, _ = _pgvector_store(retrieval_cache=retrieval_cache)
        store._search_memories_uncached = AsyncMock(return_value=[])

        await store.search_memories(
            "q", user_id=10, organization_id=1, scope="org", min_relevance=0.6
        )
        assert seen_stores[0] == "memory:org:0.6"

    async def test_uncached_session_id_adds_filter(self):
        """A session_id argument appends a session_id filter to the similarity search SQL."""
        store, db = _pgvector_store(results=[[]])
        await store.search_memories(
            "q", user_id=5, organization_id=3, session_id="s1", scope="user"
        )
        sql, params = db.calls[0]
        assert "session_id = %s" in sql
        assert "s1" in params

    async def test_malformed_metadata_falls_back_to_empty_dict(self):
        """Invalid JSON in the metadata column is caught and treated as an empty dict."""
        row = (1, 5, 3, "s1", "hi", "user", datetime.utcnow(), "{not valid json", "user", 5, 0.9)
        store, _ = _pgvector_store(results=[[row]])
        entries = await store.search_memories("q", user_id=5, organization_id=3)
        assert entries[0].metadata["role"] == "user"

    async def test_exception_returns_empty_list(self):
        """An embed/query failure is caught and returns [] rather than raising."""
        db = FakeDB(raises=RuntimeError("boom"))
        store = PgvectorMemoryStore(write_db=db, embedding_manager=FakeEmbedder())
        entries = await store.search_memories("q", user_id=5, organization_id=3)
        assert entries == []


class TestPgvectorMemoryStoreConversationHistory:
    """get_conversation_history scope handling, malformed metadata, and error handling."""

    async def test_default_scope_is_user(self):
        """No scope argument filters to the caller's own personal rows."""
        store, db = _pgvector_store(results=[[]])
        await store.get_conversation_history(user_id=5, organization_id=3, session_id="s1")
        sql, _ = db.calls[0]
        assert "scope_type = 'user' AND user_id = %s" in sql

    async def test_org_scope_has_no_user_filter(self):
        """scope='org' filters on scope_type='org' only, with no user_id restriction."""
        store, db = _pgvector_store(results=[[]])
        await store.get_conversation_history(
            user_id=5, organization_id=3, session_id="s1", scope="org"
        )
        sql, _ = db.calls[0]
        assert "scope_type = 'org'" in sql
        assert "user_id = %s" not in sql

    async def test_returns_entries_with_full_relevance(self):
        """Conversation history rows are converted with relevance_score fixed at 1.0."""
        row = (11, 5, 3, "s1", "hello", "user", datetime.utcnow(), json.dumps({}), "user", 5)
        store, db = _pgvector_store(results=[[row]])
        entries = await store.get_conversation_history(
            user_id=5, organization_id=3, session_id="s1"
        )
        assert len(entries) == 1
        assert entries[0].content == "hello"
        assert entries[0].relevance_score == 1.0

    async def test_malformed_metadata_falls_back_to_empty_dict(self):
        """Invalid JSON in the metadata column is caught and treated as an empty dict."""
        row = (11, 5, 3, "s1", "hello", "user", datetime.utcnow(), "{not valid json", "user", 5)
        store, _ = _pgvector_store(results=[[row]])
        entries = await store.get_conversation_history(
            user_id=5, organization_id=3, session_id="s1"
        )
        assert entries[0].metadata["role"] == "user"

    async def test_exception_returns_empty_list(self):
        """A query failure is caught and returns [] rather than raising."""
        store, _ = _pgvector_store()
        store.write_db.raises = RuntimeError("boom")
        entries = await store.get_conversation_history(
            user_id=5, organization_id=3, session_id="s1"
        )
        assert entries == []


class TestPgvectorMemoryStoreClearMemories:
    """clear_memories session filter and error handling."""

    async def test_session_id_adds_filter_and_param(self):
        """A session_id argument appends a session_id filter with its bound value."""
        store, db = _pgvector_store()
        await store.clear_memories(user_id=5, organization_id=3, session_id="s9")
        sql, params = db.calls[0]
        assert "session_id = %s" in sql
        assert "s9" in params

    async def test_exception_returns_false(self):
        """A delete failure is caught and returns False rather than raising."""
        db = FakeDB(raises=RuntimeError("boom"))
        store = PgvectorMemoryStore(write_db=db, embedding_manager=FakeEmbedder())
        ok = await store.clear_memories(user_id=5, organization_id=3)
        assert ok is False


class TestPgvectorMemoryStoreStats:
    """get_memory_stats: populated, empty, and error branches."""

    async def test_returns_counts_from_first_row(self):
        """A non-empty result row populates total_memories/earliest/latest."""
        now = datetime.utcnow()
        store, _ = _pgvector_store(results=[[(4, now - timedelta(days=1), now)]])
        stats = await store.get_memory_stats(user_id=5, organization_id=3)
        assert stats["total_memories"] == 4
        assert stats["backend"] == "pgvector"
        assert stats["earliest"] is not None
        assert stats["latest"] is not None

    async def test_no_rows_returns_zero_defaults(self):
        """An empty rows result returns the zero-count default dict."""
        store, _ = _pgvector_store(results=[[]])
        stats = await store.get_memory_stats(user_id=5, organization_id=3)
        assert stats == {"total_memories": 0, "backend": "pgvector"}

    async def test_exception_includes_error_key(self):
        """A query failure is caught and the exception message surfaces in the response."""
        db = FakeDB(raises=RuntimeError("db down"))
        store = PgvectorMemoryStore(write_db=db, embedding_manager=FakeEmbedder())
        stats = await store.get_memory_stats(user_id=5, organization_id=3)
        assert stats["total_memories"] == 0
        assert "error" in stats


class TestPgvectorMemoryStoreGetRecentMemories:
    """get_recent_memories session filter and error handling."""

    async def test_session_filter_reaches_sql_and_entries_convert(self):
        """A session_id argument appends a session_id filter and rows convert to entries."""
        now = datetime.utcnow()
        row = (1, 5, 3, "s1", "hi", "user", now, json.dumps({}))
        store, db = _pgvector_store(results=[[row]])
        entries = await store.get_recent_memories(
            user_id=5, organization_id=3, session_id="s1", hours=1
        )
        sql, _ = db.calls[0]
        assert "session_id = %s" in sql
        assert len(entries) == 1
        assert entries[0].content == "hi"

    async def test_no_rows_returns_empty_list(self):
        """An empty result set converts to an empty entry list, not an error."""
        store, _ = _pgvector_store(results=[[]])
        entries = await store.get_recent_memories(user_id=5, organization_id=3)
        assert entries == []

    async def test_malformed_metadata_falls_back_to_empty_dict(self):
        """Invalid JSON in the metadata column is caught and treated as an empty dict."""
        row = (1, 5, 3, "s1", "hi", "user", datetime.utcnow(), "{not valid json")
        store, _ = _pgvector_store(results=[[row]])
        entries = await store.get_recent_memories(user_id=5, organization_id=3)
        assert entries[0].metadata["role"] == "user"

    async def test_exception_returns_empty_list(self):
        """A query failure is caught and returns [] rather than raising."""
        db = FakeDB(raises=RuntimeError("boom"))
        store = PgvectorMemoryStore(write_db=db, embedding_manager=FakeEmbedder())
        entries = await store.get_recent_memories(user_id=5, organization_id=3)
        assert entries == []


class TestPgvectorMemoryStoreDeleteMemory:
    """delete_memory without a retrieval_cache, and error handling."""

    async def test_without_retrieval_cache_skips_org_lookup(self):
        """No retrieval_cache configured means no SELECT lookup -- only the DELETE runs."""
        store, db = _pgvector_store(results=[[]])
        ok = await store.delete_memory("42")
        assert ok is True
        assert len(db.calls) == 1

    async def test_exception_returns_false(self):
        """A delete failure is caught and returns False rather than raising."""
        db = FakeDB(raises=RuntimeError("boom"))
        store = PgvectorMemoryStore(write_db=db, embedding_manager=FakeEmbedder())
        ok = await store.delete_memory("42")
        assert ok is False


class TestPgvectorMemoryStoreCleanupOldMemories:
    """cleanup_old_memories: counted deletes, no rows, and error handling."""

    async def test_returns_deleted_count(self):
        """The number of RETURNING rows becomes the deleted count."""
        store, _ = _pgvector_store(results=[[(1,), (2,), (3,)]])
        count = await store.cleanup_old_memories(days=30)
        assert count == 3

    async def test_no_rows_returns_zero(self):
        """An empty RETURNING result means nothing was deleted."""
        store, _ = _pgvector_store(results=[[]])
        count = await store.cleanup_old_memories(days=30)
        assert count == 0

    async def test_exception_returns_zero(self):
        """A delete failure is caught and returns 0 rather than raising."""
        db = FakeDB(raises=RuntimeError("boom"))
        store = PgvectorMemoryStore(write_db=db, embedding_manager=FakeEmbedder())
        count = await store.cleanup_old_memories(days=30)
        assert count == 0


# ---------------------------------------------------------------------------
# WaddleAIMemoryManager
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FakeMemoryStore(MemoryStore):
    """In-memory MemoryStore double for exercising WaddleAIMemoryManager in isolation."""

    initialized: bool = False
    store_calls: list = field(default_factory=list)
    store_result: bool = True
    store_raises: bool = False
    search_result: list = field(default_factory=list)
    search_raises: bool = False
    recent_result: list = field(default_factory=list)
    recent_raises: bool = False
    delete_result: bool = True
    cleanup_result: int = 0

    async def initialize(self):
        """Mark the store as initialized."""
        self.initialized = True

    async def store_memory(self, entry: MemoryEntry) -> bool:
        """Record the entry and return the configured result, or raise if configured."""
        if self.store_raises:
            raise RuntimeError("store boom")
        self.store_calls.append(entry)
        return self.store_result

    async def search_memories(
        self,
        query: str,
        user_id: int,
        organization_id: int,
        session_id: str | None = None,
        limit: int = 10,
        min_relevance: float = 0.7,
        scope: str = "user",
    ) -> list:
        """Return the configured search result, or raise if configured."""
        if self.search_raises:
            raise RuntimeError("search boom")
        return self.search_result

    async def get_recent_memories(
        self,
        user_id: int,
        organization_id: int,
        session_id: str | None = None,
        hours: int = 24,
        limit: int = 20,
    ) -> list:
        """Return the configured recent-memories result, or raise if configured."""
        if self.recent_raises:
            raise RuntimeError("recent boom")
        return self.recent_result

    async def delete_memory(self, memory_id: str) -> bool:
        """Return the configured delete result."""
        return self.delete_result

    async def cleanup_old_memories(self, days: int = 90) -> int:
        """Return the configured cleanup count."""
        return self.cleanup_result


class TestWaddleAIMemoryManagerInitAndAddTurn:
    """initialize() delegation and add_conversation_turn metadata handling."""

    async def test_initialize_calls_store_initialize(self):
        """manager.initialize() delegates to the underlying memory_store."""
        store = FakeMemoryStore()
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        await manager.initialize()
        assert store.initialized is True

    async def test_uses_last_user_message_and_defaults_when_no_metadata(self):
        """The most recent user message is combined with the response; metadata defaults apply."""
        store = FakeMemoryStore()
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ack"},
            {"role": "user", "content": "second"},
        ]
        ok = await manager.add_conversation_turn(
            user_id=1, organization_id=2, messages=messages, response="the answer"
        )
        assert ok is True
        entry = store.store_calls[0]
        assert "User: second" in entry.content
        assert "Assistant: the answer" in entry.content
        assert entry.metadata["model_used"] == "unknown"
        assert entry.metadata["message_count"] == 3

    async def test_applies_provided_routing_metadata(self):
        """Provided metadata (model/provider/routing/tokens) overrides the "unknown" defaults."""
        store = FakeMemoryStore()
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        meta = {
            "model": "gpt-4",
            "provider": "openai",
            "routing_decision": "direct",
            "waddleai_tokens": 12,
        }
        await manager.add_conversation_turn(
            user_id=1,
            organization_id=2,
            messages=[{"role": "user", "content": "hi"}],
            response="hey",
            metadata=meta,
        )
        entry_meta = store.store_calls[0].metadata
        assert entry_meta["model_used"] == "gpt-4"
        assert entry_meta["provider"] == "openai"
        assert entry_meta["waddleai_tokens"] == 12

    async def test_no_user_messages_uses_empty_prefix(self):
        """With no user-role messages, the conversation text still forms with an empty prefix."""
        store = FakeMemoryStore()
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        await manager.add_conversation_turn(
            user_id=1,
            organization_id=2,
            messages=[{"role": "assistant", "content": "hi"}],
            response="hey",
        )
        assert store.store_calls[0].content.startswith("User: \n")

    async def test_store_failure_returns_false(self):
        """A False return from the backing store propagates as a failed add."""
        store = FakeMemoryStore(store_result=False)
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        ok = await manager.add_conversation_turn(
            user_id=1,
            organization_id=2,
            messages=[{"role": "user", "content": "hi"}],
            response="hey",
        )
        assert ok is False

    async def test_store_exception_returns_false(self):
        """A raised exception from the backing store is caught and returns False."""
        store = FakeMemoryStore(store_raises=True)
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        ok = await manager.add_conversation_turn(
            user_id=1,
            organization_id=2,
            messages=[{"role": "user", "content": "hi"}],
            response="hey",
        )
        assert ok is False


class TestWaddleAIMemoryManagerConversationContext:
    """get_conversation_context: dedup/merge, no-memory branch, and error handling."""

    async def test_merges_and_dedupes_relevant_and_recent(self):
        """Relevant and recent memories are merged, deduped by id, and summarized."""
        t0 = datetime.utcnow()
        relevant = [
            _memory_entry(entry_id="a", content="alpha", relevance_score=0.9, created_at=t0)
        ]
        recent = [
            _memory_entry(entry_id="a", content="alpha-dup", relevance_score=0.1, created_at=t0),
            _memory_entry(
                entry_id="b",
                content="beta",
                relevance_score=0.2,
                created_at=t0 - timedelta(hours=1),
            ),
        ]
        store = FakeMemoryStore(search_result=relevant, recent_result=recent)
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        ctx = await manager.get_conversation_context(
            user_id=1, organization_id=2, current_messages=[{"role": "user", "content": "q"}]
        )
        assert len(ctx.relevant_memories) == 2
        assert {m.id for m in ctx.relevant_memories} == {"a", "b"}
        assert ctx.conversation_summary is not None

    async def test_no_memories_returns_no_summary(self):
        """With no relevant or recent memories, no summary is generated."""
        store = FakeMemoryStore(search_result=[], recent_result=[])
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        ctx = await manager.get_conversation_context(
            user_id=1, organization_id=2, current_messages=[{"role": "user", "content": "q"}]
        )
        assert ctx.relevant_memories == []
        assert ctx.conversation_summary is None

    async def test_exception_returns_empty_context(self):
        """A search failure is caught and returns a ConversationContext with no memories."""
        store = FakeMemoryStore(search_raises=True)
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        ctx = await manager.get_conversation_context(
            user_id=1, organization_id=2, current_messages=[{"role": "user", "content": "q"}]
        )
        assert ctx.relevant_memories == []
        assert ctx.conversation_summary is None
        assert ctx.user_id == 1


class TestGenerateConversationSummary:
    """_generate_conversation_summary truncation and cap-at-three behaviour."""

    async def test_empty_memories_returns_empty_string(self):
        """No memories means no summary text."""
        manager = WaddleAIMemoryManager(db=None, memory_store=FakeMemoryStore())
        summary = await manager._generate_conversation_summary([])
        assert summary == ""

    async def test_truncates_long_content_and_caps_at_three(self):
        """Only the first three memories are summarized, each truncated past 200 chars."""
        manager = WaddleAIMemoryManager(db=None, memory_store=FakeMemoryStore())
        memories = [_memory_entry(entry_id=str(i), content="x" * 250) for i in range(5)]
        summary = await manager._generate_conversation_summary(memories)
        parts = summary.split(" | ")
        assert len(parts) == 3
        assert all(p.endswith("...") and len(p) == 203 for p in parts)


class TestEnhanceMessagesWithContext:
    """enhance_messages_with_context: no-op, append, insert, truncation, and error branches."""

    async def test_returns_unchanged_without_memories_or_summary(self):
        """No relevant memories and no summary leaves the messages list untouched."""
        manager = WaddleAIMemoryManager(db=None, memory_store=FakeMemoryStore())
        ctx = ConversationContext(
            user_id=1,
            organization_id=2,
            session_id=None,
            recent_messages=[],
            relevant_memories=[],
            conversation_summary=None,
        )
        messages = [{"role": "user", "content": "hi"}]
        result = await manager.enhance_messages_with_context(messages, ctx)
        assert result == messages

    async def test_appends_to_existing_system_message(self):
        """An existing system message has the context text appended, not replaced."""
        manager = WaddleAIMemoryManager(db=None, memory_store=FakeMemoryStore())
        ctx = ConversationContext(
            user_id=1,
            organization_id=2,
            session_id=None,
            recent_messages=[],
            relevant_memories=[],
            conversation_summary="user likes dark mode",
        )
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hi"},
        ]
        result = await manager.enhance_messages_with_context(messages, ctx)
        assert len(result) == 2
        assert result[0]["role"] == "system"
        assert "You are helpful." in result[0]["content"]
        assert "user likes dark mode" in result[0]["content"]

    async def test_inserts_new_system_message_when_absent(self):
        """With no existing system message, a new one is inserted at position 0."""
        manager = WaddleAIMemoryManager(db=None, memory_store=FakeMemoryStore())
        ctx = ConversationContext(
            user_id=1,
            organization_id=2,
            session_id=None,
            recent_messages=[],
            relevant_memories=[_memory_entry(content="likes python")],
            conversation_summary=None,
        )
        messages = [{"role": "user", "content": "hi"}]
        result = await manager.enhance_messages_with_context(messages, ctx)
        assert len(result) == 2
        assert result[0]["role"] == "system"
        assert "likes python" in result[0]["content"]
        assert result[1] == messages[0]

    async def test_truncates_long_memory_content(self):
        """Memory content longer than 300 chars is truncated with an ellipsis."""
        manager = WaddleAIMemoryManager(db=None, memory_store=FakeMemoryStore())
        long_memory = _memory_entry(content="y" * 400)
        ctx = ConversationContext(
            user_id=1,
            organization_id=2,
            session_id=None,
            recent_messages=[],
            relevant_memories=[long_memory],
            conversation_summary=None,
        )
        result = await manager.enhance_messages_with_context(
            [{"role": "user", "content": "hi"}], ctx
        )
        assert "..." in result[0]["content"]

    async def test_exception_returns_original_messages(self):
        """A malformed memory (non-datetime created_at) is caught, returning messages unchanged."""
        manager = WaddleAIMemoryManager(db=None, memory_store=FakeMemoryStore())
        bad_memory = _memory_entry(content="x")
        bad_memory.created_at = "not-a-datetime"
        ctx = ConversationContext(
            user_id=1,
            organization_id=2,
            session_id=None,
            recent_messages=[],
            relevant_memories=[bad_memory],
            conversation_summary=None,
        )
        messages = [{"role": "user", "content": "hi"}]
        result = await manager.enhance_messages_with_context(messages, ctx)
        assert result == messages


class TestWaddleAIMemoryManagerDelegatesAndStats:
    """cleanup_old_memories delegation and get_memory_stats aggregation/error branches."""

    async def test_cleanup_old_memories_delegates_to_store(self):
        """cleanup_old_memories returns exactly what the backing store reports."""
        store = FakeMemoryStore(cleanup_result=7)
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        count = await manager.cleanup_old_memories(days=45)
        assert count == 7

    async def test_computes_daily_counts_and_bounds(self):
        """get_memory_stats aggregates counts per day and reports oldest/newest timestamps."""
        t0 = datetime(2026, 1, 1, 10, 0, 0)
        t1 = datetime(2026, 1, 2, 10, 0, 0)
        recent = [
            _memory_entry(entry_id="a", content="short", created_at=t0),
            _memory_entry(entry_id="b", content="a longer memory body", created_at=t0),
            _memory_entry(entry_id="c", content="x", created_at=t1),
        ]
        store = FakeMemoryStore(recent_result=recent)
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        stats = await manager.get_memory_stats(user_id=1, organization_id=2)
        assert stats["total_memories"] == 3
        assert stats["daily_counts"] == {"2026-01-01": 2, "2026-01-02": 1}
        assert stats["oldest_memory"] == t0.isoformat()
        assert stats["newest_memory"] == t1.isoformat()

    async def test_empty_returns_zero_defaults(self):
        """No recent memories means every stat falls back to its zero/None default."""
        store = FakeMemoryStore(recent_result=[])
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        stats = await manager.get_memory_stats(user_id=1, organization_id=2)
        assert stats["total_memories"] == 0
        assert stats["average_content_length"] == 0
        assert stats["oldest_memory"] is None

    async def test_exception_returns_defaults(self):
        """A backing-store failure is caught and returns the zero-stat default dict."""
        store = FakeMemoryStore(recent_raises=True)
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        stats = await manager.get_memory_stats(user_id=1, organization_id=2)
        assert stats == {
            "total_memories": 0,
            "average_content_length": 0,
            "daily_counts": {},
            "oldest_memory": None,
            "newest_memory": None,
        }


class TestSemanticSearchAndStoreComplete:
    """semantic_search_conversations and store_complete_conversation."""

    async def test_semantic_search_with_user_id_returns_conversations(self):
        """A user-scoped search converts MemoryEntry results into conversation dicts."""
        store = FakeMemoryStore(search_result=[_memory_entry(entry_id="a", content="alpha")])
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        results = await manager.semantic_search_conversations("q", user_id=1, organization_id=2)
        assert results[0]["id"] == "a"
        assert results[0]["content"] == "alpha"

    async def test_semantic_search_without_user_id_returns_empty_list(self):
        """Admin (no user_id) search is not implemented and returns []."""
        store = FakeMemoryStore(search_result=[_memory_entry()])
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        results = await manager.semantic_search_conversations("q", user_id=None)
        assert results == []

    async def test_semantic_search_exception_returns_empty_list(self):
        """A search failure is caught and returns [] rather than raising."""
        store = FakeMemoryStore(search_raises=True)
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        results = await manager.semantic_search_conversations("q", user_id=1, organization_id=2)
        assert results == []

    async def test_store_complete_conversation_success(self):
        """A full conversation (all messages + response) is combined and stored."""
        store = FakeMemoryStore()
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        ok = await manager.store_complete_conversation(
            user_id=1,
            organization_id=2,
            messages=[{"role": "user", "content": "hi"}],
            response="hello",
            model_used="gpt-4",
            routing_decision="direct",
            routing_reasoning="cheap",
            metadata={"request_type": "chat"},
        )
        assert ok is True
        assert "User: hi" in store.store_calls[0].content
        assert "Assistant: hello" in store.store_calls[0].content

    async def test_store_complete_conversation_exception_returns_false(self):
        """A backing-store failure is caught and returns False rather than raising."""
        store = FakeMemoryStore(store_raises=True)
        manager = WaddleAIMemoryManager(db=None, memory_store=store)
        ok = await manager.store_complete_conversation(
            user_id=1,
            organization_id=2,
            messages=[],
            response="x",
            model_used="m",
            routing_decision="d",
            routing_reasoning="r",
            metadata={},
        )
        assert ok is False


# ---------------------------------------------------------------------------
# create_memory_manager: backend dispatch
# ---------------------------------------------------------------------------


class TestCreateMemoryManager:
    """create_memory_manager's backend selection and error handling."""

    def test_pgvector_requires_db_or_write_db(self):
        """The pgvector backend refuses to build without any database connection."""
        with pytest.raises(ValueError, match="requires a database connection"):
            create_memory_manager(backend="pgvector")

    def test_pgvector_uses_provided_embedding_manager(self):
        """An explicitly passed embedding_manager is used as-is, no default import."""
        write_db = object()
        embedding_manager = FakeEmbedder()
        manager = create_memory_manager(
            write_db=write_db, backend="pgvector", embedding_manager=embedding_manager
        )
        assert isinstance(manager, WaddleAIMemoryManager)
        assert isinstance(manager.memory_store, PgvectorMemoryStore)
        assert manager.memory_store.embedding_manager is embedding_manager
        assert manager.db is write_db

    def test_pgvector_default_embedding_manager_created_via_factory(self, monkeypatch):
        """With no embedding_manager passed, the default factory is imported and called."""
        write_db = object()
        sentinel = FakeEmbedder()
        monkeypatch.setattr(
            "shared.utils.embedding_manager.create_embedding_manager", lambda: sentinel
        )
        manager = create_memory_manager(write_db=write_db, backend="pgvector")
        assert manager.memory_store.embedding_manager is sentinel

    def test_pgvector_missing_embedding_manager_module_raises_helpful_error(self, monkeypatch):
        """An ImportError from the embedding_manager module is wrapped with a clearer message."""
        write_db = object()
        monkeypatch.delattr("shared.utils.embedding_manager.create_embedding_manager")
        with pytest.raises(ImportError, match="embedding_manager is required"):
            create_memory_manager(write_db=write_db, backend="pgvector")

    def test_mem0_backend_used_when_available(self):
        """backend='mem0' builds a Mem0MemoryStore when mem0ai is importable."""
        with patch("shared.utils.memory_integration.HAS_MEM0", True):
            manager = create_memory_manager(db=object(), backend="mem0", api_key="k", org_id="o")
        assert isinstance(manager.memory_store, Mem0MemoryStore)

    def test_mem0_backend_falls_back_to_chromadb_when_unavailable(self, tmp_path):
        """backend='mem0' without mem0ai installed silently falls back to ChromaDB."""
        with (
            patch("shared.utils.memory_integration.HAS_MEM0", False),
            patch("shared.utils.memory_integration._sentence_transformer"),
        ):
            manager = create_memory_manager(
                db=object(), backend="mem0", persist_directory=str(tmp_path)
            )
        assert isinstance(manager.memory_store, ChromaDBMemoryStore)

    def test_chromadb_backend_uses_custom_collection_name(self, tmp_path):
        """backend='chromadb' reads collection_name out of the config dict."""
        with patch("shared.utils.memory_integration._sentence_transformer"):
            manager = create_memory_manager(
                db=object(),
                backend="chromadb",
                persist_directory=str(tmp_path),
                config={"collection_name": "custom_memories"},
            )
        assert manager.memory_store.collection_name == "custom_memories"

    def test_unknown_backend_raises_value_error(self):
        """An unrecognized backend name raises ValueError, not a silent no-op."""
        with pytest.raises(ValueError, match="Unknown memory backend"):
            create_memory_manager(db=object(), backend="not-a-backend")


# ---------------------------------------------------------------------------
# ReadReplicaPool
# ---------------------------------------------------------------------------


class TestReadReplicaPool:
    """Round-robin selection and from_env construction."""

    def test_get_returns_none_when_no_replicas(self):
        """An empty pool has nothing to return."""
        pool = ReadReplicaPool([])
        assert pool.get() is None
        assert len(pool) == 0

    def test_get_round_robins_through_replicas(self):
        """get() cycles through the configured replicas in order, wrapping around."""
        pool = ReadReplicaPool(["db-a", "db-b", "db-c"])
        seq = [pool.get() for _ in range(4)]
        assert seq == ["db-a", "db-b", "db-c", "db-a"]

    def test_from_env_returns_empty_pool_when_var_unset(self, monkeypatch):
        """No DATABASE_REPLICA_URL means an empty pool (reads fall back to write_db)."""
        monkeypatch.delenv("DATABASE_REPLICA_URL", raising=False)
        pool = ReadReplicaPool.from_env()
        assert len(pool) == 0

    def test_from_env_builds_pool_and_normalizes_postgres_scheme(self, monkeypatch):
        """Comma-separated URLs are each connected, normalizing postgres:// to postgresql://."""
        seen_urls = []

        class FakeDAL:
            def __init__(self, url, pool_size=5, migrate=False):
                seen_urls.append(url)

        monkeypatch.setattr("penguin_dal.DAL", FakeDAL)
        monkeypatch.setenv("DATABASE_REPLICA_URL", "postgres://a/db, postgresql://b/db")
        pool = ReadReplicaPool.from_env()
        assert len(pool) == 2
        assert seen_urls[0] == "postgresql://a/db"
        assert seen_urls[1] == "postgresql://b/db"

    def test_from_env_skips_url_that_fails_to_connect(self, monkeypatch):
        """A replica connection failure is logged and skipped, not raised."""

        def raising_dal(url, pool_size=5, migrate=False):
            raise RuntimeError("connection refused")

        monkeypatch.setattr("penguin_dal.DAL", raising_dal)
        monkeypatch.setenv("DATABASE_REPLICA_URL", "postgresql://bad/db")
        pool = ReadReplicaPool.from_env()
        assert len(pool) == 0
