"""OpenAI cached_tokens surfacing + Gemini CachedContent lifecycle (spec §6.3)."""

from unittest.mock import AsyncMock, MagicMock

from shared.cache.config import ResolvedCacheConfig
from shared.cache.upstream import (
    GeminiCachedContentManager,
    extract_gemini_cached_tokens,
    extract_openai_cached_tokens,
)


class TestOpenAICachedTokens:
    """Tests for open a i cached tokens."""

    def test_surfaces_cached_tokens_when_present(self):
        """Surfaces cached tokens when present."""
        usage = {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 40}}
        assert extract_openai_cached_tokens(usage) == 40

    def test_defaults_to_zero_when_details_absent(self):
        """Defaults to zero when details absent."""
        usage = {"prompt_tokens": 100}
        assert extract_openai_cached_tokens(usage) == 0

    def test_defaults_to_zero_when_cached_tokens_key_absent(self):
        """Defaults to zero when cached tokens key absent."""
        usage = {"prompt_tokens": 100, "prompt_tokens_details": {}}
        assert extract_openai_cached_tokens(usage) == 0


class TestGeminiCachedTokens:
    """Tests for gemini cached tokens."""

    def test_surfaces_cached_content_token_count(self):
        """Surfaces cached content token count."""
        assert extract_gemini_cached_tokens({"cached_content_token_count": 500}) == 500

    def test_defaults_to_zero_when_absent(self):
        """Defaults to zero when absent."""
        assert extract_gemini_cached_tokens({}) == 0


def _long_text(n_repeats: int = 400) -> str:
    """Long text."""
    return " ".join(["stable context sentence number"] * n_repeats)


def _big_prefix_body(last_user_content: str = "What's next?") -> dict:
    """Big prefix body."""
    return {
        "model": "gemini-1.5-pro",
        "system": _long_text(),
        "messages": [
            {"role": "user", "content": "Here is a lot of background."},
            {"role": "assistant", "content": "Understood, I have it."},
            {"role": "user", "content": last_user_content},
        ],
    }


def _make_genai_client(cache_name: str = "cachedContents/abc123"):
    """Make genai client."""
    client = MagicMock()
    create_result = MagicMock()
    create_result.name = cache_name  # MagicMock(name=...) sets repr, not .name -- set explicitly
    client.aio.caches.create = AsyncMock(return_value=create_result)
    client.aio.caches.delete = AsyncMock(return_value=None)
    return client


class TestGeminiCachedContentLifecycle:
    """Tests for gemini cached content lifecycle."""

    async def test_first_observation_no_create(self, fake_valkey):
        """First observation no create."""
        client = _make_genai_client()
        manager = GeminiCachedContentManager(fake_valkey, client)
        cfg = ResolvedCacheConfig(anthropic_cache_control=True)
        body = _big_prefix_body()

        name = await manager.get_or_create(body, vkey_id=1, model="gemini-1.5-pro", cfg=cfg)

        assert name is None
        client.aio.caches.create.assert_not_called()

    async def test_second_observation_creates_cache_once(self, fake_valkey):
        """Second observation creates cache once."""
        client = _make_genai_client()
        manager = GeminiCachedContentManager(fake_valkey, client)
        cfg = ResolvedCacheConfig(anthropic_cache_control=True)
        body = _big_prefix_body()

        await manager.get_or_create(body, vkey_id=1, model="gemini-1.5-pro", cfg=cfg)
        name = await manager.get_or_create(body, vkey_id=1, model="gemini-1.5-pro", cfg=cfg)

        assert name == "cachedContents/abc123"
        client.aio.caches.create.assert_called_once()
        call_kwargs = client.aio.caches.create.call_args.kwargs
        assert call_kwargs["model"] == "gemini-1.5-pro"
        assert (
            call_kwargs["config"]["ttl"]
            == f"{GeminiCachedContentManager.DEFAULT_CACHE_TTL_SECONDS}s"
        )

    async def test_subsequent_matching_request_reuses_without_recreating(self, fake_valkey):
        """Subsequent matching request reuses without recreating."""
        client = _make_genai_client()
        manager = GeminiCachedContentManager(fake_valkey, client)
        cfg = ResolvedCacheConfig(anthropic_cache_control=True)
        body = _big_prefix_body()

        await manager.get_or_create(body, vkey_id=1, model="gemini-1.5-pro", cfg=cfg)
        await manager.get_or_create(body, vkey_id=1, model="gemini-1.5-pro", cfg=cfg)
        name_again = await manager.get_or_create(body, vkey_id=1, model="gemini-1.5-pro", cfg=cfg)

        assert name_again == "cachedContents/abc123"
        client.aio.caches.create.assert_called_once()

    async def test_toggle_off_never_creates(self, fake_valkey):
        """Toggle off never creates."""
        client = _make_genai_client()
        manager = GeminiCachedContentManager(fake_valkey, client)
        cfg = ResolvedCacheConfig(anthropic_cache_control=False)
        body = _big_prefix_body()

        for _ in range(3):
            name = await manager.get_or_create(body, vkey_id=1, model="gemini-1.5-pro", cfg=cfg)
            assert name is None
        client.aio.caches.create.assert_not_called()

    async def test_expire_deletes_upstream_and_valkey_mapping(self, fake_valkey):
        """Expire deletes upstream and valkey mapping."""
        client = _make_genai_client()
        manager = GeminiCachedContentManager(fake_valkey, client)
        cfg = ResolvedCacheConfig(anthropic_cache_control=True)
        body = _big_prefix_body()

        await manager.get_or_create(body, vkey_id=1, model="gemini-1.5-pro", cfg=cfg)
        await manager.get_or_create(body, vkey_id=1, model="gemini-1.5-pro", cfg=cfg)

        from shared.cache.upstream import _prefix_hash, _stable_prefix_messages

        prefix_sha = _prefix_hash(body, _stable_prefix_messages(body))
        await manager.expire(vkey_id=1, prefix_sha=prefix_sha)

        client.aio.caches.delete.assert_called_once_with(name="cachedContents/abc123")
        mapping_key = manager._mapping_key(1, prefix_sha)
        assert await fake_valkey.get(mapping_key) is None

    async def test_expire_on_missing_mapping_is_a_noop(self, fake_valkey):
        """Expire on missing mapping is a noop."""
        client = _make_genai_client()
        manager = GeminiCachedContentManager(fake_valkey, client)

        await manager.expire(vkey_id=1, prefix_sha="never-existed")

        client.aio.caches.delete.assert_not_called()
