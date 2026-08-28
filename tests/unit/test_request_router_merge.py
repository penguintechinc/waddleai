"""Tests for merged AILB router integration into request_router.py.

Verifies all 6 routing strategies, circuit breaker parity, and cost optimization.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.utils.request_router import (
    LLMRequestRouter,
    ModelConfig,
    ProviderStats,
    RoutingStrategy,
)


class MockConnector:
    """Mock LLM connector for testing (not slotted to allow method override in tests)."""

    def __init__(self, model_list, name):
        """Record the models this mock connector claims to serve and its provider name."""
        self.model_list = model_list
        self.name = name

    async def chat_completion(self, messages, model, **kwargs):
        """Mock chat completion."""
        return f"Response from {self.name}", {"tokens": 10}


@pytest.fixture
def mock_llm_manager():
    """Create mock LLM manager with connectors."""
    manager = MagicMock()

    connectors = {
        "openai": MockConnector(["gpt-4", "gpt-3.5"], "openai"),
        "anthropic": MockConnector(["claude-3"], "anthropic"),
        "ollama": MockConnector(["llama3"], "ollama"),
    }

    manager.connectors = connectors

    def get_connector(name):
        return connectors.get(name)

    manager.get_connector = get_connector
    manager.health_check_all = AsyncMock(
        return_value={
            "openai": {"status": "healthy"},
            "anthropic": {"status": "healthy"},
            "ollama": {"status": "healthy"},
        }
    )

    return manager


@pytest.fixture
def router_with_costs(mock_llm_manager):
    """Create router with cost-based model configurations."""
    router = LLMRequestRouter(mock_llm_manager, None)

    # Wrap connectors to allow mocking
    for name in list(router.llm_manager.connectors.keys()):
        original = router.llm_manager.connectors[name]
        # Create a wrapper that can be mocked
        wrapped = MagicMock(wraps=original)
        wrapped.model_list = original.model_list
        wrapped.name = original.name
        router.llm_manager.connectors[name] = wrapped
        router.llm_manager.get_connector = lambda n: router.llm_manager.connectors.get(n)

    # Add model configs with costs for COST_OPTIMIZED tests
    router.model_configs["gpt-4"] = ModelConfig(
        model_name="gpt-4",
        preferred_providers=["openai"],
        cost_per_token={"openai": 0.03, "anthropic": float("inf"), "ollama": 0.0},
        max_tokens=8192,
        context_length=8192,
        capabilities=["chat"],
    )

    router.model_configs["claude-3"] = ModelConfig(
        model_name="claude-3",
        preferred_providers=["anthropic"],
        cost_per_token={"openai": float("inf"), "anthropic": 0.015, "ollama": 0.0},
        max_tokens=200000,
        context_length=200000,
        capabilities=["chat"],
    )

    router.model_configs["llama3"] = ModelConfig(
        model_name="llama3",
        preferred_providers=["ollama"],
        cost_per_token={"openai": float("inf"), "anthropic": float("inf"), "ollama": 0.0},
        max_tokens=4096,
        context_length=4096,
        capabilities=["chat"],
    )

    return router


# ============================================================================
# STRATEGY TESTS - All 6 strategies select deterministically
# ============================================================================


class TestAllStrategiesDeterministic:
    """Test that each strategy selects deterministically on a stubbed connector set."""

    def test_round_robin_selects_deterministically(self, router_with_costs):
        """ROUND_ROBIN cycles through providers in order."""
        providers = ["openai", "anthropic", "ollama"]

        # First call should return first provider
        selected = router_with_costs._select_provider(
            "gpt-4", providers, RoutingStrategy.ROUND_ROBIN
        )
        assert selected == "openai"

        # Second call should return second provider
        selected = router_with_costs._select_provider(
            "gpt-4", providers, RoutingStrategy.ROUND_ROBIN
        )
        assert selected == "anthropic"

        # Third call should return third provider
        selected = router_with_costs._select_provider(
            "gpt-4", providers, RoutingStrategy.ROUND_ROBIN
        )
        assert selected == "ollama"

        # Fourth call should wrap around
        selected = router_with_costs._select_provider(
            "gpt-4", providers, RoutingStrategy.ROUND_ROBIN
        )
        assert selected == "openai"

    def test_cost_optimized_selects_deterministically(self, router_with_costs):
        """COST_OPTIMIZED picks lowest cost_per_token provider."""
        providers = ["openai", "anthropic", "ollama"]

        # For gpt-4: openai=0.03, anthropic=inf, ollama=0.0 -> should pick ollama
        selected = router_with_costs._select_provider(
            "gpt-4", providers, RoutingStrategy.COST_OPTIMIZED
        )
        assert selected == "ollama"

        # For claude-3: openai=inf, anthropic=0.015, ollama=0.0 -> should pick ollama
        selected = router_with_costs._select_provider(
            "claude-3", providers, RoutingStrategy.COST_OPTIMIZED
        )
        assert selected == "ollama"

        # Different cost scenario: modify costs so anthropic is cheapest
        router_with_costs.model_configs["gpt-4"].cost_per_token = {
            "openai": 0.03,
            "anthropic": 0.005,
            "ollama": 0.02,
        }

        selected = router_with_costs._select_provider(
            "gpt-4", providers, RoutingStrategy.COST_OPTIMIZED
        )
        assert selected == "anthropic"

    def test_latency_optimized_selects_deterministically(self, router_with_costs):
        """LATENCY_OPTIMIZED picks provider with lowest avg_latency_ms."""
        providers = ["openai", "anthropic", "ollama"]

        # Set different latencies
        router_with_costs.provider_stats["openai"] = ProviderStats(
            avg_latency_ms=100.0, successful_requests=1
        )
        router_with_costs.provider_stats["anthropic"] = ProviderStats(
            avg_latency_ms=50.0, successful_requests=1
        )
        router_with_costs.provider_stats["ollama"] = ProviderStats(
            avg_latency_ms=200.0, successful_requests=1
        )

        # Should pick anthropic (lowest latency: 50ms)
        selected = router_with_costs._select_provider(
            "gpt-4", providers, RoutingStrategy.LATENCY_OPTIMIZED
        )
        assert selected == "anthropic"

    def test_load_balanced_selects_deterministically(self, router_with_costs):
        """LOAD_BALANCED picks provider with lowest load score."""
        providers = ["openai", "anthropic", "ollama"]

        # Set different load scenarios
        router_with_costs.provider_stats["openai"] = ProviderStats(
            total_requests=100, successful_requests=50, consecutive_failures=0
        )
        router_with_costs.provider_stats["anthropic"] = ProviderStats(
            total_requests=50, successful_requests=45, consecutive_failures=0
        )
        router_with_costs.provider_stats["ollama"] = ProviderStats(
            total_requests=200, successful_requests=50, consecutive_failures=5
        )

        # Load score = total - successful + (consecutive_failures * 10)
        # openai: 100 - 50 + 0 = 50
        # anthropic: 50 - 45 + 0 = 5
        # ollama: 200 - 50 + 50 = 200
        # Should pick anthropic (lowest: 5)

        selected = router_with_costs._select_provider(
            "gpt-4", providers, RoutingStrategy.LOAD_BALANCED
        )
        assert selected == "anthropic"

    def test_failover_selects_deterministically(self, router_with_costs):
        """FAILOVER picks according to preferred_providers priority."""
        providers = ["openai", "anthropic", "ollama"]

        # For gpt-4, preferred is ["openai"]
        selected = router_with_costs._select_provider("gpt-4", providers, RoutingStrategy.FAILOVER)
        assert selected == "openai"

        # For claude-3, preferred is ["anthropic"]
        selected = router_with_costs._select_provider(
            "claude-3", providers, RoutingStrategy.FAILOVER
        )
        assert selected == "anthropic"

    def test_random_selects_from_available(self, router_with_costs):
        """RANDOM picks from available providers (deterministic set membership)."""
        providers = ["openai", "anthropic", "ollama"]

        # Run multiple times to ensure it picks from available set
        for _ in range(10):
            selected = router_with_costs._select_provider(
                "gpt-4", providers, RoutingStrategy.RANDOM
            )
            assert selected in providers


# ============================================================================
# CIRCUIT BREAKER TESTS
# ============================================================================


class TestCircuitBreakerParity:
    """Test circuit breaker behavior: skip after 3 failures, re-admit on success."""

    def test_breaker_skips_provider_after_3_consecutive_failures(self, router_with_costs):
        """Provider is excluded from available list after 3 consecutive failures."""
        # Ensure all providers support the model
        router_with_costs.llm_manager.connectors["openai"].model_list = ["gpt-4"]
        router_with_costs.llm_manager.connectors["anthropic"].model_list = ["gpt-4"]
        router_with_costs.llm_manager.connectors["ollama"].model_list = ["gpt-4"]

        # Set up a provider with 3 consecutive failures
        router_with_costs.provider_stats["openai"] = ProviderStats(
            consecutive_failures=3, last_failure=datetime.utcnow()
        )

        available = router_with_costs._get_available_providers("gpt-4")

        # openai should not be in available list
        assert "openai" not in available
        assert "anthropic" in available
        assert "ollama" in available

    def test_breaker_resets_on_success(self, router_with_costs):
        """consecutive_failures resets to 0 on success."""
        router_with_costs.provider_stats["openai"] = ProviderStats(
            consecutive_failures=2, last_failure=datetime.utcnow(), avg_latency_ms=0
        )

        # Simulate a success
        router_with_costs._update_provider_stats("openai", success=True, latency=50.0)

        # consecutive_failures should reset to 0
        assert router_with_costs.provider_stats["openai"].consecutive_failures == 0
        assert router_with_costs.provider_stats["openai"].successful_requests == 1

    def test_breaker_skips_recent_failure_within_5min(self, router_with_costs):
        """Provider is excluded if it failed within last 5 minutes (without recent success)."""
        now = datetime.utcnow()

        router_with_costs.provider_stats["openai"] = ProviderStats(
            consecutive_failures=1,
            last_failure=now - timedelta(minutes=2),  # Failed 2 minutes ago
            last_success=now - timedelta(minutes=10),  # Last success was 10 min ago
        )

        available = router_with_costs._get_available_providers("gpt-4")

        # openai should be excluded (recent failure, no recent success)
        assert "openai" not in available

    def test_breaker_allows_provider_after_5min_cooldown(self, router_with_costs):
        """Provider is allowed again after 5 minutes pass since last failure."""
        now = datetime.utcnow()

        router_with_costs.provider_stats["openai"] = ProviderStats(
            consecutive_failures=1,
            last_failure=now - timedelta(minutes=6),  # Failed 6 minutes ago (past 5-min window)
            last_success=now - timedelta(minutes=10),
        )

        available = router_with_costs._get_available_providers("gpt-4")

        # openai should be included (past cooldown)
        assert "openai" in available

    def test_breaker_allows_provider_with_recent_success(self, router_with_costs):
        """Provider is allowed if it had a recent success, even with a failure."""
        now = datetime.utcnow()

        router_with_costs.provider_stats["openai"] = ProviderStats(
            consecutive_failures=1,
            last_failure=now - timedelta(minutes=2),
            last_success=now - timedelta(seconds=30),  # Success 30 seconds ago
        )

        available = router_with_costs._get_available_providers("gpt-4")

        # openai should be included (recent success)
        assert "openai" in available


# ============================================================================
# EMA LATENCY TESTS
# ============================================================================


class TestEMALatency:
    """Test exponential moving average latency calculation."""

    def test_ema_calculation_0_9_0_1_weights(self, router_with_costs):
        """EMA uses 0.9 weight for old, 0.1 for new."""
        # Start with no latency
        router_with_costs._update_provider_stats("openai", success=True, latency=100.0)
        assert router_with_costs.provider_stats["openai"].avg_latency_ms == 100.0

        # Add a new latency measurement
        router_with_costs._update_provider_stats("openai", success=True, latency=200.0)

        # Expected: 100 * 0.9 + 200 * 0.1 = 90 + 20 = 110
        expected = 110.0
        assert router_with_costs.provider_stats["openai"].avg_latency_ms == expected

    def test_ema_converges_over_time(self, router_with_costs):
        """EMA gradually converges to new latency values."""
        router_with_costs._update_provider_stats("openai", success=True, latency=100.0)

        # Add multiple high latency measurements
        for _ in range(10):
            router_with_costs._update_provider_stats("openai", success=True, latency=200.0)

        # Should be closer to 200 but not quite there yet after 10 measurements
        # With EMA = old*0.9 + new*0.1, convergence is slow
        latency = router_with_costs.provider_stats["openai"].avg_latency_ms
        assert 150.0 < latency < 200.0


# ============================================================================
# FAILOVER BEHAVIOR TESTS
# ============================================================================


class TestFailoverBehavior:
    """Test failover fallback chain raises only when all providers fail."""

    @pytest.mark.asyncio
    async def test_failover_raises_when_all_providers_fail(self, router_with_costs):
        """Failover raises exception only when every provider fails."""
        # Make all providers fail by mocking chat_completion
        for provider_name in ["openai", "anthropic", "ollama"]:
            connector = router_with_costs.llm_manager.connectors[provider_name]
            connector.chat_completion = AsyncMock(side_effect=Exception(f"{provider_name} is down"))

        with pytest.raises(Exception) as exc_info:
            await router_with_costs._execute_with_fallback(
                "openai",
                ["openai", "anthropic", "ollama"],
                "gpt-4",
                [{"role": "user", "content": "test"}],
            )

        # Exception should mention all providers failed
        assert "All providers failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_failover_uses_fallback_chain(self, router_with_costs):
        """Failover tries fallback providers when primary fails."""
        # Make primary fail, but secondary succeed
        openai_connector = router_with_costs.llm_manager.connectors["openai"]
        openai_connector.chat_completion = AsyncMock(side_effect=Exception("openai is down"))

        anthropic_connector = router_with_costs.llm_manager.connectors["anthropic"]
        anthropic_connector.chat_completion = AsyncMock(
            return_value=("Response from anthropic", {"tokens": 10})
        )

        response, usage = await router_with_costs._execute_with_fallback(
            "openai",
            ["openai", "anthropic", "ollama"],
            "gpt-4",
            [{"role": "user", "content": "test"}],
        )

        # Should have succeeded with anthropic
        assert "anthropic" in response
        assert usage["provider"] == "anthropic"


# ============================================================================
# COST OPTIMIZATION SPECIFIC TESTS
# ============================================================================


class TestCostOptimization:
    """Test COST_OPTIMIZED strategy picks lowest cost_per_token."""

    def test_cost_optimized_picks_lowest_cost(self, router_with_costs):
        """COST_OPTIMIZED selects provider with minimum cost_per_token."""
        # Set up model with different costs per provider
        router_with_costs.model_configs["test-model"] = ModelConfig(
            model_name="test-model",
            preferred_providers=["openai"],
            cost_per_token={"openai": 0.05, "anthropic": 0.02, "ollama": 0.0},
            max_tokens=4096,
            context_length=4096,
            capabilities=["chat"],
        )

        providers = ["openai", "anthropic", "ollama"]
        selected = router_with_costs._select_provider(
            "test-model", providers, RoutingStrategy.COST_OPTIMIZED
        )

        # Should pick ollama (free)
        assert selected == "ollama"

    def test_cost_optimized_fallback_when_no_config(self, router_with_costs):
        """COST_OPTIMIZED falls back to first provider if model config missing."""
        providers = ["anthropic", "openai", "ollama"]
        selected = router_with_costs._select_provider(
            "unknown-model", providers, RoutingStrategy.COST_OPTIMIZED
        )

        # Should pick first available
        assert selected == providers[0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
