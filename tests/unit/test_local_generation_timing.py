"""Generation timing: engine-reported when valid; proxy-measured excludes consumer stalls."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp import web

from shared.utils.llm_connectors import (
    LlamaCppConnector,
    OllamaConnector,
    TimedStream,
    VllmConnector,
    engine_timing,
)
from tests.unit.test_local_usage_kind import MSGS, _serve

STALL = 0.3  # seconds a slow consumer parks the generator after every chunk

pytestmark = pytest.mark.usefixtures("restore_obs_meter_cache")


def _vllm(server: Any) -> VllmConnector:
    """A vLLM connector wired to the in-process fake server."""
    conn = VllmConnector(
        "vllm-1-x",
        {
            "endpoint_url": "http://fake/v1",
            "api_key": "k",
            "model_list": ["m"],
            "management_capabilities": {"local_deployment_id": 7, "fleet_status": 5},
        },
    )
    conn.client = server.sdk_client("k")
    return conn


async def _slow_final(stream: Any) -> dict[str, Any]:
    """Drain a stream like a slow client (sleeping after each chunk); return the final usage."""
    final = None
    async for chunk in stream:
        if chunk.done:
            final = chunk.usage
        else:
            await asyncio.sleep(STALL)
    assert final is not None
    return final


@pytest.mark.parametrize(
    ("ms", "tokens", "ok"),
    [
        (1234.0, 7, True),
        (50, 8, True),
        (0, 7, False),
        (-5, 7, False),
        (float("nan"), 7, False),
        (3_600_001, 7, False),
        (True, 7, False),
        ("5", 7, False),
        (100, 0, False),
        (100, True, False),
        (100, 2**40, False),
        (100, None, False),
    ],
)
def test_engine_timing_validation(ms: Any, tokens: Any, ok: bool) -> None:
    """Only a finite positive ms <= 1h with a plausible int token count is trusted."""
    assert (engine_timing(ms, tokens) is not None) is ok


async def _source(items: list[Any], delay: float = 0.0) -> Any:
    """An async source yielding ``items`` with ``delay`` seconds between them."""
    for item in items:
        if delay:
            await asyncio.sleep(delay)
        yield item


async def test_timed_stream_measures_upstream_receipt_not_consumer_pace() -> None:
    """Chunks arrive 100ms apart; a consumer stalling 500ms after each still sees ~200ms.

    The stall exceeds the whole upstream span, so a measurement that subtracted stalls from
    consumer-side stamps (or stamped after yield) would be wrong; a reader task is not.
    """
    ts = TimedStream(_source(["a", "b", "c"], delay=0.1), lambda _c: True)
    seen = []
    async for item in ts:
        seen.append(item)
        await asyncio.sleep(0.5)
    assert seen == ["a", "b", "c"]
    elapsed = ts.ms()
    assert elapsed is not None
    assert 150 <= elapsed <= 320  # first-to-last upstream receipt (2 gaps of ~100ms)
    assert ts.tainted is False  # 3 chunks never filled the 256-slot queue


async def test_timed_stream_full_queue_marks_the_sample_tainted() -> None:
    """A consumer slower than the engine fills the bounded queue: tainted, backpressure applied."""
    ts = TimedStream(_source(list(range(10))), lambda _c: True, maxsize=2)
    out = []
    async for item in ts:
        out.append(item)
        await asyncio.sleep(0.01)
    assert out == list(range(10))  # nothing lost, order kept
    assert ts.tainted is True


async def test_timed_stream_propagates_source_errors_and_cancels_reader() -> None:
    """An upstream failure surfaces in the consumer; stopping early cancels the reader."""

    async def failing() -> Any:
        yield "a"
        raise RuntimeError("upstream broke")

    with pytest.raises(RuntimeError, match="upstream broke"):
        async for _ in TimedStream(failing(), lambda _c: True):
            pass

    closed = asyncio.Event()

    async def endless() -> Any:
        try:
            while True:
                yield "x"
                await asyncio.sleep(0)
        finally:
            closed.set()

    ts = TimedStream(endless(), lambda _c: True)
    async for _ in ts:
        break
    await asyncio.sleep(0.05)
    assert closed.is_set()


async def test_vllm_tainted_stream_is_labelled_proxy_tainted(
    fake_openai_server, monkeypatch
) -> None:
    """A backpressured stream is stamped proxy_tainted (and never sampled downstream)."""
    monkeypatch.setattr("shared.utils.llm_connectors.STREAM_QUEUE_MAX", 1)
    fake_openai_server.mode = "sse_usage"
    usage = await _slow_final(_vllm(fake_openai_server).stream_chat_completion(MSGS, "m"))
    assert usage["timing_source"] == "proxy_tainted"


async def test_vllm_engine_timing_unaffected_by_slow_consumer(fake_openai_server) -> None:
    """Engine-reported timings win; a consumer stalling between chunks changes nothing."""
    fake_openai_server.mode = "sse_usage"
    fake_openai_server.timings = {"predicted_ms": 1234.0, "predicted_n": 7}
    fast = [c async for c in _vllm(fake_openai_server).stream_chat_completion(MSGS, "m")]
    slow = await _slow_final(_vllm(fake_openai_server).stream_chat_completion(MSGS, "m"))
    assert fast[-1].usage is not None
    for usage in (fast[-1].usage, slow):
        assert (usage["generation_ms"], usage["timed_tokens"], usage["timing_source"]) == (
            1234,
            7,
            "engine",
        )


async def test_vllm_proxy_timing_excludes_consumer_stall(fake_openai_server) -> None:
    """No engine timing -> proxy-measured from receipt; the consumer's stalls are subtracted."""
    fake_openai_server.mode = "sse_usage"
    usage = await _slow_final(_vllm(fake_openai_server).stream_chat_completion(MSGS, "m"))
    assert usage["timing_source"] == "proxy" and "timed_tokens" not in usage
    # two content chunks, one STALL between them: unsubtracted this is >= 300ms
    assert usage["generation_ms"] < STALL * 1000 * 0.5


async def test_vllm_non_stream_prefers_engine_timings_and_rejects_garbage(
    fake_openai_server,
) -> None:
    """Non-stream: valid engine timings used; invalid ones fall back to proxy wall-clock."""
    fake_openai_server.mode = "json_usage"
    fake_openai_server.timings = {"predicted_ms": 900, "predicted_n": 7}
    _t, good = await _vllm(fake_openai_server).chat_completion(MSGS, "m")
    assert (good["generation_ms"], good["timing_source"]) == (900, "engine")
    fake_openai_server.timings = {"predicted_ms": -1, "predicted_n": 7}
    _t, bad = await _vllm(fake_openai_server).chat_completion(MSGS, "m")
    assert bad["timing_source"] == "proxy" and bad["generation_ms"] >= 1


async def test_ollama_uses_eval_duration_and_eval_count() -> None:
    """Ollama: eval_duration (ns) + eval_count are the engine timing, stream and non-stream."""

    async def chat(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        final = {
            "message": {"content": ""},
            "done": True,
            "prompt_eval_count": 9,
            "eval_count": 40,
            "eval_duration": 2_000_000_000,
        }
        if not body.get("stream"):
            return web.json_response({**final, "message": {"content": "one two"}})
        resp = web.StreamResponse()
        await resp.prepare(request)
        for obj in (
            {"message": {"content": "one "}, "done": False},
            {"message": {"content": "two"}, "done": False},
            final,
        ):
            await resp.write(json.dumps(obj).encode() + b"\n")
        return resp

    server = await _serve({"/api/chat": chat})
    try:
        conn = OllamaConnector("o", {"endpoint_url": str(server.make_url("")).rstrip("/")})
        _t, plain = await conn.chat_completion(MSGS, "m")
        stream = await _slow_final(conn.stream_chat_completion(MSGS, "m"))
        await conn.close()
    finally:
        await server.close()
    for usage in (plain, stream):
        assert (usage["generation_ms"], usage["timed_tokens"], usage["timing_source"]) == (
            2000,
            40,
            "engine",
        )


async def test_llamacpp_uses_predicted_ms_and_predicted_n() -> None:
    """llama.cpp: timings.predicted_ms/predicted_n are the engine timing."""
    timings = {"predicted_ms": 1500.0, "predicted_n": 30}
    usage_block = {"prompt_tokens": 8, "completion_tokens": 30, "total_tokens": 38}

    async def chat(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        if not body.get("stream"):
            return web.json_response(
                {
                    "choices": [{"message": {"content": "a b"}}],
                    "usage": usage_block,
                    "timings": timings,
                }
            )
        resp = web.StreamResponse()
        await resp.prepare(request)
        for obj in (
            {"choices": [{"delta": {"content": "a "}}]},
            {"choices": [{"delta": {"content": "b"}}]},
            {"choices": [], "usage": usage_block, "timings": timings},
        ):
            await resp.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
        await resp.write(b"data: [DONE]\n\n")
        return resp

    server = await _serve({"/v1/chat/completions": chat})
    try:
        conn = LlamaCppConnector("l", {"endpoint_url": str(server.make_url("")).rstrip("/")})
        _t, plain = await conn.chat_completion(MSGS, "m")
        stream = await _slow_final(conn.stream_chat_completion(MSGS, "m"))
        await conn.close()
    finally:
        await server.close()
    for usage in (plain, stream):
        assert (usage["generation_ms"], usage["timed_tokens"], usage["timing_source"]) == (
            1500,
            30,
            "engine",
        )


async def test_ollama_proxy_timing_excludes_consumer_stall() -> None:
    """Ollama without eval_duration: proxy-timed from receipt, stalls subtracted."""

    async def chat(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse()
        await resp.prepare(request)
        for obj in (
            {"message": {"content": "one "}, "done": False},
            {"message": {"content": "two"}, "done": False},
            {"message": {"content": ""}, "done": True},
        ):
            await resp.write(json.dumps(obj).encode() + b"\n")
        return resp

    server = await _serve({"/api/chat": chat})
    try:
        conn = OllamaConnector("o", {"endpoint_url": str(server.make_url("")).rstrip("/")})
        usage = await _slow_final(conn.stream_chat_completion(MSGS, "m"))
        await conn.close()
    finally:
        await server.close()
    assert usage["timing_source"] == "proxy"
    assert usage["generation_ms"] < STALL * 1000 * 0.5
