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


class _TM:
    """Deterministic token manager: 1 token per char, credits = in + 2*out."""

    def count_tokens(self, text, provider="", model=""):
        return len(text)

    def calculate_waddleai_tokens(self, i, o, provider, model):
        return i + 2 * o


class _SettleEngine:
    """Records reconcile/release calls."""

    token_manager = _TM()

    def __init__(self):
        self.reconciled, self.released = [], []

    async def reconcile(self, reservation, **kw):
        self.reconciled.append((reservation, kw))

    async def release(self, reservation):
        self.released.append(reservation)


async def test_partial_stream_error_charges_the_partial_actual_not_a_refund():
    """A stream that dies after N partial tokens reconciles the partial credits (no full refund)."""
    from proxy.apps.proxy_server.pipeline.stages import ProxyPipeline

    engine = _SettleEngine()
    ctx = PipelineContext(user=User(), body={}, messages=[{"role": "user", "content": "abcd"}])
    ctx.provider, ctx.model, ctx.response_text = "openai", "gpt-4o", "xyz"
    ctx.quota_reservation = RES

    async def _hook():
        from proxy.apps.proxy_server.pipeline.quota_stage import settle_aborted

        await settle_aborted(engine, ctx, RES)

    ctx.quota_release = _hook
    await ProxyPipeline._release_quota(ctx)
    await ProxyPipeline._release_quota(ctx)
    ((res, kw),) = engine.reconciled
    assert res is RES and engine.released == []
    assert kw["actual_credits"] == 4 + 2 * 3 and kw["cache_hit"] is False
    assert ctx.quota_reservation is None and ctx.quota_release is None


async def test_zero_usage_abort_releases_in_full():
    """Blocked before dispatch (no provider, no output) is a full refund."""
    from proxy.apps.proxy_server.pipeline.quota_stage import settle_aborted

    engine = _SettleEngine()
    ctx = PipelineContext(user=User(), body={})
    await settle_aborted(engine, ctx, RES)
    assert engine.released == [RES] and engine.reconciled == []


async def test_cache_hit_abort_is_a_full_refund():
    """provider=='cache' consumed no upstream tokens: release."""
    from proxy.apps.proxy_server.pipeline.quota_stage import settle_aborted

    engine = _SettleEngine()
    ctx = PipelineContext(user=User(), body={}, cache_hit=True)
    ctx.provider, ctx.response_text = "cache", "cached text"
    await settle_aborted(engine, ctx, RES)
    assert engine.released == [RES] and engine.reconciled == []


async def test_provider_usage_counts_are_preferred_over_local_estimates():
    """Provider-reported usage wins when present on a post-dispatch block."""
    from proxy.apps.proxy_server.pipeline.quota_stage import settle_aborted

    engine = _SettleEngine()
    ctx = PipelineContext(user=User(), body={})
    ctx.provider, ctx.model, ctx.usage = "openai", "m", {"input_tokens": 10, "output_tokens": 5}
    await settle_aborted(engine, ctx, RES)
    assert engine.reconciled[0][1]["actual_credits"] == 20
