"""Synthetic-SSE replay of cached responses (spec §6.1).

A streaming request that hits the cache must be indistinguishable from a
genuine streaming miss: same wire framing, same content, same final usage
accounting -- just faster, because there is zero upstream latency to hide
(replay never sleeps between chunks). This module decomposes a cached
*non-streaming* response JSON (``CachedResponse.response``, exactly what
``shared.cache.exact.ExactCache`` stored, i.e. the client-facing response
body -- not the internal provider ``usage`` dict) into synthetic SSE chunks
in the requesting endpoint's own wire format.

``replay_openai_sse`` covers ``/v1/chat/completions`` framing
(``chat.completion.chunk`` objects terminated by ``data: [DONE]``);
``replay_anthropic_sse`` covers ``/v1/messages`` framing (the
``message_start`` -> ``content_block_start`` -> ``content_block_delta``* ->
``content_block_stop`` -> ``message_delta`` -> ``message_stop`` event
sequence, including ``input_json_delta`` framing for cached tool_use blocks).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import orjson

from shared.cache.exact import CachedResponse

# Arbitrary but deterministic chunk size for synthetic deltas.
_CHUNK_CHARS = 24


def _chunk_text(text: str, size: int = _CHUNK_CHARS) -> list[str]:
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]


async def replay_openai_sse(cached: CachedResponse) -> AsyncIterator[bytes]:
    """Replay a cached OpenAI chat.completion response as SSE chunks.

    Frame sequence: one role-only chunk, N content-delta chunks, one final
    chunk carrying both ``finish_reason`` and (if present) the cached
    ``usage`` block, then ``data: [DONE]``.
    """
    body = cached.response
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    finish_reason = choice.get("finish_reason")
    response_id = body.get("id") or f"chatcmpl-{int(time.time())}"
    created = body.get("created", int(time.time()))
    model = body.get("model", "")
    usage = body.get("usage")

    def _frame(
        delta: dict[str, Any], finish_reason_value: Any = None, include_usage: bool = False
    ) -> bytes:
        chunk: dict[str, Any] = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason_value}],
        }
        if include_usage and usage is not None:
            chunk["usage"] = usage
        return b"data: " + orjson.dumps(chunk) + b"\n\n"

    yield _frame({"role": "assistant"})

    for piece in _chunk_text(content):
        yield _frame({"content": piece})

    yield _frame({}, finish_reason_value=finish_reason, include_usage=True)

    yield b"data: [DONE]\n\n"


def _event_frame(event: str, data: dict[str, Any]) -> bytes:
    return b"event: " + event.encode() + b"\ndata: " + orjson.dumps(data) + b"\n\n"


async def replay_anthropic_sse(cached: CachedResponse) -> AsyncIterator[bytes]:
    """Replay a cached Anthropic Messages response as an SSE event sequence.

    Handles both text blocks (``text_delta`` chunks) and cached ``tool_use``
    blocks (``input_json_delta`` chunks of the JSON-serialized ``input``),
    in the order they appear in ``cached.response["content"]``.
    """
    body = cached.response
    response_id = body.get("id") or f"msg_{int(time.time() * 1000)}"
    model = body.get("model", "")
    stop_reason = body.get("stop_reason")
    stop_sequence = body.get("stop_sequence")
    usage = body.get("usage") or {}
    content_blocks = body.get("content") or []

    yield _event_frame(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": response_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": 0},
            },
        },
    )

    for index, block in enumerate(content_blocks):
        block_type = block.get("type")

        if block_type == "tool_use":
            yield _event_frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": {},
                    },
                },
            )
            partial_json = orjson.dumps(block.get("input") or {}).decode()
            for piece in _chunk_text(partial_json):
                yield _event_frame(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": piece},
                    },
                )
        else:
            # Default to a text block for "text" and any unrecognized type,
            # so an unexpected block shape degrades to replaying its text
            # rather than silently dropping it.
            text = block.get("text", "")
            yield _event_frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            for piece in _chunk_text(text):
                yield _event_frame(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": piece},
                    },
                )

        yield _event_frame("content_block_stop", {"type": "content_block_stop", "index": index})

    yield _event_frame(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": stop_sequence},
            "usage": usage,
        },
    )
    yield _event_frame("message_stop", {"type": "message_stop"})
