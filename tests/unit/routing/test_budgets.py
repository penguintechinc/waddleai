"""Typed token/dollar/plan budget pressure tests (spec §7.3, §7.7)."""

import pytest

from shared.routing.budgets import PlanBudgetWindow, compute_pressure


class TestComputePressure:
    """compute_pressure() -- min-headroom-wins + graduated 80/95/100 thresholds."""

    def test_below_80_percent_is_no_pressure(self):
        """Consumption under 80% raises no threshold delta and no clamps."""
        pressure = compute_pressure(token_consumed_fraction=0.5)
        assert pressure.threshold_delta == 0
        assert pressure.clamp_local is False
        assert pressure.hard_block is False

    def test_80_percent_raises_escalation_threshold(self):
        """At ~80% consumed, the escalation threshold rises (commercial harder to reach)."""
        pressure = compute_pressure(token_consumed_fraction=0.80)
        assert pressure.threshold_delta > 0
        assert pressure.clamp_local is False

    def test_95_percent_clamps_local_only(self):
        """At ~95% consumed, routing clamps local-only."""
        pressure = compute_pressure(token_consumed_fraction=0.95)
        assert pressure.clamp_local is True
        assert pressure.hard_block is False

    def test_100_percent_hard_blocks(self):
        """At 100% consumed, the existing hard-block signal is set."""
        pressure = compute_pressure(token_consumed_fraction=1.0)
        assert pressure.hard_block is True

    def test_min_headroom_wins_across_budget_types(self):
        """The tightest (highest-consumed) budget type binds the pressure level."""
        pressure = compute_pressure(
            token_consumed_fraction=0.10,
            dollar_consumed_fraction=0.30,
            plan_consumed_fraction=0.96,
        )
        assert pressure.binding_type == "plan"
        assert pressure.clamp_local is True

    def test_toggle_off_is_a_hard_noop_even_at_99_percent(self):
        """budget_pressure_enabled=False produces zero signal regardless of consumption."""
        pressure = compute_pressure(token_consumed_fraction=0.99, enabled=False)
        assert pressure.threshold_delta == 0
        assert pressure.clamp_local is False
        assert pressure.hard_block is False
        assert pressure.binding_type is None

    def test_no_budgets_configured_yields_no_pressure(self):
        """When no budget type is supplied at all, there is no pressure signal."""
        pressure = compute_pressure()
        assert pressure.binding_type is None
        assert pressure.level == 0.0


class TestPlanBudgetWindow:
    """Plan-budget window headroom, header-based correction, and pool rotation."""

    @pytest.mark.asyncio
    async def test_headroom_none_when_no_data_recorded(self, fake_valkey):
        """No usage data yet yields unknown headroom (None), not depleted."""
        window = PlanBudgetWindow(fake_valkey)
        assert await window.headroom("cred-1", "2026-08") is None

    @pytest.mark.asyncio
    async def test_correct_from_headers_sets_headroom(self, fake_valkey):
        """correct_from_headers() reconciles headroom from provider response headers."""
        window = PlanBudgetWindow(fake_valkey)
        await window.correct_from_headers("cred-1", "2026-08", remaining=200, limit=1000)

        assert await window.headroom("cred-1", "2026-08") == pytest.approx(0.2)

    @pytest.mark.asyncio
    async def test_is_depleted_true_near_exhaustion(self, fake_valkey):
        """A credential with <5% headroom is reported depleted (rotate out of pool)."""
        window = PlanBudgetWindow(fake_valkey)
        await window.correct_from_headers("cred-1", "2026-08", remaining=10, limit=1000)

        assert await window.is_depleted("cred-1", "2026-08") is True

    @pytest.mark.asyncio
    async def test_is_depleted_false_with_ample_headroom(self, fake_valkey):
        """A credential with ample headroom is not depleted -- stays in the pool."""
        window = PlanBudgetWindow(fake_valkey)
        await window.correct_from_headers("cred-1", "2026-08", remaining=800, limit=1000)

        assert await window.is_depleted("cred-1", "2026-08") is False

    @pytest.mark.asyncio
    async def test_reset_window_restores_full_headroom_state(self, fake_valkey):
        """reset_window() clears counters so headroom resets to unknown (fresh window)."""
        window = PlanBudgetWindow(fake_valkey)
        await window.correct_from_headers("cred-1", "2026-08", remaining=10, limit=1000)
        await window.reset_window("cred-1", "2026-08")

        assert await window.headroom("cred-1", "2026-08") is None

    @pytest.mark.asyncio
    async def test_different_credentials_have_independent_windows(self, fake_valkey):
        """Two credentials' plan-budget windows never interfere with each other."""
        window = PlanBudgetWindow(fake_valkey)
        await window.correct_from_headers("cred-1", "2026-08", remaining=10, limit=1000)
        await window.correct_from_headers("cred-2", "2026-08", remaining=900, limit=1000)

        assert await window.is_depleted("cred-1", "2026-08") is True
        assert await window.is_depleted("cred-2", "2026-08") is False
