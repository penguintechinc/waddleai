"""Auth-cache team plumbing and membership-change coherence (teams spec D3/D7).

Real ``RBACManager`` over a real schema (``team_world``), real ``ApiKeyAuthenticator`` and the
real in-process ``ApiKeyAuthCache`` -- the only fake is the absence of Valkey.
"""

from __future__ import annotations

import pytest
from passlib.hash import bcrypt

from proxy.apps.proxy_server.auth_cache import (
    ApiKeyAuthCache,
    ApiKeyAuthenticator,
    CachedKeyRecord,
)
from shared.auth.rbac import Permission, RBACManager

pytestmark = pytest.mark.asyncio

_SECRET = "wa-k1-secretvalue"  # noqa: S105 -- test fixture credential
_HASH = bcrypt.hash(_SECRET)
P = Permission


def _record(**over) -> CachedKeyRecord:
    """Build a CachedKeyRecord with team fields overridable."""
    base = {
        "key_record_id": 1, "key_hash": "h", "user_id": 5, "username": "u", "role": "user",
        "organization_id": 1, "managed_orgs": [], "user_enabled": True,
    }  # fmt: skip
    return CachedKeyRecord(**{**base, **over})


def _authenticator(world, user: str, team_key: str | None):
    """Seed key ``k1`` for ``user`` pinned to ``team_key`` and return (authenticator, cache)."""
    db = world.db
    db.api_keys.insert(
        key_id="k1", key_hash=_HASH, user_id=world.u[user],
        organization_id=db.users[world.u[user]].organization_id, name="k", enabled=True,
        team_id=world.t[team_key] if team_key else None,
    )  # fmt: skip
    db.commit()
    cache = ApiKeyAuthCache(valkey=None)
    return ApiKeyAuthenticator(rbac=RBACManager(db), cache=cache), cache


class TestCachedKeyRecordTeamFields:
    """The cached shape carries the resolved acting team and stays forward-compatible."""

    def test_round_trip_with_team(self):
        """to_dict/from_dict preserves team_id and team_role."""
        record = _record(team_id=3, team_role="admin")
        assert CachedKeyRecord.from_dict(record.to_dict()) == record

    def test_entry_written_by_a_pre_teams_replica_reads_as_teamless(self):
        """A payload with no team keys (older replica, rolling deploy) defaults to None/None."""
        payload = _record().to_dict()
        payload.pop("team_id")
        payload.pop("team_role")
        rebuilt = CachedKeyRecord.from_dict(payload)
        assert (rebuilt.team_id, rebuilt.team_role) == (None, None)


class TestTeamContextThroughTheAuthenticator:
    """Miss populates the team; a hit reproduces it without touching the DB."""

    async def test_miss_resolves_the_team_and_caches_it(self, team_world):
        """First auth: DB path resolves mia -> a1/member and the cache stores the same."""
        auth, cache = _authenticator(team_world, "mia", "a1")
        ctx = await auth.authenticate(_SECRET)
        assert (ctx.team_id, ctx.team_role) == (team_world.t["a1"], "member")
        cached = await cache.get("k1")
        assert isinstance(cached, CachedKeyRecord)
        assert (cached.team_id, cached.team_role) == (team_world.t["a1"], "member")

    async def test_hit_reproduces_team_scopes_without_a_db_query(self, team_world):
        """Second auth is served from cache: same team + scopes even if the DB is gone."""
        auth, _cache = _authenticator(team_world, "mia", "a1")
        await auth.authenticate(_SECRET)
        team_world.db(team_world.db.team_members.id > 0).delete()  # DB no longer knows the team
        ctx = await auth.authenticate(_SECRET)
        assert ctx.team_id == team_world.t["a1"]
        assert P.TEAM_USE in ctx.permissions


class TestMembershipChangeCoherence:
    """Review focus: a membership change is invisible until the cache entry is invalidated."""

    async def test_change_is_not_reflected_until_invalidation(self, team_world):
        """Promote mia to admin in the DB: stale cache says member; invalidate -> admin."""
        auth, cache = _authenticator(team_world, "mia", "a1")
        await auth.authenticate(_SECRET)
        db = team_world.db
        db(db.team_members.user_id == team_world.u["mia"]).update(team_role="admin")
        db.commit()

        stale = await auth.authenticate(_SECRET)
        assert stale.team_role == "member"  # bounded only by the cache TTL / invalidation

        await cache.invalidate("k1")
        fresh = await auth.authenticate(_SECRET)
        assert fresh.team_role == "admin"
        assert P.TEAM_MEMBER_MANAGE in fresh.permissions

    async def test_removal_then_invalidation_drops_the_acting_team(self, team_world):
        """Remove mia from a1: after invalidation her key acts teamless (no team scopes)."""
        auth, cache = _authenticator(team_world, "mia", "a1")
        await auth.authenticate(_SECRET)
        db = team_world.db
        db(db.team_members.user_id == team_world.u["mia"]).delete()
        db.commit()
        await cache.invalidate("k1")
        ctx = await auth.authenticate(_SECRET)
        assert ctx.team_id is None
        assert P.TEAM_USE not in ctx.permissions


class TestValkeyMissIsAuthoritative:
    """A reachable Valkey with no entry must not be answered from this process's local copy."""

    async def test_deleted_valkey_entry_is_a_miss_despite_a_local_copy(self):
        """Invalidation deletes only Valkey; the stale local write must not resurrect the entry."""
        from unittest.mock import AsyncMock, MagicMock

        valkey = MagicMock()
        valkey.get = AsyncMock(return_value=None)  # management already deleted it
        valkey.set = AsyncMock(return_value=True)
        cache = ApiKeyAuthCache(valkey=valkey)
        await cache.set("k1", _record(team_id=1, team_role="member"))  # also written locally

        assert await cache.get("k1") is None

    async def test_unreachable_valkey_still_falls_back_to_the_local_copy(self):
        """Outage degradation is unchanged: Valkey errors read from the local dict."""
        from unittest.mock import AsyncMock, MagicMock

        valkey = MagicMock()
        valkey.get = AsyncMock(side_effect=ConnectionError("valkey down"))
        valkey.set = AsyncMock(return_value=True)
        cache = ApiKeyAuthCache(valkey=valkey)
        record = _record(team_id=1, team_role="member")
        await cache.set("k1", record)

        assert await cache.get("k1") == record
