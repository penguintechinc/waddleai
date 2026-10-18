"""The lease Lua against a real Valkey: reservation -> buffer -> deny, atomically, no preemption."""

import asyncio
import json

from shared.quota import keys
from shared.quota.lease import QuotaLimiter
from shared.quota.types import OverrideRule

BASE = {"team_id": 4, "cycle": 7, "day": "20260203", "daily_subcap": None, "overrides": ()}


def _req(user_id=1, credits=4, guaranteed=10, buffer_cap=5, **kw):
    """Keyword args for one reserve() call with sensible defaults."""
    return {**BASE, "user_id": user_id, "credits": credits, "guaranteed": guaranteed,
            "buffer_cap": buffer_cap, **kw}  # fmt: skip


async def _n(client, key):
    """Integer value of a counter (0 when absent)."""
    return int(await client.get(key) or 0)


async def _snapshot(client):
    """Every wa:quota:* string key with its VALUE, plus the reservation-index size."""
    state = {}
    async for key in client.scan_iter(match="wa:quota:*"):
        if await client.type(key) == "string":
            state[key] = await client.get(key)
    return state, await client.zcard(keys.RESERVATION_INDEX)


async def test_reservation_is_consumed_before_the_buffer(valkey_async):
    """Within the guarantee: from_reservation only, buffer untouched."""
    d = await QuotaLimiter(valkey_async).reserve(**_req())
    assert (d.allowed, d.from_reservation, d.from_buffer, d.degraded) == (True, 4, 0, False)
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 4
    assert await valkey_async.get(keys.buffer_counter(4, 7)) is None


async def test_spill_into_buffer_then_exhaustion_leaves_no_residue(valkey_async):
    """Guarantee 10: 4 + 8 spills 2 into the buffer; an uncoverable request is denied untouched."""
    limiter = QuotaLimiter(valkey_async)
    await limiter.reserve(**_req(credits=4))
    d = await limiter.reserve(**_req(credits=8))
    assert (d.allowed, d.from_reservation, d.from_buffer) == (True, 6, 2)
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 10
    assert await _n(valkey_async, keys.buffer_counter(4, 7)) == 2
    before = await _snapshot(valkey_async)
    denied = await limiter.reserve(**_req(credits=4))  # needs 4 from buffer, 2+4 > 5
    assert (denied.allowed, denied.reason, denied.reservation_id) == (
        False,
        "quota_exhausted",
        None,
    )
    assert await _snapshot(valkey_async) == before
    assert await _n(valkey_async, keys.buffer_counter(4, 7)) == 2


async def test_partial_debit_deny_leaves_reservation_counter_untouched(valkey_async):
    """Headroom 6 but the 2-credit spill exceeds the 1 free buffer credit: deny, nothing debited."""
    await valkey_async.set(keys.user_counter(1, 7), 4)
    await valkey_async.set(keys.buffer_counter(4, 7), 4)
    before = await _snapshot(valkey_async)
    d = await QuotaLimiter(valkey_async).reserve(**_req(credits=8, guaranteed=10, buffer_cap=5))
    assert (d.allowed, d.reason, d.reservation_id) == (False, "quota_exhausted", None)
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 4
    assert await _n(valkey_async, keys.buffer_counter(4, 7)) == 4
    assert await valkey_async.zcard(keys.RESERVATION_INDEX) == 0
    assert await _snapshot(valkey_async) == before


async def test_exact_buffer_boundary_is_allowed_one_more_is_not(valkey_async):
    """Remaining buffer exactly equals the need -> allowed; then 1 more credit -> denied."""
    limiter = QuotaLimiter(valkey_async)
    assert (await limiter.reserve(**_req(credits=5, guaranteed=0, buffer_cap=5))).allowed is True
    assert (
        await limiter.reserve(**_req(credits=1, guaranteed=0, buffer_cap=5))
    ).reason == "quota_exhausted"


async def test_leasing_never_touches_another_users_reservation(valkey_async):
    """No preemption: B (no guarantee) drains the buffer; A's guarantee is still fully available."""
    limiter = QuotaLimiter(valkey_async)
    assert (await limiter.reserve(**_req(user_id=2, credits=5, guaranteed=0, buffer_cap=5))).allowed
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 0
    a = await limiter.reserve(**_req(user_id=1, credits=10, guaranteed=10, buffer_cap=5))
    assert (a.allowed, a.from_reservation, a.from_buffer) == (True, 10, 0)


async def test_daily_subcap_denies_without_mutation(valkey_async):
    """A per-day sub-cap of 5: 3 ok, another 3 denied, counters show only the first 3."""
    limiter = QuotaLimiter(valkey_async)
    assert (await limiter.reserve(**_req(credits=3, daily_subcap=5))).allowed
    await valkey_async.set(keys.buffer_counter(4, 7), 2)
    before = await _snapshot(valkey_async)
    denied = await limiter.reserve(**_req(credits=3, daily_subcap=5))
    assert (denied.allowed, denied.reason) == (False, "daily_subcap_exceeded")
    assert await _n(valkey_async, keys.daily_counter(1, "20260203")) == 3
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 3
    assert await _n(valkey_async, keys.buffer_counter(4, 7)) == 2
    assert await valkey_async.zcard(keys.RESERVATION_INDEX) == 1
    assert await _snapshot(valkey_async) == before


async def test_override_is_checked_but_not_debited_at_reserve(valkey_async):
    """Override cap 5, 4 used: 3 more is denied (best-effort); a pass debits nothing."""
    limiter = QuotaLimiter(valkey_async)
    rule = OverrideRule(id=9, cap_credits=5, user_id=1)
    await valkey_async.set(keys.override_counter(9, 7), 4)
    await valkey_async.set(keys.buffer_counter(4, 7), 2)
    before = await _snapshot(valkey_async)
    denied = await limiter.reserve(**_req(credits=3, overrides=(rule,)))
    assert (denied.allowed, denied.reason) == (False, "override_exceeded")
    assert await valkey_async.get(keys.user_counter(1, 7)) is None
    assert await _n(valkey_async, keys.buffer_counter(4, 7)) == 2
    assert await valkey_async.zcard(keys.RESERVATION_INDEX) == 0
    assert await _snapshot(valkey_async) == before
    ok = await limiter.reserve(**_req(credits=1, overrides=(rule,)))
    assert ok.allowed and await _n(valkey_async, keys.override_counter(9, 7)) == 4


async def test_concurrent_reserves_never_over_lease_the_buffer(valkey_async):
    """30 users race 3 credits each at a 50-credit buffer: 16 win, 48 leased, no overshoot."""
    limiter = QuotaLimiter(valkey_async)
    results = await asyncio.gather(
        *[
            limiter.reserve(**_req(user_id=100 + i, credits=3, guaranteed=0, buffer_cap=50))
            for i in range(30)
        ]
    )
    assert sum(r.allowed for r in results) == 16
    assert await _n(valkey_async, keys.buffer_counter(4, 7)) == 48
    assert all(r.reason == "quota_exhausted" for r in results if not r.allowed)


async def test_reservation_record_carries_cycle_ttl_and_is_indexed(valkey_async):
    """The record stores its own cycle + counter keys (Bug B) and is in the expiry index."""
    d = await QuotaLimiter(valkey_async, clock=lambda: 1000.0).reserve(
        **_req(credits=6, guaranteed=4)
    )
    record = json.loads(await valkey_async.get(keys.reservation(d.reservation_id)))
    assert (record["cycle"], record["from_resv"], record["from_buf"]) == ("7", 4, 2)
    assert record["user_key"] == keys.user_counter(1, 7) and record[
        "buf_key"
    ] == keys.buffer_counter(4, 7)
    assert (
        0 < await valkey_async.ttl(keys.reservation(d.reservation_id)) <= QuotaLimiter.RECORD_TTL_S
    )
    assert await valkey_async.zscore(keys.RESERVATION_INDEX, d.reservation_id) == 4600.0


async def test_script_cache_flush_is_recovered_transparently(valkey_async):
    """SCRIPT FLUSH (or a Valkey restart) must not turn every request into a fail-open."""
    limiter = QuotaLimiter(valkey_async)
    assert (await limiter.reserve(**_req())).degraded is False
    await valkey_async.script_flush()
    again = await limiter.reserve(**_req(user_id=2))
    assert again.allowed and again.degraded is False and again.reservation_id


async def test_non_positive_credits_are_clamped_to_one(valkey_async):
    """A zero-credit reserve would be free; it is clamped to 1."""
    d = await QuotaLimiter(valkey_async).reserve(**_req(credits=0))
    assert d.from_reservation + d.from_buffer == 1
