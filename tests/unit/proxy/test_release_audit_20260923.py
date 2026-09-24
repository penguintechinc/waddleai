"""Release-audit-2026-09-23 proxy data-plane hardening regressions.

Each test guards one audited High finding. Self-contained (no running app):
feature-flag caching/fail-closed, off-event-loop flag resolution, the
concurrency limiter, the async trace exporter, and the decrypt-on-read handoff.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from proxy.apps.proxy_server import feature_flag_cache as ffc
from proxy.apps.proxy_server.pipeline import (
    METERING_FLAG,
    MeterStage,
    PipelineContext,
    ProxyPipeline,
)
from proxy.apps.proxy_server.pipeline.stages import _resolve_flag


class _OutageClient:
    """PostHog stand-in whose evaluation always raises (a flag-store outage)."""

    def feature_enabled(self, flag_key: str, distinct_id: str) -> bool:
        raise RuntimeError("posthog unreachable")


class _ConstClient:
    """PostHog stand-in returning a fixed definite value."""

    def __init__(self, value: bool) -> None:
        self.value = value
        self.calls = 0

    def feature_enabled(self, flag_key: str, distinct_id: str) -> bool:
        self.calls += 1
        return self.value


@pytest.fixture(autouse=True)
def _no_env_flag_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no WADDLEAI_FLAG_* override shadows the PostHog resolution path."""
    for var in ("WADDLEAI_FLAG_SECURITY_V2", "WADDLEAI_FLAG_SMART_ROUTING"):
        monkeypatch.delenv(var, raising=False)


class TestSecurityFlagFailsClosed:
    """Finding #1: a security flag must fail CLOSED (redact) on a flag-store outage."""

    # regression: release-audit-2026-09-23
    async def test_security_v2_fails_closed_on_outage_uncached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never-cached security_v2 + PostHog outage -> ON (redact), never OFF."""
        monkeypatch.setattr(ffc, "_get_posthog_client", lambda: _OutageClient())
        helper = ffc.FeatureFlagsHelper()
        # The pre-fix behaviour returned the hardcoded default (False) here,
        # silently disabling upstream PII redaction on a PostHog hiccup.
        assert await helper.resolve(ffc.SECURITY_V2_FLAG, "org-1") is True

    # regression: release-audit-2026-09-23
    async def test_non_security_flag_fails_to_default_on_outage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """smart_routing is not security -> falls to caller default, not fail-closed."""
        monkeypatch.setattr(ffc, "_get_posthog_client", lambda: _OutageClient())
        helper = ffc.FeatureFlagsHelper()
        assert await helper.resolve("waddleai.smart_routing", "org-1", default=False) is False

    # regression: release-audit-2026-09-23
    async def test_outage_uses_last_known_cached_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A prior resolved value is served on a later outage (graceful degradation)."""
        # ttl=0 forces a re-resolve every call so the outage path is actually hit.
        helper = ffc.FeatureFlagsHelper(ttl_seconds=0.0)
        monkeypatch.setattr(ffc, "_get_posthog_client", lambda: _ConstClient(False))
        assert await helper.resolve(ffc.SECURITY_V2_FLAG, "org-1") is False  # resolved + cached
        monkeypatch.setattr(ffc, "_get_posthog_client", lambda: _OutageClient())
        # last-known False wins over the fail-closed default because it was cached.
        assert await helper.resolve(ffc.SECURITY_V2_FLAG, "org-1") is False

    # regression: release-audit-2026-09-23
    async def test_definite_disable_is_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A reachable store that says OFF for this org is respected (no over-redaction)."""
        monkeypatch.setattr(ffc, "_get_posthog_client", lambda: _ConstClient(False))
        helper = ffc.FeatureFlagsHelper()
        assert await helper.resolve(ffc.SECURITY_V2_FLAG, "org-1") is False

    # regression: release-audit-2026-09-23
    async def test_unconfigured_store_is_not_an_outage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No PostHog key at all is deliberate -> caller default, not fail-closed redaction."""
        monkeypatch.setattr(ffc, "_get_posthog_client", lambda: None)
        helper = ffc.FeatureFlagsHelper()
        assert await helper.resolve(ffc.SECURITY_V2_FLAG, "org-1") is False


class TestFlagCaching:
    """Finding #7: cached resolution avoids repeat blocking lookups."""

    # regression: release-audit-2026-09-23
    async def test_fresh_cache_hit_skips_the_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Within TTL, a second resolve serves the cache without calling PostHog."""
        client = _ConstClient(True)
        monkeypatch.setattr(ffc, "_get_posthog_client", lambda: client)
        helper = ffc.FeatureFlagsHelper(ttl_seconds=60.0)
        assert await helper.resolve("waddleai.smart_routing", "org-1") is True
        assert await helper.resolve("waddleai.smart_routing", "org-1") is True
        assert client.calls == 1  # second call was a cache hit


class TestResolveFlagOffEventLoop:
    """Finding #7: sync flag stubs are moved off the event loop, never blocking it."""

    # regression: release-audit-2026-09-23
    async def test_sync_stub_does_not_block_the_loop(self) -> None:
        """A blocking sync is_feature_enabled runs in a thread; other tasks keep running."""

        class _SlowFeatures:
            def is_feature_enabled(self, flag_key: str, **_: object) -> bool:
                time.sleep(0.2)  # noqa: ASYNC251 -- deliberately blocking stub
                return True

        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(20):
                ticks += 1
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        result = await _resolve_flag(_SlowFeatures(), "waddleai.smart_routing", "org-1")
        ticks_during_resolve = ticks
        await task

        assert result is True
        # If the 0.2s sleep had run on the loop, the ticker could not have ticked.
        assert ticks_during_resolve >= 3

    # regression: release-audit-2026-09-23
    async def test_none_features_resolves_to_default(self) -> None:
        """A None features helper resolves to the caller default without raising."""
        assert await _resolve_flag(None, "waddleai.smart_routing", "org-1", default=False) is False


class TestConcurrencyLimiter:
    """Finding #6: max_concurrent_requests is actually enforced."""

    # regression: release-audit-2026-09-23
    def test_sheds_beyond_limit_and_frees_on_leave(self) -> None:
        """The N+1th concurrent entry is shed; a leave() frees a slot."""
        from proxy.apps.proxy_server.main import ConcurrencyLimiter

        limiter = ConcurrencyLimiter(limit=2)
        assert limiter.try_enter() is True
        assert limiter.try_enter() is True
        assert limiter.try_enter() is False  # N+1 shed
        assert limiter.active == 2
        limiter.leave()
        assert limiter.try_enter() is True  # slot freed after leave

    # regression: release-audit-2026-09-23
    def test_zero_limit_disables_the_gate(self) -> None:
        """limit<=0 means unlimited: every entry is admitted."""
        from proxy.apps.proxy_server.main import ConcurrencyLimiter

        limiter = ConcurrencyLimiter(limit=0)
        for _ in range(1000):
            assert limiter.try_enter() is True


class TestTracingAsyncExport:
    """Finding #5: the proxy uses a non-blocking (batch) span processor."""

    # regression: release-audit-2026-09-23
    def test_uses_batch_span_processor_not_simple(self) -> None:
        """init_tracing registers a BatchSpanProcessor (async export), never Simple."""
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

        import shared.observability.tracing as tracing

        added: list[object] = []

        class _CaptureProvider(TracerProvider):
            def add_span_processor(self, processor: object) -> None:  # type: ignore[override]
                added.append(processor)

        tracing._tracer = None
        tracing._initialized = False
        monkey = TracerProvider  # noqa: F841 -- readability marker only
        orig = tracing.TracerProvider
        tracing.TracerProvider = _CaptureProvider  # type: ignore[misc]
        try:
            tracing.init_tracing(tracing.TracingConfig(otlp_endpoint="http://localhost:4317"))
        finally:
            tracing.TracerProvider = orig  # type: ignore[misc]
            tracing._tracer = None
            tracing._initialized = False

        assert added, "no span processor was registered"
        assert any(isinstance(p, BatchSpanProcessor) for p in added)
        assert not any(isinstance(p, SimpleSpanProcessor) for p in added)


class TestDecryptOnRead:
    """Finding #8: the provider-dispatch read path decrypts an enc: credential."""

    # regression: release-audit-2026-09-23
    def test_select_credential_decrypts_enc_prefixed_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An enc: connection_link api_key is decrypted before the connector uses it."""
        import types

        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "release-audit-test-secret")
        from shared.security.credential_encryption import encrypt_credential
        from shared.utils.llm_connectors import LLMConnectionManager

        stored = encrypt_credential("sk-real-upstream-secret")
        assert stored.startswith("enc:")

        # Build a manager without running _load_connectors (needs a live DB); the
        # object under test is the real _select_credential fallback read path.
        manager = object.__new__(LLMConnectionManager)
        manager.db = object()  # no provider_credentials attr -> falls back to link.api_key
        link = types.SimpleNamespace(name="openai", api_key=stored)

        assert manager._select_credential(link) == "sk-real-upstream-secret"


class TestMeterStageGh216:
    """gh-216: MeterStage is explicitly flag-gated OFF, not a silent dead gate."""

    @staticmethod
    def _pipeline() -> tuple[Mock, ProxyPipeline]:
        """A one-stage pipeline (MeterStage behind METERING_FLAG) + real flag helper."""
        buf = Mock()
        limiter = Mock()
        limiter.reconcile = AsyncMock()
        stage = MeterStage(
            name="meter", metering_buffer=buf, token_limiter=limiter, flag=METERING_FLAG
        )
        return buf, ProxyPipeline([stage], ffc.FeatureFlagsHelper())

    # regression: release-audit-2026-09-23 / gh-216
    async def test_meter_skipped_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no flag configured (default), MeterStage is skipped explicitly, never run."""
        monkeypatch.delenv("WADDLEAI_FLAG_METERING", raising=False)
        # Unconfigured flag store -> DEFAULTED -> default OFF (non-security, never fail-closed).
        monkeypatch.setattr(ffc, "_get_posthog_client", lambda: None)
        buf, pipe = self._pipeline()
        ctx = await pipe.run(PipelineContext(user=SimpleNamespace(id=1), body={}))
        assert "skipped:meter" in ctx.stage_log
        assert "ran:meter" not in ctx.stage_log
        buf.record.assert_not_called()

    # regression: release-audit-2026-09-23 / gh-216
    async def test_meter_runs_when_flag_forced_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Forcing waddleai.metering ON makes the pipeline execute MeterStage."""
        monkeypatch.setenv("WADDLEAI_FLAG_METERING", "1")
        buf, pipe = self._pipeline()
        ctx = await pipe.run(PipelineContext(user=SimpleNamespace(id=1), body={}))
        assert "ran:meter" in ctx.stage_log
        assert "skipped:meter" not in ctx.stage_log

    # regression: release-audit-2026-09-23 / gh-216
    async def test_metering_flag_is_non_security_and_off_on_outage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A flag-store OUTAGE leaves metering OFF (fail-open default), never fail-closed."""
        monkeypatch.delenv("WADDLEAI_FLAG_METERING", raising=False)

        class _OutageClient:
            def feature_enabled(self, flag_key: str, distinct_id: str) -> bool:
                raise RuntimeError("posthog unreachable")

        monkeypatch.setattr(ffc, "_get_posthog_client", lambda: _OutageClient())
        assert (
            await ffc.FeatureFlagsHelper().resolve(METERING_FLAG, "org-1", default=False) is False
        )
