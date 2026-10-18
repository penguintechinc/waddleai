"""Spec §10 flag-on E2E: real Valkey, real schema DB, real engine/stage/pipeline."""

import asyncio
from datetime import date
from types import SimpleNamespace

import redis.asyncio as aredis

from proxy.apps.proxy_server.pipeline.quota_stage import QuotaStage
from proxy.apps.proxy_server.pipeline.stages import PipelineContext, ProxyPipeline, Stage
from proxy.apps.proxy_server.quota_hooks import reconcile_quota
from services.management.app.services import quota_pools as qp
from shared.quota import keys
from shared.quota.engine import QuotaEngine
from shared.quota.lease import QuotaLimiter
from tests.integration.quota.world import MSG, build_world, user_of


async def _n(client, key):
    """Integer value of a counter (0 when absent)."""
    return int(await client.get(key) or 0)


async def _burn(w, name, count):
    """``count`` sequential reserves for ``name``; returns the outcomes."""
    return [await w.engine.reserve(user_of(w, name), "gpt-4o", MSG) for _ in range(count)]


async def test_exhaust_reservation_lease_buffer_then_429_and_other_reservations_survive(
    team_world, valkey_async
):
    """mia: 5 x 4 from her 20 guarantee, 2 x 4 leased from the 10 buffer."""
    w = build_world(team_world, valkey_async, credits=4)
    outcomes = await _burn(w, "mia", 8)
    assert [o.reservation.from_buffer if o.reservation else None for o in outcomes] == [0] * 5 + [
        4,
        4,
        None,
    ]
    assert outcomes[7].blocked and outcomes[7].reason == "quota_exhausted"
    assert await _n(valkey_async, keys.user_counter(w.u["mia"], 0)) == 20
    assert await _n(valkey_async, keys.buffer_counter(w.team, 0)) == 8
    olive = await w.engine.reserve(
        user_of(w, "olive"), "gpt-4o", MSG
    )  # buffer is nearly empty, her guarantee is not
    assert olive.blocked is False and (
        olive.reservation.from_reservation,
        olive.reservation.from_buffer,
    ) == (4, 0)
    adam = await w.engine.reserve(
        user_of(w, "adam"), "gpt-4o", MSG
    )  # no reservation: buffer only, 8+4 > 10
    assert adam.blocked is True


async def test_reconcile_refunds_and_the_ledger_records_the_final_split(team_world, valkey_async):
    """Actual 1 < reserved 4: the counter drops to 1 and the durable ledger shows mia: 1 in."""
    w = build_world(team_world, valkey_async, credits=4)
    out = await w.engine.reserve(user_of(w, "mia"), "gpt-4o", MSG)
    await w.engine.reconcile(out.reservation, actual_credits=1, provider="openai", model="gpt-4o")
    assert await _n(valkey_async, keys.user_counter(w.u["mia"], 0)) == 1
    usage = {(e.scope_type, e.scope_id): e.credits for e in w.ledger.usage(w.org, 0)}
    assert usage == {("user", w.u["mia"]): 1}


async def test_cycle_boundary_restores_capacity_and_leaves_the_old_cycle_alone(
    team_world, valkey_async
):
    """Day 30 starts cycle 1: mia is allowed again from a fresh reservation; cycle-0 counter."""
    w = build_world(team_world, valkey_async, credits=4)
    await _burn(w, "mia", 8)
    assert (await w.engine.reserve(user_of(w, "mia"), "gpt-4o", MSG)).blocked is True
    w.clock.value = date(2026, 1, 31)  # (Jan 31 - Jan 1) = 30 days -> cycle 1
    fresh = await w.engine.reserve(user_of(w, "mia"), "gpt-4o", MSG)
    assert (
        fresh.blocked is False
        and fresh.reservation.cycle == 1
        and fresh.reservation.from_buffer == 0
    )
    assert await _n(valkey_async, keys.user_counter(w.u["mia"], 0)) == 20
    assert await _n(valkey_async, keys.user_counter(w.u["mia"], 1)) == 4


async def test_reconcile_after_rollover_still_charges_the_reservations_cycle(
    team_world, valkey_async
):
    """Reserved in cycle 0, clock moves to cycle 1, reconcile still adjusts cycle 0."""
    w = build_world(team_world, valkey_async, credits=4)
    out = await w.engine.reserve(user_of(w, "olive"), "gpt-4o", MSG)
    w.clock.value = date(2026, 1, 31)
    await w.engine.reconcile(out.reservation, actual_credits=2, provider="openai", model="gpt-4o")
    assert await _n(valkey_async, keys.user_counter(w.u["olive"], 0)) == 2
    assert await valkey_async.get(keys.user_counter(w.u["olive"], 1)) is None
    assert [(e.scope_id, e.credits) for e in w.ledger.usage(w.org, 0)] == [(w.u["olive"], 2)]


async def test_provider_override_blocks_the_next_request_after_reconcile(team_world, valkey_async):
    """Override cap 6 on openai: admitted, reconciled at 7 (over cap, recorded)."""
    w = build_world(team_world, valkey_async, credits=4)
    first = await w.engine.reserve(user_of(w, "olive"), "gpt-4o", MSG)  # primes the config cache
    qp.create_override(w.db, w.org, user_id=None, model=None, provider="openai", cap_credits=6)
    await valkey_async.delete(
        keys.org_config(w.org)
    )  # what invalidate_quota_config does after the edit
    result = await w.engine.reconcile(
        first.reservation, actual_credits=7, provider="openai", model="gpt-4o"
    )
    assert result.over_cap_override_ids and result.applied
    nxt = await w.engine.reserve(user_of(w, "olive"), "gpt-4o", MSG)
    assert (nxt.blocked, nxt.reason) == (True, "override_exceeded")
    assert (
        await _n(valkey_async, keys.user_counter(w.u["olive"], 0)) == 7
    )  # the denial mutated nothing


async def test_valkey_outage_fails_open_without_a_reservation(team_world, valkey_async):
    """D10: the limiter pointed at a dead Valkey allows the request and holds nothing."""
    w = build_world(team_world, valkey_async, credits=4)
    dead = aredis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    engine = QuotaEngine(
        QuotaLimiter(dead), w.store, w.ledger, w.tm, None, today=lambda: w.clock.value
    )
    out = await engine.reserve(user_of(w, "mia"), "gpt-4o", MSG)
    assert (out.blocked, out.reservation) == (False, None)
    await dead.aclose()


async def test_unconfigured_org_writes_nothing_to_valkey(team_world, valkey_async):
    """Disabled org: reserve is a strict no-op -- no counter."""
    w = build_world(team_world, valkey_async, credits=4)
    qp.set_org_pool(w.db, w.org, cycle_anchor_date=None, pool_credits=None, enabled=False)
    await valkey_async.delete(keys.org_config(w.org))
    out = await w.engine.reserve(user_of(w, "mia"), "gpt-4o", MSG)
    assert (out.blocked, out.reservation) == (False, None)
    assert [k async for k in valkey_async.scan_iter(match="wa:quota:user:*")] == []
    assert [k async for k in valkey_async.scan_iter(match="wa:quota:resv*")] == []


class _Features:
    """Flag helper stub: every flag on."""

    def is_feature_enabled(self, flag, distinct_id=None, **kw):
        return True


class _Blocker(Stage):
    """A later stage that blocks (stands in for security_out)."""

    async def __call__(self, ctx):
        ctx.blocked, ctx.status_code, ctx.block_reason = True, 400, "blocked downstream"
        return ctx


def _ctx(w, name):
    """A pipeline context for ``name`` asking for gpt-4o."""
    return PipelineContext(user=user_of(w, name), body={}, model="gpt-4o", messages=MSG)


async def test_stage_blocks_with_429_and_a_downstream_block_refunds_the_reservation(
    team_world, valkey_async
):
    """Review-focus 4 with real counters: admit."""
    w = build_world(team_world, valkey_async, credits=4)
    stage = QuotaStage("quota", w.engine, _Features())
    ctx = await ProxyPipeline([stage, _Blocker("sec", None)], None).run(_ctx(w, "mia"))
    assert ctx.blocked and ctx.block_reason == "blocked downstream"
    assert await _n(valkey_async, keys.user_counter(w.u["mia"], 0)) == 0  # refunded, not leaked
    await _burn(w, "mia", 7)  # 5 from the guarantee + 2 buffer leases
    denied = await ProxyPipeline([stage], None).run(_ctx(w, "mia"))
    assert (denied.blocked, denied.status_code) == (True, 429)
    assert denied.block_reason == "Token quota exhausted for the current billing cycle"


async def test_finalize_hook_settles_the_reservation_exactly_once(team_world, valkey_async):
    """reconcile_quota adjusts to the actual credits and clears the hooks; a second call is."""
    w = build_world(team_world, valkey_async, credits=4)
    ctx = await ProxyPipeline([QuotaStage("quota", w.engine, _Features())], None).run(
        _ctx(w, "mia")
    )
    assert ctx.quota_reservation is not None
    await reconcile_quota(w.engine, ctx, waddleai_tokens=2, provider="openai", model="gpt-4o")
    await reconcile_quota(w.engine, ctx, waddleai_tokens=2, provider="openai", model="gpt-4o")
    assert await _n(valkey_async, keys.user_counter(w.u["mia"], 0)) == 2
    assert (ctx.quota_reservation, ctx.quota_release) == (None, None)


class _FlagsOff:
    """Flag helper stub: every flag off."""

    def is_feature_enabled(self, flag, distinct_id=None, **kw):
        """Report the flag as off."""
        return False


async def _all_keys(client):
    """Every key currently in Valkey (the harness flushes per test, so this is the whole db)."""
    return [k async for k in client.scan_iter(match="*")]


async def test_flag_off_is_a_strict_noop_with_zero_valkey_writes(team_world, valkey_async):
    """Review-focus 2: flag OFF -> admitted, never 429, and Valkey holds ZERO keys afterwards."""
    w = build_world(team_world, valkey_async, credits=4)
    stage = QuotaStage("quota", w.engine, _FlagsOff())
    for _ in range(12):  # far more than mia's guarantee + the buffer would ever allow
        ctx = await ProxyPipeline([stage], None).run(_ctx(w, "mia"))
        assert ctx.blocked is False and ctx.status_code != 429 and ctx.quota_reservation is None
    assert await valkey_async.dbsize() == 0
    assert await _all_keys(valkey_async) == []


async def test_teamless_user_is_a_noop_with_zero_valkey_writes(team_world, valkey_async):
    """A user with no team is never metered -- no quota key of any kind is written."""
    w = build_world(team_world, valkey_async, credits=4)
    qp.set_org_pool(w.db, w.org, cycle_anchor_date=None, pool_credits=None, enabled=False)
    await valkey_async.delete(keys.org_config(w.org))
    loner = SimpleNamespace(user_id=w.u["mia"], organization_id=w.org, team_id=None)
    for _ in range(5):
        out = await w.engine.reserve(loner, "gpt-4o", MSG)
        assert (out.blocked, out.reservation) == (False, None)
    assert [
        k for k in await _all_keys(valkey_async) if k.startswith("wa:quota:") and "config" not in k
    ] == []


async def test_future_anchor_enforces_nothing_and_writes_no_counters(team_world, valkey_async):
    """Before the cycle anchor nothing is enforced and no consumption counter moves."""
    w = build_world(team_world, valkey_async, credits=4)
    qp.set_org_pool(
        w.db, w.org, cycle_anchor_date=date(2026, 3, 1), pool_credits=None, enabled=None
    )
    await valkey_async.delete(keys.org_config(w.org))
    outcomes = await _burn(w, "mia", 12)  # would be blocked from the 8th request if enforced
    assert all(o.blocked is False for o in outcomes)
    assert await _n(valkey_async, keys.user_counter(w.u["mia"], 0)) == 0
    assert await _n(valkey_async, keys.buffer_counter(w.team, 0)) == 0


async def test_concurrent_race_on_the_last_credits_never_over_grants(team_world, valkey_async):
    """30 parallel reserves at adam (buffer only, 10 credits, 4 each) grant at most 2."""
    w = build_world(team_world, valkey_async, credits=4)
    outcomes = await asyncio.gather(
        *[w.engine.reserve(user_of(w, "adam"), "gpt-4o", MSG) for _ in range(30)]
    )
    granted = [o for o in outcomes if not o.blocked]
    assert len(granted) == 2
    assert sum(o.reservation.from_buffer for o in granted) == 8
    assert (
        await _n(valkey_async, keys.buffer_counter(w.team, 0)) == 8
    )  # denied calls left no residue
