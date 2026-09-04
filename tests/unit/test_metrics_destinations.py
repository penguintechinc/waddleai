"""Tests for destination-failover metrics.

Tests the WaddleAIMetrics destination attempt, failover, breaker, and gate counters.
"""

from __future__ import annotations

from shared.utils.metrics import WaddleAIMetrics


def _value(metric, **labels):
    """Extract the current value of a metric with specific labels."""
    return metric.labels(**labels)._value.get()


def test_attempt_and_failover_and_gate_counters():
    """Test destination attempt, failover, and gate counters.

    WaddleAIMetrics is Borg-style: every instance shares the same
    process-wide Prometheus Counters. Other tests in the same pytest
    process increment these exact label combinations (dispatcher and
    failover-harness tests), so this asserts the DELTA each call
    produces rather than an absolute value.
    """
    m = WaddleAIMetrics("test-proxy")

    before_ok = _value(m.destination_attempts_total, provider_type="openai", outcome="ok")
    before_failed = _value(m.destination_attempts_total, provider_type="openai", outcome="failed")
    before_failover = _value(
        m.destination_failover_total,
        from_provider="openai",
        to_provider="anthropic",
        reason="server_error",
    )
    before_gate = _value(m.destination_gate_denied_total, reason="flag_off")

    m.record_destination_attempt("openai", "ok")
    m.record_destination_attempt("openai", "failed")
    m.record_destination_failover("openai", "anthropic", "server_error")
    m.record_destination_gate_denied("flag_off")

    assert (
        _value(m.destination_attempts_total, provider_type="openai", outcome="ok") - before_ok
        == 1.0
    )
    assert (
        _value(m.destination_attempts_total, provider_type="openai", outcome="failed")
        - before_failed
        == 1.0
    )
    assert (
        _value(
            m.destination_failover_total,
            from_provider="openai",
            to_provider="anthropic",
            reason="server_error",
        )
        - before_failover
        == 1.0
    )
    assert _value(m.destination_gate_denied_total, reason="flag_off") - before_gate == 1.0


def test_breaker_gauge_set():
    """Test destination breaker gauge set/clear."""
    m = WaddleAIMetrics("test-proxy")
    m.set_destination_breaker_open("42", True)
    assert _value(m.destination_breaker_open, destination_id="42") == 1.0
    m.set_destination_breaker_open("42", False)
    assert _value(m.destination_breaker_open, destination_id="42") == 0.0
