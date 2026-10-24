"""Regression: Anthropic connector must omit ``system`` when there is no system prompt."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import pytest

from shared.utils.llm_connectors import AnthropicConnector, ProviderClientError

MODEL = "claude-3-haiku-20240307"
USER = [{"role": "user", "content": "Hello"}]
WITH_SYSTEM = [{"role": "system", "content": "Be terse."}, *USER]


def _connector() -> tuple[AnthropicConnector, MagicMock]:
    """Build a connector whose SDK client is a mock (no network)."""
    with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as cls:
        client = MagicMock()
        cls.return_value = client
        conn = AnthropicConnector("t", {"api_key": "k", "model_list": [MODEL]})
    return conn, client


def _resp() -> MagicMock:
    """Fake SDK message with real-looking usage."""
    r = MagicMock()
    r.content = [MagicMock(text="hi")]
    r.stop_reason = "end_turn"
    r.usage = MagicMock(input_tokens=1, output_tokens=1)
    return r


class _FakeStream:
    """Async-context stream yielding no events."""

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    def __aiter__(self) -> "_FakeStream":
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration

    async def get_final_message(self) -> MagicMock:
        return _resp()


@pytest.mark.asyncio
@pytest.mark.parametrize(("msgs", "expect"), [(USER, None), (WITH_SYSTEM, "Be terse.")])
async def test_chat_system_kwarg(msgs: list[dict[str, str]], expect: str | None) -> None:
    """Sync path: system omitted when absent, passed verbatim when present."""
    conn, client = _connector()
    client.messages.create = AsyncMock(return_value=_resp())
    await conn.chat_completion(msgs, MODEL)
    kw = client.messages.create.call_args.kwargs
    # regression: Anthropic 400 'system: Input should be a valid array' when system=None
    if expect is None:
        assert "system" not in kw
    else:
        assert kw["system"] == expect


@pytest.mark.asyncio
@pytest.mark.parametrize(("msgs", "expect"), [(USER, None), (WITH_SYSTEM, "Be terse.")])
async def test_stream_system_kwarg(msgs: list[dict[str, str]], expect: str | None) -> None:
    """Streaming path: same omit-when-empty contract."""
    conn, client = _connector()
    client.messages.stream = MagicMock(return_value=_FakeStream())
    async for _ in conn.stream_chat_completion(msgs, MODEL):
        pass
    kw = client.messages.stream.call_args.kwargs
    # regression: Anthropic 400 'system: Input should be a valid array' when system=None
    if expect is None:
        assert "system" not in kw
    else:
        assert kw["system"] == expect


@pytest.mark.asyncio
async def test_4xx_logs_detail_but_client_message_generic(caplog: pytest.LogCaptureFixture) -> None:
    """Upstream 4xx detail is logged server-side, never put in the raised message."""
    conn, client = _connector()
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.BadRequestError(
        "secret upstream reason", response=httpx.Response(400, request=req), body={"e": "reason-x"}
    )
    client.messages.create = AsyncMock(side_effect=err)
    with caplog.at_level("WARNING"), pytest.raises(ProviderClientError) as ei:
        await conn.chat_completion(USER, MODEL)
    assert "reason-x" in caplog.text
    assert "reason-x" not in str(ei.value)
    assert "secret upstream reason" not in str(ei.value)
