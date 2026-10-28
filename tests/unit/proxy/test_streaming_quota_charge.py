"""Consumed tokens are CHARGED (never refunded) on error/cancel/disconnect, exactly once.

Covers the real OpenAI and Anthropic stream generators plus the shielded settlement helper.
"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from proxy.apps.proxy_server import streaming
from proxy.apps.proxy_server.pipeline.quota_stage import settle_aborted
from proxy.apps.proxy_server.pipeline.stages import ProxyPipeline, settle_shielded
from proxy.apps.proxy_server.quota_hooks import reconcile_quota, release_held_quota
from shared.quota.types import QuotaReservation
from shared.utils.llm_connectors import StreamChunk
from tests.unit.proxy.test_streaming import _ctx, _metrics, _miss_pipeline

pytestmark = pytest.mark.asyncio

RES = QuotaReservation("rid", 2, 4, 7, 1, 50, 30, 20, "gpt-4o")


class _TM:
    """1 token per char; credits = in + 2*out."""

    def count_tokens(self, text, provider="", model=""):
        return len(text)

    def calculate_waddleai_tokens(self, i, o, provider, model, provider_type=None):
        return i + 2 * o


class _Engine:
    """Records settlements; ``delay`` makes reconcile/release slow to expose cancellation."""

    token_manager = _TM()

    def __init__(self, delay=0.0):
        self.delay = delay
        self.reconciled, self.released = [], []
        self.telemetry = Mock()

    async def reconcile(self, reservation, **kw):
        await asyncio.sleep(self.delay)
        self.reconciled.append((reservation, kw))

    async def release(self, reservation):
        await asyncio.sleep(self.delay)
        self.released.append(reservation)


def _held(ctx, engine):
    """Install the reconcile-aware release hook, as QuotaStage does."""
    ctx.quota_reservation = RES

    async def _hook():
        await settle_aborted(engine, ctx, RES)

    ctx.quota_release = _hook


def _pipeline(chunks_fn):
    connector = Mock()
    connector.stream_chat_completion = chunks_fn
    return _miss_pipeline(connector)


async def _partial_then_error(*a, **k):
    yield StreamChunk(delta="abc", done=False)
    raise RuntimeError("boom")


async def _two_chunks(*a, **k):
    yield StreamChunk(delta="abc", done=False)
    yield StreamChunk(delta="def", done=False)


async def _full(*a, **k):
    yield StreamChunk(delta="abc", done=False)
    yield StreamChunk(delta="", usage={"input_tokens": 2, "output_tokens": 3}, done=True)


def _assert_charged(engine, ctx, credits):
    ((res, kw),) = engine.reconciled
    assert res is RES and engine.released == []
    assert kw["actual_credits"] == credits and kw["cache_hit"] is False
    assert ctx.quota_reservation is None and ctx.quota_release is None


@pytest.mark.parametrize(
    "gen", [streaming.stream_openai_chat_completion, streaming.stream_anthropic_messages]
)
async def test_midstream_error_charges_partial_once(gen):
    """A provider error after partial tokens charges them (in=len('hi')=2, out=3 -> 8)."""
    engine, ctx = _Engine(), _ctx(stream=True)
    _held(ctx, engine)
    _ = [f async for f in gen(ctx, _pipeline(_partial_then_error), _metrics())]
    _assert_charged(engine, ctx, 2 + 2 * 3)


@pytest.mark.parametrize(
    "gen", [streaming.stream_openai_chat_completion, streaming.stream_anthropic_messages]
)
async def test_cancel_midstream_charges_partial_once(gen):
    """Closing the generator mid-stream (disconnect) charges the partial, not a refund."""
    engine, ctx = _Engine(), _ctx(stream=True)
    _held(ctx, engine)
    agen = gen(ctx, _pipeline(_two_chunks), _metrics(), finalize=AsyncMock())
    async for frame in agen:
        if b"def" in frame:
            break
    await agen.aclose()
    _assert_charged(engine, ctx, 2 + 2 * 6)


@pytest.mark.parametrize(
    "gen", [streaming.stream_openai_chat_completion, streaming.stream_anthropic_messages]
)
async def test_disconnect_at_final_frame_settles_actual(gen):
    """Disconnect at the final frame (finalize never runs) still charges the consumed usage."""
    engine, ctx = _Engine(), _ctx(stream=True)
    _held(ctx, engine)
    finalize = AsyncMock()
    agen = gen(ctx, _pipeline(_full), _metrics(), finalize=finalize)
    frames = []
    async for frame in agen:
        frames.append(frame)
        if b"[DONE]" in frame or b"message_stop" in frame:
            break
    await agen.aclose()
    finalize.assert_not_awaited()
    _assert_charged(engine, ctx, 2 + 2 * 3)


async def test_double_cancel_during_settlement_still_charges():
    """Two cancellations while the charge is in flight cannot abandon it (shield)."""
    engine, ctx = _Engine(delay=0.05), _ctx()
    ctx.provider, ctx.response_text = "stub", "abc"
    _held(ctx, engine)
    task = asyncio.ensure_future(ProxyPipeline._release_quota(ctx))
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _assert_charged(engine, ctx, 2 + 2 * 3)


async def test_reconcile_quota_cancelled_charges_actual_then_reraises():
    """A cancel during reconcile still completes the charge (not a refund) and propagates."""
    engine, ctx = _Engine(delay=0.05), _ctx()
    _held(ctx, engine)
    task = asyncio.ensure_future(
        reconcile_quota(engine, ctx, waddleai_tokens=9, provider="stub", model="m")
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _assert_charged(engine, ctx, 9)


async def test_release_held_quota_shielded_and_faults_on_failure():
    """The finalize-error settle survives cancel; a failing settle emits a fault."""
    engine, ctx = _Engine(delay=0.05), _ctx()
    ctx.provider, ctx.response_text = "stub", "abc"
    _held(ctx, engine)
    task = asyncio.ensure_future(release_held_quota(ctx, engine))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(engine.reconciled) == 1

    failing, ctx2 = _Engine(), _ctx()

    async def _boom():
        raise RuntimeError("x")

    ctx2.quota_release, ctx2.quota_reservation = _boom, RES
    await release_held_quota(ctx2, failing)
    failing.telemetry.fault.assert_called_with("release_failed")


async def test_reconcile_failure_emits_fault():
    """A failing reconcile is visible as a fault, not just a log line."""
    engine, ctx = _Engine(), _ctx()
    _held(ctx, engine)
    engine.reconcile = AsyncMock(side_effect=RuntimeError("down"))
    await reconcile_quota(engine, ctx, waddleai_tokens=1, provider="p", model="m")
    engine.telemetry.fault.assert_called_with("reconcile_error")


async def test_settle_shielded_returns_result_and_propagates_errors():
    """Without cancellation the helper is transparent."""

    async def ok():
        return 5

    async def bad():
        raise ValueError("v")

    assert await settle_shielded(ok()) == 5
    with pytest.raises(ValueError):
        await settle_shielded(bad())
