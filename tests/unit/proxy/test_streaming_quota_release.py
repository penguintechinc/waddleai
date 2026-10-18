"""Streaming path releases a held quota reservation on block/error, never on success."""

from unittest.mock import AsyncMock, Mock

import pytest

from proxy.apps.proxy_server import streaming
from shared.utils.llm_connectors import ProviderServerError, StreamChunk
from tests.unit.proxy.test_streaming import _ctx, _metrics, _miss_pipeline

pytestmark = pytest.mark.asyncio


def _held(ctx):
    """Install a quota release hook on ctx and return the mock."""
    release = AsyncMock()
    ctx.quota_release = release
    ctx.quota_reservation = object()
    return release


async def test_run_after_on_already_blocked_ctx_releases_once():
    """A stream block set outside _run_stage still refunds exactly once and clears the hook."""
    pipeline = _miss_pipeline(Mock())
    ctx = _ctx()
    release = _held(ctx)
    ctx.blocked = True
    await pipeline.run_after(ctx, "dispatch")
    await pipeline.run_after(ctx, "dispatch")
    release.assert_awaited_once()
    assert ctx.quota_release is None and ctx.quota_reservation is None


async def test_stream_disconnect_mid_stream_releases_once():
    """Closing the SSE generator mid-stream (client disconnect) refunds once, no finalize."""

    async def two(*args, **kwargs):
        yield StreamChunk(delta="one", done=False)
        yield StreamChunk(delta="two", done=False)

    connector = Mock()
    connector.stream_chat_completion = two
    pipeline = _miss_pipeline(connector)
    ctx = _ctx(stream=True)
    release = _held(ctx)
    finalize = AsyncMock()
    gen = streaming.stream_openai_chat_completion(ctx, pipeline, _metrics(), finalize=finalize)
    await anext(gen)
    await gen.aclose()
    release.assert_awaited_once()
    finalize.assert_not_awaited()


async def test_stream_unexpected_exception_releases_once():
    """A non-provider exception mid-stream is converted to a block and refunded once."""

    async def boom(*args, **kwargs):
        yield StreamChunk(delta="partial", done=False)
        raise RuntimeError("boom")

    connector = Mock()
    connector.stream_chat_completion = boom
    pipeline = _miss_pipeline(connector)
    ctx = _ctx(stream=True)
    release = _held(ctx)
    _ = [f async for f in streaming.stream_openai_chat_completion(ctx, pipeline, _metrics())]
    release.assert_awaited_once()


async def test_stream_provider_error_blocked_path_releases_once():
    """A provider failure (ctx.blocked via stream_dispatch) is refunded by run_after."""

    async def fail(*args, **kwargs):
        yield StreamChunk(delta="partial", done=False)
        raise ProviderServerError(provider="stub", model="m", message="dropped", status_code=503)

    connector = Mock()
    connector.stream_chat_completion = fail
    pipeline = _miss_pipeline(connector)
    ctx = _ctx(stream=True)
    release = _held(ctx)
    _ = [f async for f in streaming.stream_openai_chat_completion(ctx, pipeline, _metrics())]
    release.assert_awaited_once()


async def test_successful_stream_leaves_the_hook_for_finalize():
    """No release on success -- Task 28's _finalize reconciles instead."""

    async def ok(*args, **kwargs):
        yield StreamChunk(delta="hi", done=False)
        yield StreamChunk(delta="", usage={"input_tokens": 1, "output_tokens": 1}, done=True)

    connector = Mock()
    connector.stream_chat_completion = ok
    pipeline = _miss_pipeline(connector)
    ctx = _ctx(stream=True)
    release = _held(ctx)
    seen = []

    async def finalize(final_ctx):
        """Stand in for _finalize: the hook is still set here, then reconcile clears it."""
        seen.append(final_ctx.quota_release)
        final_ctx.quota_release = None
        final_ctx.quota_reservation = None

    _ = [
        f
        async for f in streaming.stream_openai_chat_completion(
            ctx, pipeline, _metrics(), finalize=finalize
        )
    ]
    release.assert_not_awaited()
    assert seen == [release]


async def test_anthropic_stream_disconnect_releases_once():
    """The Anthropic framing path has the same refund-on-disconnect guard."""

    async def two(*args, **kwargs):
        yield StreamChunk(delta="one", done=False)
        yield StreamChunk(delta="two", done=False)

    connector = Mock()
    connector.stream_chat_completion = two
    pipeline = _miss_pipeline(connector)
    ctx = _ctx(stream=True)
    release = _held(ctx)
    gen = streaming.stream_anthropic_messages(ctx, pipeline, _metrics())
    await anext(gen)
    await anext(gen)
    await anext(gen)
    await gen.aclose()
    release.assert_awaited_once()
