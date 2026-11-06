"""Ollama and llama.cpp usage carries the local deployment kind/id and real counts (AM10)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from shared.utils.llm_connectors import (
    LlamaCppConnector,
    OllamaConnector,
    ProviderServerError,
    StreamChunk,
)

MSGS = [{"role": "user", "content": "count to three please"}]

pytestmark = pytest.mark.usefixtures("restore_obs_meter_cache")


async def _serve(handlers: dict[str, Any]) -> TestServer:
    """A throwaway aiohttp server with the given path handlers."""
    app = web.Application()
    for path, handler in handlers.items():
        app.router.add_post(path, handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def _ollama_chat(request: web.Request) -> web.StreamResponse:
    """Ollama /api/chat: non-stream JSON, or NDJSON stream with real final counts."""
    body = await request.json()
    if not body.get("stream"):
        return web.json_response(
            {"message": {"content": "one two three"}, "done": True, "done_reason": "stop"}
        )
    resp = web.StreamResponse()
    await resp.prepare(request)
    for obj in (
        {"message": {"content": "one "}, "done": False},
        {"message": {"content": "two"}, "done": False},
        {"message": {"content": ""}, "done": True, "prompt_eval_count": 9, "eval_count": 4},
    ):
        await resp.write(json.dumps(obj).encode() + b"\n")
    return resp


async def _llama_chat(request: web.Request) -> web.StreamResponse:
    """llama-server /v1/chat/completions: JSON with usage, or SSE with a usage chunk."""
    body = await request.json()
    usage = {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}
    if not body.get("stream"):
        return web.json_response(
            {"choices": [{"message": {"content": "one two three"}}], "usage": usage}
        )
    resp = web.StreamResponse()
    await resp.prepare(request)
    for obj in (
        {"choices": [{"delta": {"content": "one "}}]},
        {"choices": [{"delta": {"content": "two"}}]},
        {"choices": [], "usage": usage},
    ):
        await resp.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
    await resp.write(b"data: [DONE]\n\n")
    return resp


async def _collect(stream: AsyncIterator[StreamChunk]) -> StreamChunk:
    """Drain a stream and return its final chunk."""
    chunks = [c async for c in stream]
    assert chunks[-1].done
    return chunks[-1]


def _caps(kind: int, dep_id: int) -> dict[str, Any]:
    """management_capabilities as written on the link by the fleet sync."""
    return {
        "management_capabilities": {"local_deployment_kind": kind, "local_deployment_id": dep_id}
    }


async def test_ollama_usage_stamps_kind_and_id_from_link() -> None:
    """Non-stream Ollama usage carries kind 2 and the deployment id."""
    server = await _serve({"/api/chat": _ollama_chat})
    try:
        conn = OllamaConnector(
            "o",
            {"endpoint_url": str(server.make_url("")).rstrip("/"), "api_key": "k", **_caps(2, 9)},
        )
        _text, usage = await conn.chat_completion(MSGS, "m")
        await conn.close()
    finally:
        await server.close()
    assert (usage["local_deployment_kind"], usage["local_deployment_id"]) == (2, 9)
    assert usage["provider"] == "ollama"


async def test_llamacpp_usage_stamps_kind_and_id_from_link() -> None:
    """Non-stream llama.cpp usage carries kind 3, the id and metered token names."""
    server = await _serve({"/v1/chat/completions": _llama_chat})
    try:
        conn = LlamaCppConnector(
            "l",
            {"endpoint_url": str(server.make_url("")).rstrip("/"), "api_key": "k", **_caps(3, 4)},
        )
        _text, usage = await conn.chat_completion(MSGS, "m")
        await conn.close()
    finally:
        await server.close()
    assert (usage["local_deployment_kind"], usage["local_deployment_id"]) == (3, 4)
    assert (usage["input_tokens"], usage["output_tokens"]) == (8, 3)
    assert usage["provider"] == "llamacpp"


async def test_stream_usage_stamps_kind_and_real_counts() -> None:
    """Final stream chunks of both connectors carry kind/id and the engine's real counts."""
    server = await _serve({"/api/chat": _ollama_chat, "/v1/chat/completions": _llama_chat})
    url = str(server.make_url("")).rstrip("/")
    try:
        oll = OllamaConnector("o", {"endpoint_url": url, "api_key": "k", **_caps(2, 9)})
        llm = LlamaCppConnector("l", {"endpoint_url": url, "api_key": "k", **_caps(3, 4)})
        o_final = await _collect(oll.stream_chat_completion(MSGS, "m"))
        l_final = await _collect(llm.stream_chat_completion(MSGS, "m"))
        await oll.close()
        await llm.close()
    finally:
        await server.close()
    assert o_final.usage is not None and l_final.usage is not None
    assert (o_final.usage["local_deployment_kind"], o_final.usage["local_deployment_id"]) == (2, 9)
    assert (l_final.usage["local_deployment_kind"], l_final.usage["local_deployment_id"]) == (3, 4)
    assert (o_final.usage["input_tokens"], o_final.usage["output_tokens"]) == (9, 4)
    assert (l_final.usage["input_tokens"], l_final.usage["output_tokens"]) == (8, 3)
    assert o_final.usage["estimated"] is False and l_final.usage["estimated"] is False


async def test_stream_without_engine_counts_is_estimated_not_none() -> None:
    """A stream whose engine reports no counts is metered from an estimate, flagged."""

    async def bare(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse()
        await resp.prepare(request)
        await resp.write(
            json.dumps({"message": {"content": "hello there"}, "done": False}).encode() + b"\n"
        )
        await resp.write(json.dumps({"message": {"content": ""}, "done": True}).encode() + b"\n")
        return resp

    server = await _serve({"/api/chat": bare})
    try:
        conn = OllamaConnector("o", {"endpoint_url": str(server.make_url("")).rstrip("/")})
        final = await _collect(conn.stream_chat_completion(MSGS, "m"))
        await conn.close()
    finally:
        await server.close()
    assert final.usage is not None and final.usage["estimated"] is True
    assert final.usage["output_tokens"] > 0 and final.usage["input_tokens"] > 0


async def test_link_without_capabilities_leaves_kind_none() -> None:
    """A commercial/legacy link stamps None/None and keeps its provider label."""
    server = await _serve({"/api/chat": _ollama_chat})
    try:
        conn = OllamaConnector("o", {"endpoint_url": str(server.make_url("")).rstrip("/")})
        _text, usage = await conn.chat_completion(MSGS, "m")
        await conn.close()
    finally:
        await server.close()
    assert usage["local_deployment_kind"] is None and usage["local_deployment_id"] is None
    assert usage["provider"] == "ollama"


async def test_fleet_managed_llamacpp_without_key_fails_closed() -> None:
    """A fleet llama.cpp link with no per-deployment key refuses before sending (AM11)."""
    hits: list[int] = []

    async def spy(_request: web.Request) -> web.Response:
        hits.append(1)
        return web.json_response({})

    server = await _serve({"/v1/chat/completions": spy})
    try:
        conn = LlamaCppConnector(
            "l", {"endpoint_url": str(server.make_url("")).rstrip("/"), **_caps(3, 4)}
        )
        with pytest.raises(ProviderServerError, match="api key"):
            await conn.chat_completion(MSGS, "m")
        with pytest.raises(ProviderServerError, match="api key"):
            await _collect(conn.stream_chat_completion(MSGS, "m"))
        await conn.close()
    finally:
        await server.close()
    assert hits == []


async def test_ollama_and_llamacpp_stamp_generation_ms() -> None:
    """Ollama/llama.cpp usage (stream and non-stream) carries generation_ms for throughput."""
    server = await _serve({"/api/chat": _ollama_chat, "/v1/chat/completions": _llama_chat})
    url = str(server.make_url("")).rstrip("/")
    try:
        oll = OllamaConnector("o", {"endpoint_url": url})
        llm = LlamaCppConnector("l", {"endpoint_url": url})
        _t, o_usage = await oll.chat_completion(MSGS, "m")
        _t, l_usage = await llm.chat_completion(MSGS, "m")
        o_final = await _collect(oll.stream_chat_completion(MSGS, "m"))
        l_final = await _collect(llm.stream_chat_completion(MSGS, "m"))
        await oll.close()
        await llm.close()
    finally:
        await server.close()
    for usage in (o_usage, l_usage, o_final.usage, l_final.usage):
        assert usage is not None and isinstance(usage["generation_ms"], int)
        assert usage["generation_ms"] >= 1
