"""Unit tests for PgvectorMemoryStore scope support (personal vs org memory).

The store builds raw SQL against memory_embeddings; these tests capture the
SQL and parameters through a fake DAL and assert the scope branches, the
metadata scope mirror, and MemoryEntry field population.
"""

import json
from datetime import datetime
from typing import Any, List, Optional, Tuple

from shared.utils.memory_integration import MemoryEntry, PgvectorMemoryStore


class FakeDB:
    """Captures executesql calls; returns queued results."""

    def __init__(self, results: Optional[List[Any]] = None) -> None:
        self.calls: List[Tuple[str, tuple]] = []
        self._results = results or []

    def executesql(self, sql: str, params: Any = None) -> Any:
        self.calls.append((sql, tuple(params) if params else ()))
        return self._results.pop(0) if self._results else []


class FakeEmbedder:
    def embed(self, text: str) -> List[float]:
        return [0.1, 0.2, 0.3]


def _store(results: Optional[List[Any]] = None) -> Tuple[PgvectorMemoryStore, FakeDB]:
    db = FakeDB(results)
    return PgvectorMemoryStore(write_db=db, embedding_manager=FakeEmbedder()), db


def _entry(scope_type: str = "user", author: int = 0) -> MemoryEntry:
    return MemoryEntry(
        id="",
        user_id=5,
        organization_id=3,
        session_id="s1",
        content="remember the deploy runbook",
        metadata={"role": "user"},
        embedding=None,
        created_at=datetime.utcnow(),
        scope_type=scope_type,
        author_user_id=author,
    )


# --- store_memory ----------------------------------------------------------


async def test_store_memory_defaults_to_personal_scope() -> None:
    store, db = _store()
    ok = await store.store_memory(_entry())
    assert ok is True
    sql, params = db.calls[0]
    assert "scope_type" in sql and "author_user_id" in sql
    # scope_type param
    assert "user" in params
    # author defaults to the entry's user_id when author_user_id == 0
    assert 5 in params


async def test_store_memory_org_scope_writes_column_and_metadata_mirror() -> None:
    store, db = _store()
    ok = await store.store_memory(_entry(scope_type="org", author=5))
    assert ok is True
    sql, params = db.calls[0]
    assert "org" in params
    meta_param = next(p for p in params if isinstance(p, str) and p.startswith("{"))
    assert json.loads(meta_param)["scope"] == "org"


async def test_store_memory_personal_metadata_mirror() -> None:
    store, db = _store()
    await store.store_memory(_entry())
    _, params = db.calls[0]
    meta_param = next(p for p in params if isinstance(p, str) and p.startswith("{"))
    assert json.loads(meta_param)["scope"] == "user"


# --- search_memories -------------------------------------------------------


def _search_row(scope_type: str = "user", author: int = 5) -> tuple:
    # id, user_id, organization_id, session_id, content, role,
    # created_at, metadata, scope_type, author_user_id, similarity
    return (
        11,
        5,
        3,
        "s1",
        "remembered",
        "user",
        datetime.utcnow(),
        json.dumps({"scope": scope_type}),
        scope_type,
        author,
        0.91,
    )


async def test_search_scope_user_filters_to_owner() -> None:
    store, db = _store(results=[[_search_row()]])
    entries = await store.search_memories("q", user_id=5, organization_id=3, scope="user")
    sql, params = db.calls[0]
    assert "scope_type = 'user' AND user_id = %s" in sql
    assert "scope_type = 'org' OR" not in sql
    assert entries[0].scope_type == "user"
    assert entries[0].author_user_id == 5


async def test_search_scope_org_has_no_user_filter() -> None:
    store, db = _store(results=[[_search_row("org", 9)]])
    entries = await store.search_memories("q", user_id=5, organization_id=3, scope="org")
    sql, params = db.calls[0]
    assert "scope_type = 'org'" in sql
    assert "user_id = %s" not in sql
    assert entries[0].scope_type == "org"
    assert entries[0].author_user_id == 9


async def test_search_scope_all_is_merged_or_branch() -> None:
    store, db = _store(results=[[_search_row(), _search_row("org", 9)]])
    entries = await store.search_memories("q", user_id=5, organization_id=3, scope="all")
    sql, params = db.calls[0]
    assert "(scope_type = 'org' OR (scope_type = 'user' AND user_id = %s))" in sql
    assert len(entries) == 2


async def test_search_default_scope_is_user() -> None:
    """Internal callers that never pass scope keep today's personal-only behavior."""
    store, db = _store(results=[[]])
    await store.search_memories("q", user_id=5, organization_id=3)
    sql, _ = db.calls[0]
    assert "scope_type = 'user' AND user_id = %s" in sql


# --- get_conversation_history ---------------------------------------------


async def test_history_scope_all_merged() -> None:
    store, db = _store(results=[[]])
    await store.get_conversation_history(user_id=5, organization_id=3, session_id="s1", scope="all")
    sql, params = db.calls[0]
    assert "(scope_type = 'org' OR (scope_type = 'user' AND user_id = %s))" in sql
    assert "organization_id = %s" in sql


# --- clear_memories ---------------------------------------------------------


async def test_clear_default_personal_only() -> None:
    store, db = _store()
    ok = await store.clear_memories(user_id=5, organization_id=3)
    assert ok is True
    sql, params = db.calls[0]
    assert "scope_type = 'user'" in sql
    assert "user_id = %s" in sql


async def test_clear_org_author_only() -> None:
    store, db = _store()
    await store.clear_memories(user_id=5, organization_id=3, scope="org")
    sql, params = db.calls[0]
    assert "scope_type = 'org'" in sql
    assert "author_user_id = %s" in sql


async def test_clear_org_all_has_no_author_filter() -> None:
    store, db = _store()
    await store.clear_memories(user_id=5, organization_id=3, scope="org", org_all=True)
    sql, params = db.calls[0]
    assert "scope_type = 'org'" in sql
    assert "author_user_id" not in sql
    assert "user_id = %s" not in sql
