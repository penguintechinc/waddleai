# ruff: noqa: D102, D105, D107, N818, E501  -- tiny fakes; the test docstrings carry the intent
"""QuotaEngine end to end over a real Valkey lease: no-op writes nothing, fail-open, cache refund."""

from datetime import date

import redis.asyncio as aredis

from shared.quota.engine import QuotaEngine
from shared.quota.lease import QuotaLimiter
from shared.quota.types import OrgQuotaConfig, TeamQuotaConfig, UserReservation

TODAY = date(2026, 2, 3)
ANCHOR = date(2026, 1, 1)
MSG = [{"role": "user", "content": "hello " * 40}]


class User:
    """Minimal UserContext view."""

    def __init__(self, team_id=4):
        self.user_id, self.organization_id, self.team_id = 7, 2, team_id


class Store:
    """Fixed config."""

    def __init__(self, org, team):
        self.org, self.team = org, team

    async def get_org(self, org_id):
        return self.org

    async def get_team(self, team_id):
        return self.team


class TM:
    """Fixed-credit token manager."""

    def rates_stale(self):
        return False

    def estimate_credits(self, model, i, o):
        return 10

    def providers_for_model(self, model):
        return frozenset()


class Ledger:
    """Records adds."""

    def __init__(self):
        self.rows = []

    def add(self, org_id, cycle, entries):
        self.rows.append((org_id, cycle, list(entries)))


ORG = OrgQuotaConfig(2, True, ANCHOR, 1000)
TEAM = TeamQuotaConfig(4, 2, 100, 20, {7: UserReservation(7, 30, None)})


def _engine(client, store=None, ledger=None, flag=True):
    """Engine over the real limiter."""
    return QuotaEngine(
        QuotaLimiter(client), store or Store(ORG, TEAM), ledger or Ledger(), TM(),
        today=lambda: TODAY, flag=lambda _o: flag,
    )  # fmt: skip


async def test_noop_gates_write_zero_keys(valkey_async):
    """Req 1: every unconfigured gate leaves Valkey completely empty."""
    cases = [
        (User(team_id=None), Store(ORG, TEAM), True),
        (User(), Store(ORG, TEAM), False),
        (User(), Store(None, TEAM), True),
        (User(), Store(OrgQuotaConfig(2, False, ANCHOR, 1), TEAM), True),
        (User(), Store(OrgQuotaConfig(2, True, None, 1), TEAM), True),
        (User(), Store(ORG, None), True),
        (User(), Store(OrgQuotaConfig(2, True, date(2026, 6, 1), 1), TEAM), True),
    ]
    for user, store, flag in cases:
        out = await _engine(valkey_async, store, flag=flag).reserve(user, "m", MSG)
        assert (out.blocked, out.reservation) == (False, None)
    assert await valkey_async.dbsize() == 0


async def test_reserve_then_cache_hit_reconcile_refunds_fully(valkey_async):
    """Req 5: a cache hit settles to 0 credits; the user counter returns to zero."""
    eng, ledger = _engine(valkey_async), Ledger()
    eng.ledger = ledger
    out = await eng.reserve(User(), "m", MSG)
    assert out.reservation is not None and out.reservation.credits_reserved == 10
    res = await eng.reconcile(
        out.reservation, actual_credits=99, provider="cache", model="m", cache_hit=True
    )
    assert res is not None and res.applied and ledger.rows == []
    counters = [await valkey_async.get(k) async for k in valkey_async.scan_iter(match="*user*")]
    assert all(int(c) == 0 for c in counters if c is not None)


async def test_reserve_reconcile_writes_ledger_for_actual_split(valkey_async):
    """Admitted request settles at actual credits and lands in the ledger."""
    eng = _engine(valkey_async)
    out = await eng.reserve(User(), "m", MSG)
    assert out.reservation is not None
    res = await eng.reconcile(out.reservation, actual_credits=6, provider="p", model="m")
    assert res is not None and res.applied
    assert eng.ledger.rows[0][0:2] == (2, 1)
    assert sum(e.credits for e in eng.ledger.rows[0][2]) == 6


async def test_valkey_outage_fails_open_without_500(valkey_async):
    """Req 3: a dead Valkey allows the request, no reservation, no exception."""
    dead = aredis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    out = await _engine(dead).reserve(User(), "m", MSG)
    assert (out.blocked, out.reservation) == (False, None)
    await dead.aclose()


async def test_exhausted_pool_blocks_then_release_is_idempotent(valkey_async):
    """A deny becomes a block; release of a real reservation refunds, repeat is harmless."""
    tight = Store(ORG, TeamQuotaConfig(4, 2, 100, 0, {7: UserReservation(7, 10, None)}))
    eng = _engine(valkey_async, tight)
    first = await eng.reserve(User(), "m", MSG)
    assert first.reservation is not None
    second = await eng.reserve(User(), "m", MSG)
    assert second.blocked is True
    await eng.release(first.reservation)
    await eng.release(first.reservation)
    third = await eng.reserve(User(), "m", MSG)
    assert third.blocked is False
