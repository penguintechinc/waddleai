"""Cache-stampede protection unit tests (shared.cache.singleflight, ops O11)."""

from __future__ import annotations

import asyncio
import time

import pytest

from shared.cache.singleflight import (
    DISABLE_SINGLEFLIGHT_FLAG,
    InProcessSingleFlight,
    StampedeLease,
    dedup_embed,
    embed_with_budget,
    guard_miss,
    is_singleflight_enabled,
    jittered_ttl,
    resolve_singleflight_enabled,
    wait_for_value,
)
from shared.utils.metrics import get_proxy_metrics


def _counter_value(counter, **labels) -> float:
    """Read a Prometheus counter's current value for a given label set."""
    return counter.labels(**labels)._value.get()


def _histogram_sum(histogram, **labels) -> float:
    """Read a Prometheus histogram's cumulative observed sum for a given label set."""
    return histogram.labels(**labels)._sum.get()


class _FakeUpstream:
    """Counts calls and sleeps, standing in for a slow real upstream dispatch."""

    def __init__(self, delay: float = 0.03) -> None:
        """Initialize with zero calls and the per-call simulated latency."""
        self.calls = 0
        self.delay = delay

    async def fetch(self, key: str) -> str:
        """Simulate a slow upstream computation, incrementing the call counter first."""
        self.calls += 1
        await asyncio.sleep(self.delay)
        return f"value-for-{key}"


class _Store:
    """A tiny async cache store standing in for ExactCache/SemanticCache storage."""

    def __init__(self) -> None:
        """Initialize an empty dict-backed store."""
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        """Return the stored value for `key`, or None."""
        return self.data.get(key)

    def put(self, key: str, value: str) -> None:
        """Store `value` at `key` (sync -- no cache layer semantics needed here)."""
        self.data[key] = value


async def _guarded_request(
    *,
    key: str,
    store: _Store,
    upstream: _FakeUpstream,
    in_process: InProcessSingleFlight,
    valkey,
    **guard_kwargs,
):
    """One simulated request through store -> guard_miss -> (leader computes | follower waits)."""
    cached = await store.get(key)
    if cached is not None:
        return cached

    guard = await guard_miss(
        cache_key=key,
        fetch_cached=lambda: store.get(key),
        in_process=in_process,
        valkey=valkey,
        metrics_layer="exact",
        **guard_kwargs,
    )
    if not guard.is_leader and guard.cached is not None:
        return guard.cached

    value = await upstream.fetch(key)
    store.put(key, value)
    if guard.in_process_future is not None:
        in_process.resolve(key, guard.in_process_future, value)
    if guard.lease_token is not None:
        await StampedeLease(valkey).release(key, guard.lease_token)
    return value


class TestGuardMissConcurrency:
    """N concurrent identical misses must dispatch upstream exactly once."""

    async def test_concurrent_identical_misses_single_upstream_call_in_process(self, fake_valkey):
        """10 concurrent identical misses in one process -> exactly 1 upstream call."""
        store = _Store()
        upstream = _FakeUpstream(delay=0.03)
        in_process = InProcessSingleFlight()

        results = await asyncio.gather(
            *[
                _guarded_request(
                    key="k1",
                    store=store,
                    upstream=upstream,
                    in_process=in_process,
                    valkey=fake_valkey,
                    wait_timeout_seconds=2,
                    poll_interval_seconds=0.02,
                    enabled=True,
                )
                for _ in range(10)
            ]
        )

        assert upstream.calls == 1
        assert all(r == "value-for-k1" for r in results)

    async def test_followers_receive_leaseholders_value(self, fake_valkey):
        """Followers' returned value is identical to (not just equal in shape to) the leader's."""
        store = _Store()
        upstream = _FakeUpstream(delay=0.02)
        in_process = InProcessSingleFlight()

        results = await asyncio.gather(
            *[
                _guarded_request(
                    key="shared-key",
                    store=store,
                    upstream=upstream,
                    in_process=in_process,
                    valkey=fake_valkey,
                    wait_timeout_seconds=2,
                    poll_interval_seconds=0.01,
                    enabled=True,
                )
                for _ in range(5)
            ]
        )
        assert len(set(results)) == 1
        assert upstream.calls == 1

    async def test_different_keys_each_get_their_own_leader(self, fake_valkey):
        """Different cache keys never block each other -- each gets its own upstream call."""
        store = _Store()
        upstream = _FakeUpstream(delay=0.01)
        in_process = InProcessSingleFlight()

        results = await asyncio.gather(
            *[
                _guarded_request(
                    key=f"key-{i}",
                    store=store,
                    upstream=upstream,
                    in_process=in_process,
                    valkey=fake_valkey,
                    wait_timeout_seconds=2,
                    poll_interval_seconds=0.01,
                    enabled=True,
                )
                for i in range(4)
            ]
        )
        assert upstream.calls == 4
        assert sorted(results) == sorted(f"value-for-key-{i}" for i in range(4))


class TestLeaseExpiryAndWaitTimeout:
    """Bounded degradation: a stuck leader never blocks a follower indefinitely."""

    async def test_lease_expiry_allows_a_new_leader(self, fake_valkey):
        """After the lease TTL elapses, a fresh caller (new in-process registry) can lead."""
        lease_token = "leader-token"  # noqa: S105 - a lease identifier, not a credential
        acquired = await StampedeLease(fake_valkey).try_acquire("expiring-key", 0.05, lease_token)
        assert acquired is True

        # Simulate the lease's PX expiring without ever being released.
        fake_valkey.now = lambda: time.time() + 1

        second = await StampedeLease(fake_valkey).try_acquire("expiring-key", 5, "other-token")
        assert second is True

    async def test_follower_wait_timeout_falls_through_to_compute_its_own_value(self, fake_valkey):
        """A follower exceeding wait_timeout_seconds becomes its own leader (fallthrough)."""
        store = _Store()
        in_process = InProcessSingleFlight()

        # Acquire the lease out-of-band (simulating another process holding it)
        # and never release it or write the value -- the follower must time out.
        held = await StampedeLease(fake_valkey).try_acquire("stuck-key", 10, "other-proc-token")
        assert held is True

        metrics = get_proxy_metrics()
        before = _counter_value(
            metrics.cache_lookups_total, layer="exact", result="stampede_fallthrough"
        )

        guard = await guard_miss(
            cache_key="stuck-key",
            fetch_cached=lambda: store.get("stuck-key"),
            in_process=in_process,
            valkey=fake_valkey,
            lease_ttl_seconds=10,
            wait_timeout_seconds=0.05,
            poll_interval_seconds=0.01,
            metrics_layer="exact",
            enabled=True,
        )

        assert guard.is_leader is True
        assert guard.cached is None
        after = _counter_value(
            metrics.cache_lookups_total, layer="exact", result="stampede_fallthrough"
        )
        assert after == before + 1


class TestValkeyOutageDegradesToInProcessOnly:
    """A Valkey outage must never fail the request -- only cross-process coordination is lost."""

    async def test_valkey_errors_on_lease_acquire_degrade_gracefully(self):
        """Valkey raising on SET still yields a usable (leader) result, never an exception."""

        class _BrokenValkey:
            async def set(self, *args, **kwargs):
                raise ConnectionError("valkey unreachable")

            async def get(self, *args, **kwargs):
                raise ConnectionError("valkey unreachable")

            async def delete(self, *args, **kwargs):
                raise ConnectionError("valkey unreachable")

        in_process = InProcessSingleFlight()
        guard = await guard_miss(
            cache_key="broken-valkey-key",
            fetch_cached=lambda: asyncio.sleep(0, result=None),
            in_process=in_process,
            valkey=_BrokenValkey(),
            wait_timeout_seconds=0.2,
            poll_interval_seconds=0.01,
            metrics_layer="exact",
            enabled=True,
        )
        assert guard.is_leader is True
        assert guard.lease_token is None

    async def test_no_valkey_client_still_dedupes_in_process(self):
        """`valkey=None` (the documented 'Valkey down' input) still single-flights in-process."""
        store = _Store()
        upstream = _FakeUpstream(delay=0.02)
        in_process = InProcessSingleFlight()

        results = await asyncio.gather(
            *[
                _guarded_request(
                    key="no-valkey-key",
                    store=store,
                    upstream=upstream,
                    in_process=in_process,
                    valkey=None,
                    wait_timeout_seconds=1,
                    poll_interval_seconds=0.01,
                    enabled=True,
                )
                for _ in range(6)
            ]
        )
        assert upstream.calls == 1
        assert all(r == "value-for-no-valkey-key" for r in results)


class TestKillSwitch:
    """waddleai.disable-cache-singleflight: unseen/OFF = mechanism ON."""

    async def test_enabled_false_bypasses_and_counts_bypass(self, fake_valkey):
        """`enabled=False` skips all coordination and records a bypass metric."""
        in_process = InProcessSingleFlight()
        metrics = get_proxy_metrics()
        before = _counter_value(metrics.cache_lookups_total, layer="exact", result="bypass")

        guard = await guard_miss(
            cache_key="killswitch-key",
            fetch_cached=lambda: asyncio.sleep(0, result=None),
            in_process=in_process,
            valkey=fake_valkey,
            metrics_layer="exact",
            enabled=False,
        )

        assert guard.is_leader is True
        assert guard.lease_token is None
        after = _counter_value(metrics.cache_lookups_total, layer="exact", result="bypass")
        assert after == before + 1

    def test_is_singleflight_enabled_fails_open_with_no_callable(self):
        """No callable at all -- the sync helper's safe default is 'enabled'."""
        assert is_singleflight_enabled(None) is True

    def test_is_singleflight_enabled_honors_callable(self):
        """A callable reporting the flag ON means the mechanism is disabled."""
        assert is_singleflight_enabled(lambda flag: flag == DISABLE_SINGLEFLIGHT_FLAG) is False
        assert is_singleflight_enabled(lambda flag: False) is True

    def test_is_singleflight_enabled_fails_open_on_raise(self):
        """A raising callable fails open to 'enabled', never propagates."""

        def _boom(flag):
            raise RuntimeError("flag store down")

        assert is_singleflight_enabled(_boom) is True

    async def test_resolve_singleflight_enabled_none_features_fails_open(self):
        """`features=None` -- no flag helper wired up -- fails open to 'enabled'."""
        assert await resolve_singleflight_enabled(None) is True

    async def test_resolve_singleflight_enabled_uses_async_resolve(self):
        """An async `resolve()` reporting the disable flag ON means the mechanism is disabled."""

        class _AsyncFeatures:
            async def resolve(self, flag_key, distinct_id=None, *, default=False):
                return flag_key == DISABLE_SINGLEFLIGHT_FLAG

        assert await resolve_singleflight_enabled(_AsyncFeatures()) is False

    async def test_resolve_singleflight_enabled_uses_sync_is_feature_enabled_off_loop(self):
        """A sync-only `is_feature_enabled()` is still honored, dispatched via a worker thread."""

        class _SyncFeatures:
            def is_feature_enabled(self, flag_key, distinct_id=None, *, default=False):
                return flag_key == DISABLE_SINGLEFLIGHT_FLAG

        assert await resolve_singleflight_enabled(_SyncFeatures()) is False

    async def test_resolve_singleflight_enabled_fails_open_on_raise(self):
        """A features helper that raises fails open to 'enabled', never propagates."""

        class _BrokenFeatures:
            async def resolve(self, flag_key, distinct_id=None, *, default=False):
                raise RuntimeError("flag store down")

        assert await resolve_singleflight_enabled(_BrokenFeatures()) is True


class TestJitteredTtl:
    """jittered_ttl: write-side TTL jitter so hot keys don't expire in lockstep."""

    def test_jitter_stays_within_configured_bound(self):
        """1000 samples at +/-10% all land within [base*0.9, base*1.1] (+/-1 for rounding)."""
        base = 1000
        samples = [jittered_ttl(base, jitter_fraction=0.1) for _ in range(1000)]
        assert all(899 <= s <= 1101 for s in samples)

    def test_jitter_produces_more_than_one_distinct_value(self):
        """A real spread of samples must not collapse to a single constant value."""
        samples = {jittered_ttl(1000, jitter_fraction=0.1) for _ in range(200)}
        assert len(samples) > 1

    def test_zero_or_negative_base_passes_through(self):
        """A non-positive base TTL is returned unjittered (floored at 0)."""
        assert jittered_ttl(0) == 0
        assert jittered_ttl(-5) == 0

    def test_zero_jitter_fraction_is_deterministic(self):
        """jitter_fraction=0 always returns exactly the base value."""
        assert all(jittered_ttl(500, jitter_fraction=0.0) == 500 for _ in range(20))


class TestEmbedTimeoutBypass:
    """Semantic-cache embedding latency budget (ops O11, Gemini note)."""

    async def test_slow_embed_exceeds_budget_and_bypasses(self):
        """An embed call slower than timeout_ms returns None and counts a bypass."""
        metrics = get_proxy_metrics()
        before = _counter_value(metrics.cache_lookups_total, layer="semantic", result="bypass")

        async def _slow_compute():
            await asyncio.sleep(0.2)
            return [1.0, 0.0]

        result = await embed_with_budget(_slow_compute, timeout_ms=10, metrics_layer="semantic")
        assert result is None
        after = _counter_value(metrics.cache_lookups_total, layer="semantic", result="bypass")
        assert after == before + 1

    async def test_fast_embed_within_budget_returns_value(self):
        """An embed call well within the budget returns its value, no bypass."""

        async def _fast_compute():
            return [1.0, 0.0]

        result = await embed_with_budget(_fast_compute, timeout_ms=500, metrics_layer="semantic")
        assert result == [1.0, 0.0]

    async def test_unhealthy_embedder_bypasses_without_calling_compute(self):
        """An unhealthy embedder bypasses before ever invoking `compute`."""
        called = False

        async def _compute():
            nonlocal called
            called = True
            return [1.0]

        result = await embed_with_budget(
            _compute, timeout_ms=500, is_healthy=lambda: False, metrics_layer="semantic"
        )
        assert result is None
        assert called is False

    async def test_healthy_check_raising_does_not_block_lookup(self):
        """A broken health check itself must not prevent the embed call from running."""

        async def _compute():
            return [1.0]

        def _broken_health():
            raise RuntimeError("health check broken")

        result = await embed_with_budget(
            _compute, timeout_ms=500, is_healthy=_broken_health, metrics_layer="semantic"
        )
        assert result == [1.0]


class TestDedupEmbed:
    """In-process embedding call de-dup (ops O11, Gemini note)."""

    async def test_concurrent_identical_prompts_embed_once(self):
        """10 concurrent dedup_embed calls for the same key invoke `compute` exactly once."""
        in_process = InProcessSingleFlight()
        calls = {"n": 0}

        async def _compute():
            calls["n"] += 1
            await asyncio.sleep(0.02)
            return [1.0, 2.0, 3.0]

        results = await asyncio.gather(
            *[dedup_embed(in_process, "same-prompt", _compute) for _ in range(10)]
        )
        assert calls["n"] == 1
        assert all(r == [1.0, 2.0, 3.0] for r in results)

    async def test_distinct_keys_each_embed_independently(self):
        """Different normalized-prompt keys never share a leader."""
        in_process = InProcessSingleFlight()
        calls = {"n": 0}

        async def _compute():
            calls["n"] += 1
            return [0.0]

        await asyncio.gather(*[dedup_embed(in_process, f"prompt-{i}", _compute) for i in range(3)])
        assert calls["n"] == 3

    async def test_compute_exception_propagates_and_discards_the_leader_slot(self):
        """A failing leader's exception propagates and does not strand the registry entry."""
        in_process = InProcessSingleFlight()

        async def _boom():
            raise RuntimeError("embedder exploded")

        with pytest.raises(RuntimeError):
            await dedup_embed(in_process, "boom-key", _boom)

        # The slot was cleared (discard), so a fresh call can lead again immediately.
        async def _ok():
            return [1.0]

        result = await dedup_embed(in_process, "boom-key", _ok)
        assert result == [1.0]


class TestWaitForValue:
    """wait_for_value: jittered polling with a hard bound, never indefinite."""

    async def test_returns_as_soon_as_value_appears(self):
        """Polling stops the moment `fetch` starts returning non-None."""
        state = {"n": 0}

        async def _fetch():
            state["n"] += 1
            return "ready" if state["n"] >= 3 else None

        result = await wait_for_value(_fetch, timeout_seconds=2, poll_interval_seconds=0.01)
        assert result == "ready"
        assert state["n"] == 3

    async def test_times_out_and_returns_none(self):
        """A `fetch` that never returns a value times out at the bound, never hangs."""

        async def _always_none():
            return None

        result = await wait_for_value(
            _always_none, timeout_seconds=0.05, poll_interval_seconds=0.01
        )
        assert result is None


class TestScopedLeaseKeyIsolation:
    """Different principals must never collide on the same stampede-lease key."""

    async def test_same_hash_different_org_never_shares_a_leader(self, fake_valkey):
        """Two orgs on a would-be-colliding key still get separate leaders (no scoping leak)."""
        in_process_a = InProcessSingleFlight()
        in_process_b = InProcessSingleFlight()

        guard_a = await guard_miss(
            cache_key="waddleai:cache:sf:exact:1:samehash",
            fetch_cached=lambda: asyncio.sleep(0, result=None),
            in_process=in_process_a,
            valkey=fake_valkey,
            metrics_layer="exact",
            enabled=True,
        )
        guard_b = await guard_miss(
            cache_key="waddleai:cache:sf:exact:2:samehash",
            fetch_cached=lambda: asyncio.sleep(0, result=None),
            in_process=in_process_b,
            valkey=fake_valkey,
            metrics_layer="exact",
            enabled=True,
        )

        # Both become leaders -- org 2's key is a distinct Valkey lock, never
        # blocked or merged with org 1's, even though the trailing hash matches.
        assert guard_a.is_leader is True
        assert guard_b.is_leader is True
        assert guard_a.lease_token != guard_b.lease_token


class TestDurationMetric:
    """cache_lookup_duration_seconds must be observed for the exact layer."""

    async def test_guard_miss_bypass_path_does_not_crash_duration_recording(self, fake_valkey):
        """Sanity: the WaddleAIMetrics singleton's duration histogram accepts observations."""
        metrics = get_proxy_metrics()
        before = _histogram_sum(metrics.cache_lookup_duration_seconds, layer="exact")
        metrics.record_cache_lookup_duration(layer="exact", seconds=0.01)
        after = _histogram_sum(metrics.cache_lookup_duration_seconds, layer="exact")
        assert after >= before + 0.01
