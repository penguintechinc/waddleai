"""Integration tests for the Anthropic Claude API.

These tests use real API calls and require ANTHROPIC_API_KEY to be set.
All tests are skipped when the key is absent to avoid CI failures.

Model used: claude-3-haiku-20240307 (fastest / cheapest for integration checks).
"""

import os

import pytest

pytestmark = pytest.mark.integration

_HAIKU = "claude-3-haiku-20240307"
_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
_HAS_KEY = bool(_API_KEY)


def _skip_if_no_key() -> None:
    if not _HAS_KEY:
        pytest.skip("ANTHROPIC_API_KEY not set")


# ---------------------------------------------------------------------------
# SDK import and instantiation
# ---------------------------------------------------------------------------


def test_anthropic_sdk_importable() -> None:
    """The anthropic package should be importable."""
    import anthropic  # noqa: F401  # type: ignore[import]

    assert hasattr(anthropic, "Anthropic")


def test_anthropic_client_instantiates_with_key() -> None:
    """Anthropic client can be instantiated when API key is provided."""
    _skip_if_no_key()
    import anthropic  # type: ignore[import]

    client = anthropic.Anthropic(api_key=_API_KEY)
    assert client is not None


# ---------------------------------------------------------------------------
# Basic message round-trips
# ---------------------------------------------------------------------------


def test_claude_simple_message_response() -> None:
    """Send a single user message and verify top-level response structure."""
    _skip_if_no_key()
    import anthropic  # type: ignore[import]

    client = anthropic.Anthropic(api_key=_API_KEY)
    message = client.messages.create(
        model=_HAIKU,
        max_tokens=32,
        messages=[{"role": "user", "content": "Reply with one word: hello"}],
    )

    assert message.content is not None
    assert len(message.content) > 0
    assert message.content[0].type == "text"
    assert isinstance(message.content[0].text, str)
    assert len(message.content[0].text) > 0


def test_claude_response_has_usage_info() -> None:
    """Response should include token usage metadata."""
    _skip_if_no_key()
    import anthropic  # type: ignore[import]

    client = anthropic.Anthropic(api_key=_API_KEY)
    message = client.messages.create(
        model=_HAIKU,
        max_tokens=16,
        messages=[{"role": "user", "content": "Say: ok"}],
    )

    usage = message.usage
    assert usage is not None
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0


def test_claude_response_stop_reason_is_end_turn() -> None:
    """A normal completion should have stop_reason == 'end_turn'."""
    _skip_if_no_key()
    import anthropic  # type: ignore[import]

    client = anthropic.Anthropic(api_key=_API_KEY)
    message = client.messages.create(
        model=_HAIKU,
        max_tokens=32,
        messages=[{"role": "user", "content": "Reply: done"}],
    )

    assert message.stop_reason == "end_turn"


def test_claude_response_model_field_matches_requested() -> None:
    """Response model field should match the requested model."""
    _skip_if_no_key()
    import anthropic  # type: ignore[import]

    client = anthropic.Anthropic(api_key=_API_KEY)
    message = client.messages.create(
        model=_HAIKU,
        max_tokens=16,
        messages=[{"role": "user", "content": "hi"}],
    )

    assert message.model == _HAIKU


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def test_claude_streaming_response_yields_text() -> None:
    """Streaming mode should yield text delta events."""
    _skip_if_no_key()
    import anthropic  # type: ignore[import]

    client = anthropic.Anthropic(api_key=_API_KEY)
    collected: list[str] = []

    with client.messages.stream(
        model=_HAIKU,
        max_tokens=32,
        messages=[{"role": "user", "content": "Count to three, one per line"}],
    ) as stream:
        for text in stream.text_stream:
            collected.append(text)

    assert len(collected) > 0
    full_text = "".join(collected)
    assert len(full_text) > 0


# ---------------------------------------------------------------------------
# Multi-turn conversation
# ---------------------------------------------------------------------------


def test_claude_multi_turn_conversation() -> None:
    """Multi-turn message list should work without errors."""
    _skip_if_no_key()
    import anthropic  # type: ignore[import]

    client = anthropic.Anthropic(api_key=_API_KEY)
    messages: list[dict[str, str]] = [
        {"role": "user", "content": "My name is TestUser."},
        {"role": "assistant", "content": "Hello, TestUser! How can I help you today?"},
        {"role": "user", "content": "What is my name?"},
    ]
    message = client.messages.create(
        model=_HAIKU,
        max_tokens=32,
        messages=messages,
    )

    reply = message.content[0].text
    assert "TestUser" in reply or "test" in reply.lower()


# ---------------------------------------------------------------------------
# Provider config helpers (pure-Python, no API call)
# ---------------------------------------------------------------------------


def test_providers_resolve_model_alias() -> None:
    """resolve_model_alias should map known aliases to canonical model names."""
    from services.management.app.services.providers import (
        resolve_model_alias,  # type: ignore[import]
    )

    assert resolve_model_alias("claude-haiku") == "claude-3-haiku-20240307"
    assert resolve_model_alias("claude-opus") == "claude-3-opus-20240229"
    # Unknown alias should pass through unchanged
    assert resolve_model_alias("gpt-99-turbo") == "gpt-99-turbo"


def test_providers_get_provider_for_model_anthropic() -> None:
    """get_provider_for_model should return ANTHROPIC for claude models."""
    from services.management.app.services.providers import (  # type: ignore[import]
        ProviderType,
        get_provider_for_model,
    )

    provider = get_provider_for_model("claude-3-haiku-20240307")
    assert provider == ProviderType.ANTHROPIC


def test_providers_create_anthropic_config() -> None:
    """create_provider_config should build a valid AnthropicConfig."""
    from services.management.app.services.providers import (  # type: ignore[import]
        AnthropicConfig,
        ProviderType,
        create_provider_config,
    )

    config = create_provider_config(
        provider_type="anthropic",
        name="test-anthropic",
        api_key="sk-test",
    )

    assert isinstance(config, AnthropicConfig)
    assert config.provider_type == ProviderType.ANTHROPIC
    assert config.api_key == "sk-test"
    assert "claude-3-haiku-20240307" in config.model_list
