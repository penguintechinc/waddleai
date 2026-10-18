"""Reconcile/release/sweep against a real Valkey: refunds, overage, stored-cycle accounting."""

from shared.quota import keys
from shared.quota.lease import QuotaLimiter

BASE = {"team_id": 4, "cycle": 7, "day": "20260203", "daily_subcap": None, "overrides": ()}


def _req(user_id=1, credits=4, guaranteed=10, buffer_cap=5, **kw):
    """Keyword args for one reserve() call."""
    return {**BASE, "user_id": user_id, "credits": credits, "guaranteed": guaranteed,
            "buffer_cap": buffer_cap, **kw}  # fmt: skip


async def _n(client, key):
    """Integer value of a counter (0 when absent)."""
    return int(await client.get(key) or 0)


async def test_actual_below_reserved_refunds_buffer_first(valkey_async):
    """Guarantee 5, reserve 8 (5 resv + 3 buf), actual 6: 2 credits return to the BUFFER."""
    limiter = QuotaLimiter(valkey_async)
    d = await limiter.reserve(**_req(credits=8, guaranteed=5, buffer_cap=9))
    r = await limiter.reconcile(d.reservation_id, 6)
    assert (r.applied, r.from_reservation, r.from_buffer, r.overage) == (True, 5, 1, 0)
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 5
    assert await _n(valkey_async, keys.buffer_counter(4, 7)) == 1
    assert await valkey_async.get(keys.reservation(d.reservation_id)) is None
    assert await valkey_async.zscore(keys.RESERVATION_INDEX, d.reservation_id) is None


async def test_actual_above_reserved_takes_remaining_guarantee_first(valkey_async):
    """Reserve 4 of a 10 guarantee, actual 9: the 5 extra come from the guarantee."""
    limiter = QuotaLimiter(valkey_async)
    d = await limiter.reserve(**_req(credits=4))
    r = await limiter.reconcile(d.reservation_id, 9)
    assert (r.from_reservation, r.from_buffer, r.overage) == (9, 0, 0)
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 9
    assert await valkey_async.get(keys.buffer_counter(4, 7)) is None


async def test_buffer_overage_is_recorded_not_blocked(valkey_async):
    """Underestimated request: buffer may exceed its cap; overage reports the excess."""
    limiter = QuotaLimiter(valkey_async)
    d = await limiter.reserve(**_req(credits=2, guaranteed=0, buffer_cap=3))
    r = await limiter.reconcile(d.reservation_id, 6)  # delta 4 -> buffer 6 vs cap 3
    assert (r.applied, r.from_buffer, r.overage) == (True, 6, 3)
    assert await _n(valkey_async, keys.buffer_counter(4, 7)) == 6
    denied = await limiter.reserve(**_req(user_id=2, credits=1, guaranteed=0, buffer_cap=3))
    assert denied.reason == "quota_exhausted"  # the NEXT request is blocked


async def test_release_refunds_everything_and_never_goes_negative(valkey_async):
    """Full refund returns counters to zero; an expired counter is not recreated."""
    limiter = QuotaLimiter(valkey_async)
    d = await limiter.reserve(**_req(credits=8, guaranteed=5, buffer_cap=9))
    r = await limiter.release(d.reservation_id)
    assert (r.applied, r.from_reservation, r.from_buffer) == (True, 0, 0)
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 0
    assert await _n(valkey_async, keys.buffer_counter(4, 7)) == 0
    d2 = await limiter.reserve(**_req(user_id=3, credits=4))
    await valkey_async.delete(keys.user_counter(3, 7))  # counter expired meanwhile
    await limiter.release(d2.reservation_id)
    assert await valkey_async.get(keys.user_counter(3, 7)) is None


async def test_reconcile_charges_the_reservations_own_cycle_not_a_later_one(valkey_async):
    """Reserved in cycle 7, another lands in cycle 8: reconcile still charges 7."""
    limiter = QuotaLimiter(valkey_async)
    first = await limiter.reserve(**_req(credits=4, cycle=7))
    await limiter.reserve(**{**_req(credits=2), "cycle": 8})
    await limiter.reconcile(first.reservation_id, 1)
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 1
    assert await _n(valkey_async, keys.user_counter(1, 8)) == 2


async def test_override_counters_are_debited_actual_and_over_cap_is_reported(valkey_async):
    """Override caps are authoritative at reconcile: debit actual, report over-cap ids."""
    limiter = QuotaLimiter(valkey_async)
    d = await limiter.reserve(**_req(credits=4))
    r = await limiter.reconcile(d.reservation_id, 12, [(5, 10), (6, 0)])
    assert r.over_cap_override_ids == [5]  # cap 0 = unlimited, never reported
    assert await _n(valkey_async, keys.override_counter(5, 7)) == 12
    d2 = await limiter.reserve(**_req(credits=1))
    r2 = await limiter.reconcile(d2.reservation_id, 1, [(5, 10)])
    assert (
        r2.over_cap_override_ids == [5]
        and await _n(valkey_async, keys.override_counter(5, 7)) == 13
    )


async def test_daily_counter_follows_the_actual(valkey_async):
    """Daily sub-cap counter is adjusted by the same delta: reserve 10, actual 4 -> 4."""
    limiter = QuotaLimiter(valkey_async)
    d = await limiter.reserve(**_req(credits=10, daily_subcap=100))
    await limiter.reconcile(d.reservation_id, 4)
    assert await _n(valkey_async, keys.daily_counter(1, "20260203")) == 4


async def test_unknown_reservation_is_not_applied_and_creates_nothing(valkey_async):
    """Orphan reconcile (record expired/swept): applied False, no stray keys."""
    r = await QuotaLimiter(valkey_async).reconcile("does-not-exist", 5, [(5, 10)])
    assert r.applied is False
    assert [k async for k in valkey_async.scan_iter(match="wa:quota:*")] == []


async def test_sweep_refunds_only_expired_reservations(valkey_async):
    """Index score = reserve time + 3600: a sweep before expiry does nothing, after it refunds."""
    limiter = QuotaLimiter(valkey_async, clock=lambda: 1000.0)
    d = await limiter.reserve(**_req(credits=4))
    assert await limiter.sweep_expired(now=2000.0) == 0
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 4
    assert await limiter.sweep_expired(now=5000.0) == 1
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 0
    assert await valkey_async.zscore(keys.RESERVATION_INDEX, d.reservation_id) is None
    assert await limiter.sweep_expired(now=9999.0) == 0


async def test_sweep_drops_index_entries_whose_record_is_gone(valkey_async):
    """An index entry with no record is removed and not counted as a refund."""
    await valkey_async.zadd(keys.RESERVATION_INDEX, {"ghost": 1.0})
    assert await QuotaLimiter(valkey_async).sweep_expired(now=10.0) == 0
    assert await valkey_async.zscore(keys.RESERVATION_INDEX, "ghost") is None


async def test_sweep_refunds_lost_reply_orphan(valkey_async):
    """Reply lost after a server-side apply: the sweep still refunds the orphan."""
    limiter = QuotaLimiter(valkey_async, clock=lambda: 1000.0)
    await limiter.reserve(**_req(credits=8, guaranteed=5, buffer_cap=9))  # caller discards the id
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 5
    assert await _n(valkey_async, keys.buffer_counter(4, 7)) == 3
    assert await limiter.sweep_expired(now=5000.0) == 1
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 0
    assert await _n(valkey_async, keys.buffer_counter(4, 7)) == 0
    assert await valkey_async.zcard(keys.RESERVATION_INDEX) == 0


async def test_double_reconcile_and_release_never_double_refund(valkey_async):
    """Second call on a settled reservation is a no-op: a neighbour's credits are untouched."""
    limiter = QuotaLimiter(valkey_async)
    a = await limiter.reserve(**_req(credits=4))
    await limiter.reserve(**_req(credits=3))  # same counter, another reservation
    assert (await limiter.reconcile(a.reservation_id, 1)).applied is True
    assert (await limiter.reconcile(a.reservation_id, 0)).applied is False
    assert (await limiter.release(a.reservation_id)).applied is False
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 1 + 3


async def test_swept_then_reconciled_is_not_applied(valkey_async):
    """Sweep refunds first; a late reconcile must not charge or refund again."""
    limiter = QuotaLimiter(valkey_async, clock=lambda: 1000.0)
    d = await limiter.reserve(**_req(credits=4))
    assert await limiter.sweep_expired(now=5000.0) == 1
    assert (await limiter.reconcile(d.reservation_id, 4, [(5, 10)])).applied is False
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 0
    assert await valkey_async.get(keys.override_counter(5, 7)) is None


async def test_refund_floors_at_zero(valkey_async):
    """A counter already drained below the stored amount floors at 0, never negative."""
    limiter = QuotaLimiter(valkey_async)
    d = await limiter.reserve(**_req(credits=4))
    await valkey_async.set(keys.user_counter(1, 7), 1)  # drifted below the reserved 4
    await limiter.release(d.reservation_id)
    assert await _n(valkey_async, keys.user_counter(1, 7)) == 0
