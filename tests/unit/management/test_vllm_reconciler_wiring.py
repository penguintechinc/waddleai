"""Plan C integration: the reconciler's hooks into the proxy stream's metrics/throughput/routing."""

from __future__ import annotations

from typing import Any

import pytest

from services.management.app.services import vllm_reconciler as mod
from services.management.app.services.vllm_reconciler import SampleUsageSource
from shared.routing.prefer_local import create_prefer_local_rule
from shared.routing.providers import LOCAL_PROVIDERS
from tests.unit.management.test_vllm_reconciler import (
    FakeUsage,
    _offer,
    _reconciler,
    _tick,
    env,  # noqa: F401 -- fixture
)
from tests.unit.management.vllm_harness import seed_backend, seed_deployment


def test_metrics_hooks_called_each_tick(env, monkeypatch) -> None:  # noqa: F811
    """Inventory records and per-status deployment counts reach the metrics hooks."""
    inventory: list[Any] = []
    counts: list[dict[int, int]] = []
    monkeypatch.setattr(mod, "record_inventory", inventory.append)
    monkeypatch.setattr(mod, "record_deployment_counts", counts.append)
    b = seed_backend(env)
    seed_deployment(env, b, name="a", status=5)
    seed_deployment(env, b, name="b", status=5)
    seed_deployment(env, b, name="c", status=8)
    rec, _, _ = _reconciler(env)
    _tick(rec)
    assert len(inventory) == 1 and isinstance(inventory[0], list)
    assert counts[-1] == {5: 2, 8: 1}


def test_auto_recompute_passes_per_replica_estimate(env) -> None:  # noqa: F811
    """The throughput lookup gets the GPU-class per-replica estimate as its sanity bound."""
    b = seed_backend(env)
    seed_deployment(env, b, status=5, price_mode=1)
    usage = FakeUsage(None)
    rec, _, _ = _reconciler(env, usage_source=usage)
    _tick(rec)
    assert usage.estimates and usage.estimates[0] is not None and usage.estimates[0] > 0


def test_default_usage_source_reads_real_samples(env, monkeypatch) -> None:  # noqa: F811
    """With no injected source the daily recompute calls observed_tokens_per_s over 24h."""
    calls: list[dict[str, Any]] = []

    def fake(db, kind, dep_id, window_hours=24, estimate_aggregate_tokens_per_s=None):
        calls.append({"window": window_hours, "estimate": estimate_aggregate_tokens_per_s})
        return None

    monkeypatch.setattr(
        "services.management.app.services.vllm_reconciler.observed_aggregate_tokens_per_s", fake
    )
    b = seed_backend(env)
    dep = seed_deployment(env, b, status=5, price_mode=1)
    rec, _, _ = _reconciler(env)
    assert isinstance(rec.usage_source, SampleUsageSource)
    _tick(rec)
    assert calls and calls[0]["window"] == 24 and calls[0]["estimate"]
    row = env(env.vllm_deployments.id == dep.id).select().first()
    assert row.price_breakdown["throughput_source"] == "estimate"  # None keeps the estimate


def test_observed_value_overrides_estimate(env, monkeypatch) -> None:  # noqa: F811
    """A trustworthy observation becomes the price's throughput source."""
    monkeypatch.setattr(
        "services.management.app.services.vllm_reconciler.observed_aggregate_tokens_per_s",
        lambda *a, **k: 33.0,
    )
    b = seed_backend(env)
    dep = seed_deployment(env, b, status=5, price_mode=1)
    rec, _, _ = _reconciler(env)
    _tick(rec)
    row = env(env.vllm_deployments.id == dep.id).select().first()
    assert float(row.observed_tokens_per_s) == 33.0


def test_sample_prune_runs_once_per_day(env, monkeypatch) -> None:  # noqa: F811
    """prune_generation_samples(72h) fires on the first tick then only after a full day."""
    pruned: list[int] = []

    def fake(db, older_than_hours=72):
        pruned.append(older_than_hours)
        return 3

    monkeypatch.setattr(mod, "prune_generation_samples", fake)
    seed_backend(env)
    rec, _, clock = _reconciler(env)
    assert _tick(rec)["samples_pruned"] == 3
    clock.now = 86399
    assert _tick(rec)["samples_pruned"] == 0
    clock.now = 86400
    assert _tick(rec)["samples_pruned"] == 3
    assert pruned == [72, 72]


def test_prune_failure_counted_not_fatal(env, monkeypatch) -> None:  # noqa: F811
    """A failing prune is an error count, never a crashed tick."""

    def boom(db, older_than_hours=72):
        raise RuntimeError("db down")

    monkeypatch.setattr(mod, "prune_generation_samples", boom)
    seed_backend(env)
    rec, _, _ = _reconciler(env)
    assert _tick(rec)["reconcile_errors"] == 1


def test_default_prefer_local_factory_is_the_real_one_and_writes_a_rule(env) -> None:  # noqa: F811
    """No injected factory: the reconciler creates the real routing rule exactly once."""
    b = seed_backend(env)
    dep = seed_deployment(
        env,
        b,
        status=5,
        prefer_local={"fallback_model": "gpt-4o", "match": {"model_prefix": "model"}},
    )
    _offer(env, ["openai"], name="gpt-4o", enabled=True)
    rec, _, clock = _reconciler(env)
    assert rec.prefer_local_fn is create_prefer_local_rule
    _tick(rec)
    clock.now = 100
    _tick(rec)
    row = env(env.vllm_deployments.id == dep.id).select().first()
    rule = env(env.routing_rules_v2.id == row.prefer_local["rule_id"]).select().first()
    assert rule.name == f"prefer-local:{dep.slug}"
    assert env(env.routing_rules_v2.id > 0).count() == 1


def test_local_providers_is_the_shared_constant() -> None:
    """No module keeps its own provider literal."""
    from shared.fleet import vllm_pricing

    assert mod.LOCAL_PROVIDERS is LOCAL_PROVIDERS
    assert vllm_pricing.LOCAL_PROVIDERS is LOCAL_PROVIDERS


@pytest.mark.parametrize("outcome", ["placed", "no_fit"])
def test_placement_outcomes_are_valid_metric_labels(outcome) -> None:
    """The outcomes the create route emits are accepted by record_placement."""
    from shared.observability.vllm_metrics import record_placement

    record_placement(outcome)
