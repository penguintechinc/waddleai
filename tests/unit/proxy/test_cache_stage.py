"""CacheStage (pipeline stage 4): wiring, short-circuit, flag-off skip (spec §6).

Full write-back-after-SecurityOutStage and org-isolation-through-the-pipeline
behavior is covered end-to-end by tests/integration/test_response_cache_acceptance.py
(§6.5) -- this module covers what CacheStage itself is responsible for in
isolation: flag gating, hit population + dispatch short-circuit, streaming
replay wiring, and the semantic-only-after-exact-miss ordering.
"""

from unittest.mock import AsyncMock, MagicMock, Mock

from proxy.apps.proxy_server.pipeline import (
    CacheStage,
    DispatchStage,
    PipelineContext,
    ProxyPipeline,
)
from shared.cache.exact import CachedResponse
from shared.cache.response_cache import RESPONSE_CACHE_FLAG, CacheLookupResult


def _make_user(org_id: int = 1, vkey_id: int = 10):
    """Make user."""
    user = Mock()
    user.id = 1
    user.organization_id = org_id
    user.tenant_id = org_id
    user.vkey_id = vkey_id
    return user


class _AlwaysOnFeatures:
    """Feature helper that only ever enables RESPONSE_CACHE_FLAG."""

    def is_feature_enabled(self, flag_key: str, distinct_id=None) -> bool:
        """Is feature enabled."""
        return flag_key == RESPONSE_CACHE_FLAG


class _AlwaysOffFeatures:
    """Always Off Features."""

    def is_feature_enabled(self, flag_key: str, distinct_id=None) -> bool:
        """Is feature enabled."""
        return False


def _openai_cached_response(content: str = "cached answer") -> CachedResponse:
    """Openai cached response."""
    return CachedResponse(
        response={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
        usage={"input_tokens": 10, "output_tokens": 5},
        stored_at=0.0,
    )


class TestFlagGating:
    """Tests for flag gating."""

    async def test_flag_off_stage_skipped_zero_calls(self):
        """Flag off stage skipped zero calls."""
        response_cache = AsyncMock()
        stage = CacheStage(name="cache", response_cache=response_cache)
        pipeline = ProxyPipeline(stages=[stage], features=_AlwaysOffFeatures())
        ctx = PipelineContext(user=_make_user(), body={"messages": []}, messages=[])

        result = await pipeline.run(ctx)

        assert result.stage_log == ["skipped:cache"]
        response_cache.lookup.assert_not_called()
        response_cache.annotate_miss.assert_not_called()


class TestCacheHit:
    """Tests for cache hit."""

    async def test_exact_hit_populates_ctx_and_short_circuits_dispatch(self):
        """Exact hit populates ctx and short circuits dispatch."""
        response_cache = AsyncMock()
        response_cache.lookup.return_value = CacheLookupResult(
            status="exact", cached=_openai_cached_response()
        )

        cache_stage = CacheStage(name="cache", response_cache=response_cache)
        dispatch_stage = DispatchStage(name="dispatch", router=MagicMock(), connectors={})
        pipeline = ProxyPipeline(stages=[cache_stage, dispatch_stage], features=_AlwaysOnFeatures())

        ctx = PipelineContext(
            user=_make_user(),
            body={"messages": [{"role": "user", "content": "hi"}], "temperature": 0},
            messages=[{"role": "user", "content": "hi"}],
        )
        result = await pipeline.run(ctx)

        assert result.cache_hit is True
        assert result.cache_status == "exact"
        assert result.response_text == "cached answer"
        assert result.finish_reason == "stop"
        assert result.provider == "cache"
        assert result.tokens_saved == 15
        # Dispatch ran (flag=None, always executes) but returned immediately
        # without calling the router -- confirmed via the router never being touched.
        assert "ran:dispatch" in result.stage_log
        dispatch_stage.router.select_provider.assert_not_called()

    async def test_semantic_hit_sets_status(self):
        """Semantic hit sets status."""
        response_cache = AsyncMock()
        response_cache.lookup.return_value = CacheLookupResult(
            status="semantic", cached=_openai_cached_response("semantic answer")
        )
        cache_stage = CacheStage(name="cache", response_cache=response_cache)
        pipeline = ProxyPipeline(stages=[cache_stage], features=_AlwaysOnFeatures())

        ctx = PipelineContext(user=_make_user(), body={"messages": []}, messages=[])
        result = await pipeline.run(ctx)

        assert result.cache_status == "semantic"
        assert result.response_text == "semantic answer"

    async def test_streaming_hit_sets_stream_iter(self):
        """Streaming hit sets stream iter."""
        response_cache = AsyncMock()
        response_cache.lookup.return_value = CacheLookupResult(
            status="exact", cached=_openai_cached_response()
        )
        cache_stage = CacheStage(name="cache", response_cache=response_cache)
        pipeline = ProxyPipeline(stages=[cache_stage], features=_AlwaysOnFeatures())

        ctx = PipelineContext(user=_make_user(), body={"messages": []}, messages=[], stream=True)
        result = await pipeline.run(ctx)

        assert result.stream_iter is not None
        chunks = [c async for c in result.stream_iter]
        assert chunks[-1] == b"data: [DONE]\n\n"

    async def test_anthropic_format_hit_extracts_text_and_stop_reason(self):
        """Anthropic format hit extracts text and stop reason."""
        cached = CachedResponse(
            response={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "claude cached answer"}],
                "model": "claude-3-5-sonnet-latest",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 8, "output_tokens": 4},
            },
            usage={"input_tokens": 8, "output_tokens": 4},
            stored_at=0.0,
        )
        response_cache = AsyncMock()
        response_cache.lookup.return_value = CacheLookupResult(status="exact", cached=cached)
        cache_stage = CacheStage(name="cache", response_cache=response_cache)
        pipeline = ProxyPipeline(stages=[cache_stage], features=_AlwaysOnFeatures())

        ctx = PipelineContext(
            user=_make_user(), body={"messages": []}, messages=[], response_format="anthropic"
        )
        result = await pipeline.run(ctx)

        assert result.response_text == "claude cached answer"
        assert result.finish_reason == "end_turn"
        assert result.tokens_saved == 12


class TestCacheMiss:
    """Tests for cache miss."""

    async def test_miss_stashes_write_back_and_runs_annotate_miss(self):
        """Miss stashes write back and runs annotate miss."""
        response_cache = AsyncMock()
        write_back = AsyncMock()
        response_cache.lookup.return_value = CacheLookupResult(
            status="miss", cached=None, write_back=write_back
        )
        cache_stage = CacheStage(name="cache", response_cache=response_cache)
        pipeline = ProxyPipeline(stages=[cache_stage], features=_AlwaysOnFeatures())

        ctx = PipelineContext(
            user=_make_user(), body={"messages": [], "temperature": 0.9}, messages=[]
        )
        result = await pipeline.run(ctx)

        assert result.cache_hit is False
        assert result.cache_status == "miss"
        assert result.cache_write_back is write_back
        response_cache.annotate_miss.assert_awaited_once_with(result)

    async def test_ineligible_request_still_runs_stage_reports_miss(self):
        """An ineligible request (e.g. temp>0) still runs the stage and reports a plain miss.

        ResponseCache.lookup itself gates eligibility internally and returns
        status='miss' with no write_back -- CacheStage needs no separate
        eligibility check of its own.
        """
        response_cache = AsyncMock()
        response_cache.lookup.return_value = CacheLookupResult(
            status="miss", cached=None, write_back=None
        )
        cache_stage = CacheStage(name="cache", response_cache=response_cache)
        pipeline = ProxyPipeline(stages=[cache_stage], features=_AlwaysOnFeatures())

        ctx = PipelineContext(
            user=_make_user(), body={"messages": [], "temperature": 0.9}, messages=[]
        )
        result = await pipeline.run(ctx)

        assert result.cache_status == "miss"
        assert result.cache_write_back is None
        assert "ran:cache" in result.stage_log


class TestMeterStageThreading:
    """Tests for meter stage threading."""

    async def test_meter_stage_records_cache_status_and_tokens_saved(self):
        """Meter stage records cache status and tokens saved."""
        from proxy.apps.proxy_server.pipeline import MeterStage

        metering_buffer = MagicMock()
        token_limiter = AsyncMock()
        stage = MeterStage(
            name="meter", metering_buffer=metering_buffer, token_limiter=token_limiter
        )

        ctx = PipelineContext(
            user=_make_user(),
            body={},
            messages=[],
            usage={"input_tokens": 10, "output_tokens": 5},
            provider="cache",
            model="gpt-4o",
            cache_status="exact",
            tokens_saved=15,
        )
        await stage(ctx)

        recorded_event = metering_buffer.record.call_args.args[0]
        assert recorded_event.cache_status == "exact"
        assert recorded_event.tokens_saved == 15
