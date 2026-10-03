"""True end-to-end SSE streaming for /v1/chat/completions and /v1/messages (ops O7-b, O11).

Before this module existed, ``stream: true`` never produced SSE: DispatchStage
fully buffered the upstream response (see ``pipeline.stages.DispatchStage.__call__``)
and the route handler returned one JSON blob regardless of what the client asked
for. This module drives :meth:`DispatchStage.stream_dispatch` (the true-streaming
sibling of ``__call__``) and formats each upstream chunk as an SSE frame the moment
it arrives -- OpenAI ``chat.completion.chunk`` framing for ``/v1/chat/completions``,
the Anthropic ``message_start``/``content_block_*``/``message_delta``/``message_stop``
event sequence for ``/v1/messages`` -- matching ``shared.cache.replay``'s wire
framing exactly so a streaming cache hit and a streaming miss are indistinguishable
on the wire.

Pipeline ordering is preserved without ever buffering the whole response:
auth/token_budget/security_in/cache/routing all run normally via
``ProxyPipeline.run_until(ctx, "dispatch")`` before any upstream call is made.
For a cache hit, the response is already fully known (zero upstream latency), so
security_out/meter run immediately via ``ProxyPipeline.run_after(ctx, "dispatch")``
before replay. For a genuine miss, chunks are forwarded to the client as the
upstream call produces them while ``ctx.response_text`` accumulates out-of-band;
only once the upstream stream is fully drained does ``run_after(ctx, "dispatch")``
run security_out (PII/sensitive-content filtering -- necessarily after-the-fact for
content already streamed, a known limitation of real-time SSE shared with every
other LLM gateway) and meter (billing, which only needs final usage, not
intermediate chunks).

A mid-stream dispatch failure never raises out of the SSE generator -- it ends the
`async for` cleanly (``DispatchStage.stream_dispatch`` catches and maps exceptions
onto ``ctx.blocked`` exactly like ``__call__``) and this module emits one terminal
SSE error event, logs at ERROR, records ``waddleai_proxy_stream_errors_total``, and
closes the response -- never a dangling connection.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import orjson

from .pipeline import DispatchStage, PipelineContext, ProxyPipeline

logger = logging.getLogger(__name__)

# ASGI-level headers for every SSE response (set by the route handler alongside
# the ``text/event-stream`` content type): disable any intermediary response
# buffering (nginx/other reverse proxies honor X-Accel-Buffering) and disable
# HTTP caching of a live, per-request stream.
SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}

FinalizeHook = Callable[[PipelineContext], Awaitable[None]]


def _dispatch_stage(pipeline: ProxyPipeline) -> DispatchStage:
    """Find the pipeline's wired ``DispatchStage`` instance (for ``.stream_dispatch()``)."""
    for stage in pipeline.stages:
        if stage.name == "dispatch" and isinstance(stage, DispatchStage):
            return stage
    raise RuntimeError("ProxyPipeline has no 'dispatch' stage wired")


def _error_type_for_status(status_code: int | None) -> str:
    """Map an HTTP status code DispatchStage set onto a wire-level error `type`.

    Loosely follows the Anthropic error-type vocabulary (reused for OpenAI
    framing too, since both wire formats carry a free-text `type` string) --
    this is informational for the client, not parsed by either SDK's happy path.
    """
    if status_code == 429:
        return "rate_limit_error"
    if status_code in (502, 503):
        return "overloaded_error"
    if status_code is not None and 400 <= status_code < 500:
        return "invalid_request_error"
    return "api_error"


def _sse_error_openai(block_reason: str, status_code: int | None) -> bytes:
    """Build the terminal OpenAI-framed SSE error event (no `data: [DONE]` follows)."""
    payload = {
        "error": {
            "message": block_reason,
            "type": _error_type_for_status(status_code),
        }
    }
    return b"data: " + orjson.dumps(payload) + b"\n\n"


def _sse_event_anthropic(event: str, data: dict[str, Any]) -> bytes:
    r"""Build one Anthropic-framed ``event: ...\ndata: ...\n\n`` SSE frame."""
    return b"event: " + event.encode() + b"\ndata: " + orjson.dumps(data) + b"\n\n"


def _sse_error_anthropic(block_reason: str, status_code: int | None) -> bytes:
    """Build the terminal Anthropic-framed SSE error event (no `message_stop` follows)."""
    return _sse_event_anthropic(
        "error",
        {
            "type": "error",
            "error": {"type": _error_type_for_status(status_code), "message": block_reason},
        },
    )


async def stream_openai_chat_completion(
    ctx: PipelineContext,
    pipeline: ProxyPipeline,
    metrics: Any,
    *,
    finalize: FinalizeHook | None = None,
) -> AsyncIterator[bytes]:
    """Yield OpenAI ``chat.completion.chunk`` SSE frames for ``/v1/chat/completions``.

    Call only after ``ctx = await pipeline.run_until(ctx, "dispatch")`` has
    returned with ``ctx.blocked`` False -- this function owns driving dispatch
    (live or cache-replay) and the remaining tail of the pipeline itself.
    ``finalize`` (metrics/memory-storage/cache-write-back) runs once, only on
    a successful completion -- never on a blocked/error path, mirroring the
    non-streaming route handler's poisoning-defense ordering (spec §3.6).
    """
    endpoint = "chat_completions"
    response_id = f"chatcmpl-{int(time.time())}"
    created = int(time.time())

    def _frame(
        delta: dict[str, Any], finish_reason: str | None = None, usage: dict[str, Any] | None = None
    ) -> bytes:
        frame: dict[str, Any] = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": ctx.model or "",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage is not None:
            frame["usage"] = usage
        return b"data: " + orjson.dumps(frame) + b"\n\n"

    if ctx.cache_hit and ctx.stream_iter is not None:
        cached_stream_iter = ctx.stream_iter
        ctx = await pipeline.run_after(ctx, "dispatch")
        if ctx.blocked:
            metrics.record_stream_error(provider="cache", endpoint=endpoint)
            logger.error("Streaming cache-hit blocked post-filter: %s", ctx.block_reason)
            yield _sse_error_openai(ctx.block_reason or "stream_error", ctx.status_code)
            return
        async for sse_frame in cached_stream_iter:
            metrics.record_stream_chunk(provider="cache", endpoint=endpoint)
            yield sse_frame
        if finalize is not None:
            await finalize(ctx)
        return

    dispatch_stage = _dispatch_stage(pipeline)
    sent_role = False
    first_chunk_seen = False
    start = time.time()
    async with contextlib.aclosing(dispatch_stage.stream_dispatch(ctx)) as upstream:
        async for stream_chunk in upstream:
            if not first_chunk_seen:
                first_chunk_seen = True
                metrics.observe_stream_ttfb(
                    provider=ctx.provider or "unknown",
                    endpoint=endpoint,
                    seconds=time.time() - start,
                )
            if not stream_chunk.delta:
                continue
            delta: dict[str, Any] = {"content": stream_chunk.delta}
            if not sent_role:
                delta["role"] = "assistant"
                sent_role = True
            metrics.record_stream_chunk(provider=ctx.provider or "unknown", endpoint=endpoint)
            yield _frame(delta)

    ctx = await pipeline.run_after(ctx, "dispatch")

    if ctx.blocked:
        metrics.record_stream_error(provider=ctx.provider or "unknown", endpoint=endpoint)
        logger.error("Streaming dispatch failed for %s: %s", endpoint, ctx.block_reason)
        yield _sse_error_openai(ctx.block_reason or "stream_error", ctx.status_code)
        return

    if not sent_role:
        yield _frame({"role": "assistant"})
    yield _frame({}, finish_reason=ctx.finish_reason or "stop", usage=ctx.usage)
    yield b"data: [DONE]\n\n"

    if finalize is not None:
        await finalize(ctx)


async def stream_anthropic_messages(
    ctx: PipelineContext,
    pipeline: ProxyPipeline,
    metrics: Any,
    *,
    finalize: FinalizeHook | None = None,
) -> AsyncIterator[bytes]:
    """Yield the Anthropic Messages SSE event sequence for ``/v1/messages``.

    Same calling convention and pipeline-tail ownership as
    :func:`stream_openai_chat_completion` -- see that function's docstring.
    """
    endpoint = "messages"
    response_id = f"msg_{int(time.time() * 1000)}"

    if ctx.cache_hit and ctx.stream_iter is not None:
        cached_stream_iter = ctx.stream_iter
        ctx = await pipeline.run_after(ctx, "dispatch")
        if ctx.blocked:
            metrics.record_stream_error(provider="cache", endpoint=endpoint)
            logger.error("Streaming cache-hit blocked post-filter: %s", ctx.block_reason)
            yield _sse_error_anthropic(ctx.block_reason or "stream_error", ctx.status_code)
            return
        async for sse_frame in cached_stream_iter:
            metrics.record_stream_chunk(provider="cache", endpoint=endpoint)
            yield sse_frame
        if finalize is not None:
            await finalize(ctx)
        return

    yield _sse_event_anthropic(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": response_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": ctx.model or "",
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    dispatch_stage = _dispatch_stage(pipeline)
    opened_block = False
    first_chunk_seen = False
    start = time.time()
    async with contextlib.aclosing(dispatch_stage.stream_dispatch(ctx)) as upstream:
        async for stream_chunk in upstream:
            if not first_chunk_seen:
                first_chunk_seen = True
                metrics.observe_stream_ttfb(
                    provider=ctx.provider or "unknown",
                    endpoint=endpoint,
                    seconds=time.time() - start,
                )
            if not stream_chunk.delta:
                continue
            if not opened_block:
                yield _sse_event_anthropic(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
                opened_block = True
            metrics.record_stream_chunk(provider=ctx.provider or "unknown", endpoint=endpoint)
            yield _sse_event_anthropic(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": stream_chunk.delta},
                },
            )

    ctx = await pipeline.run_after(ctx, "dispatch")

    if ctx.blocked:
        metrics.record_stream_error(provider=ctx.provider or "unknown", endpoint=endpoint)
        logger.error("Streaming dispatch failed for %s: %s", endpoint, ctx.block_reason)
        if opened_block:
            yield _sse_event_anthropic(
                "content_block_stop", {"type": "content_block_stop", "index": 0}
            )
        yield _sse_error_anthropic(ctx.block_reason or "stream_error", ctx.status_code)
        return

    if not opened_block:
        yield _sse_event_anthropic(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
    yield _sse_event_anthropic("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield _sse_event_anthropic(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": ctx.finish_reason or "end_turn", "stop_sequence": None},
            "usage": ctx.usage or {},
        },
    )
    yield _sse_event_anthropic("message_stop", {"type": "message_stop"})

    if finalize is not None:
        await finalize(ctx)
