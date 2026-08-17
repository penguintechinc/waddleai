"""In-memory fake Valkey/Redis async client for shared.cache unit tests.

The repo has no fakeredis dependency (see requirements.txt), and every other
Redis/Valkey-touching test in this codebase mocks the client directly with
unittest.mock (e.g. tests/unit/test_health_checks.py) rather than pulling in
fakeredis -- this fixture follows that existing convention with a small
purpose-built in-memory double instead of adding a new dependency.

Implements only the commands shared.cache actually issues: get/set/delete/
exists/ttl (string values with optional expiry) and zadd/zrange/zrem/zscore
(sorted sets, used for the per-org LRU index). ``now`` is overridable for
deterministic TTL/time-travel assertions without real sleeps.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest


class FakeValkey:
    """Minimal async in-memory stand-in for a redis.asyncio client."""

    def __init__(self) -> None:
        """Initialize empty string/zset stores and a real-time clock (overridable for tests)."""
        self._store: dict[str, bytes] = {}
        self._expire_at: dict[str, float] = {}
        self._zsets: dict[str, dict[str, float]] = {}
        self.now = time.time

    def _expired(self, key: str) -> bool:
        """True if `key` has a TTL set and it has already elapsed."""
        exp = self._expire_at.get(key)
        return exp is not None and self.now() >= exp

    def _purge_if_expired(self, key: str) -> None:
        """Drop `key` from the store if its TTL has elapsed."""
        if self._expired(key):
            self._store.pop(key, None)
            self._expire_at.pop(key, None)

    async def get(self, key: str) -> bytes | None:
        """Return the raw value for `key`, or None if absent/expired."""
        self._purge_if_expired(key)
        return self._store.get(key)

    async def set(self, key: str, value, ex: int | None = None) -> bool:
        """Set `key` to `value` (str auto-encoded), with an optional TTL in seconds."""
        if isinstance(value, str):
            value = value.encode()
        self._store[key] = value
        if ex is not None:
            self._expire_at[key] = self.now() + ex
        else:
            self._expire_at.pop(key, None)
        return True

    async def delete(self, *keys: str) -> int:
        """Delete each of `keys`; returns the count actually present."""
        count = 0
        for key in keys:
            if key in self._store:
                del self._store[key]
                count += 1
            self._expire_at.pop(key, None)
        return count

    async def exists(self, key: str) -> int:
        """Return 1 if `key` is present and unexpired, else 0."""
        self._purge_if_expired(key)
        return 1 if key in self._store else 0

    async def ttl(self, key: str) -> int:
        """Return seconds remaining on `key`'s TTL: -1 no TTL, -2 missing/expired."""
        self._purge_if_expired(key)
        if key not in self._store:
            return -2
        exp = self._expire_at.get(key)
        if exp is None:
            return -1
        return max(0, int(exp - self.now()))

    async def zadd(self, key: str, mapping: dict) -> int:
        """Add/update `mapping` (member -> score) in the sorted set at `key`."""
        z = self._zsets.setdefault(key, {})
        z.update(mapping)
        return len(mapping)

    async def zrange(self, key: str, start: int, end: int, desc: bool = False) -> list:
        """Return members of the sorted set at `key`, ordered by score, sliced [start:end+1]."""
        z = self._zsets.get(key, {})
        ordered = sorted(z.items(), key=lambda kv: kv[1], reverse=desc)
        members = [m for m, _ in ordered]
        if end == -1:
            return members[start:]
        return members[start : end + 1]

    async def zrem(self, key: str, *members: str) -> int:
        """Remove `members` from the sorted set at `key`; returns the count removed."""
        z = self._zsets.get(key, {})
        count = 0
        for member in members:
            if member in z:
                del z[member]
                count += 1
        return count

    async def zscore(self, key: str, member: str) -> float | None:
        """Return `member`'s score in the sorted set at `key`, or None if absent."""
        return self._zsets.get(key, {}).get(member)

    async def incr(self, key: str) -> int:
        """Increment the integer value at `key` (default 0) and return the new value."""
        self._purge_if_expired(key)
        current = int(self._store.get(key, b"0"))
        new_value = current + 1
        self._store[key] = str(new_value).encode()
        return new_value

    async def expire(self, key: str, seconds: int) -> bool:
        """Set a TTL on an existing `key`; returns False if the key is absent."""
        if key not in self._store:
            return False
        self._expire_at[key] = self.now() + seconds
        return True

    def keys_with_prefix(self, prefix: str) -> list:
        """Test helper (not a redis command): list live keys under a prefix."""
        return [k for k in self._store if k.startswith(prefix) and not self._expired(k)]


@pytest.fixture
def fake_valkey() -> FakeValkey:
    """Return a fresh FakeValkey instance for a single test."""
    return FakeValkey()


# ---------------------------------------------------------------------------
# Fake penguin-dal `db` for shared.cache.config.CacheConfigResolver tests.
#
# The management-test conftest's `_make_mock_db()` returns MagicMocks that
# don't actually filter by query content, which is unusable here: resolver
# precedence tests need real (scope_type, scope_ref) filtering. This is a
# small, self-contained in-memory double instead of a real penguin-dal
# connection, matching the write-a-fake-not-a-mock approach already used by
# tests/unit/cache/conftest.py's FakeValkey.
# ---------------------------------------------------------------------------


class _FakeField:
    """A queryable column on a fake table: supports `==`/`>` producing a `_FakeCond`."""

    def __init__(self, name: str) -> None:
        """Store the column name this field represents."""
        self.name = name

    def __eq__(self, other: Any) -> _FakeCond:  # type: ignore[override]
        return _FakeCond(lambda row: row.get(self.name) == other)

    def __gt__(self, other: Any) -> _FakeCond:
        return _FakeCond(lambda row: row.get(self.name) is not None and row.get(self.name) > other)


class _FakeCond:
    """A composable predicate over a raw dict row, combinable with `&`/`|`."""

    def __init__(self, fn) -> None:
        """Wrap a `row -> bool` predicate function."""
        self.fn = fn

    def __and__(self, other: _FakeCond) -> _FakeCond:
        return _FakeCond(lambda row: self.fn(row) and other.fn(row))

    def __or__(self, other: _FakeCond) -> _FakeCond:
        return _FakeCond(lambda row: self.fn(row) or other.fn(row))

    def __call__(self, row: dict) -> bool:
        return bool(self.fn(row))


class _FakeCacheConfigsTable:
    """Field accessors for the fake `cache_configs` table."""

    scope_type = _FakeField("scope_type")
    scope_ref = _FakeField("scope_ref")


class _FakeSelectResult(list):
    """A list of matched rows that also supports the `.select()` chaining call."""

    def select(self, *args, **kwargs):
        """Return self -- `db(query).select()` is a no-op filter step in this fake."""
        return self


class FakeCacheConfigDB:
    """Minimal in-memory stand-in for the penguin-dal `db` used by CacheConfigResolver.

    Tracks the number of `select()`-triggering calls (``db(query)``) so
    tests can assert on DB-read counts precisely.
    """

    def __init__(self) -> None:
        """Initialize an empty `cache_configs` row store and call counter."""
        self.cache_configs = _FakeCacheConfigsTable()
        self.rows: list[dict] = []
        self.call_count = 0

    def seed(self, **row) -> None:
        """Insert a `cache_configs` row, defaulting unset fields to None."""
        defaults = {
            "scope_ref": None,
            "exact_enabled": None,
            "semantic_enabled": None,
            "semantic_threshold": None,
            "ttl_seconds": None,
            "max_entry_kb": None,
            "anthropic_cache_control": None,
        }
        defaults.update(row)
        self.rows.append(defaults)

    def __call__(self, query: _FakeCond):
        """Evaluate `query` against every seeded row and return the matches."""
        self.call_count += 1
        matched = [SimpleNamespace(**r) for r in self.rows if query(r)]
        return _FakeSelectResult(matched)


@pytest.fixture
def fake_cache_config_db() -> FakeCacheConfigDB:
    """Return a fresh FakeCacheConfigDB instance for a single test."""
    return FakeCacheConfigDB()


# ---------------------------------------------------------------------------
# Fake penguin-dal `db` for shared.cache.semantic.SemanticCache tests.
# ---------------------------------------------------------------------------


class _FakeResponseCacheEntriesTable:
    """Field accessors + insert() for the fake `response_cache_entries` table."""

    org_id = _FakeField("org_id")
    model_class = _FakeField("model_class")
    context_hash = _FakeField("context_hash")
    expires_at = _FakeField("expires_at")
    id = _FakeField("id")

    def __init__(self, db: FakeSemanticDB) -> None:
        """Bind this table accessor to its owning `FakeSemanticDB`."""
        self._db = db

    def insert(self, **kwargs) -> int:
        """Append a new row to the owning db and return its assigned id."""
        new_id = self._db._next_id
        self._db._next_id += 1
        row = {"id": new_id, **kwargs}
        self._db.rows.append(row)
        return new_id


class _FakeSemanticSelectResult(list):
    """A list of matched rows supporting `.select()`/`.first()` chaining."""

    def select(self, *args, **kwargs):
        """Return self -- `db(query).select()` is a no-op filter step in this fake."""
        return self

    def first(self):
        """Return the first matched row, or None if there were no matches."""
        return self[0] if self else None


class _FakeSemanticUpdatableProxy(_FakeSemanticSelectResult):
    """_FakeSemanticSelectResult that also supports .update(**kwargs).

    Mutates the raw dict rows it was built from -- takes ``raw_rows``
    explicitly via __init__ rather than closing over an enclosing
    variable, so there's exactly one binding per instance.
    """

    def __init__(self, namespaced_rows: list, raw_rows: list) -> None:
        """Wrap the namespaced (read) rows and keep a reference to the raw (mutable) ones."""
        super().__init__(namespaced_rows)
        self._raw_rows = raw_rows

    def update(self, **kwargs) -> int:
        """Apply `kwargs` to every underlying raw row; returns the count updated."""
        for row in self._raw_rows:
            row.update(kwargs)
        return len(self._raw_rows)


class FakeSemanticDB:
    """Minimal in-memory stand-in for the penguin-dal `db` used by SemanticCache."""

    def __init__(self) -> None:
        """Initialize an empty `response_cache_entries` row store."""
        self.response_cache_entries = _FakeResponseCacheEntriesTable(self)
        self.rows: list[dict] = []
        self._next_id = 1
        self.commit_count = 0

    def seed(self, **row) -> None:
        """Insert a `response_cache_entries` row, assigning an id if not given."""
        row.setdefault("id", self._next_id)
        self._next_id = max(self._next_id, row["id"] + 1)
        self.rows.append(row)

    def commit(self) -> None:
        """Record a commit call (no-op storage-wise; this fake is always durable)."""
        self.commit_count += 1

    def __call__(self, query: _FakeCond):
        """Evaluate `query` against every seeded row and return an updatable proxy."""
        # Route .update()/.first() calls on db(query) (not db(query).select())
        # back into the same underlying dict rows for mutation.
        raw_matches = [r for r in self.rows if query(r)]
        matched = [SimpleNamespace(**r) for r in raw_matches]
        return _FakeSemanticUpdatableProxy(matched, raw_matches)


@pytest.fixture
def fake_semantic_db() -> FakeSemanticDB:
    """Return a fresh FakeSemanticDB instance for a single test."""
    return FakeSemanticDB()


class StubEmbedder:
    """Sync embedder stub: returns a fixed vector per exact text match, else a zero vector."""

    def __init__(self, vectors: dict | None = None, dimensions: int = 4) -> None:
        """Initialize with an optional text->vector map and the fallback zero-vector dimension."""
        self.vectors = vectors or {}
        self.dimensions = dimensions

    def embed(self, text: str):
        """Return the configured vector for `text`, or a zero vector if not configured."""
        if text in self.vectors:
            return list(self.vectors[text])
        return [0.0] * self.dimensions


@pytest.fixture
def stub_embedder() -> StubEmbedder:
    """Return a fresh StubEmbedder instance for a single test."""
    return StubEmbedder()
