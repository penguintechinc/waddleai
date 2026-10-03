"""Unit tests for proxy/apps/proxy_server/auth_cache.py.

release-audit-2026-10-02 O7-a/O11: the proxy's auth hot path used to run a
synchronous PyDAL query + bcrypt verify + a synchronous `last_used` write
inline on the Hypercorn event loop, with no cache in front of the DB read.
These tests cover the fix: `ApiKeyAuthCache` (Valkey-backed with an
in-process fallback) and `ApiKeyAuthenticator` (cache-fronted,
executor-offloaded authentication, debounced `last_used`, and the
`waddleai.disable-auth-cache` kill switch).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from passlib.hash import bcrypt

from proxy.apps.proxy_server.auth_cache import (
    AUTH_CACHE_DISABLE_FLAG,
    ApiKeyAuthCache,
    ApiKeyAuthenticator,
    CachedKeyRecord,
    get_auth_executor,
    run_in_auth_executor,
)
from shared.auth.rbac import AuthenticationError, Role

pytestmark = pytest.mark.asyncio


def _make_record(
    key_record_id: int = 100, user_id: int = 5, key_hash: str | None = None
) -> CachedKeyRecord:
    """Build a CachedKeyRecord with a real bcrypt hash of 'wa-abc123-realsecret'."""
    return CachedKeyRecord(
        key_record_id=key_record_id,
        key_hash=key_hash or bcrypt.hash("wa-abc123-realsecret"),
        user_id=user_id,
        username="api_user",
        role="user",
        organization_id=1,
        managed_orgs=[],
        user_enabled=True,
    )


class _StubFeatureFlags:
    """Minimal stand-in for FeatureFlagsHelper: returns a fixed bool from resolve()."""

    def __init__(self, value: bool) -> None:
        self.value = value
        self.calls: list[tuple[str, str | None]] = []

    async def resolve(
        self, flag_key: str, distinct_id: str | None = None, *, default: bool = False
    ) -> bool:
        self.calls.append((flag_key, distinct_id))
        return self.value


# ---------------------------------------------------------------------------
# CachedKeyRecord
# ---------------------------------------------------------------------------


class TestCachedKeyRecord:
    """Serialization round trip -- the shape stored in Valkey/local fallback."""

    def test_round_trip(self):
        """to_dict/from_dict round-trips to an equal record."""
        record = _make_record()
        payload = record.to_dict()
        rebuilt = CachedKeyRecord.from_dict(payload)
        assert rebuilt == record

    def test_from_dict_normalizes_missing_managed_orgs(self):
        """A payload missing managed_orgs/user_enabled defaults them sensibly."""
        rebuilt = CachedKeyRecord.from_dict(
            {
                "key_record_id": 1,
                "key_hash": "x",
                "user_id": 1,
                "username": "u",
                "role": "user",
                "organization_id": 1,
            }
        )
        assert rebuilt.managed_orgs == []
        assert rebuilt.user_enabled is True


# ---------------------------------------------------------------------------
# ApiKeyAuthCache
# ---------------------------------------------------------------------------


class TestApiKeyAuthCacheNoValkey:
    """Valkey unavailable (None): every operation still works via the in-process fallback."""

    async def test_miss_on_empty_cache(self):
        """An empty cache reports a miss (None), not an error."""
        cache = ApiKeyAuthCache(valkey=None)
        assert await cache.get("unknown") is None

    async def test_set_then_get_hits(self):
        """A record written with set() is read back unchanged by get()."""
        cache = ApiKeyAuthCache(valkey=None)
        record = _make_record()
        await cache.set("abc123", record)
        result = await cache.get("abc123")
        assert result == record

    async def test_negative_cache_round_trip(self):
        """A negative-cached key_id reads back as True, not a record."""
        cache = ApiKeyAuthCache(valkey=None)
        await cache.set_negative("ghost-key")
        assert await cache.get("ghost-key") is True

    async def test_ttl_expiry(self):
        """An entry past its TTL reads back as a miss, not a stale hit.

        Uses an injected ``clock`` -- the cache's own sanctioned test seam --
        rather than monkeypatching the global ``time`` module: every
        timestamp the cache ever reads or writes goes through the same fake
        clock from construction onward, so there is no real-wall-clock value
        baked into any entry for the fake clock to disagree with later.
        """
        fake_now = [1000.0]
        cache = ApiKeyAuthCache(valkey=None, ttl_seconds=10.0, clock=lambda: fake_now[0])
        record = _make_record()

        await cache.set("abc123", record)

        fake_now[0] += 5.0  # still within TTL
        assert await cache.get("abc123") == record

        fake_now[0] += 10.0  # now past TTL
        assert await cache.get("abc123") is None

    async def test_invalidate_drops_local_entry(self):
        """invalidate() removes the in-process fallback entry too."""
        cache = ApiKeyAuthCache(valkey=None)
        record = _make_record()
        await cache.set("abc123", record)
        await cache.invalidate("abc123")
        assert await cache.get("abc123") is None


class TestApiKeyAuthCacheWithValkey:
    """Valkey present: reads/writes go through it, with the local dict as a parallel fallback."""

    def _mock_valkey(self) -> MagicMock:
        valkey = MagicMock()
        valkey.get = AsyncMock(return_value=None)
        valkey.set = AsyncMock(return_value=True)
        valkey.delete = AsyncMock(return_value=1)
        return valkey

    async def test_set_writes_to_valkey_with_ttl(self):
        """set() writes to Valkey under the shared key name with the configured TTL."""
        valkey = self._mock_valkey()
        cache = ApiKeyAuthCache(valkey=valkey, ttl_seconds=60.0)
        record = _make_record()

        await cache.set("abc123", record)

        valkey.set.assert_awaited_once()
        args, kwargs = valkey.set.await_args
        assert args[0] == "waddleai:auth:apikey:abc123"
        assert kwargs["ex"] == 60

    async def test_get_prefers_valkey_value(self):
        """get() returns the record Valkey holds, decoded from its JSON payload."""
        valkey = self._mock_valkey()
        record = _make_record()
        import json

        valkey.get = AsyncMock(return_value=json.dumps({"kind": "record", **record.to_dict()}))
        cache = ApiKeyAuthCache(valkey=valkey)

        result = await cache.get("abc123")
        assert result == record

    async def test_valkey_read_failure_falls_back_to_local(self):
        """A Valkey outage on read never raises -- falls through to the local copy, or a miss."""
        valkey = self._mock_valkey()
        valkey.get = AsyncMock(side_effect=ConnectionError("valkey down"))
        cache = ApiKeyAuthCache(valkey=valkey)

        # Never cached anywhere: a clean miss, not an exception.
        assert await cache.get("abc123") is None

        # Written once (local write always happens even if the Valkey write
        # also failed) -- readable from local even while Valkey stays down.
        cache2 = ApiKeyAuthCache(valkey=self._mock_valkey())
        cache2.valkey.get = AsyncMock(side_effect=ConnectionError("valkey down"))
        record = _make_record()
        cache2.valkey.set = AsyncMock(side_effect=ConnectionError("valkey down"))
        await cache2.set("abc123", record)
        assert await cache2.get("abc123") == record

    async def test_valkey_write_failure_does_not_raise(self):
        """A Valkey error on write is swallowed; the local fallback still gets written."""
        valkey = self._mock_valkey()
        valkey.set = AsyncMock(side_effect=ConnectionError("valkey down"))
        cache = ApiKeyAuthCache(valkey=valkey)

        await cache.set("abc123", _make_record())  # must not raise

    async def test_malformed_json_payload_reads_as_a_miss(self):
        """Garbage bytes in Valkey (corruption, a format change) degrade to a miss, not a crash."""
        valkey = self._mock_valkey()
        valkey.get = AsyncMock(return_value="not valid json{{{")
        cache = ApiKeyAuthCache(valkey=valkey)

        assert await cache.get("abc123") is None

    async def test_malformed_record_shape_reads_as_a_miss(self):
        """Valid JSON missing required record fields degrades to a miss, not an exception."""
        import json

        valkey = self._mock_valkey()
        valkey.get = AsyncMock(return_value=json.dumps({"kind": "record"}))  # missing fields
        cache = ApiKeyAuthCache(valkey=valkey)

        assert await cache.get("abc123") is None

    async def test_invalidate_deletes_from_valkey(self):
        """invalidate() issues a Valkey DELETE for the key's cache entry."""
        valkey = self._mock_valkey()
        cache = ApiKeyAuthCache(valkey=valkey)

        await cache.invalidate("abc123")

        valkey.delete.assert_awaited_once_with("waddleai:auth:apikey:abc123")

    async def test_invalidate_failure_does_not_raise(self):
        """A Valkey error on invalidate is swallowed, not raised."""
        valkey = self._mock_valkey()
        valkey.delete = AsyncMock(side_effect=ConnectionError("valkey down"))
        cache = ApiKeyAuthCache(valkey=valkey)

        await cache.invalidate("abc123")  # must not raise


# ---------------------------------------------------------------------------
# ApiKeyAuthenticator
# ---------------------------------------------------------------------------


def _mock_rbac(key_record=None, user=None, found=None) -> MagicMock:
    """Build an RBACManager double wired for the three methods ApiKeyAuthenticator calls."""
    rbac = MagicMock()
    rbac.fetch_key_and_user.return_value = found
    rbac.touch_api_key_last_used.return_value = None

    def _build_user_context(
        *, user_id, username, role, organization_id, managed_orgs, api_key_id=None
    ):
        from shared.auth.rbac import ROLE_PERMISSIONS, UserContext

        role_enum = Role(role)
        return UserContext(
            user_id=user_id,
            username=username,
            role=role_enum,
            organization_id=organization_id,
            managed_orgs=managed_orgs or [],
            permissions=ROLE_PERMISSIONS.get(role_enum, set()),
            api_key_id=api_key_id,
        )

    rbac.build_user_context.side_effect = _build_user_context
    return rbac


class TestApiKeyAuthenticatorCacheHit:
    """A cache hit verifies bcrypt but never touches the DB."""

    async def test_hit_returns_context_without_db_call(self):
        """A cache hit builds a UserContext purely from the cached record."""
        secret = "wa-abc123-realsecret"  # noqa: S105 -- test fixture credential
        record = _make_record(key_hash=bcrypt.hash(secret))
        cache = ApiKeyAuthCache(valkey=None)
        await cache.set("abc123", record)

        rbac = _mock_rbac()
        metrics = MagicMock()
        auth = ApiKeyAuthenticator(rbac=rbac, cache=cache, metrics=metrics)

        context = await auth.authenticate(secret)

        assert context.user_id == 5
        rbac.fetch_key_and_user.assert_not_called()
        metrics.record_auth_lookup.assert_called_once()
        assert metrics.record_auth_lookup.call_args.args[0] == "hit"

    async def test_hit_with_wrong_secret_raises(self):
        """A cache hit still rejects an incorrect secret."""
        record = _make_record(key_hash=bcrypt.hash("wa-abc123-realsecret"))
        cache = ApiKeyAuthCache(valkey=None)
        await cache.set("abc123", record)

        auth = ApiKeyAuthenticator(rbac=_mock_rbac(), cache=cache)

        with pytest.raises(AuthenticationError):
            await auth.authenticate("wa-abc123-wrongsecret")


class TestApiKeyAuthenticatorCacheMiss:
    """A cache miss hits the DB exactly once and populates the cache for next time."""

    async def test_miss_normalizes_csv_managed_orgs(self):
        """PyDAL's comma-separated managed_orgs string normalizes to a list[int] when cached."""
        secret = "wa-abc123-realsecret"  # noqa: S105 -- test fixture credential
        key_record = MagicMock(id=100, key_hash=bcrypt.hash(secret))
        user = MagicMock(
            id=5,
            username="api_user",
            role="resource_manager",
            organization_id=1,
            managed_orgs="2, 3",
            enabled=True,
        )
        rbac = _mock_rbac(found=(key_record, user))
        cache = ApiKeyAuthCache(valkey=None)
        auth = ApiKeyAuthenticator(rbac=rbac, cache=cache)

        context = await auth.authenticate(secret)

        assert context.managed_orgs == [2, 3]
        cached = await cache.get("abc123")
        assert isinstance(cached, CachedKeyRecord)
        assert cached.managed_orgs == [2, 3]

    async def test_miss_populates_cache(self):
        """A cache miss resolves via the DB once, then caches the record for next time."""
        secret = "wa-abc123-realsecret"  # noqa: S105 -- test fixture credential
        key_hash = bcrypt.hash(secret)
        key_record = MagicMock(id=100, key_hash=key_hash)
        user = MagicMock(
            id=5,
            username="api_user",
            role="user",
            organization_id=1,
            managed_orgs=None,
            enabled=True,
        )
        rbac = _mock_rbac(found=(key_record, user))
        cache = ApiKeyAuthCache(valkey=None)
        metrics = MagicMock()
        auth = ApiKeyAuthenticator(rbac=rbac, cache=cache, metrics=metrics)

        context = await auth.authenticate(secret)

        assert context.user_id == 5
        rbac.fetch_key_and_user.assert_called_once_with("abc123")
        assert metrics.record_auth_lookup.call_args.args[0] == "miss"
        # A database RED metric fires for the lookup that actually hit the DB.
        metrics.record_database_operation.assert_called_once()
        db_call = metrics.record_database_operation.call_args
        assert db_call.args[:2] == ("select", "api_keys")
        assert db_call.kwargs["success"] is True

        # Second call for the same key_id is now a cache hit -- no second DB call.
        rbac.fetch_key_and_user.reset_mock()
        await auth.authenticate(secret)
        rbac.fetch_key_and_user.assert_not_called()

    async def test_unknown_key_id_negative_caches(self):
        """An unknown key_id is negative-cached so a repeat guess skips the DB."""
        rbac = _mock_rbac(found=None)
        cache = ApiKeyAuthCache(valkey=None)
        auth = ApiKeyAuthenticator(rbac=rbac, cache=cache)

        with pytest.raises(AuthenticationError):
            await auth.authenticate("wa-ghost-anything")

        rbac.fetch_key_and_user.assert_called_once_with("ghost")

        # Second attempt on the same unknown key_id is served from the
        # negative cache -- no second DB call.
        rbac.fetch_key_and_user.reset_mock()
        with pytest.raises(AuthenticationError):
            await auth.authenticate("wa-ghost-anything")
        rbac.fetch_key_and_user.assert_not_called()

    async def test_wrong_secret_on_a_real_key_id_still_caches_the_record(self):
        """A cached record survives a wrong secret -- it carries the hash, not the secret.

        The record still gets cached because it carries the already-hashed
        secret, which lets a later *correct* secret authenticate from cache.
        """
        real_hash = bcrypt.hash("wa-abc123-realsecret")
        key_record = MagicMock(id=100, key_hash=real_hash)
        user = MagicMock(
            id=5,
            username="api_user",
            role="user",
            organization_id=1,
            managed_orgs=None,
            enabled=True,
        )
        rbac = _mock_rbac(found=(key_record, user))
        cache = ApiKeyAuthCache(valkey=None)
        auth = ApiKeyAuthenticator(rbac=rbac, cache=cache)

        with pytest.raises(AuthenticationError):
            await auth.authenticate("wa-abc123-wrongsecret")

        # The now-cached record lets a *correct* secret authenticate on the
        # very next call with no further DB hit.
        rbac.fetch_key_and_user.reset_mock()
        context = await auth.authenticate("wa-abc123-realsecret")
        assert context.user_id == 5
        rbac.fetch_key_and_user.assert_not_called()


class TestApiKeyAuthenticatorKillSwitch:
    """waddleai.disable-auth-cache: bypasses the cache, never the executor offload."""

    async def test_flag_on_bypasses_cache_entirely(self):
        """The DB is hit on every call while the flag is on; the cache stays empty."""
        secret = "wa-abc123-realsecret"  # noqa: S105 -- test fixture credential
        key_hash = bcrypt.hash(secret)
        key_record = MagicMock(id=100, key_hash=key_hash)
        user = MagicMock(
            id=5,
            username="api_user",
            role="user",
            organization_id=1,
            managed_orgs=None,
            enabled=True,
        )
        rbac = _mock_rbac(found=(key_record, user))
        cache = ApiKeyAuthCache(valkey=None)
        features = _StubFeatureFlags(value=True)
        metrics = MagicMock()
        auth = ApiKeyAuthenticator(rbac=rbac, cache=cache, metrics=metrics, features=features)

        await auth.authenticate(secret)
        await auth.authenticate(secret)

        # Every call hits the DB -- the cache is never populated while bypassed.
        assert rbac.fetch_key_and_user.call_count == 2
        assert await cache.get("abc123") is None
        assert metrics.record_auth_lookup.call_args.args[0] == "bypass"
        assert features.calls == [(AUTH_CACHE_DISABLE_FLAG, "server")] * 2

    async def test_flag_off_uses_cache(self):
        """The default (flag unseen/OFF) keeps the cache mechanism on."""
        secret = "wa-abc123-realsecret"  # noqa: S105 -- test fixture credential
        key_hash = bcrypt.hash(secret)
        key_record = MagicMock(id=100, key_hash=key_hash)
        user = MagicMock(
            id=5,
            username="api_user",
            role="user",
            organization_id=1,
            managed_orgs=None,
            enabled=True,
        )
        rbac = _mock_rbac(found=(key_record, user))
        cache = ApiKeyAuthCache(valkey=None)
        features = _StubFeatureFlags(value=False)
        auth = ApiKeyAuthenticator(rbac=rbac, cache=cache, features=features)

        await auth.authenticate(secret)
        rbac.fetch_key_and_user.reset_mock()
        await auth.authenticate(secret)

        rbac.fetch_key_and_user.assert_not_called()


class TestApiKeyAuthenticatorLastUsedDebounce:
    """last_used is written at most once per key per interval, never inline."""

    async def test_rapid_successive_auths_touch_last_used_once(self):
        """Five authentications inside the debounce window write last_used exactly once."""
        secret = "wa-abc123-realsecret"  # noqa: S105 -- test fixture credential
        record = _make_record(key_record_id=100, key_hash=bcrypt.hash(secret))
        cache = ApiKeyAuthCache(valkey=None)
        await cache.set("abc123", record)

        rbac = _mock_rbac()
        auth = ApiKeyAuthenticator(rbac=rbac, cache=cache, last_used_interval_seconds=60.0)

        for _ in range(5):
            await auth.authenticate(secret)

        # Background tasks are fire-and-forget; give them one loop turn.
        await asyncio.sleep(0.05)  # let the fire-and-forget executor touch task finish

        assert rbac.touch_api_key_last_used.call_count == 1
        rbac.touch_api_key_last_used.assert_called_with(100)

    async def test_touch_failure_is_logged_not_raised(self):
        """A failing background last_used write never surfaces to the caller or breaks auth."""
        secret = "wa-abc123-realsecret"  # noqa: S105 -- test fixture credential
        record = _make_record(key_record_id=100, key_hash=bcrypt.hash(secret))
        cache = ApiKeyAuthCache(valkey=None)
        await cache.set("abc123", record)

        rbac = _mock_rbac()
        rbac.touch_api_key_last_used.side_effect = RuntimeError("db unreachable")
        metrics = MagicMock()
        auth = ApiKeyAuthenticator(rbac=rbac, cache=cache, metrics=metrics)

        context = await auth.authenticate(secret)  # must not raise despite the background failure
        assert context.user_id == 5

        await asyncio.sleep(0.05)  # let the fire-and-forget executor touch task finish

        metrics.record_database_operation.assert_any_call("update", "api_keys", success=False)

    async def test_touch_after_interval_elapses_again(self):
        """A second authentication after the debounce interval elapses writes last_used again.

        Uses a single injected ``clock`` shared by both `cache` and `auth`
        (the sanctioned test seam -- see `ApiKeyAuthCache`/`ApiKeyAuthenticator`
        docstrings), constructed *before* `cache.set()` runs. Never
        monkeypatches the global `time` module: doing so after a cache
        entry has already been written against the real wall clock mixes a
        real timestamp with a frozen fake one, and whether the frozen value
        reads as "already expired" then depends on the test host's real
        monotonic uptime at the moment `cache.set()` ran -- which is exactly
        what made the original version of this test flake in CI (it passed
        on a long-uptime dev machine, failed on a fresh low-uptime
        container where `expires_at` landed below the frozen fake `now`).
        """
        secret = "wa-abc123-realsecret"  # noqa: S105 -- test fixture credential
        record = _make_record(key_record_id=100, key_hash=bcrypt.hash(secret))

        fake_now = [2000.0]
        clock = lambda: fake_now[0]  # noqa: E731 -- shared mutable-cell clock, a def buys nothing here

        cache = ApiKeyAuthCache(valkey=None, clock=clock)
        await cache.set("abc123", record)

        rbac = _mock_rbac()
        auth = ApiKeyAuthenticator(
            rbac=rbac, cache=cache, clock=clock, last_used_interval_seconds=1.0
        )

        await auth.authenticate(secret)
        await asyncio.sleep(0.05)  # let the fire-and-forget executor touch task finish

        fake_now[0] += 2.0  # past the 1s debounce window (well within the 60s cache TTL)
        await auth.authenticate(secret)
        await asyncio.sleep(0.05)  # let the fire-and-forget executor touch task finish

        assert rbac.touch_api_key_last_used.call_count == 2


# ---------------------------------------------------------------------------
# Executor offload -- the actual event-loop-blocking fix
# ---------------------------------------------------------------------------


class TestExecutorOffload:
    """Proves bcrypt/DB work genuinely leaves the event loop free, not just 'awaited'."""

    async def test_run_in_auth_executor_does_not_block_other_coroutines(self):
        """A slow blocking call offloaded via run_in_auth_executor lets other coroutines run."""
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.01)

        def slow_blocking_call():
            time.sleep(0.2)
            return "done"

        ticker_task = asyncio.create_task(ticker())
        result = await run_in_auth_executor(slow_blocking_call)
        ticks_during_block = ticks

        assert result == "done"
        # The event loop kept running the ticker while the executor thread
        # slept -- if the blocking call ran inline, `ticks` would still be 0
        # or 1 by the time run_in_auth_executor returned.
        assert ticks_during_block >= 5

        stop = True
        ticker_task.cancel()
        try:
            await ticker_task
        except asyncio.CancelledError:
            pass

    async def test_concurrent_authenticate_calls_do_not_serialize_on_bcrypt(self):
        """Two concurrent authenticate() calls for different keys both complete promptly.

        A bounded executor with >1 worker means two independent bcrypt
        verifications run in parallel rather than queueing behind each
        other on a single thread.
        """
        workers = get_auth_executor()._max_workers
        if workers < 2:
            pytest.skip("auth executor configured with a single worker")

        secret_a = "wa-aaa111-secreta"  # noqa: S105 -- test fixture credential
        secret_b = "wa-bbb222-secretb"  # noqa: S105 -- test fixture credential
        record_a = _make_record(key_record_id=1, user_id=1, key_hash=bcrypt.hash(secret_a))
        record_b = _make_record(key_record_id=2, user_id=2, key_hash=bcrypt.hash(secret_b))
        cache = ApiKeyAuthCache(valkey=None)
        await cache.set("aaa111", record_a)
        await cache.set("bbb222", record_b)

        auth = ApiKeyAuthenticator(rbac=_mock_rbac(), cache=cache)

        start = time.monotonic()
        await asyncio.gather(auth.authenticate(secret_a), auth.authenticate(secret_b))
        elapsed = time.monotonic() - start

        # Two real bcrypt verifies (tens of ms each) run concurrently; this
        # is a generous ceiling, not a tight latency assertion.
        assert elapsed < 2.0
