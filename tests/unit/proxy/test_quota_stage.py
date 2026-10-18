"""QuotaStage: flag/cache/org gating, 429 on deny, reservation + release closure on allow."""

from dataclasses import dataclass

import pytest

from proxy.apps.proxy_server.pipeline.quota_stage import BLOCK_MESSAGES, QuotaStage
from proxy.apps.proxy_server.pipeline.stages import PipelineContext
from shared.quota.types import QuotaOutcome, QuotaReservation

RES = QuotaReservation("rid", 2, 4, 7, 1, 5, 3, 2, "gpt-4o")


@dataclass
class User:
    """The attribute the stage reads (UserContext has organization_id, no ``id``)."""

    organization_id: int | None = 2
    user_id: int = 7


class Features:
    """Sync feature helper stub recording (flag, distinct_id) lookups."""

    def __init__(self, on=True):
        """Set the flag value to return."""
        self.on, self.calls = on, []

    def is_feature_enabled(self, flag, distinct_id=None, **kw):
        """Record the lookup and return the programmed value."""
        self.calls.append((flag, distinct_id))
        return self.on


class Engine:
    """Records reserve/release; returns a programmed outcome."""

    def __init__(self, outcome=None):
        """Program the reserve outcome (default: admit with RES)."""
        self.outcome = outcome or QuotaOutcome(blocked=False, reservation=RES)
        self.reserved, self.released = [], []

    async def reserve(self, user, model, messages):
        """Record the call and return the outcome."""
        self.reserved.append((user, model, list(messages)))
        return self.outcome

    async def release(self, reservation):
        """Record the refund."""
        self.released.append(reservation)


class BoomEngine(Engine):
    """An engine whose reserve raises (the engine fails open itself; the stage must too)."""

    async def reserve(self, user, model, messages):
        """Raise."""
        raise RuntimeError("valkey down")


def _ctx(user=None, **kw):
    """A pipeline context for an authenticated org-2 user asking for gpt-4o."""
    return PipelineContext(
        user=user or User(), body={}, model="gpt-4o", messages=[{"content": "hi"}], **kw
    )


async def test_flag_is_resolved_per_org_and_off_is_a_noop():
    """distinct_id is str(organization_id); OFF means the engine is never consulted."""
    feats, engine = Features(on=False), Engine()
    ctx = await QuotaStage("quota", engine, feats)(_ctx())
    assert feats.calls == [("waddleai.pooled_quota", "2")]
    assert engine.reserved == [] and ctx.blocked is False and ctx.quota_reservation is None


@pytest.mark.parametrize(
    "make", [lambda: _ctx(cache_hit=True), lambda: _ctx(user=User(organization_id=None))]
)
async def test_cache_hits_and_unknown_org_skip_the_engine(make):
    """D9: a cache hit is free (never reserved, never 429); no org id -> nothing to key on."""
    engine = Engine()
    ctx = await QuotaStage("quota", engine, Features())(make())
    assert engine.reserved == [] and ctx.quota_reservation is None


async def test_no_engine_is_a_noop():
    """Without Valkey the proxy builds the stage with engine=None."""
    ctx = await QuotaStage("quota", None, Features())(_ctx())
    assert ctx.blocked is False


@pytest.mark.parametrize(
    "reason", ["quota_exhausted", "daily_subcap_exceeded", "override_exceeded", "something_new"]
)
async def test_deny_blocks_with_429_and_a_user_safe_message(reason):
    """A deny becomes status 429 with a stable message; unknown reasons get the generic text."""
    engine = Engine(QuotaOutcome(blocked=True, reason=reason))
    ctx = await QuotaStage("quota", engine, Features())(_ctx())
    assert (ctx.blocked, ctx.status_code) == (True, 429)
    assert ctx.block_reason == BLOCK_MESSAGES.get(reason, "Token quota exceeded")
    assert ctx.quota_reservation is None and ctx.quota_release is None


async def test_allow_stores_the_reservation_and_a_release_closure():
    """The context carries the reservation; calling the closure refunds through the engine."""
    engine = Engine()
    ctx = await QuotaStage("quota", engine, Features())(_ctx())
    assert ctx.blocked is False and ctx.quota_reservation == RES
    await ctx.quota_release()
    assert engine.released == [RES]
    assert engine.reserved[0][1] == "gpt-4o"  # the alias-resolved model is what is estimated


async def test_noop_outcome_leaves_the_context_untouched():
    """Teamless/unconfigured: the engine returns blocked=False with no reservation."""
    ctx = await QuotaStage("quota", Engine(QuotaOutcome(blocked=False)), Features())(_ctx())
    assert (ctx.blocked, ctx.quota_reservation, ctx.quota_release) == (False, None, None)


async def test_stage_level_exception_fails_open_never_500():
    """A raising engine must not crash the request or block it."""
    ctx = await QuotaStage("quota", BoomEngine(), Features())(_ctx())
    assert (ctx.blocked, ctx.quota_reservation, ctx.quota_release) == (False, None, None)
