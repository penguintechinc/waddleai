"""Tests for proxy_server.streaming -- true SSE framing for /v1/chat/completions and /v1/messages.

Covers both wire formats: a genuine-miss live dispatch (chunk ordering, [DONE]/
message_stop termination, TTFB + chunk + error metrics), a mid-stream failure
(terminal error event, no [DONE]/message_stop, finalize never called), and a
streaming cache hit (replay of ctx.stream_iter, post-filter block before replay).
"""

from typing import Any
from unittest.mock import AsyncMock, Mock

import orjson
import pytest

from proxy.apps.proxy_server import streaming
from proxy.apps.proxy_server.pipeline import DispatchStage, PipelineContext, ProxyPipeline
from shared.utils.llm_connectors import ProviderServerError, StreamChunk

pytestmark = pytest.mark.asyncio


def _fake_stage(name: str, *, block: bool = False) -> AsyncMock:
    """Build a minimal Stage stand-in for security_out/meter in a test pipeline."""

    async def _call(ctx: PipelineContext) -> PipelineContext:
        if block:
            ctx.blocked = True
            ctx.status_code = 400
            ctx.block_reason = "post_filter_blocked"
        return ctx

    stage = AsyncMock(side_effect=_call)
    stage.name = name
    stage.flag = None
    return stage


def _miss_pipeline(connector: Mock, *, tail_blocks: bool = False) -> ProxyPipeline:
    """A 3-stage pipeline (dispatch, security_out, meter) driving a real DispatchStage."""
    router = Mock()
    router.select_provider = Mock(return_value=("stub", "model-x"))
    dispatch_stage = DispatchStage(name="dispatch", router=router, connectors={"stub": connector})
    return ProxyPipeline(
        stages=[
            dispatch_stage,
            _fake_stage("security_out", block=tail_blocks),
            _fake_stage("meter"),
        ],
        features=None,
    )


def _cache_pipeline(*, tail_blocks: bool = False) -> ProxyPipeline:
    """A 3-stage pipeline for the cache-hit replay path (dispatch no-ops on ctx.cache_hit)."""
    dispatch_stage = DispatchStage(name="dispatch", router=Mock(), connectors={})
    return ProxyPipeline(
        stages=[
            dispatch_stage,
            _fake_stage("security_out", block=tail_blocks),
            _fake_stage("meter"),
        ],
        features=None,
    )


def _metrics() -> Mock:
    metrics = Mock()
    metrics.observe_stream_ttfb = Mock()
    metrics.record_stream_chunk = Mock()
    metrics.record_stream_error = Mock()
    return metrics


def _ctx(**overrides: Any) -> PipelineContext:
    user = Mock(id=1, tenant_id="org1")
    defaults: dict[str, Any] = dict(
        user=user, body={}, model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )
    defaults.update(overrides)
    return PipelineContext(**defaults)


class TestStreamOpenAIChatCompletionMiss:
    """Genuine streaming miss -- live DispatchStage.stream_dispatch drives the SSE frames."""

    async def test_chunk_ordering_and_done_terminator(self):
        """Role arrives on the first content chunk, final chunk carries usage, then [DONE]."""

        async def stream_chunks(*args, **kwargs):
            yield StreamChunk(delta="Hello ", done=False)
            yield StreamChunk(delta="world", done=False)
            yield StreamChunk(delta="", usage={"input_tokens": 1, "output_tokens": 2}, done=True)

        connector = Mock()
        connector.stream_chat_completion = stream_chunks
        pipeline = _miss_pipeline(connector)
        metrics = _metrics()
        ctx = _ctx(stream=True)
        finalize = AsyncMock(return_value={"id": "chatcmpl-x"})

        frames = [
            f
            async for f in streaming.stream_openai_chat_completion(
                ctx, pipeline, metrics, finalize=finalize
            )
        ]

        decoded = [orjson.loads(f.split(b"data: ", 1)[1]) for f in frames[:-1]]
        assert decoded[0]["choices"][0]["delta"] == {"role": "assistant", "content": "Hello "}
        assert decoded[1]["choices"][0]["delta"] == {"content": "world"}
        assert decoded[-1]["choices"][0]["finish_reason"] == "stop"
        assert decoded[-1]["usage"] == {"input_tokens": 1, "output_tokens": 2}
        assert frames[-1] == b"data: [DONE]\n\n"
        finalize.assert_awaited_once_with(ctx)
        assert metrics.observe_stream_ttfb.call_count == 1
        assert (
            metrics.record_stream_chunk.call_count == 2
        )  # "Hello " and "world", not the empty done chunk
        metrics.record_stream_error.assert_not_called()

    async def test_mid_stream_failure_emits_error_event_without_done(self):
        """A dispatch failure mid-stream ends with one error frame -- no [DONE], no finalize."""

        async def stream_then_fail(*args, **kwargs):
            yield StreamChunk(delta="partial", done=False)
            raise ProviderServerError(
                provider="stub", model="model-x", message="dropped", status_code=503
            )

        connector = Mock()
        connector.stream_chat_completion = stream_then_fail
        pipeline = _miss_pipeline(connector)
        metrics = _metrics()
        ctx = _ctx(stream=True)
        finalize = AsyncMock()

        frames = [
            f
            async for f in streaming.stream_openai_chat_completion(
                ctx, pipeline, metrics, finalize=finalize
            )
        ]

        assert b"[DONE]" not in b"".join(frames)
        error_payload = orjson.loads(frames[-1].split(b"data: ", 1)[1])
        assert error_payload["error"]["type"] == "overloaded_error"
        finalize.assert_not_awaited()
        metrics.record_stream_error.assert_called_once_with(
            provider="stub", endpoint="chat_completions"
        )

    async def test_cache_hit_replays_stream_iter_bytes_directly(self):
        """A cache hit never touches DispatchStage.stream_dispatch -- it replays ctx.stream_iter."""

        async def replay():
            yield b'data: {"id": "cached"}\n\n'
            yield b"data: [DONE]\n\n"

        pipeline = _cache_pipeline()
        metrics = _metrics()
        ctx = _ctx(stream=True, cache_hit=True, stream_iter=replay())
        finalize = AsyncMock(return_value={})

        frames = [
            f
            async for f in streaming.stream_openai_chat_completion(
                ctx, pipeline, metrics, finalize=finalize
            )
        ]

        assert frames == [b'data: {"id": "cached"}\n\n', b"data: [DONE]\n\n"]
        finalize.assert_awaited_once_with(ctx)
        assert metrics.record_stream_chunk.call_count == 2

    async def test_cache_hit_blocked_post_filter_never_replays(self):
        """If security_out blocks a cache-hit's (already-cached) text, replay never happens."""

        async def replay():
            yield b"data: should-never-be-sent\n\n"

        pipeline = _cache_pipeline(tail_blocks=True)
        metrics = _metrics()
        ctx = _ctx(stream=True, cache_hit=True, stream_iter=replay())
        finalize = AsyncMock()

        frames = [
            f
            async for f in streaming.stream_openai_chat_completion(
                ctx, pipeline, metrics, finalize=finalize
            )
        ]

        assert len(frames) == 1
        assert b"should-never-be-sent" not in frames[0]
        finalize.assert_not_awaited()
        metrics.record_stream_error.assert_called_once_with(
            provider="cache", endpoint="chat_completions"
        )


class TestStreamingFinalizeOptional:
    """`finalize` is optional on both formats/paths -- omitting it must never raise."""

    async def test_openai_miss_without_finalize(self):
        """A genuine miss with no finalize hook completes normally."""

        async def stream_chunks(*args, **kwargs):
            yield StreamChunk(delta="hi", usage={"input_tokens": 1, "output_tokens": 1}, done=True)

        connector = Mock()
        connector.stream_chat_completion = stream_chunks
        pipeline = _miss_pipeline(connector)
        ctx = _ctx(stream=True)

        frames = [
            f async for f in streaming.stream_openai_chat_completion(ctx, pipeline, _metrics())
        ]

        assert frames[-1] == b"data: [DONE]\n\n"

    async def test_openai_cache_hit_without_finalize(self):
        """A cache hit with no finalize hook completes normally."""

        async def replay():
            yield b"data: [DONE]\n\n"

        pipeline = _cache_pipeline()
        ctx = _ctx(stream=True, cache_hit=True, stream_iter=replay())

        frames = [
            f async for f in streaming.stream_openai_chat_completion(ctx, pipeline, _metrics())
        ]

        assert frames == [b"data: [DONE]\n\n"]

    async def test_anthropic_miss_without_finalize(self):
        """A genuine miss with no finalize hook completes normally."""

        async def stream_chunks(*args, **kwargs):
            yield StreamChunk(delta="hi", usage={"input_tokens": 1, "output_tokens": 1}, done=True)

        connector = Mock()
        connector.stream_chat_completion = stream_chunks
        pipeline = _miss_pipeline(connector)
        ctx = _ctx(stream=True, response_format="anthropic")

        frames = [f async for f in streaming.stream_anthropic_messages(ctx, pipeline, _metrics())]

        assert frames[-1].startswith(b"event: message_stop")

    async def test_anthropic_cache_hit_without_finalize(self):
        """A cache hit with no finalize hook completes normally."""

        async def replay():
            yield b"event: message_stop\ndata: {}\n\n"

        pipeline = _cache_pipeline()
        ctx = _ctx(stream=True, response_format="anthropic", cache_hit=True, stream_iter=replay())

        frames = [f async for f in streaming.stream_anthropic_messages(ctx, pipeline, _metrics())]

        assert frames == [b"event: message_stop\ndata: {}\n\n"]


class TestStreamingHelpers:
    """Pure helpers: error-type mapping and the dispatch-stage lookup guard."""

    @pytest.mark.parametrize(
        ("status_code", "expected"),
        [
            (429, "rate_limit_error"),
            (502, "overloaded_error"),
            (503, "overloaded_error"),
            (400, "invalid_request_error"),
            (404, "invalid_request_error"),
            (None, "api_error"),
            (500, "api_error"),
        ],
    )
    def test_error_type_for_status(self, status_code, expected):
        """Every status bucket maps to its documented wire-level error type."""
        assert streaming._error_type_for_status(status_code) == expected

    def test_dispatch_stage_lookup_raises_when_not_wired(self):
        """A pipeline with no stage named 'dispatch' is a wiring bug, not a runtime fallback."""
        pipeline = ProxyPipeline(stages=[_fake_stage("security_out")], features=None)
        with pytest.raises(RuntimeError, match="no 'dispatch' stage"):
            streaming._dispatch_stage(pipeline)


class TestStreamOpenAIChatCompletionEdgeCases:
    """Edge cases not covered by the main ordering/error/cache-hit tests above."""

    async def test_no_content_chunks_still_sends_role_and_done(self):
        """An upstream that never produces a content delta still closes out the SSE stream."""

        async def stream_chunks(*args, **kwargs):
            yield StreamChunk(delta="", usage={"input_tokens": 1, "output_tokens": 0}, done=True)

        connector = Mock()
        connector.stream_chat_completion = stream_chunks
        pipeline = _miss_pipeline(connector)
        metrics = _metrics()
        ctx = _ctx(stream=True)
        finalize = AsyncMock(return_value={})

        frames = [
            f
            async for f in streaming.stream_openai_chat_completion(
                ctx, pipeline, metrics, finalize=finalize
            )
        ]

        role_only = orjson.loads(frames[0].split(b"data: ", 1)[1])
        assert role_only["choices"][0]["delta"] == {"role": "assistant"}
        assert frames[-1] == b"data: [DONE]\n\n"
        metrics.record_stream_chunk.assert_not_called()


class TestStreamAnthropicMessagesMiss:
    """Genuine streaming miss -- Anthropic event-sequence framing."""

    async def test_event_sequence_and_message_stop_terminator(self):
        """message_start -> block_start -> delta* -> block_stop -> message_delta -> message_stop."""

        async def stream_chunks(*args, **kwargs):
            yield StreamChunk(delta="Hi", done=False)
            yield StreamChunk(delta="", usage={"input_tokens": 3, "output_tokens": 4}, done=True)

        connector = Mock()
        connector.stream_chat_completion = stream_chunks
        pipeline = _miss_pipeline(connector)
        metrics = _metrics()
        ctx = _ctx(stream=True, response_format="anthropic")
        finalize = AsyncMock(return_value={})

        frames = [
            f
            async for f in streaming.stream_anthropic_messages(
                ctx, pipeline, metrics, finalize=finalize
            )
        ]

        events = [f.split(b"event: ", 1)[1].split(b"\n", 1)[0].decode() for f in frames]
        assert events == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]
        delta_payload = orjson.loads(frames[2].split(b"data: ", 1)[1])
        assert delta_payload["delta"] == {"type": "text_delta", "text": "Hi"}
        message_delta_payload = orjson.loads(frames[4].split(b"data: ", 1)[1])
        assert message_delta_payload["usage"] == {"input_tokens": 3, "output_tokens": 4}
        finalize.assert_awaited_once_with(ctx)

    async def test_mid_stream_failure_emits_error_event_without_message_stop(self):
        """A dispatch failure mid-stream closes the open block then errors -- no message_stop."""

        async def stream_then_fail(*args, **kwargs):
            yield StreamChunk(delta="partial", done=False)
            raise ProviderServerError(
                provider="stub", model="model-x", message="dropped", status_code=503
            )

        connector = Mock()
        connector.stream_chat_completion = stream_then_fail
        pipeline = _miss_pipeline(connector)
        metrics = _metrics()
        ctx = _ctx(stream=True, response_format="anthropic")
        finalize = AsyncMock()

        frames = [
            f
            async for f in streaming.stream_anthropic_messages(
                ctx, pipeline, metrics, finalize=finalize
            )
        ]

        events = [f.split(b"event: ", 1)[1].split(b"\n", 1)[0].decode() for f in frames]
        assert events[-1] == "error"
        assert "message_stop" not in events
        finalize.assert_not_awaited()

    async def test_no_content_chunks_still_opens_and_closes_an_empty_block(self):
        """An upstream with zero text deltas still yields a well-formed (empty) content block."""

        async def stream_chunks(*args, **kwargs):
            yield StreamChunk(delta="", usage={"input_tokens": 1, "output_tokens": 0}, done=True)

        connector = Mock()
        connector.stream_chat_completion = stream_chunks
        pipeline = _miss_pipeline(connector)
        metrics = _metrics()
        ctx = _ctx(stream=True, response_format="anthropic")
        finalize = AsyncMock(return_value={})

        frames = [
            f
            async for f in streaming.stream_anthropic_messages(
                ctx, pipeline, metrics, finalize=finalize
            )
        ]

        events = [f.split(b"event: ", 1)[1].split(b"\n", 1)[0].decode() for f in frames]
        assert events == [
            "message_start",
            "content_block_start",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]
        finalize.assert_awaited_once_with(ctx)


class TestStreamAnthropicMessagesCacheHit:
    """Streaming cache hit -- Anthropic framing replays ctx.stream_iter directly."""

    async def test_cache_hit_replays_stream_iter_bytes_directly(self):
        """A cache hit never touches DispatchStage.stream_dispatch -- it replays ctx.stream_iter."""

        async def replay():
            yield b'event: message_start\ndata: {"type": "message_start"}\n\n'
            yield b'event: message_stop\ndata: {"type": "message_stop"}\n\n'

        pipeline = _cache_pipeline()
        metrics = _metrics()
        ctx = _ctx(stream=True, response_format="anthropic", cache_hit=True, stream_iter=replay())
        finalize = AsyncMock(return_value={})

        frames = [
            f
            async for f in streaming.stream_anthropic_messages(
                ctx, pipeline, metrics, finalize=finalize
            )
        ]

        assert len(frames) == 2
        assert frames[0].startswith(b"event: message_start")
        finalize.assert_awaited_once_with(ctx)
        assert metrics.record_stream_chunk.call_count == 2

    async def test_cache_hit_blocked_post_filter_never_replays(self):
        """If security_out blocks a cache-hit's (already-cached) text, replay never happens."""

        async def replay():
            yield b"event: should-never-be-sent\ndata: {}\n\n"

        pipeline = _cache_pipeline(tail_blocks=True)
        metrics = _metrics()
        ctx = _ctx(stream=True, response_format="anthropic", cache_hit=True, stream_iter=replay())
        finalize = AsyncMock()

        frames = [
            f
            async for f in streaming.stream_anthropic_messages(
                ctx, pipeline, metrics, finalize=finalize
            )
        ]

        assert len(frames) == 1
        assert b"should-never-be-sent" not in frames[0]
        finalize.assert_not_awaited()
        metrics.record_stream_error.assert_called_once_with(provider="cache", endpoint="messages")
