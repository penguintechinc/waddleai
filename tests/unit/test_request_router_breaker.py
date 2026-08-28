"""Circuit-breaker recovery tests for LLMRequestRouter.

Covers the closed -> open -> half-open -> closed cycle in
``_get_available_providers``. Before half-open existed, a provider that hit the
consecutive-failure threshold was skipped forever: the counter only resets on a
success, and a success requires being selected, which the skip prevented.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from shared.utils.request_router import LLMRequestRouter, ProviderStats


def _router_with(provider: str, stats: ProviderStats) -> LLMRequestRouter:
    """Build a router whose single provider serves every model."""
    connector = MagicMock()
    connector.model_list = []  # empty list == serves all models
    manager = MagicMock()
    manager.connectors = {provider: connector}

    router = LLMRequestRouter(llm_manager=manager, db=MagicMock())
    router.provider_stats = {provider: stats}
    return router


class TestBreakerRecovery:
    """Closed -> open -> half-open -> closed cycle for provider circuit breaking."""

    def test_provider_available_when_healthy(self):
        """A provider with no recorded failures is always offered."""
        router = _router_with("openai", ProviderStats())
        assert "openai" in router._get_available_providers("gpt-4")

    def test_provider_ejected_at_failure_threshold(self):
        """A provider at the consecutive-failure threshold is excluded, not just deprioritized."""
        stats = ProviderStats(consecutive_failures=3, last_failure=datetime.utcnow())
        router = _router_with("openai", stats)
        assert router._get_available_providers("gpt-4") == []

    def test_provider_readmitted_for_one_probe_after_cooldown(self):
        """Regression: a tripped breaker must recover, not eject permanently.

        After the cooldown elapses the provider is offered again as a single
        half-open probe instead of being skipped for the process lifetime.
        """
        stats = ProviderStats(
            consecutive_failures=5,
            last_failure=datetime.utcnow() - timedelta(minutes=10),
        )
        router = _router_with("openai", stats)
        assert "openai" in router._get_available_providers("gpt-4")

    def test_half_open_admits_only_one_probe(self):
        """The second caller during half-open must not also be admitted."""
        stats = ProviderStats(
            consecutive_failures=5,
            last_failure=datetime.utcnow() - timedelta(minutes=10),
        )
        router = _router_with("openai", stats)

        first = router._get_available_providers("gpt-4")
        second = router._get_available_providers("gpt-4")

        assert "openai" in first, "first caller should get the probe"
        assert second == [], "probe already in flight; second caller must be refused"

    def test_success_closes_breaker_and_clears_probe(self):
        """A successful half-open probe resets the failure count and re-admits the provider."""
        stats = ProviderStats(
            consecutive_failures=5,
            last_failure=datetime.utcnow() - timedelta(minutes=10),
        )
        router = _router_with("openai", stats)
        router._get_available_providers("gpt-4")  # reserve the probe

        router._update_provider_stats("openai", success=True, latency=10.0)

        assert stats.consecutive_failures == 0
        assert stats.half_open_probe_in_flight is False
        assert "openai" in router._get_available_providers("gpt-4")

    def test_failed_probe_reopens_breaker(self):
        """A failed half-open probe re-trips the breaker and restarts the cooldown."""
        stats = ProviderStats(
            consecutive_failures=5,
            last_failure=datetime.utcnow() - timedelta(minutes=10),
        )
        router = _router_with("openai", stats)
        router._get_available_providers("gpt-4")  # reserve the probe

        router._update_provider_stats("openai", success=False, latency=0.0)

        # Probe released, cooldown restarted from the new failure -> ejected again.
        assert stats.half_open_probe_in_flight is False
        assert router._get_available_providers("gpt-4") == []


@pytest.mark.parametrize("failures", [0, 1, 2])
def test_below_threshold_stays_available(failures):
    """Any failure count under the threshold (0, 1, 2) leaves the provider available."""
    stats = ProviderStats(consecutive_failures=failures, last_success=datetime.utcnow())
    router = _router_with("openai", stats)
    assert "openai" in router._get_available_providers("gpt-4")
