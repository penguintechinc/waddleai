"""§6A pipeline wiring tests: both-endpoint stage-log parity, usage.waddleai merge, flag-off proof.

Builds a real ProxyPipeline (Auth -> SecurityIn -> Scratchpad -> Summarize
-> Dedup -> Dispatch -> SecurityOut) with the same stage classes and
ordering as ProxyServer._build_pipeline, using the established fake
Valkey/DB doubles (no fakeredis in this environment). TokenBudgetStage/
MeterStage are omitted -- they are orthogonal to what this file asserts
(memory-stage wiring and additive usage accounting), and a plain (non-Mock)
FakeUser without a `vkey_id` attribute would skip them as no-ops anyway.

`_waddleai_usage_meta` is imported directly from
proxy.apps.proxy_server.main -- confirmed importable standalone in this
sandbox (no DB/network I/O at import time; only proxy_server.startup()
touches those) even without the WADDLEAI_STUB_UPSTREAM=1 test-mode flag.
"""

from dataclasses import dataclass

import pytest

from proxy.apps.proxy_server.main import _waddleai_usage_meta
from proxy.apps.proxy_server.pipeline.memory_stages import (
    DedupStage,
    ScratchpadStage,
    SummarizationStage,
)
from proxy.apps.proxy_server.pipeline.stages import (
    AuthStage,
    DispatchStage,
    PipelineContext,
    ProxyPipeline,
    SecurityInStage,
    SecurityOutStage,
)
from shared.memory.config import build_config_resolver
from shared.memory.dedup_store import DedupStore
from shared.memory.scratchpad import ScratchpadStore
from shared.memory.summarizer import ConversationSummarizer
from shared.memory.token_len_cache import TokenLenCache
from shared.security.content_filter import ContentFilter
from shared.security.prompt_security import PromptSecurityScanner
from tests.unit.memory.test_scratchpad import FakeScratchpadDB, FakeValkey
from tests.unit.memory.test_summarizer import FakeLLMManager, FakeSummarizerDB


@dataclass(slots=True)
class FakeUser:
    """Authenticated-caller identity double, deliberately NOT a Mock.

    Mock auto-creates any attribute, which would make
    `hasattr(ctx.user, "vkey_id")` always True and defeat the
    TokenBudgetStage/MeterStage omission this file relies on.
    """

    user_id: int
    organization_id: int
    api_key_id: int | None = None


class TogglableFeatures:
    """Mirrors main.py's FeatureFlagsHelper shape, but toggleable for tests."""

    def __init__(self, enabled: bool = True, raises: bool = False):
        """Set the flag's return value, or make it raise instead of returning."""
        self.enabled = enabled
        self.raises = raises

    def is_feature_enabled(self, flag_key: str, distinct_id: str = None) -> bool:
        """Return the configured value, or raise if constructed with raises=True."""
        if self.raises:
            raise RuntimeError("feature flag backend unavailable")
        return self.enabled


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


class StubRouter:
    """Always routes to the single "stub" provider, requested model unchanged."""

    def select_provider(self, model, preferred_backend=None):
        """Always route to the "stub" provider with the model unchanged.

        Accepts ``preferred_backend`` because DispatchStage always passes it
        (session-affinity hint from the response cache, §6). A stub that
        omits it raises TypeError once both features are wired together.
        """
        return "stub", model


def _build_test_pipeline(features, valkey, db, summarizer_db, connector):
    scanner = PromptSecurityScanner(db=None, policy_name="balanced")
    content_filter = ContentFilter(db=None)
    config_resolver = build_config_resolver(db=None, features=features)

    scratchpad_store = ScratchpadStore(valkey, db, scanner, content_filter)
    token_len_cache = TokenLenCache(valkey)
    summarizer = ConversationSummarizer(
        summarizer_db, FakeLLMManager(connector), token_len_cache, scanner, content_filter
    )
    dedup_store = DedupStore(valkey)

    stages = [
        AuthStage(name="auth", flag=None),
        SecurityInStage(
            name="security_in", scanner=scanner, content_filter=content_filter, flag=None
        ),
        ScratchpadStage(
            name="scratchpad",
            store=scratchpad_store,
            config_resolver=config_resolver,
            scanner=scanner,
            content_filter=content_filter,
            flag="waddleai.proxy_memory",
        ),
        SummarizationStage(
            name="summarize",
            summarizer=summarizer,
            config_resolver=config_resolver,
            scanner=scanner,
            content_filter=content_filter,
            flag="waddleai.proxy_memory",
        ),
        DedupStage(
            name="dedup",
            dedup_store=dedup_store,
            token_len_cache=token_len_cache,
            config_resolver=config_resolver,
            floor_tokens=10,
            flag="waddleai.proxy_memory",
        ),
        DispatchStage(
            name="dispatch", router=StubRouter(), connectors={"stub": connector}, flag=None
        ),
        SecurityOutStage(name="security_out", content_filter=content_filter, flag=None),
    ]
    return ProxyPipeline(stages=stages, features=features)


def _make_ctx(user, body, messages, session_id) -> PipelineContext:
    return PipelineContext(
        user=user, body=body, model="gpt-4", messages=messages, session_id=session_id
    )


class TestBothEndpointsStageLogParity:
    """Both endpoint shapes produce the same stage_log order through the shared pipeline."""

    @pytest.mark.asyncio
    async def test_stage_log_order_and_parity_with_flag_on(self):
        """Flag on: both endpoint shapes run the same stages in the same order."""
        features = TogglableFeatures(enabled=True)
        valkey = FakeValkey()
        db = FakeScratchpadDB()
        summarizer_db = FakeSummarizerDB()
        connector = StubDispatchConnector()
        pipeline = _build_test_pipeline(features, valkey, db, summarizer_db, connector)

        user = FakeUser(user_id=10, organization_id=1)
        messages = [{"role": "user", "content": "hello"}]

        # One context "as chat_completions() would build", one "as
        # claude_messages() would build" -- same shape, different body.
        ctx_openai = _make_ctx(
            user, {"model": "gpt-4", "messages": messages}, list(messages), "sess-a"
        )
        ctx_claude = _make_ctx(
            user,
            {"model": "claude-3-sonnet-20240229", "messages": messages},
            list(messages),
            "sess-a",
        )

        result_openai = await pipeline.run(ctx_openai)
        result_claude = await pipeline.run(ctx_claude)

        expected_order = [
            "auth",
            "security_in",
            "scratchpad",
            "summarize",
            "dedup",
            "dispatch",
            "security_out",
        ]
        assert [entry.split(":", 1)[1] for entry in result_openai.stage_log] == expected_order
        assert [e.split(":", 1)[1] for e in result_openai.stage_log] == [
            e.split(":", 1)[1] for e in result_claude.stage_log
        ]


class TestUsageWaddleaiMerge:
    """_waddleai_usage_meta: additive merge, empty/zeroed usage_meta omitted."""

    def test_populated_usage_meta_produces_object(self):
        """A fully-populated usage_meta round-trips through the merge helper unchanged."""
        ctx = PipelineContext(user=None, body={})
        ctx.usage_meta = {
            "summarized": True,
            "tokens_elided": 42,
            "tokens_saved": 7,
            "scratchpad_substitutions": 2,
        }
        result = _waddleai_usage_meta(ctx)
        assert result == {
            "summarized": True,
            "tokens_elided": 42,
            "tokens_saved": 7,
            "scratchpad_substitutions": 2,
        }

    def test_empty_usage_meta_returns_none(self):
        """An empty usage_meta returns None instead of an empty dict."""
        ctx = PipelineContext(user=None, body={})
        assert _waddleai_usage_meta(ctx) is None

    def test_zero_valued_fields_omitted(self):
        """All-zero usage_meta fields collapse to None, not a zeroed object."""
        ctx = PipelineContext(user=None, body={})
        ctx.usage_meta = {"tokens_elided": 0, "tokens_saved": 0, "scratchpad_substitutions": 0}
        assert _waddleai_usage_meta(ctx) is None


class TestFlagOffProof:
    """Flag off (or a raising features client) -- zero memory-layer writes, no crash."""

    @pytest.mark.asyncio
    async def test_flag_off_all_three_stages_skipped_no_writes(self):
        """Flag off: all three memory stages are skipped, zero Valkey/DB writes."""
        features = TogglableFeatures(enabled=False)
        valkey = FakeValkey()
        db = FakeScratchpadDB()
        summarizer_db = FakeSummarizerDB()
        connector = StubDispatchConnector()
        pipeline = _build_test_pipeline(features, valkey, db, summarizer_db, connector)

        user = FakeUser(user_id=10, organization_id=1)
        messages = [{"role": "user", "content": "hello"}]
        ctx = _make_ctx(user, {"model": "gpt-4", "messages": messages}, list(messages), "sess-a")

        result = await pipeline.run(ctx)

        assert "skipped:scratchpad" in result.stage_log
        assert "skipped:summarize" in result.stage_log
        assert "skipped:dedup" in result.stage_log
        assert result.usage_meta == {}
        assert _waddleai_usage_meta(result) is None

        # No memory-layer state written anywhere.
        assert not any(k.startswith("waddleai:sp:") for k in valkey.store)
        assert not any(k.startswith("waddleai:dedup:") for k in valkey.store)
        assert not any(k.startswith("waddleai:rr:") for k in valkey.store)
        assert db.rows == {}
        assert summarizer_db.rows == {}

    @pytest.mark.asyncio
    async def test_config_resolver_features_raising_is_fail_safe(self):
        """Per-key config resolution degrades to ALL_DISABLED when features raises.

        The pipeline-level whole-feature flag is on (as it would genuinely
        be in production -- FeatureFlagsHelper's underlying
        shared.utils.feature_flags.is_feature_enabled already catches every
        exception and returns the fail-safe default, so ProxyPipeline's own
        coarse gate cannot realistically raise). This isolates the fail-safe
        path that resolve_proxy_memory_config actually owns: per-key config
        resolution degrading to ALL_DISABLED when the *injected* features
        client raises, without crashing the request.
        """
        pipeline_features = TogglableFeatures(enabled=True)
        valkey = FakeValkey()
        db = FakeScratchpadDB()
        summarizer_db = FakeSummarizerDB()
        connector = StubDispatchConnector()

        scanner = PromptSecurityScanner(db=None, policy_name="balanced")
        content_filter = ContentFilter(db=None)
        # This is the features client resolve_proxy_memory_config consults --
        # deliberately broken to prove the per-key config path degrades safely.
        raising_config_resolver = build_config_resolver(
            db=None, features=TogglableFeatures(raises=True)
        )

        scratchpad_store = ScratchpadStore(valkey, db, scanner, content_filter)
        token_len_cache = TokenLenCache(valkey)
        summarizer = ConversationSummarizer(
            summarizer_db, FakeLLMManager(connector), token_len_cache, scanner, content_filter
        )
        dedup_store = DedupStore(valkey)

        stages = [
            AuthStage(name="auth", flag=None),
            SecurityInStage(
                name="security_in", scanner=scanner, content_filter=content_filter, flag=None
            ),
            ScratchpadStage(
                name="scratchpad",
                store=scratchpad_store,
                config_resolver=raising_config_resolver,
                scanner=scanner,
                content_filter=content_filter,
                flag="waddleai.proxy_memory",
            ),
            SummarizationStage(
                name="summarize",
                summarizer=summarizer,
                config_resolver=raising_config_resolver,
                scanner=scanner,
                content_filter=content_filter,
                flag="waddleai.proxy_memory",
            ),
            DedupStage(
                name="dedup",
                dedup_store=dedup_store,
                token_len_cache=token_len_cache,
                config_resolver=raising_config_resolver,
                floor_tokens=10,
                flag="waddleai.proxy_memory",
            ),
            DispatchStage(
                name="dispatch", router=StubRouter(), connectors={"stub": connector}, flag=None
            ),
            SecurityOutStage(name="security_out", content_filter=content_filter, flag=None),
        ]
        pipeline = ProxyPipeline(stages=stages, features=pipeline_features)

        user = FakeUser(user_id=10, organization_id=1)
        messages = [{"role": "user", "content": "hello"}]
        ctx = _make_ctx(user, {"model": "gpt-4", "messages": messages}, list(messages), "sess-a")

        result = await pipeline.run(ctx)

        assert result.blocked is False
        assert result.response_text == "stub completion"
        assert db.rows == {}
        assert summarizer_db.rows == {}
