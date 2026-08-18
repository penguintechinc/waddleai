"""Prometheus response-cache metrics (spec §6.4)."""

from shared.utils.metrics import get_proxy_metrics


def _counter_value(counter, **labels) -> float:
    """Read a Prometheus counter's current value for a given label set."""
    return counter.labels(**labels)._value.get()


class TestCacheMetrics:
    """Tests for cache metrics."""

    def test_record_cache_lookup_increments_hit_and_miss_independently(self):
        """Record cache lookup increments hit and miss independently."""
        metrics = get_proxy_metrics()
        before_hit = _counter_value(metrics.cache_lookups_total, layer="exact", result="hit")
        before_miss = _counter_value(metrics.cache_lookups_total, layer="exact", result="miss")

        metrics.record_cache_lookup(layer="exact", result="hit")
        metrics.record_cache_lookup(layer="exact", result="miss")
        metrics.record_cache_lookup(layer="exact", result="miss")

        hit_value = _counter_value(metrics.cache_lookups_total, layer="exact", result="hit")
        miss_value = _counter_value(metrics.cache_lookups_total, layer="exact", result="miss")
        assert hit_value == before_hit + 1
        assert miss_value == before_miss + 2

    def test_record_cache_tokens_saved_accumulates(self):
        """Record cache tokens saved accumulates."""
        metrics = get_proxy_metrics()
        before = _counter_value(metrics.cache_tokens_saved_total, layer="semantic")

        metrics.record_cache_tokens_saved(layer="semantic", tokens=150)
        metrics.record_cache_tokens_saved(layer="semantic", tokens=50)

        assert _counter_value(metrics.cache_tokens_saved_total, layer="semantic") == before + 200

    def test_record_cache_tokens_saved_zero_is_noop(self):
        """Record cache tokens saved zero is noop."""
        metrics = get_proxy_metrics()
        before = _counter_value(metrics.cache_tokens_saved_total, layer="exact")

        metrics.record_cache_tokens_saved(layer="exact", tokens=0)

        assert _counter_value(metrics.cache_tokens_saved_total, layer="exact") == before

    def test_record_cache_eviction_increments(self):
        """Record cache eviction increments."""
        metrics = get_proxy_metrics()
        before = _counter_value(metrics.cache_entries_evicted_total, layer="exact")

        metrics.record_cache_eviction(layer="exact")

        assert _counter_value(metrics.cache_entries_evicted_total, layer="exact") == before + 1
