"""KnowledgeInjectStage pipeline-wiring tests: composed-position + flag-off proof (§9.5/§9.6).

Complements tests/unit/proxy/test_knowledge_stage.py (the stage's own
in-isolation behavior matrix). This file proves the stage is actually
registered in a real ProxyPipeline and that KNOWLEDGE_INJECT_FLAG gates it
exactly like RESPONSE_CACHE_FLAG/PROXY_MEMORY_FLAG gate CacheStage/the §6A
memory stages -- see tests/unit/proxy/test_memory_pipeline_wiring.py's
TestFlagOffProof for the pattern this mirrors. The documented insertion
point itself (between SummarizationStage and DedupStage, so cache keys hash
the fully-assembled dispatch context and so retrieval/summarization/dedup
each see the request in the right shape) lives in
proxy/apps/proxy_server/main.py's _build_pipeline and
proxy/apps/proxy_server/pipeline/memory_stages.py's module docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from proxy.apps.proxy_server.pipeline.knowledge_stage import (
    KNOWLEDGE_INJECT_FLAG,
    KnowledgeInjectStage,
)
from proxy.apps.proxy_server.pipeline.stages import (
    AuthStage,
    DispatchStage,
    PipelineContext,
    ProxyPipeline,
    SecurityInStage,
    SecurityOutStage,
)
from shared.knowledge.injection_safety import InjectableBlock
from shared.knowledge.scoping import TrustTier
from shared.security.content_filter import ContentFilter
from shared.security.prompt_security import PromptSecurityScanner


@dataclass(slots=True)
class FakeUser:
    """Authenticated-caller identity double, deliberately NOT a Mock.

    See tests/unit/proxy/test_memory_pipeline_wiring.py's FakeUser for why:
    a Mock auto-creates attributes and would defeat the MCP-capability /
    per-key-override checks KnowledgeInjectStage does via getattr(..., default).
    """

    id: int
    organization_id: int


class SelectiveFeatures:
    """Feature helper returning True only for an explicit allow-set of flag keys.

    Records every flag_key it was asked about, so tests can assert exactly
    which flags a run consulted (and, by omission, which it never touched).
    """

    def __init__(self, enabled_flags: set) -> None:
        """Store the allow-set of flag keys that evaluate True."""
        self.enabled_flags = enabled_flags
        self.calls: list[str] = []

    def is_feature_enabled(self, flag_key: str, distinct_id: str | None = None) -> bool:
        """Return True iff flag_key is in the configured allow-set."""
        self.calls.append(flag_key)
        return flag_key in self.enabled_flags


class StubRouter:
    """Always routes to the single "stub" provider, requested model unchanged."""

    def select_provider(self, model, preferred_backend=None):
        """Always route to "stub" unchanged.

        Accepts ``preferred_backend`` because DispatchStage always passes it
        (session-affinity hint from the response cache, §6) -- a stub that
        omits it raises TypeError, degrading the request to a 503 for an
        unrelated reason. See test_memory_pipeline_wiring.py's StubRouter.
        """
        return "stub", model


class StubDispatchConnector:
    """Deterministic stub connector -- no real dispatch."""

    async def chat_completion(self, messages, model=None, **kwargs):
        """Return a fixed stub completion and usage dict."""
        return "stub completion", {
            "provider": "stub",
            "input_tokens": 5,
            "output_tokens": 3,
            "finish_reason": "stop",
        }


def _block(record_id: str, tokens: int = 50) -> InjectableBlock:
    return InjectableBlock(
        record_id=record_id,
        text=f"> [derived repo-scope knowledge]\n> content for {record_id}",
        trust_tier=TrustTier.DERIVED,
        token_estimate=tokens,
    )


def _messages() -> list[dict]:
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "How do I restart the service?"},
    ]


def _build_pipeline(features, retriever, connector) -> ProxyPipeline:
    """Build Auth -> SecurityIn -> Knowledge -> Dispatch -> SecurityOut.

    ScratchpadStage/SummarizationStage/DedupStage are omitted here (they're
    covered end-to-end in test_memory_pipeline_wiring.py) -- this pipeline
    isolates KnowledgeInjectStage's own wiring while still proving it runs
    as a real Stage inside a real ProxyPipeline, gated by its own flag.
    """
    scanner = PromptSecurityScanner(db=None, policy_name="balanced")
    content_filter = ContentFilter(db=None)

    stages = [
        AuthStage(name="auth", flag=None),
        SecurityInStage(
            name="security_in", scanner=scanner, content_filter=content_filter, flag=None
        ),
        KnowledgeInjectStage(
            name="knowledge",
            retriever=retriever,
            features=features,
            flag=KNOWLEDGE_INJECT_FLAG,
        ),
        DispatchStage(
            name="dispatch", router=StubRouter(), connectors={"stub": connector}, flag=None
        ),
        SecurityOutStage(name="security_out", content_filter=content_filter, flag=None),
    ]
    return ProxyPipeline(stages=stages, features=features)


class TestKnowledgeInjectStageRunsInComposedPipeline:
    """KNOWLEDGE_INJECT_FLAG on: the stage actually runs and mutates the request."""

    @pytest.mark.asyncio
    async def test_flag_on_stage_runs_and_injects_context(self):
        """Flag on + a source flag on: stage runs, injects one block, ran:knowledge logged."""
        retriever = AsyncMock()
        retriever.retrieve = AsyncMock(return_value=[_block("r1")])
        features = SelectiveFeatures({KNOWLEDGE_INJECT_FLAG, "waddleai.coderag"})
        connector = StubDispatchConnector()
        pipeline = _build_pipeline(features, retriever, connector)

        user = FakeUser(id=1, organization_id=1)
        ctx = PipelineContext(
            user=user, body={"model": "gpt-4"}, model="gpt-4", messages=_messages()
        )

        result = await pipeline.run(ctx)

        assert result.stage_log == [
            "ran:auth",
            "ran:security_in",
            "ran:knowledge",
            "ran:dispatch",
            "ran:security_out",
        ]
        assert len(result.messages) == 3  # system, injected, user
        injected = result.messages[1]
        assert injected["role"] == "user"
        assert "Retrieved context" in injected["content"]
        assert "content for r1" in injected["content"]
        # Only the enabled source ("code") was queried.
        retriever.retrieve.assert_awaited_once()
        assert retriever.retrieve.await_args.kwargs["sources"] == ["code"]
        # The request still dispatches normally after injection.
        assert result.response_text == "stub completion"


class TestKnowledgeInjectStageFlagOff:
    """KNOWLEDGE_INJECT_FLAG off: the stage is skipped -- zero queries, request unchanged."""

    @pytest.mark.asyncio
    async def test_flag_off_stage_skipped_zero_queries_no_mutation(self):
        """Flag off: skipped:knowledge logged, retriever never called, messages byte-for-byte."""
        retriever = AsyncMock()
        retriever.retrieve = AsyncMock(return_value=[_block("r1")])
        # KNOWLEDGE_INJECT_FLAG deliberately absent from the allow-set; the
        # per-source flags being on wouldn't matter -- ProxyPipeline gates on
        # stage.flag before __call__ (and therefore _resolve_enabled_sources)
        # ever runs.
        features = SelectiveFeatures({"waddleai.coderag", "waddleai.docs_cache"})
        connector = StubDispatchConnector()
        pipeline = _build_pipeline(features, retriever, connector)

        user = FakeUser(id=1, organization_id=1)
        original_messages = _messages()
        ctx = PipelineContext(
            user=user, body={"model": "gpt-4"}, model="gpt-4", messages=list(original_messages)
        )

        result = await pipeline.run(ctx)

        assert result.stage_log == [
            "ran:auth",
            "ran:security_in",
            "skipped:knowledge",
            "ran:dispatch",
            "ran:security_out",
        ]
        assert result.messages == original_messages
        retriever.retrieve.assert_not_called()
        # ProxyPipeline itself evaluates stage.flag to decide the skip;
        # KnowledgeInjectStage.__call__ (and therefore the four per-source
        # flag checks inside it) never runs.
        assert features.calls == [KNOWLEDGE_INJECT_FLAG]
