"""Tests for KnowledgeInjectStage: client-type matrix, token budget, flag gating (§9.5/§9.6)."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from proxy.apps.proxy_server.pipeline.knowledge_stage import KnowledgeInjectStage
from proxy.apps.proxy_server.pipeline.stages import PipelineContext
from shared.knowledge.injection_safety import InjectableBlock
from shared.knowledge.scoping import TrustTier


@dataclass(slots=True)
class _FakeUser:
    id: int = 1
    tenant_id: str = "org-a"
    mcp_capable: bool = False
    mcp_session_active: bool = False
    memory_injection_enabled: bool | None = None
    memory_injection_sources: list[str] | None = None
    memory_injection_token_budget: int | None = None


class _AllFlagsOnFeatures:
    def is_feature_enabled(self, flag_key: str, distinct_id: str | None = None) -> bool:
        return True


class _AllFlagsOffFeatures:
    def is_feature_enabled(self, flag_key: str, distinct_id: str | None = None) -> bool:
        return False


def _block(record_id: str, tokens: int, text: str | None = None) -> InjectableBlock:
    return InjectableBlock(
        record_id=record_id,
        text=text or f"> [derived repo-scope knowledge]\n> content for {record_id}",
        trust_tier=TrustTier.DERIVED,
        token_estimate=tokens,
    )


def _stub_retriever(blocks: list[InjectableBlock]) -> Any:
    retriever = AsyncMock()
    retriever.retrieve = AsyncMock(return_value=blocks)
    return retriever


def _messages() -> list[dict]:
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "How do I restart the service?"},
    ]


def _ctx(user: _FakeUser, messages: list[dict] | None = None) -> PipelineContext:
    return PipelineContext(user=user, body={}, messages=messages or _messages())


class TestClientTypeMatrix:
    """(b) MCP-capable key -> no injection; plain key -> ranked, budgeted, single message."""

    @pytest.mark.asyncio
    async def test_mcp_capable_key_gets_no_injection(self) -> None:
        """An MCP-capable key never triggers retrieval or message mutation."""
        retriever = _stub_retriever([_block("r1", 100)])
        stage = KnowledgeInjectStage("knowledge", retriever, features=_AllFlagsOnFeatures())
        original_messages = _messages()
        ctx = _ctx(_FakeUser(mcp_capable=True), messages=copy.deepcopy(original_messages))

        result = await stage(ctx)

        assert result.messages == original_messages
        retriever.retrieve.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_mcp_session_also_skips_injection(self) -> None:
        """An active MCP session (not just the mcp_capable flag) also skips injection."""
        retriever = _stub_retriever([_block("r1", 100)])
        stage = KnowledgeInjectStage("knowledge", retriever, features=_AllFlagsOnFeatures())
        ctx = _ctx(_FakeUser(mcp_session_active=True))

        result = await stage(ctx)

        assert result.messages == _messages()
        retriever.retrieve.assert_not_called()

    @pytest.mark.asyncio
    async def test_plain_client_gets_ranked_context_injected_as_one_message(self) -> None:
        """A plain (non-MCP) client gets exactly one injected, provenance-headed message."""
        retriever = _stub_retriever([_block("r1", 50), _block("r2", 50)])
        stage = KnowledgeInjectStage("knowledge", retriever, features=_AllFlagsOnFeatures())
        ctx = _ctx(_FakeUser())

        result = await stage(ctx)

        assert len(result.messages) == 3  # system, injected, user
        injected = result.messages[1]
        assert injected["role"] == "user"
        assert "Retrieved context" in injected["content"]
        assert "content for r1" in injected["content"]
        assert "content for r2" in injected["content"]
        # The original user message stays last, unchanged.
        assert result.messages[-1] == _messages()[-1]


class TestTokenBudgetTruncation:
    """(c) Default 2000, per-key override honored; truncation never splits a block."""

    @pytest.mark.asyncio
    async def test_default_budget_is_2000(self) -> None:
        """One 1900-token block fits the default 2000 budget; a second 200-token block doesn't."""
        retriever = _stub_retriever([_block("r1", 1900), _block("r2", 200)])
        stage = KnowledgeInjectStage("knowledge", retriever, features=_AllFlagsOnFeatures())
        ctx = _ctx(_FakeUser())

        result = await stage(ctx)

        injected = result.messages[1]["content"]
        assert "content for r1" in injected
        assert "content for r2" not in injected
        assert result.usage["waddleai"]["injected_tokens"] == 1900

    @pytest.mark.asyncio
    async def test_per_key_override_honored(self) -> None:
        """A per-key token_budget override changes how many blocks fit."""
        retriever = _stub_retriever([_block("r1", 100), _block("r2", 100), _block("r3", 100)])
        stage = KnowledgeInjectStage("knowledge", retriever, features=_AllFlagsOnFeatures())
        ctx = _ctx(_FakeUser(memory_injection_token_budget=150))

        result = await stage(ctx)

        injected = result.messages[1]["content"]
        assert "content for r1" in injected
        assert "content for r2" not in injected
        assert "content for r3" not in injected

    @pytest.mark.asyncio
    async def test_truncation_never_splits_a_block(self) -> None:
        """A block that would exceed the budget is dropped whole, not truncated mid-text."""
        retriever = _stub_retriever([_block("r1", 1500), _block("r2", 600)])
        stage = KnowledgeInjectStage("knowledge", retriever, features=_AllFlagsOnFeatures())
        ctx = _ctx(_FakeUser(memory_injection_token_budget=2000))

        result = await stage(ctx)

        injected = result.messages[1]["content"]
        # r2 (600) doesn't fit after r1 (1500) within a 2000 budget -- it is
        # dropped entirely, never partially included.
        assert "content for r1" in injected
        assert "content for r2" not in injected


class TestUsageAccounting:
    """(d) usage.waddleai.injected_tokens accounts the injected count."""

    @pytest.mark.asyncio
    async def test_injected_tokens_recorded_in_usage(self) -> None:
        """The sum of injected blocks' token_estimate lands in usage.waddleai.injected_tokens."""
        retriever = _stub_retriever([_block("r1", 42), _block("r2", 58)])
        stage = KnowledgeInjectStage("knowledge", retriever, features=_AllFlagsOnFeatures())
        ctx = _ctx(_FakeUser())

        result = await stage(ctx)

        assert result.usage["waddleai"]["injected_tokens"] == 100

    @pytest.mark.asyncio
    async def test_usage_dict_preserved_when_preexisting(self) -> None:
        """Existing usage keys (e.g. from earlier stages) survive alongside the new waddleai key."""
        retriever = _stub_retriever([_block("r1", 10)])
        stage = KnowledgeInjectStage("knowledge", retriever, features=_AllFlagsOnFeatures())
        ctx = _ctx(_FakeUser())
        ctx.usage = {"input_tokens": 5}

        result = await stage(ctx)

        assert result.usage["input_tokens"] == 5
        assert result.usage["waddleai"]["injected_tokens"] == 10


class TestPerKeyOverride:
    """(e) Per-key memory_injection override in both directions."""

    @pytest.mark.asyncio
    async def test_memory_injection_disabled_skips_entirely(self) -> None:
        """memory_injection_enabled=False skips injection for an otherwise-plain client."""
        retriever = _stub_retriever([_block("r1", 10)])
        stage = KnowledgeInjectStage("knowledge", retriever, features=_AllFlagsOnFeatures())
        ctx = _ctx(_FakeUser(memory_injection_enabled=False))

        result = await stage(ctx)

        assert result.messages == _messages()
        retriever.retrieve.assert_not_called()

    @pytest.mark.asyncio
    async def test_sources_override_restricts_which_sources_are_queried(self) -> None:
        """memory_injection_sources=['code'] queries only 'code', not 'docs'/'uploaded'/'memory'."""
        retriever = _stub_retriever([_block("r1", 10)])
        stage = KnowledgeInjectStage("knowledge", retriever, features=_AllFlagsOnFeatures())
        ctx = _ctx(_FakeUser(memory_injection_sources=["code"]))

        await stage(ctx)

        retriever.retrieve.assert_awaited_once()
        _query, _caller = retriever.retrieve.await_args.args
        assert retriever.retrieve.await_args.kwargs["sources"] == ["code"]


class TestFlagOffNoOp:
    """(g) With all four §9/§6A flags off, the stage is a no-op -- request bytes unchanged."""

    @pytest.mark.asyncio
    async def test_all_sources_flagged_off_is_a_noop(self) -> None:
        """No source is queried and messages are byte-for-byte unchanged when all flags are off."""
        retriever = _stub_retriever([_block("r1", 10)])
        stage = KnowledgeInjectStage("knowledge", retriever, features=_AllFlagsOffFeatures())
        original = copy.deepcopy(_messages())
        ctx = _ctx(_FakeUser(), messages=copy.deepcopy(original))

        result = await stage(ctx)

        assert result.messages == original
        retriever.retrieve.assert_not_called()
        assert result.usage is None
