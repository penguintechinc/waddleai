"""Synthetic-SSE replay byte-equivalence tests (spec §6.1/§6.5)."""

import orjson

from shared.cache.exact import CachedResponse
from shared.cache.replay import replay_anthropic_sse, replay_openai_sse


def _parse_openai_sse(lines: list) -> list:
    """Parse `data: {...}` frames (excluding [DONE]) into dicts."""
    parsed = []
    for line in lines:
        text = line.decode()
        assert text.endswith("\n\n")
        payload = text[len("data: ") : -2]
        if payload == "[DONE]":
            continue
        parsed.append(orjson.loads(payload))
    return parsed


class TestOpenAIReplay:
    """Tests for open a i replay."""

    def _cached(self, content="Hello, world! This is a cached response.", finish_reason="stop"):
        """Cached."""
        return CachedResponse(
            response={
                "id": "chatcmpl-abc123",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 8,
                    "total_tokens": 18,
                    "waddleai_tokens": 2,
                },
            },
            usage={"input_tokens": 10, "output_tokens": 8},
            stored_at=1000.0,
        )

    async def test_byte_equivalence_and_framing(self):
        """Byte equivalence and framing."""
        cached = self._cached()
        chunks = [c async for c in replay_openai_sse(cached)]

        assert chunks[-1] == b"data: [DONE]\n\n"

        parsed = _parse_openai_sse(chunks[:-1])
        assert parsed[0]["choices"][0]["delta"] == {"role": "assistant"}

        assembled = "".join(c["choices"][0]["delta"].get("content", "") for c in parsed)
        assert assembled == cached.response["choices"][0]["message"]["content"]

        last = parsed[-1]
        assert last["choices"][0]["finish_reason"] == "stop"

    async def test_replayed_usage_equals_cached_usage(self):
        """Replayed usage equals cached usage."""
        cached = self._cached()
        chunks = [c async for c in replay_openai_sse(cached)]
        parsed = _parse_openai_sse(chunks[:-1])
        last = parsed[-1]
        assert last["usage"] == cached.response["usage"]

    async def test_id_and_created_consistent_across_frames(self):
        """Id and created consistent across frames."""
        cached = self._cached()
        chunks = [c async for c in replay_openai_sse(cached)]
        parsed = _parse_openai_sse(chunks[:-1])
        ids = {c["id"] for c in parsed}
        created = {c["created"] for c in parsed}
        assert ids == {cached.response["id"]}
        assert created == {cached.response["created"]}

    async def test_empty_content_still_frames_role_and_finish(self):
        """Empty content still frames role and finish."""
        cached = self._cached(content="")
        chunks = [c async for c in replay_openai_sse(cached)]
        parsed = _parse_openai_sse(chunks[:-1])
        assert parsed[0]["choices"][0]["delta"] == {"role": "assistant"}
        assert parsed[-1]["choices"][0]["finish_reason"] == "stop"


def _parse_anthropic_sse(lines: list) -> list:
    """Parse anthropic sse."""
    events = []
    for line in lines:
        text = line.decode()
        assert text.startswith("event: ")
        assert text.endswith("\n\n")
        _, rest = text.split("\n", 1)
        assert rest.startswith("data: ")
        payload = rest[len("data: ") : -2]
        events.append(orjson.loads(payload))
    return events


class TestAnthropicReplay:
    """Tests for anthropic replay."""

    def _cached_text(self, text="Hello from the cache.", stop_reason="end_turn"):
        """Cached text."""
        return CachedResponse(
            response={
                "id": "msg_abc123",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "model": "claude-3-5-sonnet-latest",
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 6},
            },
            usage={"input_tokens": 12, "output_tokens": 6},
            stored_at=1000.0,
        )

    async def test_event_sequence_order(self):
        """Event sequence order."""
        cached = self._cached_text()
        events = _parse_anthropic_sse([c async for c in replay_anthropic_sse(cached)])
        types = [e["type"] for e in events]
        assert types == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]

    async def test_assembled_text_matches_cached_content(self):
        """Assembled text matches cached content."""
        cached = self._cached_text(text="A somewhat longer cached response to exercise chunking.")
        events = _parse_anthropic_sse([c async for c in replay_anthropic_sse(cached)])
        deltas = [e for e in events if e["type"] == "content_block_delta"]
        assembled = "".join(d["delta"]["text"] for d in deltas)
        assert assembled == cached.response["content"][0]["text"]

    async def test_message_delta_carries_stop_reason_and_usage(self):
        """Message delta carries stop reason and usage."""
        cached = self._cached_text(stop_reason="end_turn")
        events = _parse_anthropic_sse([c async for c in replay_anthropic_sse(cached)])
        message_delta = next(e for e in events if e["type"] == "message_delta")
        assert message_delta["delta"]["stop_reason"] == "end_turn"
        assert message_delta["usage"] == cached.response["usage"]

    async def test_tool_use_block_replays_as_input_json_delta(self):
        """Tool use block replays as input json delta."""
        cached = CachedResponse(
            response={
                "id": "msg_tooluse",
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_weather",
                        "input": {"city": "Dallas"},
                    }
                ],
                "model": "claude-3-5-sonnet-latest",
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 20, "output_tokens": 15},
            },
            usage={"input_tokens": 20, "output_tokens": 15},
            stored_at=1000.0,
        )
        events = _parse_anthropic_sse([c async for c in replay_anthropic_sse(cached)])

        start = next(e for e in events if e["type"] == "content_block_start")
        assert start["content_block"]["type"] == "tool_use"
        assert start["content_block"]["id"] == "toolu_1"
        assert start["content_block"]["name"] == "get_weather"

        deltas = [e for e in events if e["type"] == "content_block_delta"]
        assert all(d["delta"]["type"] == "input_json_delta" for d in deltas)
        assembled_json = "".join(d["delta"]["partial_json"] for d in deltas)
        assert orjson.loads(assembled_json) == {"city": "Dallas"}

    async def test_ids_consistent_across_frames(self):
        """Ids consistent across frames."""
        cached = self._cached_text()
        events = _parse_anthropic_sse([c async for c in replay_anthropic_sse(cached)])
        message_start = next(e for e in events if e["type"] == "message_start")
        assert message_start["message"]["id"] == cached.response["id"]
