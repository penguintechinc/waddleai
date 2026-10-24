"""Regression: Anthropic connector must omit ``system`` when there is no system prompt."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import pytest

from shared.utils.llm_connectors import AnthropicConnector, ProviderClientError, ProviderServerError

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


class _Block:
    """Minimal SDK content-block double exposing only the given attributes."""

    def __init__(self, **attrs: Any) -> None:
        self.__dict__.update(attrs)


@pytest.mark.asyncio
async def test_leading_non_text_block_is_skipped() -> None:
    """Text is extracted even when a thinking block precedes it."""
    conn, client = _connector()
    r = _resp()
    # regression: Anthropic 502 'first content block was not text'
    # (thinking/tool_use block can precede text)
    r.content = [_Block(type="thinking", thinking="hmm"), _Block(type="text", text="hello")]
    client.messages.create = AsyncMock(return_value=r)
    out = await conn.chat_completion(USER, MODEL)
    assert out[0] == "hello"


@pytest.mark.asyncio
async def test_no_text_block_raises() -> None:
    """A response with only non-text blocks is a ProviderServerError."""
    conn, client = _connector()
    r = _resp()
    # regression: Anthropic 502 'first content block was not text'
    # (thinking/tool_use block can precede text)
    r.content = [_Block(type="thinking", thinking="hmm"), _Block(type="tool_use")]
    r.stop_reason = None  # no stop_reason = genuinely malformed -> fail loud
    client.messages.create = AsyncMock(return_value=r)
    with pytest.raises(ProviderServerError, match="no text content block"):
        await conn.chat_completion(USER, MODEL)


def _thinking_only(stop_reason: str) -> MagicMock:
    """Thinking-only response with real (non-zero) usage and the given stop_reason."""
    r = _resp()
    r.content = [_Block(type="thinking", thinking="hmm")]
    r.stop_reason = stop_reason
    r.usage = MagicMock(input_tokens=11, output_tokens=2048)
    return r


@pytest.mark.asyncio
async def test_max_tokens_empty_text_returns_failure_reason() -> None:
    """Budget exhausted by thinking: graceful empty completion, usage still real."""
    conn, client = _connector()
    # regression: gh — thinking model exhausts max_tokens before text; return
    # truncated completion w/ failure_reason, no retry
    client.messages.create = AsyncMock(return_value=_thinking_only("max_tokens"))
    content, usage = await conn.chat_completion(USER, MODEL)
    assert content == ""
    assert usage["failure_reason"] == "output_budget_exhausted"
    assert usage["finish_reason"] == "max_tokens"
    assert (usage["input_tokens"], usage["output_tokens"]) == (11, 2048)
    assert usage["total_tokens"] == 2059


@pytest.mark.asyncio
async def test_refusal_empty_text_meters_real_usage_no_raise() -> None:
    """Refusal (HTTP 200, no text) returns gracefully with real usage + failure_reason."""
    conn, client = _connector()
    # regression: refusal/empty-but-valid responses must meter real usage + return
    # failure_reason, never raise/retry/discard-usage
    client.messages.create = AsyncMock(return_value=_thinking_only("refusal"))
    content, usage = await conn.chat_completion(USER, MODEL)
    assert content == ""
    assert usage["failure_reason"] == "refusal"
    assert (usage["input_tokens"], usage["output_tokens"]) == (11, 2048)
    assert usage["total_tokens"] == 2059


@pytest.mark.asyncio
async def test_pause_turn_empty_text_generic_failure_reason() -> None:
    """Any other valid stop_reason maps to empty_response_<reason>, no raise."""
    conn, client = _connector()
    # regression: refusal/empty-but-valid responses must meter real usage + return
    # failure_reason, never raise/retry/discard-usage
    client.messages.create = AsyncMock(return_value=_thinking_only("pause_turn"))
    content, usage = await conn.chat_completion(USER, MODEL)
    assert content == ""
    assert usage["failure_reason"] == "empty_response_pause_turn"
    assert usage["output_tokens"] == 2048


@pytest.mark.asyncio
async def test_empty_text_no_stop_reason_still_raises() -> None:
    """Fail-loud preserved only for a true anomaly: empty text AND no stop_reason."""
    conn, client = _connector()
    r = _thinking_only("end_turn")
    r.stop_reason = None
    client.messages.create = AsyncMock(return_value=r)
    with pytest.raises(ProviderServerError, match="no text content block"):
        await conn.chat_completion(USER, MODEL)


@pytest.mark.asyncio
async def test_stream_refusal_empty_failure_reason() -> None:
    """Streaming: empty stream + refusal -> done chunk carries failure_reason, no raise."""
    conn, client = _connector()

    class _S(_FakeStream):
        async def get_final_message(self) -> MagicMock:
            return _thinking_only("refusal")

    client.messages.stream = MagicMock(return_value=_S())
    # regression: refusal/empty-but-valid responses must meter real usage + return
    # failure_reason, never raise/retry/discard-usage
    chunks = [c async for c in conn.stream_chat_completion(USER, MODEL)]
    assert chunks[-1].done is True
    assert chunks[-1].usage is not None
    assert chunks[-1].usage["failure_reason"] == "refusal"
    assert chunks[-1].usage["output_tokens"] == 2048


def test_openai_finish_reason_mapping() -> None:
    """failure_reason maps to OpenAI finish_reason without forcing 'length'."""
    from proxy.apps.proxy_server.streaming import openai_finish_reason

    assert openai_finish_reason("output_budget_exhausted", "max_tokens") == "length"
    assert openai_finish_reason("refusal", "refusal") == "content_filter"
    assert openai_finish_reason("empty_response_pause_turn", "pause_turn") == "pause_turn"
    assert openai_finish_reason(None, None) == "stop"


@pytest.mark.asyncio
async def test_stream_max_tokens_empty_text_failure_reason() -> None:
    """Streaming: no text + max_tokens -> terminal chunk carries failure_reason."""
    conn, client = _connector()

    class _S(_FakeStream):
        async def get_final_message(self) -> MagicMock:
            return _thinking_only("max_tokens")

    client.messages.stream = MagicMock(return_value=_S())
    chunks = [c async for c in conn.stream_chat_completion(USER, MODEL)]
    assert chunks[-1].done is True
    assert chunks[-1].usage is not None
    assert chunks[-1].usage["failure_reason"] == "output_budget_exhausted"
    assert chunks[-1].usage["output_tokens"] == 2048
