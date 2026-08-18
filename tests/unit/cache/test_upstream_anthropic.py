"""Anthropic cache_control auto-injection + usage extraction (spec §6.3)."""

import json
import os

from shared.cache.config import ResolvedCacheConfig
from shared.cache.upstream import AnthropicPromptCacheOrchestrator

_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _long_text(n_repeats: int = 400) -> str:
    # ~1 token per short word; comfortably exceeds 1024 tokens.
    """Long text."""
    return " ".join(["stable context sentence number"] * n_repeats)


def _big_prefix_body(last_user_content: str = "What's next?") -> dict:
    """Big prefix body."""
    return {
        "model": "claude-3-5-sonnet-latest",
        "system": _long_text(),
        "messages": [
            {"role": "user", "content": "Here is a lot of background."},
            {"role": "assistant", "content": "Understood, I have it."},
            {"role": "user", "content": last_user_content},
        ],
    }


class TestPrefixTrackingAndInjection:
    """Tests for prefix tracking and injection."""

    async def test_first_observation_no_injection_increments_counter(self, fake_valkey):
        """First observation no injection increments counter."""
        orchestrator = AnthropicPromptCacheOrchestrator(fake_valkey)
        cfg = ResolvedCacheConfig(anthropic_cache_control=True)
        body = _big_prefix_body()

        result = await orchestrator.annotate_request(body, vkey_id=1, cfg=cfg)

        assert result is body  # untouched -- no injection yet
        prefix_keys = fake_valkey.keys_with_prefix("waddleai:cache:prefix:1:")
        assert len(prefix_keys) == 1

    async def test_second_identical_prefix_injects_breakpoint(self, fake_valkey):
        """Second identical prefix injects breakpoint."""
        orchestrator = AnthropicPromptCacheOrchestrator(fake_valkey)
        cfg = ResolvedCacheConfig(anthropic_cache_control=True)
        body = _big_prefix_body()

        await orchestrator.annotate_request(body, vkey_id=1, cfg=cfg)
        result = await orchestrator.annotate_request(body, vkey_id=1, cfg=cfg)

        assert result is not body
        # Injected on the last block of the last prefix message (index 1: the
        # assistant turn, since messages[:-1] is the stable prefix).
        injected_message = result["messages"][1]
        assert injected_message["content"][-1]["cache_control"] == {"type": "ephemeral"}
        # Original body never mutated.
        assert body["messages"][1]["content"] == "Understood, I have it."

    async def test_short_prefix_never_injected_regardless_of_count(self, fake_valkey):
        """Short prefix never injected regardless of count."""
        orchestrator = AnthropicPromptCacheOrchestrator(fake_valkey)
        cfg = ResolvedCacheConfig(anthropic_cache_control=True)
        body = {
            "model": "claude-3-5-sonnet-latest",
            "messages": [
                {"role": "user", "content": "short"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "next"},
            ],
        }

        for _ in range(5):
            result = await orchestrator.annotate_request(body, vkey_id=1, cfg=cfg)
            assert result is body

    async def test_client_supplied_cache_control_disables_injection_byte_identical(
        self, fake_valkey
    ):
        """Client supplied cache control disables injection byte identical."""
        orchestrator = AnthropicPromptCacheOrchestrator(fake_valkey)
        cfg = ResolvedCacheConfig(anthropic_cache_control=True)
        body = _big_prefix_body()
        body["messages"][1]["content"] = [
            {
                "type": "text",
                "text": "Understood, I have it.",
                "cache_control": {"type": "ephemeral"},
            }
        ]

        # Even after repeated calls, a client-managed request is never touched
        # and never contributes to the prefix counter.
        for _ in range(3):
            result = await orchestrator.annotate_request(body, vkey_id=1, cfg=cfg)
            assert result is body

        assert fake_valkey.keys_with_prefix("waddleai:cache:prefix:1:") == []

    async def test_toggle_off_disables_tracking_and_injection(self, fake_valkey):
        """Toggle off disables tracking and injection."""
        orchestrator = AnthropicPromptCacheOrchestrator(fake_valkey)
        cfg = ResolvedCacheConfig(anthropic_cache_control=False)
        body = _big_prefix_body()

        for _ in range(3):
            result = await orchestrator.annotate_request(body, vkey_id=1, cfg=cfg)
            assert result is body

        assert fake_valkey.keys_with_prefix("waddleai:cache:prefix:1:") == []

    async def test_never_more_than_max_breakpoints(self, fake_valkey):
        """Never more than max breakpoints."""
        orchestrator = AnthropicPromptCacheOrchestrator(fake_valkey)
        cfg = ResolvedCacheConfig(anthropic_cache_control=True)
        body = _big_prefix_body()

        await orchestrator.annotate_request(body, vkey_id=1, cfg=cfg)
        result = await orchestrator.annotate_request(body, vkey_id=1, cfg=cfg)

        breakpoints = 0
        for message in result["messages"]:
            content = message.get("content")
            if isinstance(content, list):
                breakpoints += sum(1 for block in content if "cache_control" in block)
        assert breakpoints <= AnthropicPromptCacheOrchestrator.MAX_BREAKPOINTS


class TestPrefixTextExtractionEdgeCases:
    """Tests for prefix text extraction edge cases."""

    async def test_system_as_block_array_counts_toward_prefix_tokens(self, fake_valkey):
        """System as block array counts toward prefix tokens."""
        orchestrator = AnthropicPromptCacheOrchestrator(fake_valkey)
        cfg = ResolvedCacheConfig(anthropic_cache_control=True)
        body = {
            "model": "claude-3-5-sonnet-latest",
            "system": [{"type": "text", "text": _long_text()}],
            "messages": [
                {"role": "user", "content": "background"},
                {"role": "assistant", "content": "ack"},
                {"role": "user", "content": "next"},
            ],
        }

        await orchestrator.annotate_request(body, vkey_id=1, cfg=cfg)
        result = await orchestrator.annotate_request(body, vkey_id=1, cfg=cfg)
        assert result is not body  # crossed the threshold via the block-array system prompt

    async def test_client_cache_control_in_system_block_disables_injection(self, fake_valkey):
        """Client cache control in system block disables injection."""
        orchestrator = AnthropicPromptCacheOrchestrator(fake_valkey)
        cfg = ResolvedCacheConfig(anthropic_cache_control=True)
        body = {
            "model": "claude-3-5-sonnet-latest",
            "system": [
                {"type": "text", "text": _long_text(), "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [
                {"role": "user", "content": "background"},
                {"role": "assistant", "content": "ack"},
                {"role": "user", "content": "next"},
            ],
        }

        for _ in range(3):
            result = await orchestrator.annotate_request(body, vkey_id=1, cfg=cfg)
            assert result is body

    async def test_tools_schema_counts_toward_prefix_tokens(self, fake_valkey):
        """Tools schema counts toward prefix tokens."""
        orchestrator = AnthropicPromptCacheOrchestrator(fake_valkey)
        cfg = ResolvedCacheConfig(anthropic_cache_control=True)
        big_tools = [
            {"name": f"tool_{i}", "description": "a" * 400, "parameters": {"type": "object"}}
            for i in range(40)
        ]
        body = {
            "model": "claude-3-5-sonnet-latest",
            "tools": big_tools,
            "messages": [
                {"role": "user", "content": "background"},
                {"role": "assistant", "content": "ack"},
                {"role": "user", "content": "next"},
            ],
        }

        await orchestrator.annotate_request(body, vkey_id=1, cfg=cfg)
        result = await orchestrator.annotate_request(body, vkey_id=1, cfg=cfg)
        assert result is not body

    async def test_single_message_conversation_has_no_stable_prefix(self, fake_valkey):
        """Single message conversation has no stable prefix."""
        orchestrator = AnthropicPromptCacheOrchestrator(fake_valkey)
        cfg = ResolvedCacheConfig(anthropic_cache_control=True)
        body = {
            "model": "claude-3-5-sonnet-latest",
            "system": _long_text(),
            "messages": [{"role": "user", "content": "hi"}],
        }

        for _ in range(3):
            result = await orchestrator.annotate_request(body, vkey_id=1, cfg=cfg)
            assert result is body


class TestUsageExtraction:
    """Tests for usage extraction."""

    def test_extract_cache_usage_from_recorded_fixture(self):
        """Extract cache usage from recorded fixture."""
        with open(os.path.join(_FIXTURES_DIR, "anthropic_cached_response.json")) as f:
            fixture = json.load(f)

        creation, read = AnthropicPromptCacheOrchestrator.extract_cache_usage(fixture["usage"])
        assert read == fixture["usage"]["cache_read_input_tokens"]
        assert creation == fixture["usage"]["cache_creation_input_tokens"]
        assert read > 0

    def test_extract_cache_usage_defaults_to_zero_when_absent(self):
        """Extract cache usage defaults to zero when absent."""
        creation, read = AnthropicPromptCacheOrchestrator.extract_cache_usage({"input_tokens": 10})
        assert creation == 0
        assert read == 0
