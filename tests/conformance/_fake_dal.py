"""Minimal PyDAL-calling-convention fake used by the fleet conformance suite.

Real penguin-dal/PyDAL query objects are non-trivial to construct outside a
live DB connection, and the rest of this codebase mocks ``db`` with a bare
``MagicMock`` per call site (see ``tests/unit/management/test_llamacpp_manager.py``)
which can't express "these two different queries return different rows" —
exactly what the conformance suite needs (list-all vs. lookup-by-name).
This fake implements just enough of ``db(query).select()/.first()/.update()/
.delete()`` and ``db.table.insert(**kwargs)`` against an in-memory row store
to exercise the real ``InferenceFleetBackend`` method bodies end to end.
"""

from collections.abc import Callable
from typing import Any


class _FakeRow:
    """A row with real DB semantics: unset columns read as ``None``, not ``AttributeError``.

    Real PyDAL/penguin-dal rows reflect the full table schema, so a nullable
    column nobody passed to ``insert()`` still reads as ``None`` rather than
    raising — this fake mirrors that so manager code written against real
    rows (e.g. ``deployment.cpu_request or "2000m"``) works unmodified.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)

    def __getattr__(self, name: str) -> None:
        return None


class _FakeField:
    """A ``db.table.column`` stand-in that builds a ``_FakeQuery`` on comparison."""

    def __init__(self, table: str, name: str) -> None:
        self.table = table
        self.name = name

    def __eq__(self, other: Any) -> "_FakeQuery":  # type: ignore[override]
        return _FakeQuery(self.table, lambda row: getattr(row, self.name, None) == other)

    def __gt__(self, other: Any) -> "_FakeQuery":
        return _FakeQuery(self.table, lambda row: getattr(row, self.name, 0) > other)

    def belongs(self, values: Any) -> "_FakeQuery":
        return _FakeQuery(self.table, lambda row: getattr(row, self.name, None) in values)


class _FakeQuery:
    """A predicate over rows of a single table, composable with ``&``."""

    def __init__(self, table: str, predicate: Callable[[_FakeRow], bool]) -> None:
        self.table = table
        self.predicate = predicate

    def __and__(self, other: "_FakeQuery") -> "_FakeQuery":
        return _FakeQuery(self.table, lambda row: self.predicate(row) and other.predicate(row))

    def __call__(self, row: _FakeRow) -> bool:
        return self.predicate(row)


class _FakeSelectResult(list):
    """List of matched rows, plus PyDAL's ``.first()``."""

    def first(self) -> _FakeRow | None:
        return self[0] if self else None


class _FakeRowSet:
    """The object returned by ``db(query)`` — supports select/update/delete."""

    def __init__(self, db: "FakeDAL", query: _FakeQuery) -> None:
        self._db = db
        self._query = query

    def select(self) -> _FakeSelectResult:
        rows = self._db._tables.get(self._query.table, [])
        return _FakeSelectResult(row for row in rows if self._query(row))

    def update(self, **kwargs: Any) -> int:
        rows = self._db._tables.get(self._query.table, [])
        matched = [row for row in rows if self._query(row)]
        for row in matched:
            for key, value in kwargs.items():
                setattr(row, key, value)
        return len(matched)

    def delete(self) -> int:
        rows = self._db._tables.get(self._query.table, [])
        kept = [row for row in rows if not self._query(row)]
        removed = len(rows) - len(kept)
        self._db._tables[self._query.table] = kept
        return removed


class _FakeTable:
    """A ``db.table_name`` stand-in — column access builds fields; ``.insert`` writes rows."""

    def __init__(self, db: "FakeDAL", name: str) -> None:
        self._db = db
        self._name = name

    def __getattr__(self, field_name: str) -> _FakeField:
        return _FakeField(self._name, field_name)

    def insert(self, **kwargs: Any) -> int:
        row_id = self._db._next_id[self._name]
        self._db._next_id[self._name] += 1
        self._db._tables.setdefault(self._name, []).append(_FakeRow(id=row_id, **kwargs))
        return row_id


class FakeDAL:
    """In-memory stand-in for a penguin-dal ``DB`` used only by these tests."""

    def __init__(self) -> None:
        self._tables: dict[str, list[_FakeRow]] = {}
        self._next_id: dict[str, int] = {}

    def __getattr__(self, name: str) -> _FakeTable:
        self._tables.setdefault(name, [])
        self._next_id.setdefault(name, 1)
        return _FakeTable(self, name)

    def __call__(self, query: _FakeQuery) -> _FakeRowSet:
        return _FakeRowSet(self, query)

    def commit(self) -> None:
        """No-op — the fake writes rows immediately on insert/update/delete."""
