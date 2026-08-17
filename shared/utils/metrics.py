"""
Prometheus metrics collection for WaddleAI
Provides comprehensive metrics for proxy and management servers
"""

import logging
import time
from typing import Dict, Optional

from prometheus_client import Counter, Gauge, Histogram, Info, generate_latest

logger = logging.getLogger(__name__)


class WaddleAIMetrics:
    """Centralized metrics collection for WaddleAI"""

    def __init__(self, service_name: str):
        self.service_name = service_name

        # Request metrics
        self.requests_total = Counter(
            "waddleai_requests_total", "Total number of requests", ["service", "endpoint", "method", "status_code"]
        )

        self.request_duration = Histogram(
            "waddleai_request_duration_seconds", "Request duration in seconds", ["service", "endpoint", "method"]
        )

        # LLM-specific metrics
        self.llm_requests_total = Counter(
            "waddleai_llm_requests_total", "Total LLM requests by provider and model", ["provider", "model", "status"]
        )

        self.llm_tokens_total = Counter(
            "waddleai_llm_tokens_total", "Total LLM tokens processed", ["provider", "model", "token_type"]
        )

        self.waddleai_tokens_total = Counter(
            "waddleai_normalized_tokens_total", "Total WaddleAI normalized tokens", ["organization", "user", "provider"]
        )

        # Security metrics
        self.security_events_total = Counter(
            "waddleai_security_events_total", "Total security events detected", ["event_type", "severity", "action"]
        )

        # Database metrics
        self.database_operations_total = Counter(
            "waddleai_database_operations_total", "Total database operations", ["operation", "table", "status"]
        )

        self.database_operation_duration = Histogram(
            "waddleai_database_operation_duration_seconds", "Database operation duration", ["operation", "table"]
        )

        # Connection pool metrics
        self.active_connections = Gauge(
            "waddleai_active_connections", "Number of active connections", ["service", "connection_type"]
        )

        # Authentication metrics
        self.auth_attempts_total = Counter(
            "waddleai_auth_attempts_total", "Total authentication attempts", ["auth_type", "status"]
        )

        # Provider health metrics
        self.provider_health = Gauge(
            "waddleai_provider_health", "Provider health status (1=healthy, 0=unhealthy)", ["provider", "endpoint"]
        )

        # Token quota metrics
        self.token_quota_usage = Gauge(
            "waddleai_token_quota_usage", "Token quota usage percentage", ["organization", "user"]
        )

        # Rate limiting metrics
        self.rate_limit_exceeded = Counter(
            "waddleai_rate_limit_exceeded_total", "Rate limit exceeded events", ["endpoint", "limit_type"]
        )

        # System info
        self.info = Info("waddleai_info", "WaddleAI service information")
        self.info.info({"service": service_name, "version": "1.0.0", "python_version": "3.13"})

    def record_request(self, endpoint: str, method: str, status_code: int, duration: float):
        """Record HTTP request metrics"""
        self.requests_total.labels(
            service=self.service_name, endpoint=endpoint, method=method, status_code=status_code
        ).inc()

        self.request_duration.labels(service=self.service_name, endpoint=endpoint, method=method).observe(duration)

    def record_llm_request(self, provider: str, model: str, status: str, token_usage: Dict[str, int]):
        """Record LLM request metrics"""
        self.llm_requests_total.labels(provider=provider, model=model, status=status).inc()

        # Record token usage
        if "input_tokens" in token_usage:
            self.llm_tokens_total.labels(
                provider=provider, model=model, token_type="input"  # nosec B106 -- Prometheus metric label value, not a credential
            ).inc(token_usage["input_tokens"])

        if "output_tokens" in token_usage:
            self.llm_tokens_total.labels(
                provider=provider, model=model, token_type="output"  # nosec B106 -- Prometheus metric label value, not a credential
            ).inc(token_usage["output_tokens"])

        if "waddleai_tokens" in token_usage:
            self.waddleai_tokens_total.labels(
                organization=token_usage.get("organization", "unknown"),
                user=token_usage.get("user", "unknown"),
                provider=provider,
            ).inc(token_usage["waddleai_tokens"])

    def record_security_event(self, event_type: str, severity: str, action: str):
        """Record security event"""
        self.security_events_total.labels(event_type=event_type, severity=severity, action=action).inc()

    def record_database_operation(
        self, operation: str, table: str, duration: Optional[float] = None, success: bool = True
    ):
        """Record database operation"""
        status = "success" if success else "error"

        self.database_operations_total.labels(operation=operation, table=table, status=status).inc()

        if duration is not None:
            self.database_operation_duration.labels(operation=operation, table=table).observe(duration)

    def set_active_connections(self, connection_type: str, count: int):
        """Set active connection count"""
        self.active_connections.labels(service=self.service_name, connection_type=connection_type).set(count)

    def record_auth_attempt(self, auth_type: str, success: bool):
        """Record authentication attempt"""
        status = "success" if success else "failure"
        self.auth_attempts_total.labels(auth_type=auth_type, status=status).inc()

    def set_provider_health(self, provider: str, endpoint: str, healthy: bool):
        """Set provider health status"""
        self.provider_health.labels(provider=provider, endpoint=endpoint).set(1 if healthy else 0)

    def set_token_quota_usage(self, organization: str, user: str, usage_percentage: float):
        """Set token quota usage percentage"""
        self.token_quota_usage.labels(organization=organization, user=user).set(usage_percentage)

    def record_rate_limit_exceeded(self, endpoint: str, limit_type: str):
        """Record rate limit exceeded event"""
        self.rate_limit_exceeded.labels(endpoint=endpoint, limit_type=limit_type).inc()

    def get_metrics(self) -> str:
        """Get Prometheus metrics in text format"""
        return generate_latest().decode("utf-8")


class MetricsMiddleware:
    """Middleware for automatic metrics collection"""

    def __init__(self, metrics: WaddleAIMetrics):
        self.metrics = metrics

    def __call__(self, request, response, start_time: float):
        """Record request metrics"""
        duration = time.time() - start_time
        endpoint = getattr(request, "url", {}).path if hasattr(request, "url") else "unknown"
        method = getattr(request, "method", "unknown")
        status_code = getattr(response, "status_code", 0)

        self.metrics.record_request(endpoint, method, status_code, duration)


# Global metrics instances
proxy_metrics: Optional[WaddleAIMetrics] = None
management_metrics: Optional[WaddleAIMetrics] = None


def get_proxy_metrics() -> WaddleAIMetrics:
    """Get or create proxy metrics instance"""
    global proxy_metrics
    if proxy_metrics is None:
        proxy_metrics = WaddleAIMetrics("proxy")
    return proxy_metrics


def get_management_metrics() -> WaddleAIMetrics:
    """Get or create management metrics instance"""
    global management_metrics
    if management_metrics is None:
        management_metrics = WaddleAIMetrics("management")
    return management_metrics


def get_metrics_for_service(service_name: str) -> WaddleAIMetrics:
    """Get metrics instance for a service"""
    if service_name == "proxy":
        return get_proxy_metrics()
    elif service_name == "management":
        return get_management_metrics()
    else:
        return WaddleAIMetrics(service_name)
