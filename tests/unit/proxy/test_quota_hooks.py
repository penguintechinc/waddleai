# ruff: noqa: D102, D107
"""Finalize/reconcile helper: single settlement, never raises, config TTL parsing."""

import asyncio

import pytest

from proxy.apps.proxy_server.pipeline.stages import PipelineContext
from proxy.apps.proxy_server.quota_hooks import (
    build_quota_engine,
    config_ttl_seconds,
    reconcile_quota,
    release_held_quota,
)
from shared.quota.types import QuotaReservation

RES = QuotaReservation("rid", 2, 4, 7, 1, 5, 3, 2, "gpt-4o")


class Engine:
    """Records reconcile/release calls; optionally raises."""

    def __init__(self, exc=None):
        self.calls = []
        self.released = []
        self.exc = exc

    async def reconcile(self, reservation, **kw):
        self.calls.append((reservation, kw))
        if self.exc:
            raise self.exc

    async def release(self, reservation):
        self.released.append(reservation)


def _ctx(**kw):
    """A context holding RES and a release closure."""
    ctx = PipelineContext(user=None, body={}, **kw)
    ctx.quota_reservation = RES

    async def _release():
        raise AssertionError("must not be released after reconcile")

    ctx.quota_release = _release
    return ctx


async def test_reconcile_passes_actual_usage_and_clears_the_release_hook():
    """The reservation is settled once; the release hook is cleared before reconciling."""
    engine, ctx = Engine(), _ctx()
    await reconcile_quota(engine, ctx, waddleai_tokens=9, provider="openai", model="gpt-4o")
    assert engine.calls == [
        (RES, {"actual_credits": 9, "provider": "openai", "model": "gpt-4o", "cache_hit": False})
    ]
    assert ctx.quota_reservation is None and ctx.quota_release is None
    await reconcile_quota(engine, ctx, waddleai_tokens=9, provider="openai", model="gpt-4o")
    assert len(engine.calls) == 1


async def test_cache_hit_flag_is_forwarded():
    """cache_hit on the context reaches the engine (D9: 0 credits)."""
    engine = Engine()
    await reconcile_quota(
        engine, _ctx(cache_hit=True), waddleai_tokens=9, provider="cache", model="m"
    )
    assert engine.calls[0][1]["cache_hit"] is True


async def test_noop_without_engine_or_reservation():
    """No engine (no Valkey) or no reservation (flag off / unconfigured): nothing happens."""
    engine = Engine()
    await reconcile_quota(None, _ctx(), waddleai_tokens=1, provider="p", model="m")
    empty = PipelineContext(user=None, body={})
    await reconcile_quota(engine, empty, waddleai_tokens=1, provider="p", model="m")
    assert engine.calls == []


async def test_reconcile_failure_never_raises():
    """A failing reconcile is swallowed (the user already has their answer)."""
    engine = Engine(exc=RuntimeError("boom"))
    await reconcile_quota(engine, _ctx(), waddleai_tokens=1, provider="p", model="m")
    assert len(engine.calls) == 1


async def test_cancel_during_reconcile_charges_actual_then_reraises():
    """A disconnect mid-reconcile CHARGES the actual credits (never a refund), then propagates."""

    class Slow(Engine):
        async def reconcile(self, reservation, **kw):
            await asyncio.sleep(0.05)
            await super().reconcile(reservation, **kw)

    engine, ctx = Slow(), _ctx()
    task = asyncio.ensure_future(
        reconcile_quota(engine, ctx, waddleai_tokens=9, provider="p", model="m")
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(engine.calls) == 1 and engine.calls[0][1]["actual_credits"] == 9
    assert engine.released == []


async def test_release_held_quota_releases_once_and_clears():
    """The finalize-error path releases a still-held reservation exactly once."""
    released = []
    ctx = PipelineContext(user=None, body={})
    ctx.quota_reservation = RES

    async def _rel():
        released.append(1)

    ctx.quota_release = _rel
    await release_held_quota(ctx)
    await release_held_quota(ctx)
    assert released == [1] and ctx.quota_reservation is None and ctx.quota_release is None


async def test_release_held_quota_swallows_release_errors():
    """A failing refund is logged, not raised."""
    ctx = PipelineContext(user=None, body={})

    async def _rel():
        raise RuntimeError("x")

    ctx.quota_release = _rel
    await release_held_quota(ctx)
    assert ctx.quota_release is None


def test_build_quota_engine_without_valkey_is_none():
    """No Valkey -> no engine, stage stays a no-op."""
    assert build_quota_engine(None, db=object(), token_manager=object()) == (None, None)


def test_build_quota_engine_failure_is_fail_safe(monkeypatch):
    """A construction error returns (None, None) instead of crashing startup."""
    import shared.quota.lease as lease

    def _boom(*a, **k):
        raise RuntimeError("no dep")

    monkeypatch.setattr(lease, "QuotaLimiter", _boom)
    assert build_quota_engine(object(), db=object(), token_manager=object()) == (None, None)


def test_build_quota_engine_builds_engine_and_limiter():
    """With Valkey present both objects are built and wired."""
    engine, limiter = build_quota_engine(object(), db=object(), token_manager=object())
    assert engine is not None and engine.limiter is limiter


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, 300), ("45", 45), ("abc", 300), ("0", 300), ("-9", 300)]
)
def test_config_ttl_parsing(monkeypatch, raw, expected):
    """Garbage or non-positive TTLs fall back to the 300s default instead of crashing startup."""
    if raw is None:
        monkeypatch.delenv("PROXY_QUOTA_CONFIG_TTL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("PROXY_QUOTA_CONFIG_TTL_SECONDS", raw)
    assert config_ttl_seconds() == expected
