"""
Comprehensive tests for health check system
Tests all checker classes, status transitions, and the monitor
"""

import pytest
import asyncio
from datetime import datetime
from unittest.mock import Mock, AsyncMock, MagicMock, patch
from shared.utils.health_checks import (
    HealthStatus,
    HealthCheckResult,
    HealthChecker,
    DatabaseHealthChecker,
    RedisHealthChecker,
    SystemResourcesHealthChecker,
    LLMProviderHealthChecker,
    HTTPServiceHealthChecker,
    WaddleAIHealthMonitor,
)


# Tests for HealthStatus enum
class TestHealthStatus:
    """Test HealthStatus enum"""

    def test_health_status_values(self):
        """Verify enum values match expected strings"""
        assert HealthStatus.HEALTHY.value == "healthy"
        assert HealthStatus.UNHEALTHY.value == "unhealthy"
        assert HealthStatus.DEGRADED.value == "degraded"
        assert HealthStatus.UNKNOWN.value == "unknown"

    def test_health_status_comparison(self):
        """Test enum comparisons"""
        assert HealthStatus.HEALTHY == HealthStatus.HEALTHY
        assert HealthStatus.HEALTHY != HealthStatus.UNHEALTHY


# Tests for HealthCheckResult dataclass
class TestHealthCheckResult:
    """Test HealthCheckResult dataclass"""

    def test_result_initialization(self):
        """Test creating a result"""
        result = HealthCheckResult(
            name="test_check",
            status=HealthStatus.HEALTHY,
            message="All good",
            details={"key": "value"},
            timestamp="2025-01-01T00:00:00",
            duration_ms=50.5,
        )
        assert result.name == "test_check"
        assert result.status == HealthStatus.HEALTHY
        assert result.message == "All good"
        assert result.details == {"key": "value"}
        assert result.duration_ms == 50.5

    def test_result_to_dict(self):
        """Test to_dict converts status to string value"""
        result = HealthCheckResult(
            name="db_check",
            status=HealthStatus.HEALTHY,
            message="Connected",
            details={"pool_size": 10},
            timestamp="2025-01-01T00:00:00",
            duration_ms=100.0,
        )
        result_dict = result.to_dict()

        assert result_dict["name"] == "db_check"
        assert result_dict["status"] == "healthy"  # Converted from enum
        assert result_dict["message"] == "Connected"
        assert result_dict["details"] == {"pool_size": 10}
        assert result_dict["duration_ms"] == 100.0
        assert result_dict["timestamp"] == "2025-01-01T00:00:00"

    def test_result_to_dict_all_statuses(self):
        """Test to_dict with all status values"""
        for status in HealthStatus:
            result = HealthCheckResult(
                name="test",
                status=status,
                message="test",
                details={},
                timestamp="2025-01-01T00:00:00",
                duration_ms=0.0,
            )
            result_dict = result.to_dict()
            assert result_dict["status"] == status.value


# Tests for HealthChecker base class
class TestHealthChecker:
    """Test HealthChecker base class"""

    @pytest.mark.asyncio
    async def test_check_calls_perform_check(self):
        """Test that check() calls _perform_check()"""
        checker = HealthChecker("test_checker")
        checker._perform_check = AsyncMock(
            return_value=(HealthStatus.HEALTHY, "OK", {})
        )

        result = await checker.check()

        assert result.name == "test_checker"
        assert result.status == HealthStatus.HEALTHY
        assert result.message == "OK"
        checker._perform_check.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_measures_duration(self):
        """Test that check() measures execution time"""
        checker = HealthChecker("test")

        async def slow_check():
            await asyncio.sleep(0.05)
            return HealthStatus.HEALTHY, "OK", {}

        checker._perform_check = slow_check
        result = await checker.check()

        assert result.duration_ms >= 50  # At least 50ms

    @pytest.mark.asyncio
    async def test_check_captures_timestamp(self):
        """Test that check() captures ISO timestamp"""
        checker = HealthChecker("test")
        checker._perform_check = AsyncMock(
            return_value=(HealthStatus.HEALTHY, "OK", {})
        )

        result = await checker.check()

        # Verify it's a valid ISO timestamp format
        datetime.fromisoformat(result.timestamp)

    @pytest.mark.asyncio
    async def test_check_handles_exception(self):
        """Test that check() catches exceptions and returns UNHEALTHY"""
        checker = HealthChecker("test")
        checker._perform_check = AsyncMock(side_effect=ValueError("DB error"))

        result = await checker.check()

        assert result.status == HealthStatus.UNHEALTHY
        assert "DB error" in result.message
        assert result.details == {"error": "DB error"}

    @pytest.mark.asyncio
    async def test_perform_check_not_implemented(self):
        """Test that _perform_check raises NotImplementedError"""
        checker = HealthChecker("test")

        with pytest.raises(NotImplementedError):
            await checker._perform_check()


# Tests for DatabaseHealthChecker
class TestDatabaseHealthChecker:
    """Test DatabaseHealthChecker"""

    @pytest.mark.asyncio
    async def test_database_healthy_fast(self):
        """Test database is HEALTHY when SELECT 1 returns 1 quickly"""
        mock_db = Mock()
        mock_db.executesql = Mock(return_value=[[1]])
        mock_db._adapter = Mock(pool_size=5)

        checker = DatabaseHealthChecker("db", mock_db)
        result = await checker.check()

        assert result.status == HealthStatus.HEALTHY
        assert result.message == "Database connection healthy"
        assert result.details["query_time_ms"] < 1000
        assert result.details["connection_pool_size"] == 5

    @pytest.mark.asyncio
    async def test_database_degraded_slow(self):
        """Test database is DEGRADED when query takes > 1000ms"""
        mock_db = Mock()
        mock_db._adapter = Mock(pool_size=5)

        checker = DatabaseHealthChecker("db", mock_db)
        checker._perform_check = AsyncMock()

        async def slow_perform():
            import time
            start_time = time.time()
            await asyncio.sleep(1.1)
            query_time = (time.time() - start_time) * 1000
            return HealthStatus.DEGRADED, f"Database slow (query took {query_time:.1f}ms)", {
                'query_time_ms': query_time,
                'connection_pool_size': 5
            }

        checker._perform_check = slow_perform
        result = await checker.check()

        assert result.status == HealthStatus.DEGRADED
        assert "slow" in result.message

    @pytest.mark.asyncio
    async def test_database_unhealthy_wrong_result(self):
        """Test database is UNHEALTHY when SELECT 1 doesn't return 1"""
        mock_db = Mock()
        mock_db.executesql = Mock(return_value=[[0]])
        mock_db._adapter = Mock(pool_size=5)

        checker = DatabaseHealthChecker("db", mock_db)
        result = await checker.check()

        assert result.status == HealthStatus.UNHEALTHY
        assert "unexpected result" in result.message

    @pytest.mark.asyncio
    async def test_database_unhealthy_exception(self):
        """Test database is UNHEALTHY on connection error"""
        mock_db = Mock()
        mock_db.executesql = Mock(side_effect=ConnectionError("Connection refused"))

        checker = DatabaseHealthChecker("db", mock_db)
        result = await checker.check()

        assert result.status == HealthStatus.UNHEALTHY
        assert "Connection refused" in result.message
        assert result.details["error"] == "Connection refused"

    @pytest.mark.asyncio
    async def test_database_pool_size_unknown(self):
        """Test handles missing pool_size gracefully"""
        mock_db = Mock()
        mock_db.executesql = Mock(return_value=[[1]])
        mock_db._adapter = Mock(spec=[])  # No pool_size attribute

        checker = DatabaseHealthChecker("db", mock_db)
        result = await checker.check()

        assert result.status == HealthStatus.HEALTHY
        assert result.details["connection_pool_size"] == "unknown"


# Tests for RedisHealthChecker
class TestRedisHealthChecker:
    """Test RedisHealthChecker"""

    @pytest.mark.asyncio
    async def test_redis_healthy_fast(self):
        """Test Redis is HEALTHY when ping < 100ms"""
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)
        mock_client.info = AsyncMock(return_value={
            'connected_clients': 5,
            'used_memory_human': '100M',
            'redis_version': '7.0'
        })
        mock_client.close = AsyncMock()

        with patch('redis.asyncio.from_url', return_value=mock_client):
            checker = RedisHealthChecker("redis", "redis://localhost:6379")
            result = await checker.check()

            assert result.status == HealthStatus.HEALTHY
            assert "healthy" in result.message
            assert result.details["connected_clients"] == 5
            assert result.details["used_memory_human"] == "100M"
            assert result.details["redis_version"] == "7.0"
            mock_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_redis_degraded_slow(self):
        """Test Redis is DEGRADED when ping > 100ms"""
        mock_client = AsyncMock()

        async def slow_ping():
            await asyncio.sleep(0.15)
            return True

        mock_client.ping = slow_ping
        mock_client.info = AsyncMock(return_value={'connected_clients': 5})
        mock_client.close = AsyncMock()

        with patch('redis.asyncio.from_url', return_value=mock_client):
            checker = RedisHealthChecker("redis", "redis://localhost:6379")
            result = await checker.check()

            assert result.status == HealthStatus.DEGRADED
            assert "slow" in result.message
            assert result.details["ping_time_ms"] > 100

    @pytest.mark.asyncio
    async def test_redis_unhealthy_ping_fails(self):
        """Test Redis is UNHEALTHY when ping returns False"""
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        with patch('redis.asyncio.from_url', return_value=mock_client):
            checker = RedisHealthChecker("redis", "redis://localhost:6379")
            result = await checker.check()

            assert result.status == HealthStatus.UNHEALTHY
            assert "ping failed" in result.message

    @pytest.mark.asyncio
    async def test_redis_unhealthy_exception(self):
        """Test Redis is UNHEALTHY on connection error"""
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(side_effect=ConnectionError("Connection refused"))
        mock_client.close = AsyncMock()

        with patch('redis.asyncio.from_url', return_value=mock_client):
            checker = RedisHealthChecker("redis", "redis://localhost:6379")
            result = await checker.check()

            assert result.status == HealthStatus.UNHEALTHY
            assert "Connection refused" in result.message

    @pytest.mark.asyncio
    async def test_redis_client_closed_on_error(self):
        """Test Redis client is closed even on exception"""
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(side_effect=ValueError("Test error"))
        mock_client.close = AsyncMock()

        with patch('redis.asyncio.from_url', return_value=mock_client):
            checker = RedisHealthChecker("redis", "redis://localhost:6379")
            result = await checker.check()

            mock_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_redis_missing_info_fields(self):
        """Test handles missing fields in Redis info"""
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)
        mock_client.info = AsyncMock(return_value={})  # Empty info
        mock_client.close = AsyncMock()

        with patch('redis.asyncio.from_url', return_value=mock_client):
            checker = RedisHealthChecker("redis", "redis://localhost:6379")
            result = await checker.check()

            assert result.status == HealthStatus.HEALTHY
            assert result.details["connected_clients"] == 0
            assert result.details["used_memory_human"] == "unknown"
            assert result.details["redis_version"] == "unknown"


# Tests for SystemResourcesHealthChecker
class TestSystemResourcesHealthChecker:
    """Test SystemResourcesHealthChecker"""

    @pytest.mark.asyncio
    async def test_system_healthy_all_green(self):
        """Test system is HEALTHY when all metrics below thresholds"""
        with patch('psutil.cpu_percent', return_value=50.0), \
             patch('psutil.virtual_memory') as mock_mem, \
             patch('psutil.disk_usage') as mock_disk:

            mock_mem.return_value = Mock(percent=60.0, available=500*1024**3)
            mock_disk.return_value = Mock(percent=50.0, free=100*1024**3)

            checker = SystemResourcesHealthChecker("system", cpu_threshold=90.0, memory_threshold=90.0)
            result = await checker.check()

            assert result.status == HealthStatus.HEALTHY
            assert "healthy" in result.message
            assert result.details["cpu_percent"] == 50.0
            assert result.details["memory_percent"] == 60.0
            assert result.details["disk_percent"] == 50.0

    @pytest.mark.asyncio
    async def test_system_degraded_single_issue_cpu(self):
        """Test system is DEGRADED when only CPU above threshold"""
        with patch('psutil.cpu_percent', return_value=95.0), \
             patch('psutil.virtual_memory') as mock_mem, \
             patch('psutil.disk_usage') as mock_disk:

            mock_mem.return_value = Mock(percent=60.0, available=500*1024**3)
            mock_disk.return_value = Mock(percent=50.0, free=100*1024**3)

            checker = SystemResourcesHealthChecker("system", cpu_threshold=90.0, memory_threshold=90.0)
            result = await checker.check()

            assert result.status == HealthStatus.DEGRADED
            assert "High CPU usage" in result.message

    @pytest.mark.asyncio
    async def test_system_degraded_single_issue_memory(self):
        """Test system is DEGRADED when only memory above threshold"""
        with patch('psutil.cpu_percent', return_value=50.0), \
             patch('psutil.virtual_memory') as mock_mem, \
             patch('psutil.disk_usage') as mock_disk:

            mock_mem.return_value = Mock(percent=95.0, available=10*1024**3)
            mock_disk.return_value = Mock(percent=50.0, free=100*1024**3)

            checker = SystemResourcesHealthChecker("system", cpu_threshold=90.0, memory_threshold=90.0)
            result = await checker.check()

            assert result.status == HealthStatus.DEGRADED
            assert "High memory usage" in result.message

    @pytest.mark.asyncio
    async def test_system_degraded_single_issue_disk(self):
        """Test system is DEGRADED when disk > 95%"""
        with patch('psutil.cpu_percent', return_value=50.0), \
             patch('psutil.virtual_memory') as mock_mem, \
             patch('psutil.disk_usage') as mock_disk:

            mock_mem.return_value = Mock(percent=60.0, available=500*1024**3)
            mock_disk.return_value = Mock(percent=96.0, free=5*1024**3)

            checker = SystemResourcesHealthChecker("system", cpu_threshold=90.0, memory_threshold=90.0)
            result = await checker.check()

            assert result.status == HealthStatus.DEGRADED
            assert "Low disk space" in result.message

    @pytest.mark.asyncio
    async def test_system_unhealthy_multiple_issues(self):
        """Test system is UNHEALTHY when multiple issues exist"""
        with patch('psutil.cpu_percent', return_value=95.0), \
             patch('psutil.virtual_memory') as mock_mem, \
             patch('psutil.disk_usage') as mock_disk:

            mock_mem.return_value = Mock(percent=95.0, available=10*1024**3)
            mock_disk.return_value = Mock(percent=50.0, free=100*1024**3)

            checker = SystemResourcesHealthChecker("system", cpu_threshold=90.0, memory_threshold=90.0)
            result = await checker.check()

            assert result.status == HealthStatus.UNHEALTHY
            assert "High CPU usage" in result.message
            assert "High memory usage" in result.message

    @pytest.mark.asyncio
    async def test_system_unhealthy_cpu_memory_disk(self):
        """Test system is UNHEALTHY when all three metrics are issues"""
        with patch('psutil.cpu_percent', return_value=95.0), \
             patch('psutil.virtual_memory') as mock_mem, \
             patch('psutil.disk_usage') as mock_disk:

            mock_mem.return_value = Mock(percent=95.0, available=10*1024**3)
            mock_disk.return_value = Mock(percent=96.0, free=5*1024**3)

            checker = SystemResourcesHealthChecker("system", cpu_threshold=90.0, memory_threshold=90.0)
            result = await checker.check()

            assert result.status == HealthStatus.UNHEALTHY

    @pytest.mark.asyncio
    async def test_system_custom_thresholds(self):
        """Test custom CPU and memory thresholds"""
        with patch('psutil.cpu_percent', return_value=80.0), \
             patch('psutil.virtual_memory') as mock_mem, \
             patch('psutil.disk_usage') as mock_disk:

            mock_mem.return_value = Mock(percent=85.0, available=100*1024**3)
            mock_disk.return_value = Mock(percent=50.0, free=100*1024**3)

            checker = SystemResourcesHealthChecker("system", cpu_threshold=85.0, memory_threshold=90.0)
            result = await checker.check()

            assert result.status == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_system_exception(self):
        """Test system check handles exceptions"""
        with patch('psutil.cpu_percent', side_effect=OSError("psutil error")):
            checker = SystemResourcesHealthChecker("system")
            result = await checker.check()

            assert result.status == HealthStatus.UNHEALTHY
            assert "psutil error" in result.message


# Tests for LLMProviderHealthChecker
class TestLLMProviderHealthChecker:
    """Test LLMProviderHealthChecker"""

    @pytest.mark.asyncio
    async def test_llm_all_healthy(self):
        """Test LLM is HEALTHY when all providers healthy"""
        mock_manager = AsyncMock()
        mock_manager.health_check_all = AsyncMock(return_value={
            'openai': {'status': 'healthy'},
            'anthropic': {'status': 'healthy'},
        })

        checker = LLMProviderHealthChecker("llm", mock_manager)
        result = await checker.check()

        assert result.status == HealthStatus.HEALTHY
        assert "All 2 LLM providers healthy" in result.message
        assert result.details['openai']['status'] == 'healthy'

    @pytest.mark.asyncio
    async def test_llm_some_healthy(self):
        """Test LLM is DEGRADED when some providers healthy"""
        mock_manager = AsyncMock()
        mock_manager.health_check_all = AsyncMock(return_value={
            'openai': {'status': 'healthy'},
            'anthropic': {'status': 'unhealthy', 'error': 'API down'},
        })

        checker = LLMProviderHealthChecker("llm", mock_manager)
        result = await checker.check()

        assert result.status == HealthStatus.DEGRADED
        assert "1/2 LLM providers healthy" in result.message

    @pytest.mark.asyncio
    async def test_llm_all_unhealthy(self):
        """Test LLM is UNHEALTHY when all providers unhealthy"""
        mock_manager = AsyncMock()
        mock_manager.health_check_all = AsyncMock(return_value={
            'openai': {'status': 'unhealthy', 'error': 'API down'},
            'anthropic': {'status': 'unhealthy', 'error': 'API down'},
        })

        checker = LLMProviderHealthChecker("llm", mock_manager)
        result = await checker.check()

        assert result.status == HealthStatus.UNHEALTHY
        assert "All LLM providers unhealthy" in result.message

    @pytest.mark.asyncio
    async def test_llm_exception(self):
        """Test LLM check handles exceptions"""
        mock_manager = AsyncMock()
        mock_manager.health_check_all = AsyncMock(side_effect=RuntimeError("Manager error"))

        checker = LLMProviderHealthChecker("llm", mock_manager)
        result = await checker.check()

        assert result.status == HealthStatus.UNHEALTHY
        assert "Manager error" in result.message


# Tests for HTTPServiceHealthChecker
class TestHTTPServiceHealthChecker:
    """Test HTTPServiceHealthChecker"""

    @pytest.mark.asyncio
    async def test_http_healthy_200_fast(self):
        """Test HTTP is HEALTHY for 200 response < 5s"""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch('aiohttp.ClientSession', return_value=mock_session):
            checker = HTTPServiceHealthChecker("api", "http://localhost:8080/health")
            result = await checker.check()

            assert result.status == HealthStatus.HEALTHY
            assert "healthy" in result.message
            assert result.details["status_code"] == 200

    @pytest.mark.asyncio
    async def test_http_degraded_200_slow(self):
        """Test HTTP is DEGRADED for 200 response > 5s"""
        mock_response = MagicMock()
        mock_response.status = 200

        async def slow_enter(*args):
            await asyncio.sleep(5.1)
            return mock_response

        mock_response.__aenter__ = slow_enter
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch('aiohttp.ClientSession', return_value=mock_session):
            checker = HTTPServiceHealthChecker("api", "http://localhost:8080/health")
            result = await checker.check()

            assert result.status == HealthStatus.DEGRADED
            assert "slow" in result.message

    @pytest.mark.asyncio
    async def test_http_unhealthy_non_200(self):
        """Test HTTP is UNHEALTHY for non-200 status"""
        mock_response = MagicMock()
        mock_response.status = 500
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch('aiohttp.ClientSession', return_value=mock_session):
            checker = HTTPServiceHealthChecker("api", "http://localhost:8080/health")
            result = await checker.check()

            assert result.status == HealthStatus.UNHEALTHY
            assert "status 500" in result.message

    @pytest.mark.asyncio
    async def test_http_unhealthy_timeout(self):
        """Test HTTP is UNHEALTHY on timeout"""
        mock_response = MagicMock()
        mock_response.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch('aiohttp.ClientSession', return_value=mock_session):
            checker = HTTPServiceHealthChecker("api", "http://localhost:8080/health", timeout=5)
            result = await checker.check()

            assert result.status == HealthStatus.UNHEALTHY
            assert "timeout" in result.message.lower()

    @pytest.mark.asyncio
    async def test_http_unhealthy_exception(self):
        """Test HTTP is UNHEALTHY on exception"""
        mock_response = MagicMock()
        mock_response.__aenter__ = AsyncMock(side_effect=ConnectionError("Connection failed"))
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch('aiohttp.ClientSession', return_value=mock_session):
            checker = HTTPServiceHealthChecker("api", "http://localhost:8080/health")
            result = await checker.check()

            assert result.status == HealthStatus.UNHEALTHY
            assert "Connection failed" in result.message

    @pytest.mark.asyncio
    async def test_http_custom_timeout(self):
        """Test custom timeout value"""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch('aiohttp.ClientSession') as mock_client_session:
            mock_client_session.return_value = mock_session
            checker = HTTPServiceHealthChecker("api", "http://localhost:8080/health", timeout=30)
            result = await checker.check()

            assert result.status == HealthStatus.HEALTHY


# Tests for WaddleAIHealthMonitor
class TestWaddleAIHealthMonitor:
    """Test WaddleAIHealthMonitor"""

    def test_monitor_initialization(self):
        """Test monitor initializes with empty checkers"""
        monitor = WaddleAIHealthMonitor("test_service")
        assert monitor.service_name == "test_service"
        assert monitor.checkers == []
        assert monitor.last_results == {}

    def test_add_checker(self):
        """Test adding a custom checker"""
        monitor = WaddleAIHealthMonitor("service")
        checker = HealthChecker("test_check")
        monitor.add_checker(checker)

        assert len(monitor.checkers) == 1
        assert monitor.checkers[0] == checker

    def test_add_database_check(self):
        """Test helper to add database check"""
        monitor = WaddleAIHealthMonitor("service")
        mock_db = Mock()

        monitor.add_database_check("db", mock_db)

        assert len(monitor.checkers) == 1
        assert isinstance(monitor.checkers[0], DatabaseHealthChecker)
        assert monitor.checkers[0].name == "db"

    def test_add_redis_check(self):
        """Test helper to add Redis check"""
        monitor = WaddleAIHealthMonitor("service")

        monitor.add_redis_check("redis", "redis://localhost:6379")

        assert len(monitor.checkers) == 1
        assert isinstance(monitor.checkers[0], RedisHealthChecker)
        assert monitor.checkers[0].name == "redis"

    def test_add_system_resources_check(self):
        """Test helper to add system resources check"""
        monitor = WaddleAIHealthMonitor("service")

        monitor.add_system_resources_check()

        assert len(monitor.checkers) == 1
        assert isinstance(monitor.checkers[0], SystemResourcesHealthChecker)
        assert monitor.checkers[0].name == "system_resources"

    def test_add_system_resources_check_custom_name(self):
        """Test system resources check with custom name"""
        monitor = WaddleAIHealthMonitor("service")

        monitor.add_system_resources_check("custom_system")

        assert monitor.checkers[0].name == "custom_system"

    def test_add_llm_providers_check(self):
        """Test helper to add LLM providers check"""
        monitor = WaddleAIHealthMonitor("service")
        mock_manager = Mock()

        monitor.add_llm_providers_check("llm", mock_manager)

        assert len(monitor.checkers) == 1
        assert isinstance(monitor.checkers[0], LLMProviderHealthChecker)
        assert monitor.checkers[0].name == "llm"

    def test_add_http_service_check(self):
        """Test helper to add HTTP service check"""
        monitor = WaddleAIHealthMonitor("service")

        monitor.add_http_service_check("api", "http://localhost:8080")

        assert len(monitor.checkers) == 1
        assert isinstance(monitor.checkers[0], HTTPServiceHealthChecker)
        assert monitor.checkers[0].name == "api"

    def test_add_http_service_check_custom_timeout(self):
        """Test HTTP service check with custom timeout"""
        monitor = WaddleAIHealthMonitor("service")

        monitor.add_http_service_check("api", "http://localhost:8080", timeout=30)

        assert monitor.checkers[0].timeout == 30

    @pytest.mark.asyncio
    async def test_check_all_all_healthy(self):
        """Test check_all with all healthy checks"""
        monitor = WaddleAIHealthMonitor("service")

        mock_checker1 = AsyncMock(spec=HealthChecker)
        mock_checker1.name = "check1"
        mock_checker1.check = AsyncMock(return_value=HealthCheckResult(
            name="check1",
            status=HealthStatus.HEALTHY,
            message="OK",
            details={},
            timestamp="2025-01-01T00:00:00",
            duration_ms=10.0
        ))

        mock_checker2 = AsyncMock(spec=HealthChecker)
        mock_checker2.name = "check2"
        mock_checker2.check = AsyncMock(return_value=HealthCheckResult(
            name="check2",
            status=HealthStatus.HEALTHY,
            message="OK",
            details={},
            timestamp="2025-01-01T00:00:00",
            duration_ms=10.0
        ))

        monitor.checkers = [mock_checker1, mock_checker2]

        result = await monitor.check_all()

        assert result['status'] == 'healthy'
        assert result['service'] == 'service'
        assert result['checks']['healthy'] == 2
        assert result['checks']['degraded'] == 0
        assert result['checks']['unhealthy'] == 0
        assert 'check1' in result['results']
        assert 'check2' in result['results']

    @pytest.mark.asyncio
    async def test_check_all_with_degraded(self):
        """Test check_all overall status is DEGRADED with any degraded check"""
        monitor = WaddleAIHealthMonitor("service")

        mock_checker1 = AsyncMock(spec=HealthChecker)
        mock_checker1.name = "check1"
        mock_checker1.check = AsyncMock(return_value=HealthCheckResult(
            name="check1",
            status=HealthStatus.HEALTHY,
            message="OK",
            details={},
            timestamp="2025-01-01T00:00:00",
            duration_ms=10.0
        ))

        mock_checker2 = AsyncMock(spec=HealthChecker)
        mock_checker2.name = "check2"
        mock_checker2.check = AsyncMock(return_value=HealthCheckResult(
            name="check2",
            status=HealthStatus.DEGRADED,
            message="Slow",
            details={},
            timestamp="2025-01-01T00:00:00",
            duration_ms=10.0
        ))

        monitor.checkers = [mock_checker1, mock_checker2]

        result = await monitor.check_all()

        assert result['status'] == 'degraded'
        assert result['checks']['healthy'] == 1
        assert result['checks']['degraded'] == 1
        assert result['checks']['unhealthy'] == 0

    @pytest.mark.asyncio
    async def test_check_all_with_unhealthy(self):
        """Test check_all overall status is UNHEALTHY with any unhealthy check"""
        monitor = WaddleAIHealthMonitor("service")

        mock_checker1 = AsyncMock(spec=HealthChecker)
        mock_checker1.name = "check1"
        mock_checker1.check = AsyncMock(return_value=HealthCheckResult(
            name="check1",
            status=HealthStatus.HEALTHY,
            message="OK",
            details={},
            timestamp="2025-01-01T00:00:00",
            duration_ms=10.0
        ))

        mock_checker2 = AsyncMock(spec=HealthChecker)
        mock_checker2.name = "check2"
        mock_checker2.check = AsyncMock(return_value=HealthCheckResult(
            name="check2",
            status=HealthStatus.UNHEALTHY,
            message="Failed",
            details={},
            timestamp="2025-01-01T00:00:00",
            duration_ms=10.0
        ))

        monitor.checkers = [mock_checker1, mock_checker2]

        result = await monitor.check_all()

        assert result['status'] == 'unhealthy'
        assert result['checks']['unhealthy'] == 1

    @pytest.mark.asyncio
    async def test_check_all_stores_last_results(self):
        """Test check_all stores results in last_results"""
        monitor = WaddleAIHealthMonitor("service")

        mock_checker = AsyncMock(spec=HealthChecker)
        mock_checker.name = "check1"
        mock_checker.check = AsyncMock(return_value=HealthCheckResult(
            name="check1",
            status=HealthStatus.HEALTHY,
            message="OK",
            details={},
            timestamp="2025-01-01T00:00:00",
            duration_ms=10.0
        ))

        monitor.checkers = [mock_checker]

        await monitor.check_all()

        assert 'check1' in monitor.last_results
        assert monitor.last_results['check1'].name == 'check1'

    @pytest.mark.asyncio
    async def test_check_all_handles_exception(self):
        """Test check_all handles exceptions from gather"""
        monitor = WaddleAIHealthMonitor("service")

        mock_checker = AsyncMock(spec=HealthChecker)
        mock_checker.name = "check1"
        mock_checker.check = AsyncMock(side_effect=ValueError("Check error"))

        monitor.checkers = [mock_checker]

        result = await monitor.check_all()

        assert result['checks']['unhealthy'] == 1
        assert 'check1' in result['results']
        assert result['results']['check1']['status'] == 'unhealthy'

    @pytest.mark.asyncio
    async def test_check_single_found(self):
        """Test check_single returns result for found checker"""
        monitor = WaddleAIHealthMonitor("service")

        mock_checker = AsyncMock(spec=HealthChecker)
        mock_checker.name = "check1"
        mock_checker.check = AsyncMock(return_value=HealthCheckResult(
            name="check1",
            status=HealthStatus.HEALTHY,
            message="OK",
            details={},
            timestamp="2025-01-01T00:00:00",
            duration_ms=10.0
        ))

        monitor.checkers = [mock_checker]

        result = await monitor.check_single("check1")

        assert result is not None
        assert result['name'] == 'check1'
        assert result['status'] == 'healthy'

    @pytest.mark.asyncio
    async def test_check_single_not_found(self):
        """Test check_single returns None for unknown checker"""
        monitor = WaddleAIHealthMonitor("service")

        mock_checker = AsyncMock(spec=HealthChecker)
        mock_checker.name = "check1"
        monitor.checkers = [mock_checker]

        result = await monitor.check_single("unknown_check")

        assert result is None

    @pytest.mark.asyncio
    async def test_check_single_stores_result(self):
        """Test check_single stores result in last_results"""
        monitor = WaddleAIHealthMonitor("service")

        mock_checker = AsyncMock(spec=HealthChecker)
        mock_checker.name = "check1"
        mock_checker.check = AsyncMock(return_value=HealthCheckResult(
            name="check1",
            status=HealthStatus.HEALTHY,
            message="OK",
            details={},
            timestamp="2025-01-01T00:00:00",
            duration_ms=10.0
        ))

        monitor.checkers = [mock_checker]

        await monitor.check_single("check1")

        assert 'check1' in monitor.last_results

    def test_get_last_results_empty(self):
        """Test get_last_results when no checks performed"""
        monitor = WaddleAIHealthMonitor("service")

        result = monitor.get_last_results()

        assert result['service'] == 'service'
        assert result['status'] == 'unknown'
        assert 'No health checks performed' in result['message']
        assert result['results'] == {}

    def test_get_last_results_with_results(self):
        """Test get_last_results with stored results"""
        monitor = WaddleAIHealthMonitor("service")

        result1 = HealthCheckResult(
            name="check1",
            status=HealthStatus.HEALTHY,
            message="OK",
            details={},
            timestamp="2025-01-01T00:00:00",
            duration_ms=10.0
        )
        result2 = HealthCheckResult(
            name="check2",
            status=HealthStatus.DEGRADED,
            message="Slow",
            details={},
            timestamp="2025-01-01T00:00:00",
            duration_ms=10.0
        )

        monitor.last_results = {"check1": result1, "check2": result2}

        results = monitor.get_last_results()

        assert results['status'] == 'degraded'
        assert 'check1' in results['results']
        assert 'check2' in results['results']
        assert results['results']['check1']['status'] == 'healthy'
        assert results['results']['check2']['status'] == 'degraded'

    def test_get_last_results_all_unhealthy(self):
        """Test get_last_results with unhealthy check"""
        monitor = WaddleAIHealthMonitor("service")

        result = HealthCheckResult(
            name="check1",
            status=HealthStatus.UNHEALTHY,
            message="Failed",
            details={},
            timestamp="2025-01-01T00:00:00",
            duration_ms=10.0
        )

        monitor.last_results = {"check1": result}

        results = monitor.get_last_results()

        assert results['status'] == 'unhealthy'


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
