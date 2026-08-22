"""Unit tests for request routing system."""

from unittest.mock import AsyncMock, Mock

import pytest

try:
    from proxy.utils.request_router import (
        LoadBalancer,
        RequestRouter,
        RoutingStrategy,
        create_request_router,
    )
    from shared.utils.llm_connectors import ConnectionLink
except ImportError as e:
    pytest.skip(
        f"Skipping: proxy.utils.request_router not available ({e})", allow_module_level=True
    )


class TestRoutingStrategy:
    """Test RoutingStrategy enum."""

    def test_routing_strategy_values(self):
        """Test routing strategy enum values."""
        assert RoutingStrategy.ROUND_ROBIN.value == "round_robin"
        assert RoutingStrategy.COST_OPTIMIZED.value == "cost_optimized"
        assert RoutingStrategy.LATENCY_OPTIMIZED.value == "latency_optimized"
        assert RoutingStrategy.LOAD_BALANCED.value == "load_balanced"
        assert RoutingStrategy.FAILOVER.value == "failover"


class TestLoadBalancer:
    """Test LoadBalancer class."""

    def test_load_balancer_init(self):
        """Test load balancer initialization."""
        balancer = LoadBalancer()
        assert balancer.connection_stats == {}

    def test_record_success(self):
        """Test recording successful request."""
        balancer = LoadBalancer()
        connection_id = 1
        latency = 1.5

        balancer.record_success(connection_id, latency)

        stats = balancer.connection_stats[connection_id]
        assert stats["success_count"] == 1
        assert stats["failure_count"] == 0
        assert stats["total_latency"] == latency
        assert stats["avg_latency"] == latency
        assert stats["success_rate"] == 1.0

    def test_record_failure(self):
        """Test recording failed request."""
        balancer = LoadBalancer()
        connection_id = 1

        balancer.record_failure(connection_id)

        stats = balancer.connection_stats[connection_id]
        assert stats["success_count"] == 0
        assert stats["failure_count"] == 1
        assert stats["success_rate"] == 0.0

    def test_get_best_connection_latency_optimized(self):
        """Test getting best connection for latency optimization."""
        balancer = LoadBalancer()

        # Mock connections
        connections = [
            Mock(id=1, enabled=True),
            Mock(id=2, enabled=True),
            Mock(id=3, enabled=False),
        ]  # Disabled

        # Record different latencies
        balancer.record_success(1, 2.0)
        balancer.record_success(2, 0.5)  # Lowest latency

        best = balancer.get_best_connection(connections, "latency")
        assert best.id == 2

    def test_get_best_connection_load_balanced(self):
        """Test getting best connection for load balancing."""
        balancer = LoadBalancer()

        connections = [Mock(id=1, enabled=True), Mock(id=2, enabled=True)]

        # Record different success rates
        balancer.record_success(1, 1.0)
        balancer.record_failure(1)  # 50% success rate
        balancer.record_success(2, 1.0)
        balancer.record_success(2, 1.0)  # 100% success rate

        best = balancer.get_best_connection(connections, "load_balanced")
        assert best.id == 2

    def test_get_best_connection_no_stats(self):
        """Test getting connection when no stats available."""
        balancer = LoadBalancer()

        connections = [Mock(id=1, enabled=True), Mock(id=2, enabled=True)]

        # Should return first connection when no stats
        best = balancer.get_best_connection(connections, "latency")
        assert best.id == 1

    def test_get_connection_stats(self):
        """Test getting connection statistics."""
        balancer = LoadBalancer()

        # Record some stats
        balancer.record_success(1, 1.0)
        balancer.record_success(1, 2.0)
        balancer.record_failure(1)

        stats = balancer.get_connection_stats(1)
        assert stats["success_count"] == 2
        assert stats["failure_count"] == 1
        assert stats["avg_latency"] == 1.5
        assert abs(stats["success_rate"] - 0.667) < 0.01


class TestRequestRouter:
    """Test RequestRouter class."""

    def test_router_init(self, mock_db):
        """Test router initialization."""
        token_manager = Mock()
        security_scanner = Mock()
        memory_manager = Mock()
        llm_manager = Mock()

        router = RequestRouter(
            mock_db, token_manager, security_scanner, memory_manager, llm_manager
        )

        assert router.db == mock_db
        assert router.token_manager == token_manager
        assert router.security_scanner == security_scanner
        assert router.memory_manager == memory_manager
        assert router.llm_manager == llm_manager
        assert router.default_strategy == RoutingStrategy.LOAD_BALANCED
        assert isinstance(router.load_balancer, LoadBalancer)

    @pytest.mark.asyncio
    async def test_get_available_connections(self, mock_db):
        """Test getting available connections for model."""
        token_manager = Mock()
        security_scanner = Mock()
        memory_manager = Mock()
        llm_manager = Mock()

        router = RequestRouter(
            mock_db, token_manager, security_scanner, memory_manager, llm_manager
        )

        # Mock database query
        mock_connections = [
            Mock(
                id=1,
                provider="openai",
                model_list=["gpt-4", "gpt-3.5-turbo"],
                enabled=True,
                endpoint_url="https://api.openai.com/v1",
                api_key="test-key",
                rate_limits={},
                tls_config={},
            ),
            Mock(
                id=2,
                provider="openai",
                model_list=["gpt-3.5-turbo"],
                enabled=False,
                endpoint_url="https://api.openai.com/v1",
                api_key="test-key2",
                rate_limits={},
                tls_config={},
            ),
        ]
        mock_db.return_value = Mock()
        mock_db.return_value.select = Mock(return_value=mock_connections)
        mock_db.connection_links = Mock()

        connections = await router._get_available_connections("gpt-4")

        # Should only return enabled connections that support the model
        assert len(connections) == 1
        assert connections[0].id == 1

    @pytest.mark.asyncio
    async def test_select_connection_round_robin(self, mock_db):
        """Test round-robin connection selection."""
        token_manager = Mock()
        security_scanner = Mock()
        memory_manager = Mock()
        llm_manager = Mock()

        router = RequestRouter(
            mock_db,
            token_manager,
            security_scanner,
            memory_manager,
            llm_manager,
            RoutingStrategy.ROUND_ROBIN,
        )

        # Mock connections
        connections = [
            ConnectionLink(
                id=1,
                provider="openai",
                model_list=["gpt-4"],
                enabled=True,
                endpoint_url="https://api.openai.com/v1",
                api_key="test-key1",
                rate_limits={},
                tls_config={},
            ),
            ConnectionLink(
                id=2,
                provider="openai",
                model_list=["gpt-4"],
                enabled=True,
                endpoint_url="https://api.openai.com/v1",
                api_key="test-key2",
                rate_limits={},
                tls_config={},
            ),
        ]

        # Test round-robin behavior
        connection1 = await router._select_connection(connections, "gpt-4")
        connection2 = await router._select_connection(connections, "gpt-4")
        connection3 = await router._select_connection(connections, "gpt-4")

        # Should cycle through connections
        assert connection1.id != connection2.id
        assert connection1.id == connection3.id

    @pytest.mark.asyncio
    async def test_select_connection_cost_optimized(self, mock_db):
        """Test cost-optimized connection selection."""
        token_manager = Mock()
        security_scanner = Mock()
        memory_manager = Mock()
        llm_manager = Mock()

        router = RequestRouter(
            mock_db,
            token_manager,
            security_scanner,
            memory_manager,
            llm_manager,
            RoutingStrategy.COST_OPTIMIZED,
        )

        connections = [
            ConnectionLink(
                id=1,
                provider="openai",
                model_list=["gpt-4"],
                enabled=True,
                endpoint_url="https://api.openai.com/v1",
                api_key="test-key1",
                rate_limits={},
                tls_config={},
            ),
            ConnectionLink(
                id=2,
                provider="ollama",
                model_list=["gpt-4"],
                enabled=True,
                endpoint_url="http://localhost:11434/v1",
                api_key="",
                rate_limits={},
                tls_config={},
            ),
        ]

        # Ollama should be preferred (free/cheap)
        connection = await router._select_connection(connections, "gpt-4")
        assert connection.provider == "ollama"

    @pytest.mark.asyncio
    async def test_enhance_with_memory(self, mock_db, sample_user_context):
        """Test memory enhancement."""
        token_manager = Mock()
        security_scanner = Mock()
        memory_manager = Mock()
        llm_manager = Mock()

        router = RequestRouter(
            mock_db, token_manager, security_scanner, memory_manager, llm_manager
        )

        # Mock memory manager
        memory_manager.get_relevant_memories = AsyncMock(
            return_value=[{"content": "User likes Python", "similarity": 0.8}]
        )
        memory_manager.enhance_messages = AsyncMock(
            return_value=[
                {"role": "system", "content": "Enhanced with memory"},
                {"role": "user", "content": "Hello"},
            ]
        )

        messages = [{"role": "user", "content": "Hello"}]
        enhanced = await router._enhance_with_memory(messages, sample_user_context)

        assert len(enhanced) == 2
        assert enhanced[0]["role"] == "system"
        memory_manager.enhance_messages.assert_called_once()

    @pytest.mark.asyncio
    async def test_route_request_success(self, mock_db, sample_user_context):
        """Test successful request routing."""
        token_manager = Mock()
        security_scanner = Mock()
        memory_manager = Mock()
        llm_manager = Mock()

        router = RequestRouter(
            mock_db, token_manager, security_scanner, memory_manager, llm_manager
        )

        # Mock successful LLM response
        mock_response = {
            "choices": [{"message": {"content": "Hello there!"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        llm_manager.make_request = AsyncMock(
            return_value=(mock_response, {"provider": "openai", "model": "gpt-4"})
        )

        # Mock available connections
        mock_connections = [
            Mock(
                id=1,
                provider="openai",
                model_list=["gpt-4"],
                enabled=True,
                endpoint_url="https://api.openai.com/v1",
                api_key="test-key",
                rate_limits={},
                tls_config={},
            )
        ]
        mock_db.return_value = Mock()
        mock_db.return_value.select = Mock(return_value=mock_connections)
        mock_db.connection_links = Mock()

        # Mock memory enhancement
        memory_manager.enhance_messages = AsyncMock(
            return_value=[{"role": "user", "content": "Hello"}]
        )

        # Mock token processing
        mock_usage = Mock()
        mock_usage.waddleai_tokens = 15
        token_manager.process_usage = Mock(return_value=mock_usage)

        # Mock security scanning (clean)
        security_scanner.scan_prompt = Mock(return_value=([], "Hello"))

        messages = [{"role": "user", "content": "Hello"}]
        model = "gpt-4"

        response, metadata = await router.route_request(messages, model, sample_user_context)

        assert response == mock_response
        assert metadata["provider"] == "openai"
        assert metadata["model"] == "gpt-4"
        assert "waddleai_tokens" in metadata

        # Verify success was recorded
        assert len(router.load_balancer.connection_stats) > 0

    @pytest.mark.asyncio
    async def test_route_request_security_blocked(self, mock_db, sample_user_context):
        """Test request blocked by security scanner."""
        token_manager = Mock()
        security_scanner = Mock()
        memory_manager = Mock()
        llm_manager = Mock()

        router = RequestRouter(
            mock_db, token_manager, security_scanner, memory_manager, llm_manager
        )

        # Mock security threat detection
        from shared.security.prompt_security import (
            Action,
            SecurityThreat,
            SeverityLevel,
            ThreatType,
        )

        threat = SecurityThreat(
            threat_type=ThreatType.PROMPT_INJECTION,
            severity=SeverityLevel.HIGH,
            description="Blocked",
            confidence=0.9,
            pattern_matched="test",
            suggested_action=Action.BLOCK,
            metadata={},
        )
        security_scanner.scan_prompt = Mock(return_value=([threat], ""))

        messages = [{"role": "user", "content": "Malicious prompt"}]
        model = "gpt-4"

        with pytest.raises(Exception) as exc_info:
            await router.route_request(messages, model, sample_user_context)

        assert "Security threat detected" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_route_request_no_connections(self, mock_db, sample_user_context):
        """Test request with no available connections."""
        token_manager = Mock()
        security_scanner = Mock()
        memory_manager = Mock()
        llm_manager = Mock()

        router = RequestRouter(
            mock_db, token_manager, security_scanner, memory_manager, llm_manager
        )

        # Mock no available connections
        mock_db.return_value = Mock()
        mock_db.return_value.select = Mock(return_value=[])
        mock_db.connection_links = Mock()

        # Mock security scanning (clean)
        security_scanner.scan_prompt = Mock(return_value=([], "Hello"))

        messages = [{"role": "user", "content": "Hello"}]
        model = "unavailable-model"

        with pytest.raises(Exception) as exc_info:
            await router.route_request(messages, model, sample_user_context)

        assert "No available connections" in str(exc_info.value)

    def test_get_provider_stats(self, mock_db):
        """Test getting provider statistics."""
        token_manager = Mock()
        security_scanner = Mock()
        memory_manager = Mock()
        llm_manager = Mock()

        router = RequestRouter(
            mock_db, token_manager, security_scanner, memory_manager, llm_manager
        )

        # Record some stats
        router.load_balancer.record_success(1, 1.0)
        router.load_balancer.record_success(2, 2.0)
        router.load_balancer.record_failure(1)

        # Mock provider mapping
        router.provider_connections = {"openai": [1], "anthropic": [2]}

        stats = router.get_provider_stats()

        assert "openai" in stats
        assert "anthropic" in stats
        assert stats["openai"]["success_rate"] == 0.5
        assert stats["anthropic"]["success_rate"] == 1.0


class TestRequestRouterFactory:
    """Test request router factory function."""

    def test_create_request_router(self, mock_db):
        """Test creating request router."""
        token_manager = Mock()
        security_scanner = Mock()
        memory_manager = Mock()
        llm_manager = Mock()

        router = create_request_router(
            mock_db, token_manager, security_scanner, memory_manager, llm_manager, "cost_optimized"
        )

        assert isinstance(router, RequestRouter)
        assert router.default_strategy == RoutingStrategy.COST_OPTIMIZED

    def test_create_request_router_default_strategy(self, mock_db):
        """Test creating router with default strategy."""
        token_manager = Mock()
        security_scanner = Mock()
        memory_manager = Mock()
        llm_manager = Mock()

        router = create_request_router(
            mock_db, token_manager, security_scanner, memory_manager, llm_manager, "unknown"
        )

        assert router.default_strategy == RoutingStrategy.LOAD_BALANCED
