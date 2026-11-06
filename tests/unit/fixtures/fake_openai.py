"""An in-process OpenAI-compatible server (Quart over httpx ASGITransport), no sockets."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import openai
import pytest
from quart import Quart, Response, request

MODES = ("json_usage", "json_no_usage", "sse_usage", "sse_no_usage")


@dataclass(slots=True)
class FakeOpenAIServer:
    """Records every request body and replies per ``mode`` (usage present or absent)."""

    mode: str = "json_usage"
    requests: list[dict[str, Any]] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)
    usage_override: dict[str, Any] | None = None
    app: Quart = field(default_factory=lambda: Quart("fake_openai"))

    def sdk_client(self, api_key: str = "test-key") -> openai.AsyncOpenAI:
        """An AsyncOpenAI client whose transport is this app (in-process)."""
        transport = httpx.ASGITransport(app=self.app)
        return openai.AsyncOpenAI(
            api_key=api_key,
            base_url="http://fake/v1",
            http_client=httpx.AsyncClient(transport=transport),
        )


def _build() -> FakeOpenAIServer:
    """Create the server and register ``POST /v1/chat/completions``."""
    server = FakeOpenAIServer()

    @server.app.post("/v1/chat/completions")
    async def completions() -> Response:
        body = await request.get_json()
        server.requests.append(body)
        server.headers.append(dict(request.headers))
        usage = server.usage_override or {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        }
        base = {"id": "c1", "created": 1, "model": "m"}
        if server.mode.startswith("json"):
            payload: dict[str, Any] = {
                **base,
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "hello there"},
                    }
                ],
            }
            if server.mode == "json_usage":
                payload["usage"] = usage
            return Response(json.dumps(payload), content_type="application/json")

        def chunk(delta: dict[str, Any], **extra: Any) -> str:
            data = {
                **base,
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                **extra,
            }
            return f"data: {json.dumps(data)}\n\n"

        async def gen() -> Any:
            yield chunk({"role": "assistant", "content": "hello "})
            yield chunk({"content": "there"})
            if server.mode == "sse_usage":
                final = {**base, "object": "chat.completion.chunk", "choices": [], "usage": usage}
                yield f"data: {json.dumps(final)}\n\n"
            yield "data: [DONE]\n\n"

        return Response(gen(), content_type="text/event-stream")

    return server


@pytest.fixture
def fake_openai_server() -> FakeOpenAIServer:
    """A fresh fake OpenAI server per test."""
    return _build()
