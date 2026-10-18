"""The sweeper loop driving a real QuotaLimiter against a real Valkey."""

import asyncio

from proxy.apps.proxy_server.quota_sweeper import run_quota_sweeper
from shared.quota import keys
from shared.quota.lease import QuotaLimiter

BASE = {"team_id": 4, "cycle": 7, "day": "20260203", "daily_subcap": None, "overrides": (),
        "user_id": 1, "credits": 4, "guaranteed": 10, "buffer_cap": 5}  # fmt: skip


async def _run_until(limiter, predicate):
    """Run the loop at a fast interval until predicate() is true (bounded), then stop it."""
    stop = asyncio.Event()
    task = asyncio.create_task(run_quota_sweeper(limiter, interval_s=0.02, stop=stop))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if await predicate():
            break
    stop.set()
    await asyncio.wait_for(task, timeout=1)


async def test_loop_refunds_expired_reservation(valkey_async):
    """An expired abandoned reservation is refunded by the loop (no charge)."""
    clock = {"t": 1000.0}
    limiter = QuotaLimiter(valkey_async, clock=lambda: clock["t"])
    d = await limiter.reserve(**BASE)
    assert int(await valkey_async.get(keys.user_counter(1, 7))) == 4
    clock["t"] = 5000.0  # past the 3600s index score

    async def refunded():
        return await valkey_async.zscore(keys.RESERVATION_INDEX, d.reservation_id) is None

    await _run_until(limiter, refunded)
    assert int(await valkey_async.get(keys.user_counter(1, 7)) or 0) == 0


async def test_loop_leaves_live_reservation_alone(valkey_async):
    """A reservation whose score is still in the future is untouched."""
    limiter = QuotaLimiter(valkey_async, clock=lambda: 1000.0)
    d = await limiter.reserve(**BASE)

    async def never():
        return False

    stop = asyncio.Event()
    task = asyncio.create_task(run_quota_sweeper(limiter, interval_s=0.02, stop=stop))
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert int(await valkey_async.get(keys.user_counter(1, 7))) == 4
    assert await valkey_async.zscore(keys.RESERVATION_INDEX, d.reservation_id) is not None
