"""Provider-tier request routing for WaddleAI (spec §7.5).

This module used to also own model *selection* (a hardcoded model_configs
dict, Redis-backed natural-language routing instructions, and an
LLM-driven "intelligent routing" decision). All three were retired in
favor of ``shared.routing.RoutingEngine`` (spec §7.6) -- see
``shared/routing/__init__.py`` for the full replacement mapping.

What remains here is the provider-tier concern RoutingEngine does not
own: given an already-chosen model, pick a healthy *provider* to serve
it (six strategies), track per-provider circuit-breaker state (closed ->
open -> half-open -> closed), and execute a request with automatic
provider-level fallback. ``select_provider()`` is the public seam
(DispatchStage's dependency, spec §7.5) -- callers outside this class
should use it rather than the private helpers, so breaker semantics can
never be bypassed.
"""

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class RoutingStrategy(Enum):
    """Provider-selection strategies for a single already-chosen model."""

    ROUND_ROBIN = "round_robin"
    COST_OPTIMIZED = "cost_optimized"
    LATENCY_OPTIMIZED = "latency_optimized"
    LOAD_BALANCED = "load_balanced"
    FAILOVER = "failover"
    RANDOM = "random"


@dataclass(slots=True)
class ProviderStats:
    """Statistics for a provider"""

    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    avg_latency_ms: float = 0.0
    last_success: datetime | None = None
    last_failure: datetime | None = None
    consecutive_failures: int = 0
    tokens_processed: int = 0
    # Set while a single half-open probe is in flight, so a recovering provider
    # receives one trial request rather than the full concurrent load.
    half_open_probe_in_flight: bool = False


@dataclass(slots=True)
class ModelConfig:
    """Per-model provider-selection metadata (cost/capacity), optional.

    Empty by default -- no hardcoded seed data (retired per §7.6; model
    metadata now lives in the ``model_configs`` DB table, read by
    ``shared.routing`` for model-selection). Callers may still populate
    ``LLMRequestRouter.model_configs`` directly (e.g. from that same table)
    to enable the COST_OPTIMIZED/FAILOVER provider-selection strategies;
    without it those strategies degrade to "first available provider".
    """

    model_name: str
    preferred_providers: list[str]
    cost_per_token: dict[str, float]  # Provider -> cost per token
    max_tokens: int
    context_length: int
    capabilities: list[str]


class LLMRequestRouter:
    """Provider-tier router: strategy-based selection + circuit breaker (§7.5).

    Model selection itself is owned by ``shared.routing.RoutingEngine``
    (spec §7.6); this class only decides which *provider* serves an
    already-chosen model.
    """

    def __init__(self, llm_manager, db) -> None:
        """Initialize the provider-tier router.

        Args:
            llm_manager: LLMConnectionManager exposing ``.connectors``.
            db: penguin-dal DB instance (reserved; not currently queried --
                model-selection metadata is now read by shared.routing, not
                here).
        """
        self.llm_manager = llm_manager
        self.db = db
        self.provider_stats: dict[str, ProviderStats] = {}
        self.round_robin_counters: dict[str, int] = {}
        # No hardcoded seed (retired per §7.6) -- empty until a caller
        # populates it from the model_configs DB table.
        self.model_configs: dict[str, ModelConfig] = {}
        self.default_strategy = RoutingStrategy.LOAD_BALANCED
        self.health_check_interval = 300  # 5 minutes

        # Circuit breaker: trip after N consecutive retryable failures, then hold
        # the provider out for the cooldown before allowing one half-open probe.
        self.breaker_failure_threshold = 3
        self.breaker_cooldown = timedelta(minutes=5)

        self._initialize_provider_stats()

        logger.info("Initialized LLMRequestRouter (provider-tier, §7.5)")

    def _initialize_provider_stats(self):
        """Initialize statistics for all providers"""
        for provider_name in self.llm_manager.connectors:
            if provider_name not in self.provider_stats:
                self.provider_stats[provider_name] = ProviderStats()
            if provider_name not in self.round_robin_counters:
                self.round_robin_counters[provider_name] = 0

    def _get_available_providers(self, model: str) -> list[str]:
        """Get list of available providers for a model"""
        available = []

        for provider_name, connector in self.llm_manager.connectors.items():
            # Check if provider supports the model
            if model in connector.model_list or not connector.model_list:
                # Check if provider is healthy
                stats = self.provider_stats.get(provider_name, ProviderStats())

                # Breaker: closed -> open -> half-open -> closed.
                # A provider over the failure threshold is "open" until its
                # cooldown elapses. It is then offered as a SINGLE half-open
                # probe; concurrent callers are refused until that probe is
                # resolved by _update_provider_stats. Without this a tripped
                # provider could never recover, because the failure counter
                # only clears on a success it would never be selected for.
                in_cooldown = (
                    stats.last_failure is not None
                    and (not stats.last_success or stats.last_failure > stats.last_success)
                    and (datetime.utcnow() - stats.last_failure) < self.breaker_cooldown
                )

                if stats.consecutive_failures >= self.breaker_failure_threshold:
                    if in_cooldown:
                        continue  # open
                    if stats.half_open_probe_in_flight:
                        continue  # half-open, probe already reserved
                    stats.half_open_probe_in_flight = True
                    available.append(provider_name)
                    continue

                # Below threshold: still skip while a recent failure is cooling off.
                if in_cooldown:
                    continue

                available.append(provider_name)

        return available

    def _select_provider(
        self,
        model: str,
        available_providers: list[str],
        strategy: RoutingStrategy,
        user_preferences: dict[str, Any] | None = None,
    ) -> str:
        """Select provider based on routing strategy"""
        if strategy == RoutingStrategy.ROUND_ROBIN:
            return self._round_robin_selection(model, available_providers)

        elif strategy == RoutingStrategy.COST_OPTIMIZED:
            return self._cost_optimized_selection(model, available_providers)

        elif strategy == RoutingStrategy.LATENCY_OPTIMIZED:
            return self._latency_optimized_selection(available_providers)

        elif strategy == RoutingStrategy.LOAD_BALANCED:
            return self._load_balanced_selection(available_providers)

        elif strategy == RoutingStrategy.FAILOVER:
            return self._failover_selection(model, available_providers)

        elif strategy == RoutingStrategy.RANDOM:
            return random.choice(available_providers)  # nosec B311 -- upstream provider selection for load distribution, not security-sensitive

        else:
            # Default to first available
            return available_providers[0]

    def _round_robin_selection(self, model: str, providers: list[str]) -> str:
        """Round robin provider selection"""
        if not providers:
            raise ValueError("No providers available")

        # Use model-specific counter
        counter_key = f"{model}_rr"
        if counter_key not in self.round_robin_counters:
            self.round_robin_counters[counter_key] = 0

        selected_index = self.round_robin_counters[counter_key] % len(providers)
        self.round_robin_counters[counter_key] += 1

        return providers[selected_index]

    def _cost_optimized_selection(self, model: str, providers: list[str]) -> str:
        """Select provider with lowest cost"""
        model_config = self.model_configs.get(model)
        if not model_config:
            return providers[0]

        min_cost = float("inf")
        best_provider = providers[0]

        for provider in providers:
            cost = model_config.cost_per_token.get(provider, float("inf"))
            if cost < min_cost:
                min_cost = cost
                best_provider = provider

        return best_provider

    def _latency_optimized_selection(self, providers: list[str]) -> str:
        """Select provider with lowest average latency"""
        min_latency = float("inf")
        best_provider = providers[0]

        for provider in providers:
            stats = self.provider_stats.get(provider, ProviderStats())
            if stats.avg_latency_ms < min_latency:
                min_latency = stats.avg_latency_ms
                best_provider = provider

        return best_provider

    def _load_balanced_selection(self, providers: list[str]) -> str:
        """Select provider with least load"""
        min_load = float("inf")
        best_provider = providers[0]

        for provider in providers:
            stats = self.provider_stats.get(provider, ProviderStats())
            # Use recent requests as load metric
            load_score = (
                stats.total_requests - stats.successful_requests + (stats.consecutive_failures * 10)
            )

            if load_score < min_load:
                min_load = load_score
                best_provider = provider

        return best_provider

    def _failover_selection(self, model: str, providers: list[str]) -> str:
        """Select provider based on failover priority"""
        model_config = self.model_configs.get(model)
        if not model_config:
            return providers[0]

        # Use preferred providers first
        for preferred in model_config.preferred_providers:
            if preferred in providers:
                return preferred

        # Fall back to first available
        return providers[0]

    async def _execute_with_fallback(
        self,
        primary_provider: str,
        available_providers: list[str],
        model: str,
        messages: list[dict[str, str]],
        **kwargs,
    ) -> tuple[str, Any]:
        """Execute a request against primary_provider, falling back across
        available_providers on failure, updating breaker stats as it goes.

        A lower-level building block available to any caller needing
        automatic provider-level fallback; DispatchStage (spec §7.5) does
        not currently use it, calling connectors directly instead to support
        streaming.
        """
        # Try primary provider first
        providers_to_try = [primary_provider]

        # Add other providers for fallback (excluding primary)
        fallback_providers = [p for p in available_providers if p != primary_provider]
        providers_to_try.extend(fallback_providers)

        last_error = None

        for provider_name in providers_to_try:
            try:
                connector = self.llm_manager.get_connector(provider_name)
                if not connector:
                    continue

                # Execute request
                start_time = datetime.utcnow()
                response, usage_info = await connector.chat_completion(
                    messages=messages, model=model, **kwargs
                )
                end_time = datetime.utcnow()

                # Update statistics
                latency = (end_time - start_time).total_seconds() * 1000
                self._update_provider_stats(provider_name, success=True, latency=latency)

                # Add provider info to usage
                usage_info["provider"] = provider_name
                usage_info["routing_strategy"] = self.default_strategy.value

                logger.info(f"Successfully routed request to {provider_name} for model {model}")
                return response, usage_info

            except Exception as e:
                logger.warning(f"Provider {provider_name} failed for model {model}: {e}")
                # Distinguish retryable vs non-retryable errors. Client errors
                # (4xx, auth, schema) surface immediately and do NOT increment
                # consecutive_failures (they should not eject a healthy
                # provider). Only retryable errors (timeout, 429, 5xx) count
                # toward the breaker. ProviderClientError is never retried and
                # never breaker-counted.
                from shared.utils.llm_connectors import ProviderClientError

                if not isinstance(e, ProviderClientError):
                    # Retryable error — count toward breaker
                    self._update_provider_stats(provider_name, success=False)
                # Client error — do NOT count toward breaker, just skip this provider

                last_error = e
                continue

        # All providers failed
        logger.error(f"All providers failed for model {model}")
        raise Exception(f"All providers failed. Last error: {last_error}")

    def _update_provider_stats(self, provider_name: str, success: bool, latency: float = 0):
        """Update provider statistics"""
        if provider_name not in self.provider_stats:
            self.provider_stats[provider_name] = ProviderStats()

        stats = self.provider_stats[provider_name]
        stats.total_requests += 1

        # Resolve any half-open probe this call reserved, whichever way it went.
        stats.half_open_probe_in_flight = False

        if success:
            stats.successful_requests += 1
            stats.last_success = datetime.utcnow()
            stats.consecutive_failures = 0

            # Update average latency (exponential moving average)
            if stats.avg_latency_ms == 0:
                stats.avg_latency_ms = latency
            else:
                stats.avg_latency_ms = (stats.avg_latency_ms * 0.9) + (latency * 0.1)
        else:
            stats.failed_requests += 1
            stats.last_failure = datetime.utcnow()
            stats.consecutive_failures += 1

    def select_provider(
        self,
        model: str,
        strategy: RoutingStrategy | None = None,
        preferred_backend: str | None = None,
    ) -> tuple[str, str] | None:
        """Pick a healthy provider for a model.

        Public seam over availability filtering (which applies the circuit
        breaker, including the half-open probe) plus strategy selection.
        Callers outside this class must use this rather than reaching into
        the private helpers, so breaker semantics can never be bypassed.

        preferred_backend (spec §6.3, shared.cache.affinity session
        affinity) is a *hint*, never authoritative: it is honored only when
        (a) the named provider is in this call's own availability-filtered
        set (so a circuit-broken or otherwise unhealthy backend is silently
        ignored -- affinity must never pin a request to a dead pod), and
        (b) the provider's connector is Ollama/llama.cpp-family (the only
        backends session KV-cache affinity is meaningful for). Any other
        provider type falls through to normal strategy selection even if
        named as the hint.

        Returns:
            (provider_name, model) or None when no provider can serve the model.
        """
        available = self._get_available_providers(model)
        if not available:
            return None

        if (
            preferred_backend
            and preferred_backend in available
            and self._is_affinity_eligible(preferred_backend)
        ):
            return preferred_backend, model

        provider = self._select_provider(model, available, strategy or self.default_strategy)
        if not provider:
            return None
        return provider, model

    def _is_affinity_eligible(self, provider_name: str) -> bool:
        """True if `provider_name`'s connector is Ollama/llama.cpp-family."""
        from shared.utils.llm_connectors import LlamaCppConnector, OllamaConnector

        connector = self.llm_manager.connectors.get(provider_name)
        return isinstance(connector, (OllamaConnector, LlamaCppConnector))

    def get_provider_stats(self) -> dict[str, dict[str, Any]]:
        """Get current provider statistics"""
        stats_dict = {}
        for provider_name, stats in self.provider_stats.items():
            stats_dict[provider_name] = {
                "total_requests": stats.total_requests,
                "successful_requests": stats.successful_requests,
                "failed_requests": stats.failed_requests,
                "success_rate": stats.successful_requests / max(stats.total_requests, 1),
                "avg_latency_ms": stats.avg_latency_ms,
                "consecutive_failures": stats.consecutive_failures,
                "last_success": stats.last_success.isoformat() if stats.last_success else None,
                "last_failure": stats.last_failure.isoformat() if stats.last_failure else None,
            }
        return stats_dict

    async def health_check_providers(self):
        """Periodic health check of all providers"""
        logger.info("Running provider health checks")

        health_results = await self.llm_manager.health_check_all()

        for provider_name, result in health_results.items():
            is_healthy = result.get("status") == "healthy"

            if provider_name in self.provider_stats:
                if is_healthy:
                    # Reset consecutive failures on successful health check
                    self.provider_stats[provider_name].consecutive_failures = 0
                    self.provider_stats[provider_name].last_success = datetime.utcnow()
                else:
                    self.provider_stats[provider_name].consecutive_failures += 1
                    self.provider_stats[provider_name].last_failure = datetime.utcnow()

    def set_routing_strategy(self, strategy: RoutingStrategy):
        """Set the default routing strategy"""
        self.default_strategy = strategy
        logger.info(f"Routing strategy changed to: {strategy.value}")


def create_request_router(llm_manager, db) -> LLMRequestRouter:
    """Factory function to create the provider-tier request router."""
    return LLMRequestRouter(llm_manager, db)
