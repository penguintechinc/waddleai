"""Unit tests for the release-audit-2026-10-02 (ops O1-a/O1-c/O10) additions.

Covers ``shared/utils/metrics.py``: the unbounded-label guard, the upstream
LLM/provider latency histogram, and the proxy ConcurrencyLimiter
observability (in-flight gauge + rejection counter).
"""

import time as time_module

from shared.utils import metrics as metrics_module
from shared.utils.metrics import _sanitize_label, get_proxy_metrics


def _counter_value(counter, **labels) -> float:
    """Read a Prometheus counter's current value for a given label set."""
    return counter.labels(**labels)._value.get()


def _histogram_sum(histogram, **labels) -> float:
    """Read a Prometheus histogram's accumulated `_sum` for a given label set."""
    return histogram.labels(**labels)._sum.get()


def _gauge_value(gauge, **labels) -> float:
    """Read a Prometheus gauge's current value for a given label set."""
    return gauge.labels(**labels)._value.get()


class TestSanitizeLabel:
    """`_sanitize_label` -- the O1-a defensive id/UUID-in-label-value guard."""

    def test_route_template_passes_through_unchanged(self):
        """A proper route template (the intended input) is never rewritten."""
        assert _sanitize_label("/memories/<memory_id>") == "/memories/<memory_id>"

    def test_unmatched_literal_passes_through_unchanged(self):
        """The bounded 404 fallback literal is never rewritten."""
        assert _sanitize_label("unmatched") == "unmatched"

    def test_uuid_segment_is_replaced(self):
        """A concrete UUID path segment is rewritten to `<id>`."""
        sanitized = _sanitize_label("/memories/3fa85f64-5717-4562-b3fc-2c963f66afa6")
        assert sanitized == "/memories/<id>"

    def test_numeric_id_segment_is_replaced(self):
        """A concrete numeric id path segment is rewritten to `<id>`."""
        assert _sanitize_label("/api/usage/12345") == "/api/usage/<id>"

    def test_single_digit_segment_is_not_rewritten(self):
        """A single-digit segment (e.g. an API version) is left alone.

        Only 2+ digit runs are treated as an id, since `/v1/...` must stay intact.
        """
        assert _sanitize_label("/v1/models") == "/v1/models"

    def test_warns_once_on_first_sanitization(self, monkeypatch):
        """The WARN log fires exactly once across repeated unbounded-label hits."""
        monkeypatch.setattr(metrics_module, "_unbounded_label_warned", False)
        warnings = []
        monkeypatch.setattr(
            metrics_module.logger, "warning", lambda *a, **kw: warnings.append((a, kw))
        )

        _sanitize_label("/memories/11111")
        _sanitize_label("/memories/22222")

        assert len(warnings) == 1


class TestLlmLatencyHistogram:
    """`record_llm_latency` -- the O1-c upstream LLM/provider call duration histogram."""

    def test_records_duration_for_given_labels(self):
        """observe() accumulates into the histogram's `_sum` for (provider, model, status)."""
        m = get_proxy_metrics()
        before = _histogram_sum(
            m.llm_request_duration, provider="ollama", model="gemma4", status="success"
        )

        m.record_llm_latency(provider="ollama", model="gemma4", status="success", duration=1.5)

        after = _histogram_sum(
            m.llm_request_duration, provider="ollama", model="gemma4", status="success"
        )
        assert after == before + 1.5

    def test_success_and_error_status_are_independent_series(self):
        """A failed upstream call's latency never pollutes the success series."""
        m = get_proxy_metrics()
        before_ok = _histogram_sum(
            m.llm_request_duration, provider="anthropic", model="claude-x", status="success"
        )
        before_err = _histogram_sum(
            m.llm_request_duration, provider="anthropic", model="claude-x", status="error"
        )

        m.record_llm_latency(provider="anthropic", model="claude-x", status="error", duration=2.0)

        assert (
            _histogram_sum(
                m.llm_request_duration, provider="anthropic", model="claude-x", status="success"
            )
            == before_ok
        )
        assert (
            _histogram_sum(
                m.llm_request_duration, provider="anthropic", model="claude-x", status="error"
            )
            == before_err + 2.0
        )


class TestConcurrencyObservability:
    """`set_inflight_requests`/`record_concurrency_rejection` -- O10 limiter observability."""

    def test_set_inflight_requests_reports_current_count(self):
        """The gauge reflects whatever count the caller last reported."""
        m = get_proxy_metrics()
        m.set_inflight_requests(7)
        assert _gauge_value(m.proxy_inflight_requests, service=m.service_name) == 7
        m.set_inflight_requests(0)
        assert _gauge_value(m.proxy_inflight_requests, service=m.service_name) == 0

    def test_record_concurrency_rejection_increments_per_endpoint(self):
        """Rejections are counted independently per endpoint literal."""
        m = get_proxy_metrics()
        before = _counter_value(
            m.proxy_concurrency_rejections_total, endpoint="/v1/chat/completions"
        )

        m.record_concurrency_rejection(endpoint="/v1/chat/completions")
        m.record_concurrency_rejection(endpoint="/v1/chat/completions")

        after = _counter_value(
            m.proxy_concurrency_rejections_total, endpoint="/v1/chat/completions"
        )
        assert after == before + 2


class TestTokenQuotaUsageSetter:
    """`set_token_quota_usage` -- defined but not yet wired into the pipeline.

    Quota computation itself is currently a mocked/inert stand-in
    (`TokenBudgetStage`'s "mock scenario; in production fetch from DB/config"
    comment) pending the budget/meter work tracked separately
    (gh-212/gh-217) -- this proves the gauge itself works so it is ready to
    wire in once real quota numbers exist, per the release-audit note not to
    delete unused-but-correct instrumentation.
    """

    def test_set_token_quota_usage_reports_percentage(self):
        """observe() writes straight through to the gauge for the given labels."""
        m = get_proxy_metrics()
        m.set_token_quota_usage(organization="org-1", user="user-1", usage_percentage=42.5)
        assert _gauge_value(m.token_quota_usage, organization="org-1", user="user-1") == 42.5


class TestPreExistingMetricsMethodsCoverage:
    """Direct coverage for the rest of `WaddleAIMetrics`/`MetricsMiddleware`.

    These predate the release-audit-2026-10-02 change but weren't
    independently unit-tested anywhere in the suite; touching this module
    means bringing the whole file to the required coverage bar rather than
    leaving the gap for the next person to trip over.
    """

    def test_shared_collector_reuse_across_service_instances(self):
        """A second WaddleAIMetrics instance (e.g. management) reuses the first's collectors.

        Borg-style sharing (see the class docstring) means `proxy_metrics`
        and `management_metrics` are different Python objects with the same
        underlying Counter/Histogram/Gauge collectors.
        """
        proxy = get_proxy_metrics()
        mgmt = metrics_module.get_management_metrics()
        assert mgmt is not proxy
        assert mgmt.requests_total is proxy.requests_total
        assert mgmt.service_name == "management"

    def test_get_metrics_for_service_dispatches_by_name(self):
        """get_metrics_for_service() routes to the matching singleton, or builds a fresh one."""
        assert metrics_module.get_metrics_for_service("proxy") is get_proxy_metrics()
        assert (
            metrics_module.get_metrics_for_service("management")
            is metrics_module.get_management_metrics()
        )
        other = metrics_module.get_metrics_for_service("some-other-service")
        assert other.service_name == "some-other-service"

    def test_record_llm_request_records_all_token_usage_branches(self):
        """input_tokens/output_tokens/waddleai_tokens are each independently optional."""
        m = get_proxy_metrics()
        before_in = _counter_value(
            m.llm_tokens_total,
            provider="p",
            model="m",
            token_type="input",  # nosec B106 # noqa: S106 -- label value, not a credential
        )
        before_out = _counter_value(
            m.llm_tokens_total,
            provider="p",
            model="m",
            token_type="output",  # nosec B106 # noqa: S106 -- label value, not a credential
        )
        before_norm = _counter_value(
            m.waddleai_tokens_total, organization="org", user="unknown", provider="p"
        )

        m.record_llm_request(
            provider="p",
            model="m",
            status="success",
            token_usage={
                "input_tokens": 3,
                "output_tokens": 5,
                "waddleai_tokens": 7,
                "organization": "org",
            },
        )

        assert (
            _counter_value(
                m.llm_tokens_total,
                provider="p",
                model="m",
                token_type="input",  # nosec B106 # noqa: S106
            )
            == before_in + 3
        )
        assert (
            _counter_value(
                m.llm_tokens_total,
                provider="p",
                model="m",
                token_type="output",  # nosec B106 # noqa: S106
            )
            == before_out + 5
        )
        assert (
            _counter_value(
                m.waddleai_tokens_total, organization="org", user="unknown", provider="p"
            )
            == before_norm + 7
        )

    def test_record_security_event(self):
        """record_security_event() increments the bounded (event_type, severity, action) counter."""
        m = get_proxy_metrics()
        before = _counter_value(
            m.security_events_total, event_type="pii", severity="high", action="block"
        )
        m.record_security_event(event_type="pii", severity="high", action="block")
        assert (
            _counter_value(
                m.security_events_total, event_type="pii", severity="high", action="block"
            )
            == before + 1
        )

    def test_record_database_operation_with_and_without_duration(self):
        """Duration is optional; the histogram is only observed when one is given."""
        m = get_proxy_metrics()
        before_total = _counter_value(
            m.database_operations_total, operation="select", table="users", status="success"
        )
        before_dur = _histogram_sum(
            m.database_operation_duration, operation="select", table="users"
        )

        m.record_database_operation(operation="select", table="users", duration=0.01, success=True)
        m.record_database_operation(operation="select", table="users", success=False)

        assert (
            _counter_value(
                m.database_operations_total, operation="select", table="users", status="success"
            )
            == before_total + 1
        )
        assert (
            _counter_value(
                m.database_operations_total, operation="select", table="users", status="error"
            )
            >= 1
        )
        assert (
            _histogram_sum(m.database_operation_duration, operation="select", table="users")
            == before_dur + 0.01
        )

    def test_set_active_connections(self):
        """set_active_connections() reports the current count for a connection type."""
        m = get_proxy_metrics()
        m.set_active_connections("requests_in_flight", 3)
        assert (
            _gauge_value(
                m.active_connections, service=m.service_name, connection_type="requests_in_flight"
            )
            == 3
        )

    def test_record_auth_attempt_success_and_failure(self):
        """Success and failure are counted on independent series for the same auth_type."""
        m = get_proxy_metrics()
        before_ok = _counter_value(m.auth_attempts_total, auth_type="jwt", status="success")
        before_fail = _counter_value(m.auth_attempts_total, auth_type="jwt", status="failure")
        m.record_auth_attempt("jwt", True)
        m.record_auth_attempt("jwt", False)
        assert (
            _counter_value(m.auth_attempts_total, auth_type="jwt", status="success")
            == before_ok + 1
        )
        assert (
            _counter_value(m.auth_attempts_total, auth_type="jwt", status="failure")
            == before_fail + 1
        )

    def test_set_provider_health(self):
        """set_provider_health() maps healthy/unhealthy to 1/0 on the gauge."""
        m = get_proxy_metrics()
        m.set_provider_health("ollama", "http://ollama:11434", True)
        assert (
            _gauge_value(m.provider_health, provider="ollama", endpoint="http://ollama:11434") == 1
        )
        m.set_provider_health("ollama", "http://ollama:11434", False)
        assert (
            _gauge_value(m.provider_health, provider="ollama", endpoint="http://ollama:11434") == 0
        )

    def test_record_rate_limit_exceeded(self):
        """record_rate_limit_exceeded() increments the bounded (endpoint, limit_type) counter."""
        m = get_proxy_metrics()
        before = _counter_value(
            m.rate_limit_exceeded, endpoint="/v1/chat/completions", limit_type="rpm"
        )
        m.record_rate_limit_exceeded(endpoint="/v1/chat/completions", limit_type="rpm")
        assert (
            _counter_value(m.rate_limit_exceeded, endpoint="/v1/chat/completions", limit_type="rpm")
            == before + 1
        )

    def test_hook_metrics_methods(self):
        """Each §18 agent-hooks recorder writes to its own bounded-label collector."""
        m = get_proxy_metrics()
        m.record_hook_invocation(ecosystem="claude-code", event="pre_tool", decision="allow")
        m.observe_hook_evaluation_duration(ecosystem="claude-code", event="pre_tool", seconds=0.002)
        m.record_hook_timeout(tier="tier2")
        m.record_hook_fail_mode(mode="fail_open")
        m.record_hook_tool_call(ecosystem="claude-code", tool_name="bash", organization="org-1")
        m.record_hook_rule_evaluation(rule_id="r1", scope="org")
        m.record_hook_rule_decision(rule_id="r1", scope="org", decision="block")

        assert (
            _counter_value(
                m.hook_invocations_total,
                ecosystem="claude-code",
                event="pre_tool",
                decision="allow",
            )
            >= 1
        )
        assert (
            _histogram_sum(
                m.hook_evaluation_duration_seconds, ecosystem="claude-code", event="pre_tool"
            )
            >= 0.002
        )
        assert _counter_value(m.hook_timeouts_total, tier="tier2") >= 1
        assert _counter_value(m.hook_fail_mode_total, mode="fail_open") >= 1
        assert (
            _counter_value(
                m.hook_tool_calls_total,
                ecosystem="claude-code",
                tool_name="bash",
                organization="org-1",
            )
            >= 1
        )
        assert _counter_value(m.hook_rule_evaluations_total, rule_id="r1", scope="org") >= 1
        assert (
            _counter_value(m.hook_rule_decisions_total, rule_id="r1", scope="org", decision="block")
            >= 1
        )

    def test_get_metrics_returns_prometheus_text_format(self):
        """get_metrics() renders the process-wide registry as Prometheus text exposition."""
        m = get_proxy_metrics()
        text = m.get_metrics()
        assert "waddleai_requests_total" in text

    def test_metrics_middleware_records_from_request_response_objects(self):
        """MetricsMiddleware.__call__ extracts endpoint/method/status from duck-typed objects."""
        from shared.utils.metrics import MetricsMiddleware

        m = get_proxy_metrics()
        recorded = []
        monkeypatch_target = m.record_request

        def spy(endpoint, method, status_code, duration):
            recorded.append((endpoint, method, status_code))

        m.record_request = spy
        try:
            middleware = MetricsMiddleware(m)

            class _URL:
                path = "/v1/models"

            class _Request:
                url = _URL()
                method = "GET"

            class _Response:
                status_code = 200

            middleware(_Request(), _Response(), start_time=time_module.time())
        finally:
            m.record_request = monkeypatch_target

        assert recorded == [("/v1/models", "GET", 200)]

    def test_metrics_middleware_falls_back_when_request_has_no_url(self):
        """No `.url` attribute on the request object -> endpoint defaults to "unknown"."""
        from shared.utils.metrics import MetricsMiddleware

        m = get_proxy_metrics()
        recorded = []
        original = m.record_request
        m.record_request = lambda endpoint, method, status_code, duration: recorded.append(endpoint)
        try:
            middleware = MetricsMiddleware(m)
            middleware(object(), object(), start_time=time_module.time())
        finally:
            m.record_request = original

        assert recorded == ["unknown"]
