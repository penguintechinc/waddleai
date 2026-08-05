"""
Intelligent request routing system for WaddleAI
Routes requests to optimal LLM providers based on model, cost, availability, and load
Supports Redis-based natural language routing instructions and routing LLM integration
"""

import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


class RoutingStrategy(Enum):
    """Routing strategies for LLM requests"""

    ROUND_ROBIN = "round_robin"
    COST_OPTIMIZED = "cost_optimized"
    LATENCY_OPTIMIZED = "latency_optimized"
    LOAD_BALANCED = "load_balanced"
    FAILOVER = "failover"
    RANDOM = "random"


@dataclass
class ProviderStats:
    """Statistics for a provider"""

    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    avg_latency_ms: float = 0.0
    last_success: Optional[datetime] = None
    last_failure: Optional[datetime] = None
    consecutive_failures: int = 0
    tokens_processed: int = 0
    # Set while a single half-open probe is in flight, so a recovering provider
    # receives one trial request rather than the full concurrent load.
    half_open_probe_in_flight: bool = False


@dataclass
class ModelConfig:
    """Configuration for model routing"""

    model_name: str
    preferred_providers: List[str]
    cost_per_token: Dict[str, float]  # Provider -> cost per token
    max_tokens: int
    context_length: int
    capabilities: List[str]


class LLMRequestRouter:
    """Intelligent request router for LLM providers with Redis-based intelligent routing and RAG enrichment"""

    def __init__(self, llm_manager, db, redis_client: Optional[aioredis.Redis] = None, rag_manager=None):
        self.llm_manager = llm_manager
        self.db = db
        self.redis_client = redis_client
        self.rag_manager = rag_manager  # Optional RAG manager for context enrichment
        self.provider_stats: Dict[str, ProviderStats] = {}
        self.round_robin_counters: Dict[str, int] = {}
        self.model_configs: Dict[str, ModelConfig] = {}
        self.default_strategy = RoutingStrategy.LOAD_BALANCED
        self.health_check_interval = 300  # 5 minutes

        # Circuit breaker: trip after N consecutive retryable failures, then hold
        # the provider out for the cooldown before allowing one half-open probe.
        self.breaker_failure_threshold = 3
        self.breaker_cooldown = timedelta(minutes=5)

        # Routing LLM configuration
        self.routing_llm_model = os.getenv("ROUTING_LLM", "llama3.2:1b")
        self.use_intelligent_routing = os.getenv("USE_INTELLIGENT_ROUTING", "true").lower() == "true"

        # RAG configuration
        self.enable_rag = os.getenv("ENABLE_RAG", "false").lower() == "true" and rag_manager is not None
        self.rag_collection = os.getenv("RAG_COLLECTION", "default")
        self.rag_top_k = int(os.getenv("RAG_TOP_K", "3"))

        # Load model configurations
        self._load_model_configs()

        # Initialize provider stats
        self._initialize_provider_stats()

        logger.info(
            f"Initialized LLMRequestRouter with routing_llm={self.routing_llm_model}, "
            f"intelligent_routing={self.use_intelligent_routing}, rag_enabled={self.enable_rag}"
        )

    def _load_model_configs(self):
        """Load model configurations from database"""
        try:
            # This would be loaded from a model_configs table in a real implementation
            self.model_configs = {
                "gpt-4": ModelConfig(
                    model_name="gpt-4",
                    preferred_providers=["openai"],
                    cost_per_token={"openai": 0.00003},
                    max_tokens=8192,
                    context_length=8192,
                    capabilities=["chat", "completion", "reasoning"],
                ),
                "gpt-3.5-turbo": ModelConfig(
                    model_name="gpt-3.5-turbo",
                    preferred_providers=["openai"],
                    cost_per_token={"openai": 0.0000015},
                    max_tokens=4096,
                    context_length=4096,
                    capabilities=["chat", "completion"],
                ),
                "claude-3-opus-20240229": ModelConfig(
                    model_name="claude-3-opus-20240229",
                    preferred_providers=["anthropic"],
                    cost_per_token={"anthropic": 0.000015},
                    max_tokens=200000,
                    context_length=200000,
                    capabilities=["chat", "reasoning", "analysis"],
                ),
                "claude-3-sonnet-20240229": ModelConfig(
                    model_name="claude-3-sonnet-20240229",
                    preferred_providers=["anthropic"],
                    cost_per_token={"anthropic": 0.000003},
                    max_tokens=200000,
                    context_length=200000,
                    capabilities=["chat", "reasoning"],
                ),
                "llama3": ModelConfig(
                    model_name="llama3",
                    preferred_providers=["ollama"],
                    cost_per_token={"ollama": 0.0},  # Local is free
                    max_tokens=4096,
                    context_length=4096,
                    capabilities=["chat", "completion"],
                ),
            }
        except Exception as e:
            logger.error(f"Failed to load model configs: {e}")

    def _initialize_provider_stats(self):
        """Initialize statistics for all providers"""
        for provider_name in self.llm_manager.connectors:
            if provider_name not in self.provider_stats:
                self.provider_stats[provider_name] = ProviderStats()
            if provider_name not in self.round_robin_counters:
                self.round_robin_counters[provider_name] = 0

    async def enrich_request_with_rag_context(
        self, messages: List[Dict[str, str]], collection: Optional[str] = None, top_k: Optional[int] = None
    ) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
        """
        Enrich request messages with RAG context from knowledge base

        Args:
            messages: Original messages
            collection: RAG collection to search (defaults to self.rag_collection)
            top_k: Number of documents to retrieve (defaults to self.rag_top_k)

        Returns:
            Tuple of (enriched_messages, rag_metadata)
        """
        if not self.enable_rag or not self.rag_manager:
            return messages, {}

        try:
            # Extract query from user messages
            user_messages = [msg["content"] for msg in messages if msg.get("role") == "user"]
            if not user_messages:
                return messages, {}

            query = user_messages[-1]  # Use last user message as query
            collection = collection or self.rag_collection
            top_k = top_k or self.rag_top_k

            # Search knowledge base
            search_results = await self.rag_manager.search_knowledge_base(
                query=query, collection=collection, limit=top_k, min_score=0.7
            )

            if not search_results:
                logger.debug("No relevant RAG documents found")
                return messages, {"rag_enabled": True, "rag_documents_found": 0}

            # Build context from results
            context_parts = []
            for idx, result in enumerate(search_results):
                context_parts.append(f"[Document {idx+1}] (Relevance: {result.score:.2f})\n{result.document.content}")

            rag_context = "\n\n".join(context_parts)

            # Inject context into messages
            enriched_messages = []
            context_injected = False

            for msg in messages:
                if msg.get("role") == "system" and not context_injected:
                    # Add to existing system message
                    enriched_content = msg["content"] + f"\n\n## Relevant Knowledge Base Context:\n{rag_context}"
                    enriched_messages.append({"role": "system", "content": enriched_content})
                    context_injected = True
                else:
                    enriched_messages.append(msg)

            # If no system message, inject before first user message
            if not context_injected:
                enriched_messages.insert(
                    0,
                    {
                        "role": "system",
                        "content": (
                            f"## Relevant Knowledge Base Context:\n{rag_context}\n\n"
                            "Use the above context to help answer the user's question."
                        ),
                    },
                )

            rag_metadata = {
                "rag_enabled": True,
                "rag_documents_found": len(search_results),
                "rag_collection": collection,
                "rag_query": query,
                "rag_scores": [r.score for r in search_results],
            }

            logger.info(f"Enriched request with {len(search_results)} RAG documents from collection '{collection}'")
            return enriched_messages, rag_metadata

        except Exception as e:
            logger.error(f"Failed to enrich request with RAG context: {e}")
            return messages, {"rag_enabled": True, "rag_error": str(e)}

    async def route_request(
        self,
        model: str,
        messages: List[Dict[str, str]],
        strategy: Optional[RoutingStrategy] = None,
        user_preferences: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Tuple[str, Any]:
        """
        Route a request to the best available provider

        Returns:
            Tuple of (response_content, usage_info)
        """
        routing_strategy = strategy or self.default_strategy

        # Enrich with RAG context if enabled
        enriched_messages, rag_metadata = await self.enrich_request_with_rag_context(messages)

        # Get available providers for the model
        available_providers = self._get_available_providers(model)

        if not available_providers:
            raise ValueError(f"No available providers for model {model}")

        # Select provider based on strategy
        selected_provider = self._select_provider(model, available_providers, routing_strategy, user_preferences)

        # Execute request with fallback (using enriched messages)
        response, usage_info = await self._execute_with_fallback(
            selected_provider, available_providers, model, enriched_messages, **kwargs
        )

        # Add RAG metadata to usage info
        if rag_metadata:
            usage_info["rag_metadata"] = rag_metadata

        return response, usage_info

    def _get_available_providers(self, model: str) -> List[str]:
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
        available_providers: List[str],
        strategy: RoutingStrategy,
        user_preferences: Optional[Dict[str, Any]] = None,
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
            return random.choice(available_providers)

        else:
            # Default to first available
            return available_providers[0]

    def _round_robin_selection(self, model: str, providers: List[str]) -> str:
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

    def _cost_optimized_selection(self, model: str, providers: List[str]) -> str:
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

    def _latency_optimized_selection(self, providers: List[str]) -> str:
        """Select provider with lowest average latency"""
        min_latency = float("inf")
        best_provider = providers[0]

        for provider in providers:
            stats = self.provider_stats.get(provider, ProviderStats())
            if stats.avg_latency_ms < min_latency:
                min_latency = stats.avg_latency_ms
                best_provider = provider

        return best_provider

    def _load_balanced_selection(self, providers: List[str]) -> str:
        """Select provider with least load"""
        min_load = float("inf")
        best_provider = providers[0]

        for provider in providers:
            stats = self.provider_stats.get(provider, ProviderStats())
            # Use recent requests as load metric
            load_score = stats.total_requests - stats.successful_requests + (stats.consecutive_failures * 10)

            if load_score < min_load:
                min_load = load_score
                best_provider = provider

        return best_provider

    def _failover_selection(self, model: str, providers: List[str]) -> str:
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
        available_providers: List[str],
        model: str,
        messages: List[Dict[str, str]],
        **kwargs,
    ) -> Tuple[str, Any]:
        """Execute request with automatic fallback to other providers"""

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
                response, usage_info = await connector.chat_completion(messages=messages, model=model, **kwargs)
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
                # Task 8 resolution: distinguish retryable vs non-retryable errors.
                # Client errors (4xx, auth, schema) surface immediately and do NOT
                # increment consecutive_failures (they should not eject a healthy provider).
                # Only retryable errors (timeout, 429, 5xx) count toward the breaker.
                # ProviderClientError is never retried and never breaker-counted.
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
        strategy: Optional[RoutingStrategy] = None,
    ) -> Optional[Tuple[str, str]]:
        """Pick a healthy provider for a model.

        Public seam over availability filtering (which applies the circuit
        breaker, including the half-open probe) plus strategy selection.
        Callers outside this class must use this rather than reaching into
        the private helpers, so breaker semantics can never be bypassed.

        Returns:
            (provider_name, model) or None when no provider can serve the model.
        """
        available = self._get_available_providers(model)
        if not available:
            return None

        provider = self._select_provider(model, available, strategy or self.default_strategy)
        if not provider:
            return None
        return provider, model

    def get_provider_stats(self) -> Dict[str, Dict[str, Any]]:
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

    async def get_routing_instructions(self) -> str:
        """Get routing instructions from Redis"""
        try:
            if self.redis_client:
                instructions = await self.redis_client.get("routing:instructions")
                if instructions:
                    return instructions.decode("utf-8") if isinstance(instructions, bytes) else instructions

            # Default fallback instructions
            return (
                "Route to fastest available LLM. Use codellama or Claude for programming tasks. "
                "Use GPT-4 for complex reasoning. Use local Ollama for simple queries."
            )

        except Exception as e:
            logger.error(f"Failed to get routing instructions from Redis: {e}")
            return "Route to fastest available LLM"

    async def set_routing_instructions(self, instructions: str) -> bool:
        """Set routing instructions in Redis"""
        try:
            if self.redis_client:
                await self.redis_client.set("routing:instructions", instructions)
                logger.info(f"Updated routing instructions: {instructions[:100]}...")
                return True
            else:
                logger.warning("Redis client not available, cannot set routing instructions")
                return False
        except Exception as e:
            logger.error(f"Failed to set routing instructions: {e}")
            return False

    def _classify_request_type(self, messages: List[Dict[str, str]]) -> str:
        """Classify request type for better routing"""
        if not messages:
            return "general_chat"

        last_message = messages[-1].get("content", "").lower()

        # Programming keywords
        if any(
            kw in last_message
            for kw in ["code", "function", "debug", "programming", "python", "javascript", "java", "c++", "rust", "go"]
        ):
            return "programming"

        # Data analysis keywords
        elif any(kw in last_message for kw in ["analyze", "data", "statistics", "chart", "graph", "dataset"]):
            return "analysis"

        # Explanation keywords
        elif any(kw in last_message for kw in ["explain", "what is", "how does", "why", "define"]):
            return "explanation"

        # Complex reasoning
        elif any(kw in last_message for kw in ["complex", "advanced", "detailed", "comprehensive", "thorough"]):
            return "complex_reasoning"

        # Simple query
        elif len(last_message) < 50:
            return "simple_query"

        else:
            return "general_chat"

    def _summarize_request(self, messages: List[Dict[str, str]]) -> str:
        """Summarize request for routing decision"""
        if not messages:
            return "Empty request"

        # Extract user messages
        user_messages = [msg.get("content", "") for msg in messages if msg.get("role") == "user"]

        if not user_messages:
            return "No user messages"

        # Use last user message or combine last 2
        if len(user_messages) == 1:
            summary = user_messages[-1]
        else:
            summary = f"{user_messages[-2][:100]}... {user_messages[-1][:100]}"

        # Truncate if too long
        if len(summary) > 300:
            summary = summary[:300] + "..."

        return summary

    def _format_available_llms(self) -> str:
        """Format available LLMs for routing prompt"""
        llm_list = []
        for provider_name, connector in self.llm_manager.connectors.items():
            stats = self.provider_stats.get(provider_name, ProviderStats())
            status = "healthy" if stats.consecutive_failures < 3 else "unhealthy"
            models = ", ".join(connector.model_list[:3]) if connector.model_list else "all models"
            llm_list.append(f"{provider_name} ({status}): {models}")

        return "\n".join(llm_list) if llm_list else "No LLMs available"

    async def _call_routing_llm(self, prompt: str) -> str:
        """Call routing LLM for intelligent decision"""
        try:
            # Get routing LLM connector
            connector = None
            for provider_name, conn in self.llm_manager.connectors.items():
                if self.routing_llm_model in conn.model_list or provider_name == "ollama":
                    connector = conn
                    break

            if not connector:
                logger.warning(f"Routing LLM {self.routing_llm_model} not available, using fallback")
                return "default"

            # Call routing LLM with low temperature for consistency
            response, _ = await connector.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=self.routing_llm_model,
                max_tokens=50,
                temperature=0.1,
            )

            # Extract LLM name from response
            llm_name = response.strip().lower()

            # Map common variations to actual provider names
            if "claude" in llm_name:
                return "anthropic"
            elif "gpt" in llm_name or "openai" in llm_name:
                return "openai"
            elif "ollama" in llm_name or "llama" in llm_name or "mistral" in llm_name:
                return "ollama"
            elif "codellama" in llm_name:
                return "ollama"  # codellama typically runs on Ollama
            else:
                return llm_name

        except Exception as e:
            logger.error(f"Routing LLM call failed: {e}")
            return "default"

    async def _intelligent_routing_decision(
        self, messages: List[Dict[str, str]], user_preferences: Optional[Dict[str, Any]] = None
    ) -> str:
        """Make intelligent routing decision using routing LLM"""
        try:
            # Get routing instructions from Redis
            instructions = await self.get_routing_instructions()

            # Classify request type
            request_type = self._classify_request_type(messages)

            # Summarize request
            request_summary = self._summarize_request(messages)

            # Format available LLMs
            available_llms = self._format_available_llms()

            # Build routing prompt
            routing_prompt = f"""Routing Rules: {instructions}

Request Type: {request_type}
Request Summary: {request_summary}

Available LLMs:
{available_llms}

Based on the routing rules, which LLM should handle this request?
Reply with ONLY the LLM provider name (anthropic, openai, ollama, etc.), nothing else."""

            # Call routing LLM
            target_llm = await self._call_routing_llm(routing_prompt)

            logger.info(f"Intelligent routing decision: {target_llm} for request_type={request_type}")

            return target_llm

        except Exception as e:
            logger.error(f"Intelligent routing failed: {e}")
            return "default"

    async def route_request_with_intelligence(
        self,
        model: str,
        messages: List[Dict[str, str]],
        strategy: Optional[RoutingStrategy] = None,
        user_preferences: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Tuple[str, Any]:
        """
        Route request using intelligent routing with LLM decision

        Returns:
            Tuple of (response_content, usage_info with routing_decision and routing_reasoning)
        """
        routing_decision = None
        routing_reasoning = None

        if self.use_intelligent_routing and model == "auto":
            # Use intelligent routing
            routing_decision = await self._intelligent_routing_decision(messages, user_preferences)
            routing_reasoning = "Intelligent routing based on request type and Redis instructions"

            # If routing LLM returned a valid provider, use it
            if routing_decision and routing_decision != "default":
                # Check if provider is available
                available_providers = self._get_available_providers(model)

                if routing_decision in available_providers:
                    selected_provider = routing_decision
                else:
                    # Fallback to strategy-based selection
                    logger.warning(f"Routing LLM suggested {routing_decision}, but it's not available. Using fallback.")
                    selected_provider = self._select_provider(
                        model, available_providers, strategy or self.default_strategy, user_preferences
                    )
                    routing_decision = selected_provider
                    routing_reasoning = f"Fallback to {selected_provider} (routing LLM suggestion unavailable)"
            else:
                # Fallback to strategy-based selection
                available_providers = self._get_available_providers(model)
                selected_provider = self._select_provider(
                    model, available_providers, strategy or self.default_strategy, user_preferences
                )
                routing_decision = selected_provider
                routing_reasoning = "Strategy-based routing (intelligent routing unavailable)"
        else:
            # Use traditional strategy-based routing
            available_providers = self._get_available_providers(model)
            selected_provider = self._select_provider(
                model, available_providers, strategy or self.default_strategy, user_preferences
            )
            routing_decision = selected_provider
            routing_reasoning = f"Strategy-based routing: {self.default_strategy.value}"

        # Execute request with fallback
        response, usage_info = await self._execute_with_fallback(
            selected_provider,
            available_providers if "available_providers" in locals() else self._get_available_providers(model),
            model,
            messages,
            **kwargs,
        )

        # Add routing information to usage_info
        usage_info["routing_decision"] = routing_decision
        usage_info["routing_reasoning"] = routing_reasoning
        usage_info["request_type"] = self._classify_request_type(messages)

        return response, usage_info


def create_request_router(
    llm_manager, db, redis_client: Optional[aioredis.Redis] = None, rag_manager=None
) -> LLMRequestRouter:
    """Factory function to create request router with optional RAG manager"""
    return LLMRequestRouter(llm_manager, db, redis_client, rag_manager)
