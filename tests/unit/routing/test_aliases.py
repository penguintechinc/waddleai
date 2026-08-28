"""Cascade stage 0 tests: explicit tool type + provider parsing + aliasing (spec §7.2)."""

import pytest

from shared.routing.aliases import AliasResolver, explicit_tool_type, split_provider_prefix


class TestSplitProviderPrefix:
    """split_provider_prefix() -- first-colon-only, registered-provider-only rule."""

    def test_registered_provider_prefix_splits(self):
        """anthropic:claude-opus-5-1m splits into provider + model."""
        assert split_provider_prefix("anthropic:claude-opus-5-1m") == (
            "anthropic",
            "claude-opus-5-1m",
        )

    def test_bare_ollama_tag_is_not_misparsed_as_provider(self):
        """gemma4:e2b stays a whole model name -- 'gemma4' is not a registered provider."""
        assert split_provider_prefix("gemma4:e2b") == (None, "gemma4:e2b")

    def test_ollama_provider_prefix_with_colon_tag_splits_on_first_colon_only(self):
        """ollama:gemma4:e2b splits provider=ollama, model=gemma4:e2b (not a double split)."""
        assert split_provider_prefix("ollama:gemma4:e2b") == ("ollama", "gemma4:e2b")

    def test_no_colon_returns_whole_string_as_model(self):
        """A model string with no colon has no provider prefix."""
        assert split_provider_prefix("gpt-4o") == (None, "gpt-4o")

    def test_bedrock_provider_prefix_splits(self):
        """bedrock:claude-opus-5-1m splits into provider + model."""
        assert split_provider_prefix("bedrock:claude-opus-5-1m") == ("bedrock", "claude-opus-5-1m")


class TestExplicitToolType:
    """explicit_tool_type() -- header > mcp tool > waddleai/<tool-type> alias."""

    def test_header_wins_over_everything(self):
        """The X-WaddleAI-Tool-Type header takes priority."""
        result = explicit_tool_type(
            header_value="research", mcp_tool="other-tool", model="waddleai/chat"
        )
        assert result == "research"

    def test_mcp_tool_used_when_no_header(self):
        """An invoked MCP tool implies the tool type when no header is set."""
        result = explicit_tool_type(header_value=None, mcp_tool="code-gen", model=None)
        assert result == "code-gen"

    def test_waddleai_model_alias_used_as_last_resort(self):
        """model='waddleai/<tool-type>' implies the tool type."""
        result = explicit_tool_type(header_value=None, mcp_tool=None, model="waddleai/summarize")
        assert result == "summarize"

    def test_none_when_no_explicit_signal(self):
        """No explicit signal at all falls through to stage 1 (returns None)."""
        result = explicit_tool_type(header_value=None, mcp_tool=None, model="gpt-4o")
        assert result is None


def _alias_row(source_model, target_model, org_id=None, target_provider=None, enabled=True):
    return {
        "id": 1,
        "organization_id": org_id,
        "source_model": source_model,
        "target_model": target_model,
        "target_provider": target_provider,
        "enabled": enabled,
    }


class TestAliasResolver:
    """resolve_alias() -- provider-pin parsing then model_aliases redirect."""

    @pytest.mark.asyncio
    async def test_unaliased_model_passes_through_with_no_routed_from(self, fake_db):
        """A model with no matching alias row passes through unchanged."""
        resolver = AliasResolver(fake_db)
        result = await resolver.resolve_alias("gpt-4o", org_id=1)
        assert result.model == "gpt-4o"
        assert result.routed_from is None

    @pytest.mark.asyncio
    async def test_global_alias_redirects_and_records_routed_from(self, fake_db):
        """A global alias redirects gpt-4o to a local model and records routed_from."""
        fake_db.seed("model_aliases", [_alias_row("gpt-4o", "mistral-large")])
        resolver = AliasResolver(fake_db)

        result = await resolver.resolve_alias("gpt-4o", org_id=1)

        assert result.model == "mistral-large"
        assert result.routed_from == "gpt-4o"

    @pytest.mark.asyncio
    async def test_org_alias_overrides_global_alias(self, fake_db):
        """An org-scoped alias takes precedence over a global one."""
        fake_db.seed(
            "model_aliases",
            [
                _alias_row("gpt-4o", "mistral-large"),
                _alias_row("gpt-4o", "org-specific-model", org_id=7),
            ],
        )
        resolver = AliasResolver(fake_db)

        result = await resolver.resolve_alias("gpt-4o", org_id=7)

        assert result.model == "org-specific-model"

    @pytest.mark.asyncio
    async def test_provider_prefix_is_stripped_before_alias_lookup(self, fake_db):
        """anthropic:claude-* strips the provider before matching model_aliases."""
        fake_db.seed("model_aliases", [_alias_row("claude-3-opus", "policy-x-model")])
        resolver = AliasResolver(fake_db)

        result = await resolver.resolve_alias("anthropic:claude-3-opus", org_id=1)

        assert result.model == "policy-x-model"
        assert result.provider == "anthropic"

    @pytest.mark.asyncio
    async def test_disabled_alias_is_not_applied(self, fake_db):
        """A disabled alias row is not honored."""
        fake_db.seed("model_aliases", [_alias_row("gpt-4o", "mistral-large", enabled=False)])
        resolver = AliasResolver(fake_db)

        result = await resolver.resolve_alias("gpt-4o", org_id=1)

        assert result.model == "gpt-4o"
        assert result.routed_from is None
