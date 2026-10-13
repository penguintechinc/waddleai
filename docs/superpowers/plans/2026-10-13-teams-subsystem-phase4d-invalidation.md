# Teams Subsystem — Phase 4d: Auth-Cache Invalidation on Membership Change

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §3 D7, §6 ("Membership freshness"), §12 ("Cache coherence").
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Phase 4b (the proxy auth cache bakes the resolved team per `key_id`).

The proxy caches `key_id -> {owner, role, resolved team}` for up to `PROXY_AUTH_CACHE_TTL_SECONDS` (60 s) in Valkey plus a process-local dict. A membership change makes every cached entry for the affected users wrong, so management deletes them. This phase also fixes a real coherence bug found while writing the cross-plane test: **the cache answered from its process-local copy after Valkey had been invalidated.**

---

<!-- TASK 14 -->
## Task 14: Invalidate every key of the affected users; make a Valkey miss authoritative

**Files:**
- Create: `services/management/app/services/auth_cache_invalidation.py`
- Modify: `proxy/apps/proxy_server/auth_cache.py` (`ApiKeyAuthCache._read`)
- Modify: `tests/unit/proxy/test_auth_cache_teams.py` (regression tests for the cache fix)
- Test: `tests/unit/management/test_auth_cache_invalidation.py` (create)

**Interfaces:**
- Consumes: `auth_cache_key`, `extensions.redis_client` (management's sync Valkey client, read at call time -- never bind it at import: it is `None` until `init_cache()` runs).
- Produces: `async invalidate_user_auth_cache(database, user_ids: Iterable[int]) -> int` -- deletes the cache entry of every `api_keys` row owned by those users in ONE multi-key `delete`; returns the number of keys targeted; **never raises** (a missing/unreachable cache logs a WARNING and the proxy's TTL bounds the staleness, exactly like `proxy_keys.py`'s revoke); an empty id list touches neither the DB nor the cache.
- Bug fix in `ApiKeyAuthCache._read`: previously a reachable Valkey that returned `None` (entry deleted by management) fell through to the proxy replica's **local** copy, resurrecting the invalidated entry for up to the local TTL -- so a revoked key or a changed membership stayed live. Now a reachable Valkey's miss is authoritative; the local dict is read only when Valkey errors (outage degradation is unchanged).
- Review Focus: `test_membership_change_reaches_the_proxy_after_invalidation` fails against the old `_read` and passes against the fix.

- [ ] **Step 1: Write the failing tests**

```python
"""Membership changes must invalidate the proxy auth cache (teams spec D7).

The proxy caches ``key_id -> {user, role, resolved team}`` (``auth_cache.py``). Management
deletes those entries via the shared Valkey on any team/membership/role change. Real database
(``team_world``); the Valkey is a dict-backed fake shared by the management (sync) and proxy
(async) clients so the test crosses the real plane boundary.
"""

from __future__ import annotations

import logging

import pytest
from passlib.hash import bcrypt

import services.management.app.extensions as ext_mod
from proxy.apps.proxy_server.auth_cache import ApiKeyAuthCache, ApiKeyAuthenticator
from services.management.app.services.auth_cache_invalidation import (
    invalidate_user_auth_cache,
)
from shared.auth.rbac import RBACManager, auth_cache_key

_SECRET = "wa-k1-secretvalue"  # noqa: S105 -- test fixture credential
_HASH = bcrypt.hash(_SECRET)


class _Store(dict):
    """The shared 'Valkey': one dict seen by both planes' clients."""


class _AsyncValkey:
    """The proxy's async client over the shared store."""

    def __init__(self, store: _Store) -> None:
        self.store = store

    async def get(self, key):
        """Return the stored value or None."""
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        """Store a value (TTL ignored by the fake)."""
        self.store[key] = value
        return True

    async def delete(self, *keys):
        """Delete keys, returning how many existed."""
        return sum(self.store.pop(k, None) is not None for k in keys)


class _SyncValkey:
    """Management's sync client over the same shared store."""

    def __init__(self, store: _Store) -> None:
        self.store = store

    def delete(self, *keys):
        """Delete keys, returning how many existed."""
        return sum(self.store.pop(k, None) is not None for k in keys)


def _add_key(world, user: str, key_id: str, team_key: str | None = "a1") -> None:
    """Insert an ``api_keys`` row for ``user`` (bound to ``team_key``)."""
    db = world.db
    db.api_keys.insert(
        key_id=key_id, key_hash=_HASH, user_id=world.u[user],
        organization_id=db.users[world.u[user]].organization_id, name=key_id, enabled=True,
        team_id=world.t[team_key] if team_key else None,
    )  # fmt: skip
    db.commit()


async def test_deletes_every_key_of_the_given_users_only(team_world, monkeypatch) -> None:
    """Mia's two keys are invalidated; nat's key (not in the list) is untouched."""
    store = _Store({auth_cache_key(k): "x" for k in ("m1", "m2", "n1")})
    monkeypatch.setattr(ext_mod, "redis_client", _SyncValkey(store))
    _add_key(team_world, "mia", "m1")
    _add_key(team_world, "mia", "m2")
    _add_key(team_world, "nat", "n1", "a2")

    count = await invalidate_user_auth_cache(team_world.db, [team_world.u["mia"]])

    assert count == 2
    assert set(store) == {auth_cache_key("n1")}


async def test_no_users_is_a_noop(team_world, monkeypatch) -> None:
    """An empty id list does no DB or cache work and reports zero."""
    store = _Store({auth_cache_key("m1"): "x"})
    monkeypatch.setattr(ext_mod, "redis_client", _SyncValkey(store))
    assert await invalidate_user_auth_cache(team_world.db, []) == 0
    assert auth_cache_key("m1") in store


async def test_missing_cache_client_warns_and_does_not_raise(
    team_world, monkeypatch, caplog
) -> None:
    """No Valkey configured: the write path must not fail; staleness falls back to the TTL."""
    monkeypatch.setattr(ext_mod, "redis_client", None)
    _add_key(team_world, "mia", "m1")
    with caplog.at_level(logging.WARNING):
        count = await invalidate_user_auth_cache(team_world.db, [team_world.u["mia"]])
    assert count == 1
    assert "no cache client" in caplog.text


async def test_cache_error_is_logged_not_raised(team_world, monkeypatch, caplog) -> None:
    """A Valkey outage during invalidation never fails the membership write."""

    class _Broken:
        def delete(self, *keys):
            raise ConnectionError("valkey down")

    monkeypatch.setattr(ext_mod, "redis_client", _Broken())
    _add_key(team_world, "mia", "m1")
    with caplog.at_level(logging.WARNING):
        await invalidate_user_auth_cache(team_world.db, [team_world.u["mia"]])
    assert "valkey down" in caplog.text


@pytest.fixture
def planes(team_world, monkeypatch):
    """Proxy authenticator + management invalidation wired to ONE shared fake Valkey."""
    store = _Store()
    monkeypatch.setattr(ext_mod, "redis_client", _SyncValkey(store))
    cache = ApiKeyAuthCache(valkey=_AsyncValkey(store))
    auth = ApiKeyAuthenticator(rbac=RBACManager(team_world.db), cache=cache)
    return auth, store


async def test_membership_change_reaches_the_proxy_after_invalidation(team_world, planes) -> None:
    """End to end across the plane boundary: stale until management invalidates, then fresh.

    Also the regression for the cache's local fallback: a Valkey miss after invalidation
    must be authoritative, not answered from the proxy process's own stale local copy.
    """
    auth, _store = planes
    _add_key(team_world, "mia", "k1")
    assert (await auth.authenticate(_SECRET)).team_role == "member"

    db = team_world.db
    db(db.team_members.user_id == team_world.u["mia"]).update(team_role="admin")
    db.commit()
    assert (await auth.authenticate(_SECRET)).team_role == "member"  # cached, not yet invalidated

    await invalidate_user_auth_cache(db, [team_world.u["mia"]])

    assert (await auth.authenticate(_SECRET)).team_role == "admin"
```

Add the cache-fix regressions to the proxy teams test file:

```diff
--- a/tests/unit/proxy/test_auth_cache_teams.py
+++ b/tests/unit/proxy/test_auth_cache_teams.py
@@ -114,3 +114,32 @@
         ctx = await auth.authenticate(_SECRET)
         assert ctx.team_id is None
         assert P.TEAM_USE not in ctx.permissions
+
+
+class TestValkeyMissIsAuthoritative:
+    """A reachable Valkey with no entry must not be answered from this process's local copy."""
+
+    async def test_deleted_valkey_entry_is_a_miss_despite_a_local_copy(self):
+        """Invalidation deletes only Valkey; the stale local write must not resurrect the entry."""
+        from unittest.mock import AsyncMock, MagicMock
+
+        valkey = MagicMock()
+        valkey.get = AsyncMock(return_value=None)  # management already deleted it
+        valkey.set = AsyncMock(return_value=True)
+        cache = ApiKeyAuthCache(valkey=valkey)
+        await cache.set("k1", _record(team_id=1, team_role="member"))  # also written locally
+
+        assert await cache.get("k1") is None
+
+    async def test_unreachable_valkey_still_falls_back_to_the_local_copy(self):
+        """Outage degradation is unchanged: Valkey errors read from the local dict."""
+        from unittest.mock import AsyncMock, MagicMock
+
+        valkey = MagicMock()
+        valkey.get = AsyncMock(side_effect=ConnectionError("valkey down"))
+        valkey.set = AsyncMock(return_value=True)
+        cache = ApiKeyAuthCache(valkey=valkey)
+        record = _record(team_id=1, team_role="member")
+        await cache.set("k1", record)
+
+        assert await cache.get("k1") == record
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `.venv/bin/python -m pytest tests/unit/management/test_auth_cache_invalidation.py tests/unit/proxy/test_auth_cache_teams.py --no-cov -q`
Expected: collection error `ModuleNotFoundError: No module named 'services.management.app.services.auth_cache_invalidation'`; and (once the module exists but before the cache fix) `test_membership_change_reaches_the_proxy_after_invalidation` FAILS with `assert 'member' == 'admin'` and `test_deleted_valkey_entry_is_a_miss_despite_a_local_copy` FAILS.

- [ ] **Step 3: Implement the invalidator, then the cache fix**

```python
"""Invalidate the proxy's API-key auth cache when team membership changes (teams spec D7).

The proxy caches ``key_id -> {owner, role, resolved acting team}`` in Valkey
(``proxy/apps/proxy_server/auth_cache.py``) for up to ``PROXY_AUTH_CACHE_TTL_SECONDS``.
A team/membership/role change makes every cached entry for the affected users wrong, so the
write path deletes them -- best-effort: a missing or unreachable cache is logged and the proxy's
own TTL becomes the staleness bound, exactly like ``proxy_keys.py``'s revoke.
"""

import asyncio
import logging
from collections.abc import Iterable

from penguin_dal.db import DB

from shared.auth.rbac import auth_cache_key

from .. import extensions as _ext

logger = logging.getLogger(__name__)


async def invalidate_user_auth_cache(database: DB, user_ids: Iterable[int]) -> int:
    """Delete the proxy auth-cache entry of every API key owned by ``user_ids``.

    Returns how many keys were targeted (zero for an empty id list, in which case neither the
    DB nor the cache is touched). Never raises: invalidation failure must not fail or roll back
    the membership write that triggered it.
    """
    ids = sorted({int(u) for u in user_ids})
    if not ids:
        return 0

    def _key_ids() -> list[str]:
        rows = database(database.api_keys.user_id.belongs(ids)).select(database.api_keys.key_id)
        return [str(r.key_id) for r in rows]

    key_ids = await asyncio.to_thread(_key_ids)
    if not key_ids:
        return 0

    redis_client = getattr(_ext, "redis_client", None)
    if redis_client is None:
        logger.warning(
            "teams: no cache client configured; auth-cache invalidation for %d key(s) skipped "
            "(bounded by the proxy's cache TTL instead)",
            len(key_ids),
        )
        return len(key_ids)
    try:
        await asyncio.to_thread(redis_client.delete, *[auth_cache_key(k) for k in key_ids])
    except Exception as exc:  # noqa: BLE001 -- cache outage must not fail the membership write
        logger.warning("teams: failed to invalidate %d auth-cache key(s): %s", len(key_ids), exc)
    return len(key_ids)
```

```diff
--- a/proxy/apps/proxy_server/auth_cache.py
+++ b/proxy/apps/proxy_server/auth_cache.py
@@ -280,6 +280,12 @@
                     # return bytes -- decode defensively rather than
                     # `str(b"...")`, which would wrap the bytes repr.
                     return value.decode() if isinstance(value, bytes) else str(value)
+                # A reachable Valkey that has no entry is AUTHORITATIVE: it is the store
+                # management's invalidation deletes from, so answering from this process's own
+                # local copy here would resurrect an entry that was just invalidated (a revoked
+                # key or a changed team membership) for up to the local TTL. The local dict is
+                # only a fallback for Valkey being unreachable, below.
+                return None
             except Exception as exc:  # noqa: BLE001 -- fail open to DB, never to "allowed"
                 logger.warning("Auth cache: Valkey read failed for key_id=%s: %s", key_id, exc)
         return self._read_local(key_id)
```

- [ ] **Step 4: Run them**

Run: `.venv/bin/python -m pytest tests/unit/management/test_auth_cache_invalidation.py tests/unit/proxy/test_auth_cache_teams.py tests/unit/proxy/test_auth_cache.py tests/unit/management/test_proxy_keys_routes.py --no-cov -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/services/auth_cache_invalidation.py proxy/apps/proxy_server/auth_cache.py tests/unit/proxy/test_auth_cache_teams.py tests/unit/management/test_auth_cache_invalidation.py
git commit -m "fix(teams): invalidate affected users' proxy auth cache; a Valkey miss is authoritative"
```

---

<!-- TASK 16 -->
## Task 15: Invalidate by `key_id` (used by revoke/rotate parity in Phase 5)

**Files:**
- Modify: `services/management/app/services/auth_cache_invalidation.py`
- Test: `tests/unit/management/test_auth_cache_invalidation.py` (append)

**Interfaces:**
- Produces: `async invalidate_key_ids(key_ids: Iterable[str]) -> int` (same never-raises, best-effort contract; empty input makes no cache call -- a bare `delete()` with no keys is a Valkey error); `invalidate_user_auth_cache` now delegates to it.

- [ ] **Step 1: Write the failing tests**

```diff
--- a/tests/unit/management/test_auth_cache_invalidation.py
+++ b/tests/unit/management/test_auth_cache_invalidation.py
@@ -16,6 +16,7 @@
 import services.management.app.extensions as ext_mod
 from proxy.apps.proxy_server.auth_cache import ApiKeyAuthCache, ApiKeyAuthenticator
 from services.management.app.services.auth_cache_invalidation import (
+    invalidate_key_ids,
     invalidate_user_auth_cache,
 )
 from shared.auth.rbac import RBACManager, auth_cache_key
@@ -146,3 +147,22 @@
     await invalidate_user_auth_cache(db, [team_world.u["mia"]])

     assert (await auth.authenticate(_SECRET)).team_role == "admin"
+
+
+async def test_invalidate_key_ids_deletes_exactly_those_keys(monkeypatch) -> None:
+    """Direct key-id invalidation (used by revoke/rotate parity) targets only the named keys."""
+    store = _Store({auth_cache_key(k): "x" for k in ("a", "b", "c")})
+    monkeypatch.setattr(ext_mod, "redis_client", _SyncValkey(store))
+    assert await invalidate_key_ids(["a", "c", "a"]) == 2
+    assert set(store) == {auth_cache_key("b")}
+
+
+async def test_invalidate_key_ids_empty_is_a_noop(monkeypatch) -> None:
+    """No ids -> no cache call at all (a bare ``delete()`` with no keys is a Valkey error)."""
+
+    class _Boom:
+        def delete(self, *keys):
+            raise AssertionError("must not be called")
+
+    monkeypatch.setattr(ext_mod, "redis_client", _Boom())
+    assert await invalidate_key_ids([]) == 0
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `.venv/bin/python -m pytest tests/unit/management/test_auth_cache_invalidation.py --no-cov -q`
Expected: collection error `ImportError: cannot import name 'invalidate_key_ids'`.

- [ ] **Step 3: Implement**

```diff
--- a/services/management/app/services/auth_cache_invalidation.py
+++ b/services/management/app/services/auth_cache_invalidation.py
@@ -20,6 +20,30 @@
 logger = logging.getLogger(__name__)


+async def invalidate_key_ids(key_ids: Iterable[str]) -> int:
+    """Delete the proxy auth-cache entry for each ``api_keys.key_id``; best-effort, never raises.
+
+    Returns how many keys were targeted. A missing cache client or a Valkey error is logged
+    and swallowed -- the proxy's own cache TTL then bounds the staleness.
+    """
+    ids = sorted({str(k) for k in key_ids})
+    if not ids:
+        return 0
+    redis_client = getattr(_ext, "redis_client", None)
+    if redis_client is None:
+        logger.warning(
+            "teams: no cache client configured; auth-cache invalidation for %d key(s) skipped "
+            "(bounded by the proxy's cache TTL instead)",
+            len(ids),
+        )
+        return len(ids)
+    try:
+        await asyncio.to_thread(redis_client.delete, *[auth_cache_key(k) for k in ids])
+    except Exception as exc:  # noqa: BLE001 -- cache outage must not fail the triggering write
+        logger.warning("teams: failed to invalidate %d auth-cache key(s): %s", len(ids), exc)
+    return len(ids)
+
+
 async def invalidate_user_auth_cache(database: DB, user_ids: Iterable[int]) -> int:
     """Delete the proxy auth-cache entry of every API key owned by ``user_ids``.

@@ -35,20 +59,4 @@
         rows = database(database.api_keys.user_id.belongs(ids)).select(database.api_keys.key_id)
         return [str(r.key_id) for r in rows]

-    key_ids = await asyncio.to_thread(_key_ids)
-    if not key_ids:
-        return 0
-
-    redis_client = getattr(_ext, "redis_client", None)
-    if redis_client is None:
-        logger.warning(
-            "teams: no cache client configured; auth-cache invalidation for %d key(s) skipped "
-            "(bounded by the proxy's cache TTL instead)",
-            len(key_ids),
-        )
-        return len(key_ids)
-    try:
-        await asyncio.to_thread(redis_client.delete, *[auth_cache_key(k) for k in key_ids])
-    except Exception as exc:  # noqa: BLE001 -- cache outage must not fail the membership write
-        logger.warning("teams: failed to invalidate %d auth-cache key(s): %s", len(key_ids), exc)
-    return len(key_ids)
+    return await invalidate_key_ids(await asyncio.to_thread(_key_ids))
```

- [ ] **Step 4: Run them**

Run: `.venv/bin/python -m pytest tests/unit/management/test_auth_cache_invalidation.py --no-cov -q`
Expected: `7 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/services/auth_cache_invalidation.py tests/unit/management/test_auth_cache_invalidation.py
git commit -m "feat(teams): invalidate the proxy auth cache by key id"
```

- [ ] **Phase 4 gate:** `.venv/bin/python -m pytest tests/unit/proxy tests/unit/management/test_auth_cache_invalidation.py tests/unit/management/test_auth_team_claims.py --no-cov -q` -- all pass.
