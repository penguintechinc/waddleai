"""
Health check system for WaddleAI components
Provides comprehensive health monitoring for all services and dependencies
"""

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import aiohttp
import psutil
import redis.asyncio as redis

logger = logging.getLogger(__name__)


class HealthStatus(Enum):
    """Health check status values"""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


@dataclass
class HealthCheckResult:
    """Result of a health check"""

    name: str
    status: HealthStatus
    message: str
    details: Dict[str, Any]
    timestamp: str
    duration_ms: float

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value
        return result


class HealthChecker:
    """Base health checker class"""

    def __init__(self, name: str):
        self.name = name

    async def check(self) -> HealthCheckResult:
        """Perform health check"""
        start_time = time.time()

        try:
            status, message, details = await self._perform_check()
            duration_ms = (time.time() - start_time) * 1000

            return HealthCheckResult(
                name=self.name,
                status=status,
                message=message,
                details=details,
                timestamp=datetime.utcnow().isoformat(),
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(f"Health check {self.name} failed: {e}")

            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                message=f"Health check failed: {str(e)}",
                details={"error": str(e)},
                timestamp=datetime.utcnow().isoformat(),
                duration_ms=duration_ms,
            )

    async def _perform_check(self) -> tuple[HealthStatus, str, Dict[str, Any]]:
        """Override this method in subclasses"""
        raise NotImplementedError


class DatabaseHealthChecker(HealthChecker):
    """Database health checker"""

    def __init__(self, name: str, db):
        super().__init__(name)
        self.db = db

    async def _perform_check(self) -> tuple[HealthStatus, str, Dict[str, Any]]:
        """Check database connectivity and basic operations"""
        try:
            # Test basic connectivity with a simple query
            start_time = time.time()
            result = self.db.executesql("SELECT 1")[0][0]
            query_time = (time.time() - start_time) * 1000

            if result != 1:
                return HealthStatus.UNHEALTHY, "Database query returned unexpected result", {}

            # Check connection pool status if available
            details = {
                "query_time_ms": query_time,
                "connection_pool_size": getattr(self.db._adapter, "pool_size", "unknown"),
            }

            if query_time > 1000:  # More than 1 second
                return HealthStatus.DEGRADED, f"Database slow (query took {query_time:.1f}ms)", details

            return HealthStatus.HEALTHY, "Database connection healthy", details

        except Exception as e:
            return HealthStatus.UNHEALTHY, f"Database connection failed: {str(e)}", {"error": str(e)}


class RedisHealthChecker(HealthChecker):
    """Redis health checker"""

    def __init__(self, name: str, redis_url: str):
        super().__init__(name)
        self.redis_url = redis_url

    async def _perform_check(self) -> tuple[HealthStatus, str, Dict[str, Any]]:
        """Check Redis connectivity"""
        client = None
        try:
            client = redis.from_url(self.redis_url)

            # Test basic connectivity
            start_time = time.time()
            pong = await client.ping()
            ping_time = (time.time() - start_time) * 1000

            if not pong:
                return HealthStatus.UNHEALTHY, "Redis ping failed", {}

            # Get Redis info
            info = await client.info()

            details = {
                "ping_time_ms": ping_time,
                "connected_clients": info.get("connected_clients", 0),
                "used_memory_human": info.get("used_memory_human", "unknown"),
                "redis_version": info.get("redis_version", "unknown"),
            }

            if ping_time > 100:  # More than 100ms
                return HealthStatus.DEGRADED, f"Redis slow (ping took {ping_time:.1f}ms)", details

            return HealthStatus.HEALTHY, "Redis connection healthy", details

        except Exception as e:
            return HealthStatus.UNHEALTHY, f"Redis connection failed: {str(e)}", {"error": str(e)}

        finally:
            if client:
                await client.close()


class SystemResourcesHealthChecker(HealthChecker):
    """System resources health checker"""

    def __init__(self, name: str, cpu_threshold: float = 90.0, memory_threshold: float = 90.0):
        super().__init__(name)
        self.cpu_threshold = cpu_threshold
        self.memory_threshold = memory_threshold

    async def _perform_check(self) -> tuple[HealthStatus, str, Dict[str, Any]]:
        """Check system CPU and memory usage"""
        try:
            # Get system stats
            cpu_percent = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage("/")

            details = {
                "cpu_percent": cpu_percent,
                "memory_percent": memory.percent,
                "memory_available_gb": memory.available / (1024**3),
                "disk_percent": disk.percent,
                "disk_free_gb": disk.free / (1024**3),
            }

            # Check thresholds
            issues = []

            if cpu_percent > self.cpu_threshold:
                issues.append(f"High CPU usage: {cpu_percent:.1f}%")

            if memory.percent > self.memory_threshold:
                issues.append(f"High memory usage: {memory.percent:.1f}%")

            if disk.percent > 95:
                issues.append(f"Low disk space: {disk.percent:.1f}% used")

            if issues:
                status = HealthStatus.DEGRADED if len(issues) == 1 else HealthStatus.UNHEALTHY
                message = "System resource issues: " + "; ".join(issues)
            else:
                status = HealthStatus.HEALTHY
                message = "System resources healthy"

            return status, message, details

        except Exception as e:
            return HealthStatus.UNHEALTHY, f"System check failed: {str(e)}", {"error": str(e)}


class LLMProviderHealthChecker(HealthChecker):
    """LLM provider health checker"""

    def __init__(self, name: str, llm_manager):
        super().__init__(name)
        self.llm_manager = llm_manager

    async def _perform_check(self) -> tuple[HealthStatus, str, Dict[str, Any]]:
        """Check all LLM provider connections"""
        try:
            health_results = await self.llm_manager.health_check_all()

            healthy_count = 0
            unhealthy_count = 0
            details = {}

            for provider, result in health_results.items():
                details[provider] = result
                if result.get("status") == "healthy":
                    healthy_count += 1
                else:
                    unhealthy_count += 1

            total_providers = len(health_results)

            if unhealthy_count == 0:
                status = HealthStatus.HEALTHY
                message = f"All {total_providers} LLM providers healthy"
            elif healthy_count > 0:
                status = HealthStatus.DEGRADED
                message = f"{healthy_count}/{total_providers} LLM providers healthy"
            else:
                status = HealthStatus.UNHEALTHY
                message = "All LLM providers unhealthy"

            return status, message, details

        except Exception as e:
            return HealthStatus.UNHEALTHY, f"LLM provider check failed: {str(e)}", {"error": str(e)}


class HTTPServiceHealthChecker(HealthChecker):
    """HTTP service health checker"""

    def __init__(self, name: str, url: str, timeout: int = 10):
        super().__init__(name)
        self.url = url
        self.timeout = timeout

    async def _perform_check(self) -> tuple[HealthStatus, str, Dict[str, Any]]:
        """Check HTTP service availability"""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                start_time = time.time()
                async with session.get(self.url) as response:
                    response_time = (time.time() - start_time) * 1000

                    details = {"status_code": response.status, "response_time_ms": response_time, "url": self.url}

                    if response.status == 200:
                        if response_time > 5000:  # More than 5 seconds
                            status = HealthStatus.DEGRADED
                            message = f"Service slow (response took {response_time:.1f}ms)"
                        else:
                            status = HealthStatus.HEALTHY
                            message = "Service healthy"
                    else:
                        status = HealthStatus.UNHEALTHY
                        message = f"Service returned status {response.status}"

                    return status, message, details

        except asyncio.TimeoutError:
            return HealthStatus.UNHEALTHY, f"Service timeout after {self.timeout}s", {"url": self.url}
        except Exception as e:
            return HealthStatus.UNHEALTHY, f"Service check failed: {str(e)}", {"error": str(e), "url": self.url}


class WaddleAIHealthMonitor:
    """Main health monitoring system"""

    def __init__(self, service_name: str):
        self.service_name = service_name
        self.checkers: List[HealthChecker] = []
        self.last_results: Dict[str, HealthCheckResult] = {}

    def add_checker(self, checker: HealthChecker):
        """Add a health checker"""
        self.checkers.append(checker)

    def add_database_check(self, name: str, db):
        """Add database health check"""
        self.add_checker(DatabaseHealthChecker(name, db))

    def add_redis_check(self, name: str, redis_url: str):
        """Add Redis health check"""
        self.add_checker(RedisHealthChecker(name, redis_url))

    def add_system_resources_check(self, name: str = "system_resources"):
        """Add system resources health check"""
        self.add_checker(SystemResourcesHealthChecker(name))

    def add_llm_providers_check(self, name: str, llm_manager):
        """Add LLM providers health check"""
        self.add_checker(LLMProviderHealthChecker(name, llm_manager))

    def add_http_service_check(self, name: str, url: str, timeout: int = 10):
        """Add HTTP service health check"""
        self.add_checker(HTTPServiceHealthChecker(name, url, timeout))

    async def check_all(self) -> Dict[str, Any]:
        """Run all health checks"""
        results = {}
        overall_status = HealthStatus.HEALTHY

        # Run all checks concurrently
        check_tasks = [checker.check() for checker in self.checkers]
        check_results = await asyncio.gather(*check_tasks, return_exceptions=True)

        healthy_count = 0
        unhealthy_count = 0
        degraded_count = 0

        for i, result in enumerate(check_results):
            if isinstance(result, Exception):
                # Handle task exception
                checker_name = self.checkers[i].name
                result = HealthCheckResult(
                    name=checker_name,
                    status=HealthStatus.UNHEALTHY,
                    message=f"Health check exception: {str(result)}",
                    details={"error": str(result)},
                    timestamp=datetime.utcnow().isoformat(),
                    duration_ms=0,
                )

            results[result.name] = result.to_dict()
            self.last_results[result.name] = result

            # Update counters
            if result.status == HealthStatus.HEALTHY:
                healthy_count += 1
            elif result.status == HealthStatus.DEGRADED:
                degraded_count += 1
            else:
                unhealthy_count += 1

        # Determine overall status
        if unhealthy_count > 0:
            overall_status = HealthStatus.UNHEALTHY
        elif degraded_count > 0:
            overall_status = HealthStatus.DEGRADED
        else:
            overall_status = HealthStatus.HEALTHY

        # Build summary
        summary = {
            "service": self.service_name,
            "status": overall_status.value,
            "timestamp": datetime.utcnow().isoformat(),
            "checks": {
                "total": len(self.checkers),
                "healthy": healthy_count,
                "degraded": degraded_count,
                "unhealthy": unhealthy_count,
            },
            "results": results,
        }

        return summary

    async def check_single(self, checker_name: str) -> Optional[Dict[str, Any]]:
        """Run a single health check by name"""
        checker = next((c for c in self.checkers if c.name == checker_name), None)
        if not checker:
            return None

        result = await checker.check()
        self.last_results[result.name] = result
        return result.to_dict()

    def get_last_results(self) -> Dict[str, Any]:
        """Get last health check results"""
        if not self.last_results:
            return {
                "service": self.service_name,
                "status": "unknown",
                "message": "No health checks performed yet",
                "results": {},
            }

        results = {name: result.to_dict() for name, result in self.last_results.items()}

        # Determine overall status from last results
        statuses = [result.status for result in self.last_results.values()]
        if any(s == HealthStatus.UNHEALTHY for s in statuses):
            overall_status = HealthStatus.UNHEALTHY
        elif any(s == HealthStatus.DEGRADED for s in statuses):
            overall_status = HealthStatus.DEGRADED
        else:
            overall_status = HealthStatus.HEALTHY

        return {
            "service": self.service_name,
            "status": overall_status.value,
            "timestamp": datetime.utcnow().isoformat(),
            "results": results,
        }
