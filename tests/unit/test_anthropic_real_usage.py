"""Billing-truth tests: Anthropic real usage + DispatchStage max_tokens forwarding."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from proxy.apps.proxy_server.pipeline.stages import DispatchStage, PipelineContext
from shared.utils.llm_connectors import AnthropicConnector, OpenAIConnector
from shared.utils.metrics import get_proxy_metrics

MODEL = "claude-3-haiku-20240307"
MESSAGES = [{"role": "user", "content": "Hello"}]


def _connector() -> tuple[AnthropicConnector, AsyncMock]:
    """Build an AnthropicConnector whose SDK client is a mock."""
    with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_cls:
        client = AsyncMock()
        mock_cls.return_value = client
        connector = AnthropicConnector("t", {"api_key": "k", "model_list": [MODEL]})
    return connector, client


def _response(usage: object) -> SimpleNamespace:
    """Fake SDK message with the given ``usage`` object."""
    return SimpleNamespace(
        content=[SimpleNamespace(text="Hello from Claude!")],
        stop_reason="end_turn",
        usage=usage,
    )


def _estimated_count() -> float:
    """Current value of the usage-estimated fault counter for anthropic."""
    return get_proxy_metrics().usage_estimated_total.labels(provider="anthropic")._value.get()


@pytest.mark.asyncio
async def test_chat_completion_returns_real_sdk_usage_not_estimate():
    """Real usage.input/output_tokens win; estimate is not used and no fault fires."""
    connector, client = _connector()
    usage = SimpleNamespace(
        input_tokens=4321,
        output_tokens=1234,
        cache_creation_input_tokens=7,
        cache_read_input_tokens=9,
    )
    client.messages.create = AsyncMock(return_value=_response(usage))
    before = _estimated_count()

    _, info = await connector.chat_completion(MESSAGES, MODEL)

    assert info["input_tokens"] == 4321
    assert info["output_tokens"] == 1234
    assert info["total_tokens"] == 4321 + 1234
    assert info["cache_creation_input_tokens"] == 7
    assert info["cache_read_input_tokens"] == 9
    assert _estimated_count() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [None, SimpleNamespace(), SimpleNamespace(input_tokens="x", output_tokens=None)],
)
async def test_chat_completion_falls_back_to_estimate_loudly_when_usage_absent(usage, caplog):
    """No usable usage -> tiktoken estimate, a warning log AND the fault counter increments."""
    connector, client = _connector()
    client.messages.create = AsyncMock(return_value=_response(usage))
    before = _estimated_count()

    with caplog.at_level("WARNING"):
        _, info = await connector.chat_completion(MESSAGES, MODEL)

    assert info["input_tokens"] > 0
    assert info["output_tokens"] > 0
    assert info["total_tokens"] == info["input_tokens"] + info["output_tokens"]
    assert _estimated_count() == before + 1
    assert any("ESTIMATE" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_stream_emits_real_final_usage():
    """Streaming's done chunk carries the final message's real usage."""
    connector, client = _connector()
    final = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=11, output_tokens=22), stop_reason="end_turn"
    )

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield Mock(type="content_block_delta", delta=Mock(text="Hi"))

        async def get_final_message(self):
            return final

    client.messages.stream = Mock(return_value=_Stream())

    chunks = [c async for c in connector.stream_chat_completion(MESSAGES, MODEL)]

    assert chunks[-1].done is True
    assert chunks[-1].usage is not None
    assert chunks[-1].usage["input_tokens"] == 11
    assert chunks[-1].usage["output_tokens"] == 22
    assert chunks[-1].usage["finish_reason"] == "end_turn"


def _dispatch(connector: Mock) -> DispatchStage:
    """DispatchStage wired to a single mocked 'anthropic' provider."""
    router = Mock()
    router.select_provider = Mock(return_value=("anthropic", MODEL))
    return DispatchStage(name="dispatch", router=router, connectors={"anthropic": connector})


def _ctx(body: dict, stream: bool = False) -> PipelineContext:
    """PipelineContext for dispatch tests."""
    return PipelineContext(
        user=Mock(id=1, tenant_id="org1"),
        body=body,
        model=MODEL,
        messages=list(MESSAGES),
        stream=stream,
    )


@pytest.mark.asyncio
async def test_dispatch_forwards_body_max_tokens_to_connector():
    """Caller's max_tokens reaches connector.chat_completion (not the 1000 default)."""
    connector = Mock()
    connector.chat_completion = AsyncMock(
        return_value=("ok", {"input_tokens": 1, "output_tokens": 1})
    )

    await _dispatch(connector)(_ctx({"max_tokens": 4096}))

    assert connector.chat_completion.await_args.kwargs["max_tokens"] == 4096


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"max_tokens": None}, {"max_tokens": 0}, {"max_tokens": "9"}])
async def test_dispatch_omits_max_tokens_when_absent_or_invalid(body):
    """Missing/invalid max_tokens is not forwarded, so the connector default is preserved."""
    connector = Mock()
    connector.chat_completion = AsyncMock(
        return_value=("ok", {"input_tokens": 1, "output_tokens": 1})
    )

    await _dispatch(connector)(_ctx(body))

    assert "max_tokens" not in connector.chat_completion.await_args.kwargs


@pytest.mark.asyncio
async def test_dispatch_forwards_max_tokens_on_streaming_path():
    """The streaming dispatch also forwards max_tokens."""
    seen: dict = {}

    async def _stream(messages, model, **kwargs):
        seen.update(kwargs)
        from shared.utils.llm_connectors import StreamChunk

        yield StreamChunk(delta="x", done=False)
        yield StreamChunk(delta="", usage={"input_tokens": 1, "output_tokens": 1}, done=True)

    connector = Mock()
    connector.stream_chat_completion = _stream

    await _dispatch(connector)(_ctx({"max_tokens": 77}, stream=True))

    assert seen == {"max_tokens": 77}


@pytest.mark.asyncio
async def test_stream_without_usage_falls_back_loudly_and_increments_counter(caplog):
    """Streaming with no final usage logs AND increments the usage-estimated counter."""
    connector, client = _connector()

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield Mock(type="content_block_delta", delta=Mock(text="Hi"))

        async def get_final_message(self):
            return SimpleNamespace(usage=None, stop_reason="end_turn")

    client.messages.stream = Mock(return_value=_Stream())
    before = _estimated_count()

    with caplog.at_level("WARNING"):
        chunks = [c async for c in connector.stream_chat_completion(MESSAGES, MODEL)]

    assert chunks[-1].done is True
    assert chunks[-1].usage is None
    assert _estimated_count() == before + 1
    assert any("without usage" in r.getMessage() for r in caplog.records)


def _openai_connector() -> tuple[OpenAIConnector, AsyncMock]:
    """OpenAIConnector with a mocked SDK client returning a minimal valid response."""
    with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_cls:
        client = AsyncMock()
        mock_cls.return_value = client
        connector = OpenAIConnector("o", {"api_key": "k", "model_list": []})
    usage = SimpleNamespace(
        prompt_tokens=1, completion_tokens=2, total_tokens=3, prompt_tokens_details=None
    )
    choice = SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")
    client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(choices=[choice], usage=usage, model="m")
    )
    return connector, client


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["o1", "o1-mini", "o3-mini", "o4-mini", "gpt-5"])
async def test_openai_reasoning_models_get_max_completion_tokens(model):
    """Reasoning models receive max_completion_tokens, never max_tokens (would 400)."""
    connector, client = _openai_connector()

    await connector.chat_completion(MESSAGES, model, max_tokens=512)

    kwargs = client.chat.completions.create.await_args.kwargs
    assert kwargs["max_completion_tokens"] == 512
    assert "max_tokens" not in kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4", "gpt-3.5-turbo"])
async def test_openai_regular_models_keep_max_tokens(model):
    """Non-reasoning models keep max_tokens untouched."""
    connector, client = _openai_connector()

    await connector.chat_completion(MESSAGES, model, max_tokens=512)

    kwargs = client.chat.completions.create.await_args.kwargs
    assert kwargs["max_tokens"] == 512
    assert "max_completion_tokens" not in kwargs
