"""§6A.5 per-key proxy_memory config resolution tests: flag-gating + defaults."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.memory.config import (
    ALL_DISABLED,
    DEFAULT_KEEP_RECENT,
    DEFAULT_RATIO,
    DEFAULT_THRESHOLD_TOKENS,
    resolve_proxy_memory_config,
)


def _features(enabled: bool = True, *, raises: bool = False) -> MagicMock:
    features = MagicMock()
    if raises:
        features.is_feature_enabled = MagicMock(side_effect=RuntimeError("posthog down"))
    else:
        features.is_feature_enabled = MagicMock(return_value=enabled)
    return features


def _db_with_block(block: dict) -> MagicMock:
    db = MagicMock()
    db.get_api_key_proxy_memory = AsyncMock(return_value=block)
    return db


class TestResolveProxyMemoryConfig:
    """resolve_proxy_memory_config: flag gating, defaults, and fail-safe behavior."""

    @pytest.mark.asyncio
    async def test_full_block_parses(self):
        """A fully-populated proxy_memory block resolves every field verbatim."""
        block = {
            "scratchpad": True,
            "scratchpad_substitution": True,
            "summarization": {
                "enabled": True,
                "threshold_tokens": 4000,
                "keep_recent": 2,
                "ratio": 0.2,
            },
            "embedding_cache": True,
            "schema_dedup": True,
        }
        cfg = await resolve_proxy_memory_config(
            _db_with_block(block), _features(True), api_key_id=1, org_id=1
        )
        assert cfg.scratchpad_enabled is True
        assert cfg.scratchpad_substitution is True
        assert cfg.summarization_enabled is True
        assert cfg.threshold_tokens == 4000
        assert cfg.keep_recent == 2
        assert cfg.ratio == 0.2
        assert cfg.embedding_cache is True
        assert cfg.schema_dedup is True

    @pytest.mark.asyncio
    async def test_missing_block_uses_documented_defaults(self):
        """No stored block falls back to the documented off-by-default settings."""
        db = MagicMock()
        db.get_api_key_proxy_memory = AsyncMock(return_value=None)
        cfg = await resolve_proxy_memory_config(db, _features(True), api_key_id=1, org_id=1)
        assert cfg.summarization_enabled is False
        assert cfg.scratchpad_substitution is False
        assert cfg.threshold_tokens == DEFAULT_THRESHOLD_TOKENS
        assert cfg.keep_recent == DEFAULT_KEEP_RECENT
        assert cfg.ratio == DEFAULT_RATIO

    @pytest.mark.asyncio
    async def test_flag_off_disables_regardless_of_per_key_config(self):
        """The whole-feature flag off overrides an otherwise fully-enabled per-key block."""
        block = {
            "scratchpad": True,
            "scratchpad_substitution": True,
            "summarization": {"enabled": True},
            "embedding_cache": True,
            "schema_dedup": True,
        }
        cfg = await resolve_proxy_memory_config(
            _db_with_block(block), _features(False), api_key_id=1, org_id=1
        )
        assert cfg == ALL_DISABLED

    @pytest.mark.asyncio
    async def test_features_raising_is_fail_safe_off(self):
        """A raising features client resolves to ALL_DISABLED instead of propagating."""
        block = {"summarization": {"enabled": True}}
        cfg = await resolve_proxy_memory_config(
            _db_with_block(block), _features(raises=True), api_key_id=1, org_id=1
        )
        assert cfg == ALL_DISABLED

    @pytest.mark.asyncio
    async def test_db_lookup_failure_is_fail_safe(self):
        """A raising db lookup falls back to documented defaults, not a crash."""
        db = MagicMock()
        db.get_api_key_proxy_memory = AsyncMock(side_effect=RuntimeError("db down"))
        cfg = await resolve_proxy_memory_config(db, _features(True), api_key_id=1, org_id=1)
        # Flag is on but config lookup failed -> falls back to defaults, not a crash.
        assert cfg.summarization_enabled is False
        assert cfg.embedding_cache is True
