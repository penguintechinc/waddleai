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
