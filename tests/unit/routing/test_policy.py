"""Org routing-policy resolution + filter/sort fallback-chain tests (spec §7.1, §7.3)."""

import pytest

from shared.routing.capability import ModelOffer
from shared.routing.policy import PolicyResolver, RoutingPolicyConfig, filter_and_sort


def _policy_row(org_id, **overrides):
    row = {
        "id": 1,
        "organization_id": org_id,
        "mode": "local_first",
        "escalation_threshold": 3,
        "escalation_target": None,
        "classifier_prompt": None,
        "de_escalation": "idle_reset",
        "idle_reset_minutes": 10,
        "sensitivity_routing": "local_only",
        "budget_pressure_enabled": True,
        "provider_failover": "off",
    }
    row.update(overrides)
    return row


class TestPolicyResolver:
    """resolve() default fallback + row resolution + caching."""

    @pytest.mark.asyncio
    async def test_no_row_returns_defaults(self, fake_db):
        """A missing routing_policies row resolves to engine defaults."""
        resolver = PolicyResolver(fake_db)
        config = await resolver.resolve(org_id=1)
        assert config == RoutingPolicyConfig()

    @pytest.mark.asyncio
    async def test_row_values_override_defaults(self, fake_db):
        """An existing row's values are used instead of the defaults."""
        fake_db.seed("routing_policies", [_policy_row(1, mode="cost", escalation_threshold=4)])
        resolver = PolicyResolver(fake_db)

        config = await resolver.resolve(org_id=1)

        assert config.mode == "cost"
        assert config.escalation_threshold == 4

    @pytest.mark.asyncio
    async def test_cache_hit_avoids_second_lookup(self, fake_db, fake_valkey):
        """A second resolve() for the same org is served from cache."""
        fake_db.seed("routing_policies", [_policy_row(1, mode="cost")])
        resolver = PolicyResolver(fake_db, valkey=fake_valkey)

        await resolver.resolve(org_id=1)
        fake_db._tables["routing_policies"][0]["mode"] = "latency"
        second = await resolver.resolve(org_id=1)

        assert second.mode == "cost"

    @pytest.mark.asyncio
    async def test_invalidate_clears_cache(self, fake_db, fake_valkey):
        """invalidate() forces the next resolve() to see updated data."""
        fake_db.seed("routing_policies", [_policy_row(1, mode="cost")])
        resolver = PolicyResolver(fake_db, valkey=fake_valkey)

        await resolver.resolve(org_id=1)
        fake_db._tables["routing_policies"][0]["mode"] = "latency"
        await resolver.invalidate(org_id=1)
        second = await resolver.resolve(org_id=1)

        assert second.mode == "latency"


class TestFilterAndSort:
    """filter_and_sort() -- allow-list/tier filtering then mode-based ordering."""

    def test_cost_mode_sorts_ascending_by_cost(self):
        """mode='cost' orders candidates from cheapest to most expensive."""
        offers = [
            ModelOffer(model_name="expensive", cost_per_token=0.01),
            ModelOffer(model_name="cheap", cost_per_token=0.0001),
        ]
        result = filter_and_sort(offers, RoutingPolicyConfig(mode="cost"))
        assert [o.model_name for o in result] == ["cheap", "expensive"]

    def test_latency_mode_sorts_ascending_by_latency(self):
        """mode='latency' orders candidates by EMA latency lookup."""
        offers = [ModelOffer(model_name="slow"), ModelOffer(model_name="fast")]
        latency = {"slow": 800.0, "fast": 50.0}
        policy = RoutingPolicyConfig(mode="latency")
        result = filter_and_sort(offers, policy, latency_by_model=latency)
        assert [o.model_name for o in result] == ["fast", "slow"]

    def test_local_first_orders_local_before_commercial(self):
        """mode='local_first' puts every local candidate ahead of commercial ones."""
        offers = [
            ModelOffer(model_name="cloud", location="commercial", cost_per_token=0.0),
            ModelOffer(model_name="onprem", location="local", cost_per_token=0.0),
        ]
        result = filter_and_sort(offers, RoutingPolicyConfig(mode="local_first"))
        assert [o.model_name for o in result] == ["onprem", "cloud"]

    def test_local_only_drops_commercial_candidates(self):
        """mode='local_only' removes commercial candidates from the chain entirely."""
        offers = [
            ModelOffer(model_name="cloud", location="commercial"),
            ModelOffer(model_name="onprem", location="local"),
        ]
        result = filter_and_sort(offers, RoutingPolicyConfig(mode="local_only"))
        assert [o.model_name for o in result] == ["onprem"]

    def test_commercial_only_drops_local_candidates(self):
        """mode='commercial_only' removes local candidates from the chain entirely."""
        offers = [
            ModelOffer(model_name="cloud", location="commercial"),
            ModelOffer(model_name="onprem", location="local"),
        ]
        result = filter_and_sort(offers, RoutingPolicyConfig(mode="commercial_only"))
        assert [o.model_name for o in result] == ["cloud"]

    def test_allow_list_filters_out_disallowed_models(self):
        """Models outside the org allow-list are dropped from the chain."""
        offers = [ModelOffer(model_name="a"), ModelOffer(model_name="b")]
        result = filter_and_sort(offers, RoutingPolicyConfig(mode="cost"), allowed_models={"a"})
        assert [o.model_name for o in result] == ["a"]

    def test_tier_cap_filters_out_too_expensive_models(self):
        """Models exceeding the license-tier cost cap are dropped."""
        offers = [
            ModelOffer(model_name="cheap", cost_per_token=0.0001),
            ModelOffer(model_name="pricey", cost_per_token=0.01),
        ]
        result = filter_and_sort(offers, RoutingPolicyConfig(mode="cost"), tier_cap=0.001)
        assert [o.model_name for o in result] == ["cheap"]
