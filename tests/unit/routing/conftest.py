"""Shared fixtures for shared/routing/* unit tests.

fakeredis is not a project dependency (see requirements.in); FakeAsyncRedis
stands in for redis.asyncio's get/set/delete surface so cache-hit and
invalidation tests can make real call-count assertions without a live Valkey.

FakeDB is a minimal in-memory penguin-dal/PyDAL stand-in (field comparisons,
insert/select/update/delete) so resolver tests exercise real filtering logic
instead of asserting against a MagicMock call graph.
"""

from collections.abc import Callable
from typing import Any

import pytest


class FakeAsyncRedis:
    """Minimal in-memory async Redis stand-in: get/set/delete + call counts."""

    def __init__(self) -> None:
        """Initialize an empty in-memory store with zeroed call counters."""
        self._store: dict[str, Any] = {}
        self.get_calls = 0
        self.set_calls = 0

    async def get(self, key: str) -> Any | None:
        """Return the stored value for key, tracking call count."""
        self.get_calls += 1
        return self._store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> None:
        """Store value under key (TTL is accepted but not enforced)."""
        self.set_calls += 1
        self._store[key] = value

    async def delete(self, *keys: str) -> None:
        """Remove keys from the store if present."""
        for key in keys:
            self._store.pop(key, None)


@pytest.fixture
def fake_valkey() -> FakeAsyncRedis:
    """A fresh in-memory fake Valkey client per test."""
    return FakeAsyncRedis()


class _FakeRow(dict):
    """Dict that also supports attribute access, like a penguin-dal row."""

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc


class _Predicate:
    """A composable row predicate carrying which table(s) it references."""

    def __init__(self, fn: Callable[[dict], bool], tables: frozenset[str]) -> None:
        self.fn = fn
        self.tables = tables

    def __and__(self, other: "_Predicate") -> "_Predicate":
        return _Predicate(lambda row: self.fn(row) and other.fn(row), self.tables | other.tables)

    def __or__(self, other: "_Predicate") -> "_Predicate":
        return _Predicate(lambda row: self.fn(row) or other.fn(row), self.tables | other.tables)


class _FakeField:
    """A PyDAL-style field reference supporting comparison operators."""

    def __init__(self, table_name: str, field_name: str) -> None:
        self.table_name = table_name
        self.field_name = field_name

    def _pred(self, fn: Callable[[Any], bool]) -> _Predicate:
        return _Predicate(lambda row: fn(row.get(self.field_name)), frozenset({self.table_name}))

    def __eq__(self, value: Any) -> _Predicate:  # type: ignore[override]
        return self._pred(lambda v: v == value)

    def __ne__(self, value: Any) -> _Predicate:  # type: ignore[override]
        return self._pred(lambda v: v != value)

    def __gt__(self, value: Any) -> _Predicate:
        return self._pred(lambda v: v is not None and v > value)

    def __lt__(self, value: Any) -> _Predicate:
        return self._pred(lambda v: v is not None and v < value)


class _FakeSelectResult(list):
    """List of matching rows that also supports penguin-dal's ``.first()``."""

    def first(self) -> _FakeRow | None:
        """Return the first row, or None when there are no matches."""
        return self[0] if self else None


class _FakeQueryResult:
    """Result of ``db(predicate)`` -- supports select/update/delete."""

    def __init__(self, db: "FakeDB", table_name: str, predicate: _Predicate | None) -> None:
        self._db = db
        self._table_name = table_name
        self._predicate = predicate

    def _matching_rows(self) -> list[dict]:
        rows = self._db._tables.setdefault(self._table_name, [])
        if self._predicate is None:
            return rows
        return [r for r in rows if self._predicate.fn(r)]

    def select(self, orderby: Any = None) -> _FakeSelectResult:
        """Return matching rows as attribute-accessible fake rows."""
        return _FakeSelectResult(_FakeRow(r) for r in self._matching_rows())

    def update(self, **kwargs: Any) -> int:
        """Update matching rows in place; returns the count updated."""
        matched = self._matching_rows()
        for row in matched:
            row.update(kwargs)
        return len(matched)

    def delete(self) -> int:
        """Delete matching rows; returns the count deleted."""
        matched = self._matching_rows()
        table = self._db._tables[self._table_name]
        for row in matched:
            table.remove(row)
        return len(matched)


class _FakeTable:
    """A PyDAL-style table accessor: field lookup + insert."""

    def __init__(self, db: "FakeDB", name: str) -> None:
        self._db = db
        self._name = name

    def __getattr__(self, field_name: str) -> _FakeField:
        return _FakeField(self._name, field_name)

    def insert(self, **kwargs: Any) -> int:
        """Insert a new row, auto-assigning id; returns the new id."""
        rows = self._db._tables.setdefault(self._name, [])
        new_id = kwargs.get("id") or (max((r["id"] for r in rows), default=0) + 1)
        rows.append({"id": new_id, **kwargs})
        return new_id


class FakeDB:
    """Minimal in-memory penguin-dal/PyDAL stand-in for resolver unit tests."""

    def __init__(self) -> None:
        """Initialize empty table storage and the per-table accessor cache."""
        self._tables: dict[str, list[dict]] = {}
        self._table_objs: dict[str, _FakeTable] = {}
        self.commit_calls = 0

    def __getattr__(self, table_name: str) -> _FakeTable:
        """Return (creating if needed) the cached table accessor for db.<table_name>."""
        self._tables.setdefault(table_name, [])
        # Cached per table name so a test can monkeypatch e.g.
        # fake_db.some_table.insert and have later accesses see it.
        if table_name not in self._table_objs:
            self._table_objs[table_name] = _FakeTable(self, table_name)
        return self._table_objs[table_name]

    def __call__(self, predicate: _Predicate | None = None) -> _FakeQueryResult:
        """Emulate ``db(query)``, inferring the target table from the predicate."""
        table_name = next(iter(predicate.tables)) if predicate is not None else None
        return _FakeQueryResult(self, table_name, predicate)

    def commit(self) -> None:
        """No-op commit, tracked for call-count assertions."""
        self.commit_calls += 1

    def seed(self, table_name: str, rows: list[dict]) -> None:
        """Directly populate a table with rows (test setup convenience)."""
        self._tables.setdefault(table_name, []).extend(rows)


@pytest.fixture
def fake_db() -> FakeDB:
    """A fresh in-memory FakeDB per test."""
    return FakeDB()
