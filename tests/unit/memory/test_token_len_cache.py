"""Tokenizer-length cache tests: hit avoids recount, correctness, TTL, model isolation."""

import pytest

from shared.memory.token_len_cache import TokenLenCache
from tests.unit.memory.test_scratchpad import FakeValkey


def _fresh_counter(calls: list):
    async def _count(text: str) -> int:
        calls.append(text)
        return len(text.split())

    return _count


@pytest.fixture
def valkey() -> FakeValkey:
    """Fresh in-memory Valkey double per test."""
    return FakeValkey()


class TestTokenLenCache:
    """TokenLenCache: cache-hit avoidance, correctness, TTL, and failure propagation."""

    @pytest.mark.asyncio
    async def test_first_call_invokes_counter_and_caches(self, valkey):
        """The first count() call invokes the counter and caches its result."""
        cache = TokenLenCache(valkey)
        calls: list = []
        result = await cache.count("gpt-4", "hello world", _fresh_counter(calls))
        assert result == 2
        assert calls == ["hello world"]

    @pytest.mark.asyncio
    async def test_second_identical_call_does_not_invoke_counter(self, valkey):
        """A repeat count() for the same (model, text) hits the cache -- zero counter calls."""
        cache = TokenLenCache(valkey)
        calls: list = []
        counter = _fresh_counter(calls)
        r1 = await cache.count("gpt-4", "hello world", counter)
        r2 = await cache.count("gpt-4", "hello world", counter)
        assert r1 == r2 == 2
        assert calls == ["hello world"]  # only invoked once

    @pytest.mark.asyncio
    async def test_correctness_matches_fresh_count_for_matrix(self, valkey):
        """Cached counts equal a fresh count across a matrix of texts."""
        cache = TokenLenCache(valkey)
        texts = ["a", "a b c", "the quick brown fox jumps", ""]
        for text in texts:
            fresh = len(text.split())
            cached = await cache.count("gpt-4", text, _fresh_counter([]))
            assert cached == fresh

    @pytest.mark.asyncio
    async def test_different_model_is_separate_entry(self, valkey):
        """The same text under a different model is a separate cache entry."""
        cache = TokenLenCache(valkey)
        calls: list = []
        counter = _fresh_counter(calls)
        await cache.count("gpt-4", "hello world", counter)
        await cache.count("claude-3", "hello world", counter)
        assert calls == ["hello world", "hello world"]  # invoked once per model

    @pytest.mark.asyncio
    async def test_ttl_passed_to_valkey_set(self, valkey, monkeypatch):
        """The configured ttl_seconds is passed through to the Valkey set() call."""
        cache = TokenLenCache(valkey, ttl_seconds=123)
        captured = {}
        orig_set = valkey.set

        async def spy_set(key, value, ex=None):
            captured["ex"] = ex
            await orig_set(key, value, ex=ex)

        valkey.set = spy_set
        await cache.count("gpt-4", "hello world", _fresh_counter([]))
        assert captured["ex"] == 123

    @pytest.mark.asyncio
    async def test_counter_exception_propagates_and_nothing_cached(self, valkey):
        """A counter exception propagates and leaves nothing cached."""
        cache = TokenLenCache(valkey)

        async def failing_counter(text: str) -> int:
            raise RuntimeError("tokenizer unavailable")

        with pytest.raises(RuntimeError):
            await cache.count("gpt-4", "hello world", failing_counter)

        assert valkey.store == {}
