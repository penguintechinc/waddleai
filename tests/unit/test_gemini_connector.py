"""GeminiConnector: system instruction via config + real (not estimated) usage."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from shared.utils.llm_connectors import GeminiConnector
from shared.utils.metrics import get_proxy_metrics

MODEL = "gemini-1.5-flash"
MESSAGES = [
    {"role": "system", "content": "Be terse."},
    {"role": "user", "content": "Hello"},
]


def _connector() -> tuple[GeminiConnector, Mock]:
    """Build a GeminiConnector whose SDK client is a mock."""
    with patch("shared.utils.llm_connectors.genai.Client") as mock_cls:
        client = Mock()
        client.aio = Mock()
        client.aio.models = Mock()
        mock_cls.return_value = client
        connector = GeminiConnector("t", {"api_key": "k", "model_list": [MODEL]})
    return connector, client.aio.models


def _usage(prompt: object = 100, cand: object = 7, cached: object = 3) -> SimpleNamespace:
    """Fake usage_metadata."""
    return SimpleNamespace(
        prompt_token_count=prompt, candidates_token_count=cand, cached_content_token_count=cached
    )


def _response(usage: object) -> SimpleNamespace:
    """Fake SDK response with the given usage_metadata."""
    return SimpleNamespace(
        text="Hi there",
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
        usage_metadata=usage,
    )


def _estimated_count() -> float:
    """Current value of the usage-estimated fault counter for gemini."""
    return get_proxy_metrics().usage_estimated_total.labels(provider="gemini")._value.get()


def _stream(chunks: list[SimpleNamespace]) -> AsyncMock:
    """AsyncMock for generate_content_stream yielding ``chunks``."""

    async def _gen():
        for c in chunks:
            yield c

    return AsyncMock(return_value=_gen())


def _chunk(text: str, usage: object = None) -> SimpleNamespace:
    """Fake stream chunk."""
    return SimpleNamespace(text=text, usage_metadata=usage, candidates=None)


@pytest.mark.asyncio
async def test_chat_completion_basic_returns_text_and_usage():
    """Regression: the call no longer raises TypeError (system_prompt kwarg)."""
    connector, models = _connector()
    models.generate_content = AsyncMock(return_value=_response(_usage()))

    text, info = await connector.chat_completion(MESSAGES, MODEL)

    assert text == "Hi there"
    assert info["provider"] == "gemini"
    assert info["finish_reason"] == "STOP"


@pytest.mark.asyncio
async def test_system_instruction_passed_via_config_not_kwarg():
    """System text lands in GenerateContentConfig.system_instruction; no system_prompt kwarg."""
    connector, models = _connector()
    models.generate_content = AsyncMock(return_value=_response(_usage()))

    await connector.chat_completion(MESSAGES, MODEL, max_tokens=16)

    kwargs = models.generate_content.await_args.kwargs
    assert "system_prompt" not in kwargs
    assert kwargs["config"].system_instruction == "Be terse."
    assert kwargs["config"].max_output_tokens == 16
    assert all(c["role"] != "system" for c in kwargs["contents"])


@pytest.mark.asyncio
async def test_no_system_message_leaves_system_instruction_unset():
    """No system role -> system_instruction stays None."""
    connector, models = _connector()
    models.generate_content = AsyncMock(return_value=_response(_usage()))

    await connector.chat_completion(MESSAGES[1:], MODEL)

    assert models.generate_content.await_args.kwargs["config"].system_instruction is None


@pytest.mark.asyncio
async def test_real_usage_returned_not_estimate():
    """usage_metadata prompt/candidates counts win; no fault counter fires."""
    connector, models = _connector()
    models.generate_content = AsyncMock(return_value=_response(_usage(4321, 1234, 9)))
    before = _estimated_count()

    _, info = await connector.chat_completion(MESSAGES, MODEL)

    assert info["input_tokens"] == 4321
    assert info["output_tokens"] == 1234
    assert info["total_tokens"] == 4321 + 1234
    assert info["cached_content_token_count"] == 9
    assert _estimated_count() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [None, SimpleNamespace(), _usage(prompt="x", cand=None), _usage(prompt=True, cand=5)],
)
async def test_chat_completion_loud_fallback_when_usage_absent(usage, caplog):
    """No usable usage -> estimate + WARNING + counter increment."""
    connector, models = _connector()
    models.generate_content = AsyncMock(return_value=_response(usage))
    before = _estimated_count()

    with caplog.at_level("WARNING"):
        _, info = await connector.chat_completion(MESSAGES, MODEL)

    assert info["input_tokens"] > 0
    assert info["output_tokens"] > 0
    assert _estimated_count() == before + 1
    assert any("ESTIMATE" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_stream_system_instruction_via_config_and_real_final_usage():
    """Streaming: config carries system_instruction; done chunk has the last chunk's usage."""
    connector, models = _connector()
    models.generate_content_stream = _stream(
        [_chunk("Hi", _usage(5, 1)), _chunk(" there", _usage(11, 22, 4))]
    )
    before = _estimated_count()

    chunks = [c async for c in connector.stream_chat_completion(MESSAGES, MODEL)]

    kwargs = models.generate_content_stream.await_args.kwargs
    assert "system_prompt" not in kwargs
    assert kwargs["config"].system_instruction == "Be terse."
    assert [c.delta for c in chunks[:-1]] == ["Hi", " there"]
    assert chunks[-1].done is True
    assert chunks[-1].usage is not None
    assert chunks[-1].usage["input_tokens"] == 11
    assert chunks[-1].usage["output_tokens"] == 22
    assert chunks[-1].usage["cached_content_token_count"] == 4
    assert _estimated_count() == before


@pytest.mark.asyncio
async def test_stream_loud_fallback_when_usage_absent(caplog):
    """Streaming without usage_metadata -> estimate from streamed text + WARNING + counter."""
    connector, models = _connector()
    models.generate_content_stream = _stream([_chunk("Hello"), _chunk(" world")])
    before = _estimated_count()

    with caplog.at_level("WARNING"):
        chunks = [c async for c in connector.stream_chat_completion(MESSAGES, MODEL)]

    assert chunks[-1].usage is not None
    assert chunks[-1].usage["input_tokens"] > 0
    assert chunks[-1].usage["output_tokens"] > 0
    assert _estimated_count() == before + 1
    assert any("ESTIMATE" in r.getMessage() for r in caplog.records)
