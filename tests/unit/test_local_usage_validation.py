"""Untrusted engine counts: validated, estimated-and-flagged when missing/garbage (never 0)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp import web

from shared.utils.llm_connectors import (
    USAGE_COUNT_CAP,
    LlamaCppConnector,
    OllamaConnector,
    ProviderServerError,
    VllmConnector,
    validate_token_count,
)
from tests.unit.test_local_usage_kind import MSGS, _caps, _collect, _serve

CAPS = {"local_deployment_id": 7, "fleet_status": 5, "max_model_len": 4096}


@pytest.mark.parametrize(
    ("value", "cap", "expected"),
    [
        (5, 100, 5),
        (0, 100, 0),
        (100, 100, 100),
        (101, 100, None),
        (-1, 100, None),
        (True, 100, None),
        (False, 100, None),
        (2.5, 100, None),
        ("7", 100, None),
        (None, 100, None),
        (10**12, USAGE_COUNT_CAP, None),
    ],
)
def test_validate_token_count(value: Any, cap: int, expected: int | None) -> None:
    """Only non-bool ints in [0, cap] are accepted."""
    assert validate_token_count(value, cap) == expected


def _vllm(server: Any, caps: dict[str, Any] | None = None) -> VllmConnector:
    """A vLLM connector wired to the in-process fake server."""
    conn = VllmConnector(
        "vllm-1-x",
        {
            "endpoint_url": "http://fake/v1",
            "api_key": "k",
            "model_list": ["m"],
            "management_capabilities": caps or CAPS,
        },
    )
    conn.client = server.sdk_client("k")
    return conn


@pytest.mark.parametrize("bad", [None, -5, 10**9, True])
async def test_vllm_non_stream_bad_prompt_count_is_estimated(fake_openai_server, bad) -> None:
    """A missing/garbage prompt_tokens is estimated for THAT field and flagged."""
    fake_openai_server.mode = "json_usage"
    fake_openai_server.usage_override = {"prompt_tokens": bad, "completion_tokens": 7}
    _text, usage = await _vllm(fake_openai_server).chat_completion(MSGS, "m")
    assert usage["estimated"] is True
    assert usage["input_tokens"] > 0 and usage["input_tokens"] != bad
    assert usage["output_tokens"] == 7  # the valid field is kept as reported
    assert usage["total_tokens"] == usage["input_tokens"] + 7


async def test_vllm_non_stream_bad_output_count_is_estimated(fake_openai_server) -> None:
    """A garbage completion_tokens is estimated; the valid prompt count is kept."""
    fake_openai_server.usage_override = {"prompt_tokens": 11, "completion_tokens": 10**9}
    _text, usage = await _vllm(fake_openai_server).chat_completion(MSGS, "m", max_tokens=50)
    assert usage["estimated"] is True and usage["input_tokens"] == 11
    assert 0 < usage["output_tokens"] < 1000


async def test_vllm_stream_bad_counts_estimated_and_counted(
    fake_openai_server, metric_reader
) -> None:
    """A streamed usage chunk with a negative count is estimated, flagged and counted."""
    from tests.unit.fixtures.otel import points

    fake_openai_server.mode = "sse_usage"
    fake_openai_server.usage_override = {"prompt_tokens": -3, "completion_tokens": 7}
    final = await _collect(_vllm(fake_openai_server).stream_chat_completion(MSGS, "m"))
    assert final.usage is not None and final.usage["estimated"] is True
    assert final.usage["input_tokens"] > 0 and final.usage["output_tokens"] == 7
    assert points(metric_reader, "waddleai_vllm_usage_missing_total")


async def test_ollama_missing_prompt_count_is_estimated_not_zero() -> None:
    """Ollama final chunk without prompt_eval_count: input estimated, row flagged."""

    async def chat(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse()
        await resp.prepare(request)
        for obj in (
            {"message": {"content": "hello there"}, "done": False},
            {"message": {"content": ""}, "done": True, "eval_count": 4},
        ):
            await resp.write(json.dumps(obj).encode() + b"\n")
        return resp

    server = await _serve({"/api/chat": chat})
    try:
        conn = OllamaConnector(
            "o", {"endpoint_url": str(server.make_url("")).rstrip("/"), "api_key": "k"}
        )
        final = await _collect(conn.stream_chat_completion(MSGS, "m"))
        await conn.close()
    finally:
        await server.close()
    assert final.usage is not None and final.usage["estimated"] is True
    assert final.usage["input_tokens"] > 0 and final.usage["output_tokens"] == 4


async def test_ollama_non_stream_uses_valid_real_counts_and_rejects_garbage() -> None:
    """Non-stream Ollama: valid counts kept (estimated False); garbage -> flagged estimate."""

    async def chat(request: web.Request) -> web.Response:
        body = await request.json()
        extra = (
            {"prompt_eval_count": 9, "eval_count": 4}
            if body["model"] == "good"
            else {"prompt_eval_count": -1, "eval_count": 10**12}
        )
        return web.json_response({"message": {"content": "one two"}, "done": True, **extra})

    server = await _serve({"/api/chat": chat})
    try:
        conn = OllamaConnector(
            "o", {"endpoint_url": str(server.make_url("")).rstrip("/"), "api_key": "k"}
        )
        _t, good = await conn.chat_completion(MSGS, "good")
        _t, bad = await conn.chat_completion(MSGS, "bad")
        await conn.close()
    finally:
        await server.close()
    assert (good["input_tokens"], good["output_tokens"], good["estimated"]) == (9, 4, False)
    assert bad["estimated"] is True and 0 < bad["input_tokens"] < 1000
    assert 0 < bad["output_tokens"] < 1000


async def test_llamacpp_non_stream_builds_fresh_validated_usage() -> None:
    """llama.cpp non-stream: extra/garbage server keys are dropped; counts validated."""

    async def chat(_r: web.Request) -> web.Response:
        return web.json_response(
            {
                "choices": [{"message": {"content": "one two three"}}],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": -9,
                    "total_tokens": 10**12,
                    "input_tokens": 10**12,
                    "evil": "x",
                },
            }
        )

    server = await _serve({"/v1/chat/completions": chat})
    try:
        conn = LlamaCppConnector(
            "l",
            {"endpoint_url": str(server.make_url("")).rstrip("/"), "api_key": "k", **_caps(3, 4)},
        )
        _t, usage = await conn.chat_completion(MSGS, "m")
        await conn.close()
    finally:
        await server.close()
    assert "evil" not in usage
    assert usage["input_tokens"] == 8 and usage["estimated"] is True
    assert 0 < usage["output_tokens"] < 1000
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]


async def test_fleet_ollama_sends_bearer_key() -> None:
    """A fleet-managed Ollama link authenticates every request with its deployment key."""
    seen: list[str | None] = []

    async def chat(request: web.Request) -> web.Response:
        seen.append(request.headers.get("Authorization"))
        return web.json_response(
            {"message": {"content": "x"}, "done": True, "prompt_eval_count": 1, "eval_count": 1}
        )

    server = await _serve({"/api/chat": chat})
    try:
        conn = OllamaConnector(
            "o",
            {
                "endpoint_url": str(server.make_url("")).rstrip("/"),
                "api_key": "dep-key",
                **_caps(2, 9),
            },
        )
        await conn.chat_completion(MSGS, "m")
        await conn.close()
    finally:
        await server.close()
    assert seen == ["Bearer dep-key"]


async def test_fleet_ollama_without_key_fails_closed() -> None:
    """A fleet Ollama link with no key refuses before sending; a legacy link is unchanged."""
    hits: list[int] = []

    async def chat(_r: web.Request) -> web.Response:
        hits.append(1)
        return web.json_response(
            {"message": {"content": "x"}, "done": True, "prompt_eval_count": 1, "eval_count": 1}
        )

    server = await _serve({"/api/chat": chat})
    url = str(server.make_url("")).rstrip("/")
    try:
        fleet = OllamaConnector("o", {"endpoint_url": url, **_caps(2, 9)})
        with pytest.raises(ProviderServerError, match="api key"):
            await fleet.chat_completion(MSGS, "m")
        with pytest.raises(ProviderServerError, match="api key"):
            await _collect(fleet.stream_chat_completion(MSGS, "m"))
        assert hits == []
        legacy = OllamaConnector("o2", {"endpoint_url": url})
        await legacy.chat_completion(MSGS, "m")
        await fleet.close()
        await legacy.close()
    finally:
        await server.close()
    assert hits == [1]
