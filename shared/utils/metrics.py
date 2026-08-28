"""Prometheus metrics collection for WaddleAI.

Provides comprehensive metrics for proxy and management servers.
"""

import logging
import time

from prometheus_client import Counter, Gauge, Histogram, Info, generate_latest

logger = logging.getLogger(__name__)


class WaddleAIMetrics:
    """Centralized metrics collection for WaddleAI.

    Every Counter/Histogram/Gauge below is registered by name in
    prometheus_client's process-global default registry -- constructing a
    second `WaddleAIMetrics` instance (e.g. `get_proxy_metrics()` AND
    `get_management_metrics()` both being exercised in one process, as
    happens across the combined test suite) would re-register the exact
    same metric names and raise `ValueError: Duplicated timeseries`.
    `service`/`organization`/etc. are already carried as *label values* at
    record time, not baked into the metric identity, so the collectors
    themselves are safe to share process-wide: built once on the first
    instantiation and reused (not rebuilt) by every subsequent one,
    Borg-style. `service_name` itself stays per-instance so
    `record_request`'s `service=self.service_name` default still reflects
    whichever service actually constructed this instance.
    """

    _shared_collectors: dict[str, object] | None = None

    def __init__(self, service_name: str):
        """Bind this instance to `service_name`, reusing shared collectors after the first build."""
        self.service_name = service_name

        if WaddleAIMetrics._shared_collectors is not None:
            for name, collector in WaddleAIMetrics._shared_collectors.items():
                setattr(self, name, collector)
            self.info.info({"service": service_name, "version": "1.0.0", "python_version": "3.13"})
            return

        # Request metrics
        self.requests_total = Counter(
            "waddleai_requests_total",
            "Total number of requests",
            ["service", "endpoint", "method", "status_code"],
        )

        self.request_duration = Histogram(
            "waddleai_request_duration_seconds",
            "Request duration in seconds",
            ["service", "endpoint", "method"],
        )

        # LLM-specific metrics
        self.llm_requests_total = Counter(
            "waddleai_llm_requests_total",
            "Total LLM requests by provider and model",
            ["provider", "model", "status"],
        )

        self.llm_tokens_total = Counter(
            "waddleai_llm_tokens_total",
            "Total LLM tokens processed",
            ["provider", "model", "token_type"],
        )

        self.waddleai_tokens_total = Counter(
            "waddleai_normalized_tokens_total",
            "Total WaddleAI normalized tokens",
            ["organization", "user", "provider"],
        )

        # Security metrics
        self.security_events_total = Counter(
            "waddleai_security_events_total",
            "Total security events detected",
            ["event_type", "severity", "action"],
        )

        # Database metrics
        self.database_operations_total = Counter(
            "waddleai_database_operations_total",
            "Total database operations",
            ["operation", "table", "status"],
        )

        self.database_operation_duration = Histogram(
            "waddleai_database_operation_duration_seconds",
            "Database operation duration",
            ["operation", "table"],
        )

        # Connection pool metrics
        self.active_connections = Gauge(
            "waddleai_active_connections",
            "Number of active connections",
            ["service", "connection_type"],
        )

        # Authentication metrics
        self.auth_attempts_total = Counter(
            "waddleai_auth_attempts_total", "Total authentication attempts", ["auth_type", "status"]
        )

        # Provider health metrics
        self.provider_health = Gauge(
            "waddleai_provider_health",
            "Provider health status (1=healthy, 0=unhealthy)",
            ["provider", "endpoint"],
        )

        # Token quota metrics
        self.token_quota_usage = Gauge(
            "waddleai_token_quota_usage", "Token quota usage percentage", ["organization", "user"]
        )

        # Rate limiting metrics
        self.rate_limit_exceeded = Counter(
            "waddleai_rate_limit_exceeded_total",
            "Rate limit exceeded events",
            ["endpoint", "limit_type"],
        )

        # Response cache metrics (spec §6.4)
        self.cache_lookups_total = Counter(
            "waddleai_cache_lookups_total", "Cache lookups by layer and result", ["layer", "result"]
        )
        self.cache_tokens_saved_total = Counter(
            "waddleai_cache_tokens_saved_total", "Tokens saved by cache layer", ["layer"]
        )
        self.cache_entries_evicted_total = Counter(
            "waddleai_cache_entries_evicted_total", "Cache entries evicted (LRU/quota)", ["layer"]
        )

        # Agent-hooks metrics (§18) -- hooks sit on the developer's
        # interactive tool-call path, so the evaluation-latency histogram is
        # the number that matters (p50/p95/p99), and fail-open vs
        # fail-closed are counted on SEPARATE counters: a spike in fail-open
        # is a silent security degradation and must be independently
        # alertable, not buried inside a generic error counter.
        self.hook_invocations_total = Counter(
            "waddleai_hook_invocations_total",
            "Agent-hook evaluations by ecosystem/event/decision",
            ["ecosystem", "event", "decision"],
        )
        self.hook_evaluation_duration_seconds = Histogram(
            "waddleai_hook_evaluation_duration_seconds",
            "Server-side agent-hook evaluation latency (§18 evaluate endpoint)",
            ["ecosystem", "event"],
            # Tier-1/hook_rule decisions are pure in-memory checks (sub-ms to
            # low-ms); the default prometheus_client buckets bottom out at
            # 5ms, which would collapse the entire useful range into one
            # bucket and make p50/p95/p99 meaningless for exactly the case
            # this histogram exists to measure. Only Tier-2 (network round
            # trip) reaches into the tens/hundreds-of-ms tail.
            buckets=(
                0.0005,
                0.001,
                0.0025,
                0.005,
                0.01,
                0.025,
                0.05,
                0.1,
                0.25,
                0.5,
                1.0,
                2.5,
                5.0,
            ),
        )
        self.hook_timeouts_total = Counter(
            "waddleai_hook_timeouts_total", "Agent-hook Tier-2 evaluation timeouts", ["tier"]
        )
        self.hook_fail_mode_total = Counter(
            "waddleai_hook_fail_mode_total",
            "Agent-hook Tier-2 fail-open vs fail-closed events",
            ["mode"],
        )
        self.hook_tool_calls_total = Counter(
            "waddleai_hook_tool_calls_total",
            "Agent-hook tool-call volume by ecosystem/tool/org (efficiency + cache-hit analysis)",
            ["ecosystem", "tool_name", "organization"],
        )
        self.hook_rule_evaluations_total = Counter(
            "waddleai_hook_rule_evaluations_total",
            "Admin hook_rules matched (regardless of whether they won), by rule id and scope",
            ["rule_id", "scope"],
        )
        self.hook_rule_decisions_total = Counter(
            "waddleai_hook_rule_decisions_total",
            "Admin hook_rules that actually decided the outcome, by rule id/scope/decision",
            ["rule_id", "scope", "decision"],
        )

        # System info
        self.info = Info("waddleai_info", "WaddleAI service information")
        self.info.info({"service": service_name, "version": "1.0.0", "python_version": "3.13"})

        WaddleAIMetrics._shared_collectors = {
            k: v for k, v in vars(self).items() if k != "service_name"
        }

    def record_request(self, endpoint: str, method: str, status_code: int, duration: float):
        """Record HTTP request metrics."""
        self.requests_total.labels(
            service=self.service_name, endpoint=endpoint, method=method, status_code=status_code
        ).inc()

        self.request_duration.labels(
            service=self.service_name, endpoint=endpoint, method=method
        ).observe(duration)

    def record_llm_request(
        self, provider: str, model: str, status: str, token_usage: dict[str, int]
    ):
        """Record LLM request metrics."""
        self.llm_requests_total.labels(provider=provider, model=model, status=status).inc()

        # Record token usage
        if "input_tokens" in token_usage:
            self.llm_tokens_total.labels(
                provider=provider,
                model=model,
                token_type="input",  # nosec B106 # noqa: S106 -- label value, not a credential
            ).inc(token_usage["input_tokens"])

        if "output_tokens" in token_usage:
            self.llm_tokens_total.labels(
                provider=provider,
                model=model,
                token_type="output",  # nosec B106 # noqa: S106 -- label value, not a credential
            ).inc(token_usage["output_tokens"])

        if "waddleai_tokens" in token_usage:
            self.waddleai_tokens_total.labels(
                organization=token_usage.get("organization", "unknown"),
                user=token_usage.get("user", "unknown"),
                provider=provider,
            ).inc(token_usage["waddleai_tokens"])

    def record_security_event(self, event_type: str, severity: str, action: str):
        """Record security event."""
        self.security_events_total.labels(
            event_type=event_type, severity=severity, action=action
        ).inc()

    def record_database_operation(
        self, operation: str, table: str, duration: float | None = None, success: bool = True
    ):
        """Record database operation."""
        status = "success" if success else "error"

        self.database_operations_total.labels(operation=operation, table=table, status=status).inc()

        if duration is not None:
            self.database_operation_duration.labels(operation=operation, table=table).observe(
                duration
            )

    def set_active_connections(self, connection_type: str, count: int):
        """Set active connection count."""
        self.active_connections.labels(
            service=self.service_name, connection_type=connection_type
        ).set(count)

    def record_auth_attempt(self, auth_type: str, success: bool):
        """Record authentication attempt."""
        status = "success" if success else "failure"
        self.auth_attempts_total.labels(auth_type=auth_type, status=status).inc()

    def set_provider_health(self, provider: str, endpoint: str, healthy: bool):
        """Set provider health status."""
        self.provider_health.labels(provider=provider, endpoint=endpoint).set(1 if healthy else 0)

    def set_token_quota_usage(self, organization: str, user: str, usage_percentage: float):
        """Set token quota usage percentage."""
        self.token_quota_usage.labels(organization=organization, user=user).set(usage_percentage)

    def record_rate_limit_exceeded(self, endpoint: str, limit_type: str):
        """Record rate limit exceeded event."""
        self.rate_limit_exceeded.labels(endpoint=endpoint, limit_type=limit_type).inc()

    def record_cache_lookup(self, layer: str, result: str) -> None:
        """Record a response-cache lookup outcome (spec §6.4).

        layer: exact|semantic; result: hit|miss.
        """
        self.cache_lookups_total.labels(layer=layer, result=result).inc()

    def record_cache_tokens_saved(self, layer: str, tokens: int) -> None:
        """Record tokens saved by a cache hit on the given layer."""
        if tokens > 0:
            self.cache_tokens_saved_total.labels(layer=layer).inc(tokens)

    def record_cache_eviction(self, layer: str) -> None:
        """Record an LRU/quota eviction on the given cache layer."""
        self.cache_entries_evicted_total.labels(layer=layer).inc()

    def record_hook_invocation(self, ecosystem: str, event: str, decision: str) -> None:
        """Record one agent-hook evaluation outcome (§18)."""
        self.hook_invocations_total.labels(
            ecosystem=ecosystem, event=event, decision=decision
        ).inc()

    def observe_hook_evaluation_duration(self, ecosystem: str, event: str, seconds: float) -> None:
        """Record server-side agent-hook evaluation latency -- the critical-path number."""
        self.hook_evaluation_duration_seconds.labels(ecosystem=ecosystem, event=event).observe(
            seconds
        )

    def record_hook_timeout(self, tier: str) -> None:
        """Record an agent-hook evaluation tier timing out (e.g. Tier-2 remote eval)."""
        self.hook_timeouts_total.labels(tier=tier).inc()

    def record_hook_fail_mode(self, mode: str) -> None:
        """Record a Tier-2 fail-open/fail-closed event. `mode`: fail_open|fail_closed."""
        self.hook_fail_mode_total.labels(mode=mode).inc()

    def record_hook_tool_call(self, ecosystem: str, tool_name: str, organization: str) -> None:
        """Record one hook-covered tool call for volume/cache-hit-potential analysis."""
        self.hook_tool_calls_total.labels(
            ecosystem=ecosystem, tool_name=tool_name, organization=organization
        ).inc()

    def record_hook_rule_evaluation(self, rule_id: str, scope: str) -> None:
        """Record an admin hook_rule matching an event (whether or not it won)."""
        self.hook_rule_evaluations_total.labels(rule_id=rule_id, scope=scope).inc()

    def record_hook_rule_decision(self, rule_id: str, scope: str, decision: str) -> None:
        """Record an admin hook_rule actually deciding an event's outcome."""
        self.hook_rule_decisions_total.labels(rule_id=rule_id, scope=scope, decision=decision).inc()

    def get_metrics(self) -> str:
        """Get Prometheus metrics in text format."""
        return generate_latest().decode("utf-8")


class MetricsMiddleware:
    """Middleware for automatic metrics collection."""

    def __init__(self, metrics: WaddleAIMetrics):
        """Bind the WaddleAIMetrics collector this middleware records into."""
        self.metrics = metrics

    def __call__(self, request, response, start_time: float):
        """Record request metrics."""
        duration = time.time() - start_time
        endpoint = getattr(request, "url", {}).path if hasattr(request, "url") else "unknown"
        method = getattr(request, "method", "unknown")
        status_code = getattr(response, "status_code", 0)

        self.metrics.record_request(endpoint, method, status_code, duration)


# Global metrics instances
proxy_metrics: WaddleAIMetrics | None = None
management_metrics: WaddleAIMetrics | None = None


def get_proxy_metrics() -> WaddleAIMetrics:
    """Get or create proxy metrics instance."""
    global proxy_metrics
    if proxy_metrics is None:
        proxy_metrics = WaddleAIMetrics("proxy")
    return proxy_metrics


def get_management_metrics() -> WaddleAIMetrics:
    """Get or create management metrics instance."""
    global management_metrics
    if management_metrics is None:
        management_metrics = WaddleAIMetrics("management")
    return management_metrics


def get_metrics_for_service(service_name: str) -> WaddleAIMetrics:
    """Get metrics instance for a service."""
    if service_name == "proxy":
        return get_proxy_metrics()
    elif service_name == "management":
        return get_management_metrics()
    else:
        return WaddleAIMetrics(service_name)
