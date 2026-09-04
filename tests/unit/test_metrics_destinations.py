"""Tests for destination-failover metrics.

Tests the WaddleAIMetrics destination attempt, failover, breaker, and gate counters.
"""

from __future__ import annotations

from shared.utils.metrics import WaddleAIMetrics


def _value(metric, **labels):
    """Extract the current value of a metric with specific labels."""
    return metric.labels(**labels)._value.get()


def test_attempt_and_failover_and_gate_counters():
    """Test destination attempt, failover, and gate counters."""
    m = WaddleAIMetrics("test-proxy")
    m.record_destination_attempt("openai", "ok")
    m.record_destination_attempt("openai", "failed")
    m.record_destination_failover("openai", "anthropic", "server_error")
    m.record_destination_gate_denied("flag_off")
    assert _value(m.destination_attempts_total, provider_type="openai", outcome="ok") == 1.0
    assert (
        _value(
            m.destination_failover_total,
            from_provider="openai",
            to_provider="anthropic",
            reason="server_error",
        )
        == 1.0
    )
    assert _value(m.destination_gate_denied_total, reason="flag_off") == 1.0


def test_breaker_gauge_set():
    """Test destination breaker gauge set/clear."""
    m = WaddleAIMetrics("test-proxy")
    m.set_destination_breaker_open("42", True)
    assert _value(m.destination_breaker_open, destination_id="42") == 1.0
    m.set_destination_breaker_open("42", False)
    assert _value(m.destination_breaker_open, destination_id="42") == 0.0
