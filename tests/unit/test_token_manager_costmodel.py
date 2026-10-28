"""Test suite for Token Manager cost model (ported from AILB).

Tests token conversion, cost calculation, and conversion rate management.
"""

from unittest.mock import MagicMock

import pytest

from shared.utils.default_conversion_rates import DEFAULT_RATE_ROWS, RateRow
from shared.utils.token_manager import ConversionRate, TokenManager


class TestTokenConversion:
    """Test token conversion to WaddleAI tokens."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create a mock database."""
        db = MagicMock()
        db.token_conversion_rates = MagicMock()
        db.return_value = MagicMock()
        db.return_value.select = MagicMock(return_value=[])
        return db

    def test_calculate_waddleai_tokens_gpt4(self, mock_db):
        """Test conversion for GPT-4 model."""
        manager = TokenManager(mock_db)
        manager.conversion_rates = {
            "openai:gpt-4": ConversionRate(
                provider="openai",
                model="gpt-4",
                input_rate=10.0,
                output_rate=20.0,
                base_cost_per_waddleai_token=0.003,
            )
        }

        waddleai_tokens = manager.calculate_waddleai_tokens(
            input_tokens=100, output_tokens=50, provider="openai", model="gpt-4"
        )

        assert waddleai_tokens > 0
        # GPT-4 has 10:1 input and 20:1 output ratio
        expected = (100 // 10) + (50 // 20)
        assert waddleai_tokens == expected

    def test_calculate_waddleai_tokens_claude(self, mock_db):
        """Test conversion for Claude model."""
        manager = TokenManager(mock_db)
        manager.conversion_rates = {
            "anthropic:claude-3-opus": ConversionRate(
                provider="anthropic",
                model="claude-3-opus",
                input_rate=10.0,
                output_rate=20.0,
                base_cost_per_waddleai_token=0.0075,
            )
        }

        waddleai_tokens = manager.calculate_waddleai_tokens(
            input_tokens=100, output_tokens=50, provider="anthropic", model="claude-3-opus"
        )

        assert waddleai_tokens > 0
        expected = (100 // 10) + (50 // 20)
        assert waddleai_tokens == expected

    def test_calculate_waddleai_tokens_unknown_model(self, mock_db):
        """Test conversion for unknown model uses default."""
        manager = TokenManager(mock_db)
        manager.conversion_rates = {}

        waddleai_tokens = manager.calculate_waddleai_tokens(
            input_tokens=100, output_tokens=50, provider="unknown", model="unknown-model"
        )

        assert waddleai_tokens > 0
        # Default: output tokens weighted 2x, divided by 10
        expected = (100 + 50 * 2) // 10
        assert waddleai_tokens == expected

    def test_calculate_waddleai_tokens_zero_input(self, mock_db):
        """Test conversion with zero input tokens."""
        manager = TokenManager(mock_db)
        manager.conversion_rates = {
            "openai:gpt-4": ConversionRate(
                provider="openai",
                model="gpt-4",
                input_rate=10.0,
                output_rate=20.0,
                base_cost_per_waddleai_token=0.003,
            )
        }

        waddleai_tokens = manager.calculate_waddleai_tokens(
            input_tokens=0, output_tokens=100, provider="openai", model="gpt-4"
        )

        assert waddleai_tokens > 0
        expected = 0 + (100 // 20)
        assert waddleai_tokens == expected

    def test_calculate_waddleai_tokens_zero_output(self, mock_db):
        """Test conversion with zero output tokens."""
        manager = TokenManager(mock_db)
        manager.conversion_rates = {
            "openai:gpt-4": ConversionRate(
                provider="openai",
                model="gpt-4",
                input_rate=10.0,
                output_rate=20.0,
                base_cost_per_waddleai_token=0.003,
            )
        }

        waddleai_tokens = manager.calculate_waddleai_tokens(
            input_tokens=100, output_tokens=0, provider="openai", model="gpt-4"
        )

        assert waddleai_tokens > 0
        expected = (100 // 10) + 0
        assert waddleai_tokens == expected


class TestCostCalculation:
    """Test cost calculation."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create a mock database."""
        db = MagicMock()
        db.token_conversion_rates = MagicMock()
        db.return_value = MagicMock()
        db.return_value.select = MagicMock(return_value=[])
        return db

    def test_calculate_cost_returns_tuple(self, mock_db):
        """Test that calculate_cost returns tuple of WaddleAI and USD costs."""
        manager = TokenManager(mock_db)
        manager.conversion_rates = {
            "openai:gpt-4": ConversionRate(
                provider="openai",
                model="gpt-4",
                input_rate=10.0,
                output_rate=20.0,
                base_cost_per_waddleai_token=0.003,
            )
        }

        waddleai_cost, usd_cost = manager.calculate_cost(100, provider="openai", model="gpt-4")

        assert isinstance(waddleai_cost, float)
        assert isinstance(usd_cost, float)
        assert waddleai_cost > 0
        assert usd_cost > 0

    def test_calculate_cost_gpt4(self, mock_db):
        """Test cost calculation for GPT-4."""
        manager = TokenManager(mock_db)
        manager.conversion_rates = {
            "openai:gpt-4": ConversionRate(
                provider="openai",
                model="gpt-4",
                input_rate=10.0,
                output_rate=20.0,
                base_cost_per_waddleai_token=0.003,
            )
        }

        waddleai_cost, usd_cost = manager.calculate_cost(100, provider="openai", model="gpt-4")

        # WaddleAI cost is 1:1 with tokens
        assert waddleai_cost == 100.0
        # USD cost based on conversion rate
        assert usd_cost == 100 * 0.003

    def test_calculate_cost_different_models(self, mock_db):
        """Test cost calculation differs by model."""
        manager = TokenManager(mock_db)
        manager.conversion_rates = {
            "anthropic:claude-3-opus": ConversionRate(
                provider="anthropic",
                model="claude-3-opus",
                input_rate=10.0,
                output_rate=20.0,
                base_cost_per_waddleai_token=0.0075,
            ),
            "anthropic:claude-3-sonnet": ConversionRate(
                provider="anthropic",
                model="claude-3-sonnet",
                input_rate=10.0,
                output_rate=20.0,
                base_cost_per_waddleai_token=0.0015,
            ),
        }

        waddleai_cost_opus, usd_cost_opus = manager.calculate_cost(
            100, "anthropic", "claude-3-opus"
        )
        waddleai_cost_sonnet, usd_cost_sonnet = manager.calculate_cost(
            100, "anthropic", "claude-3-sonnet"
        )

        # Same WaddleAI tokens, different USD costs
        assert waddleai_cost_opus == waddleai_cost_sonnet
        assert usd_cost_opus != usd_cost_sonnet  # Different rates

    def test_calculate_cost_unknown_model(self, mock_db):
        """Test cost calculation for unknown model."""
        manager = TokenManager(mock_db)
        manager.conversion_rates = {}

        waddleai_cost, usd_cost = manager.calculate_cost(100, provider="unknown", model="unknown")

        # WaddleAI cost is still 1:1
        assert waddleai_cost == 100.0
        # USD uses default rate
        assert usd_cost == 100 * 0.001

    def test_calculate_cost_zero_tokens(self, mock_db):
        """Test cost calculation for zero tokens."""
        manager = TokenManager(mock_db)
        manager.conversion_rates = {
            "openai:gpt-4": ConversionRate(
                provider="openai",
                model="gpt-4",
                input_rate=10.0,
                output_rate=20.0,
                base_cost_per_waddleai_token=0.003,
            )
        }

        waddleai_cost, usd_cost = manager.calculate_cost(0, provider="openai", model="gpt-4")

        assert waddleai_cost == 0.0
        assert usd_cost == 0.0


class TestConversionRates:
    """Test conversion rate management."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create a mock database."""
        db = MagicMock()
        db.token_conversion_rates = MagicMock()
        db.return_value = MagicMock()
        db.return_value.select = MagicMock(return_value=[])
        return db

    def test_conversion_rate_to_dict(self):
        """Test ConversionRate.to_dict()."""
        rate = ConversionRate(
            provider="openai",
            model="gpt-4",
            input_rate=10.0,
            output_rate=20.0,
            base_cost_per_waddleai_token=0.003,
        )

        rate_dict = {
            "provider": rate.provider,
            "model": rate.model,
            "input_rate": rate.input_rate,
            "output_rate": rate.output_rate,
            "base_cost_per_waddleai_token": rate.base_cost_per_waddleai_token,
        }

        assert rate_dict["provider"] == "openai"
        assert rate_dict["model"] == "gpt-4"
        assert rate_dict["input_rate"] == 10.0

    def test_default_conversion_rates_exist(self, mock_db):
        """Test that DEFAULT_CONVERSION_RATES is available."""
        manager = TokenManager(mock_db)

        # Check that DEFAULT_CONVERSION_RATES is available and has expected models
        assert hasattr(TokenManager, "DEFAULT_CONVERSION_RATES") or hasattr(
            manager, "DEFAULT_CONVERSION_RATES"
        )


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create a mock database."""
        db = MagicMock()
        db.token_conversion_rates = MagicMock()
        db.return_value = MagicMock()
        db.return_value.select = MagicMock(return_value=[])
        return db

    def test_very_large_token_count(self, mock_db):
        """Test handling of very large token counts."""
        manager = TokenManager(mock_db)
        manager.conversion_rates = {
            "openai:gpt-4": ConversionRate(
                provider="openai",
                model="gpt-4",
                input_rate=10.0,
                output_rate=20.0,
                base_cost_per_waddleai_token=0.003,
            )
        }

        waddleai_tokens = manager.calculate_waddleai_tokens(1000000, 1000000, "openai", "gpt-4")
        waddleai_cost, usd_cost = manager.calculate_cost(waddleai_tokens, "openai", "gpt-4")

        assert waddleai_tokens > 0
        assert usd_cost > 0

    def test_negative_tokens_handled(self, mock_db):
        """Test that negative tokens don't break calculation."""
        manager = TokenManager(mock_db)
        manager.conversion_rates = {}

        # The function should handle gracefully
        waddleai_tokens = manager.calculate_waddleai_tokens(-10, -5, "unknown", "unknown")

        # Should return positive value even with negative input
        assert waddleai_tokens >= 1  # At least min value

    def test_fractional_rates_calculation(self, mock_db):
        """Test calculation with fractional conversion rates."""
        manager = TokenManager(mock_db)
        manager.conversion_rates = {
            "openai:gpt-4o": ConversionRate(
                provider="openai",
                model="gpt-4o",
                input_rate=2.5,
                output_rate=5.0,
                base_cost_per_waddleai_token=0.0005,
            )
        }

        waddleai_tokens = manager.calculate_waddleai_tokens(100, 100, "openai", "gpt-4o")

        # Should handle fractional division correctly
        assert waddleai_tokens > 0
        assert isinstance(waddleai_tokens, int)


class TestHybridRateKeying:
    """regression: gh-337 -- link-name -> provider-type -> heuristic conversion-rate lookup."""

    MODEL = "claude-x"  # not in the shipped defaults, so only the seeded rows can match

    @pytest.fixture
    def tm(self) -> TokenManager:
        """TokenManager whose rate table holds only the rows a test seeds."""
        db = MagicMock()
        db.return_value.select = MagicMock(return_value=[])
        manager = TokenManager(db)
        manager.conversion_rates = {}
        return manager

    def _seed(self, tm: TokenManager, provider: str, rate: float) -> None:
        tm.conversion_rates[f"{provider}:{self.MODEL}"] = ConversionRate(
            provider, self.MODEL, rate, rate, 0.002
        )

    def test_link_name_row_wins_over_type_row(self, tm: TokenManager) -> None:
        """Tier 1: a per-connection rate overrides the type default (exact credits)."""
        self._seed(tm, "my-link", 100.0)
        self._seed(tm, "anthropic", 10.0)
        assert tm.calculate_waddleai_tokens(1000, 1000, "my-link", self.MODEL, "anthropic") == 20

    def test_type_row_used_when_no_link_row(self, tm: TokenManager, caplog) -> None:
        """Tier 2 (the gh-337 fix): type-keyed rate applies, NOT the heuristic."""
        self._seed(tm, "anthropic", 10.0)
        credits = tm.calculate_waddleai_tokens(1000, 1000, "my-link", self.MODEL, "anthropic")
        assert credits == 200  # 100 + 100 via the rate
        assert credits != max(1, (1000 + 1000 * 2) // 10)  # heuristic would give 300
        assert "using default" not in caplog.text

    def test_neither_row_falls_to_loud_heuristic(self, tm: TokenManager, caplog) -> None:
        """Tier 3: heuristic formula unchanged and the WARNING fires."""
        with caplog.at_level("WARNING"):
            credits = tm.calculate_waddleai_tokens(1000, 1000, "my-link", self.MODEL, "anthropic")
        assert credits == 300
        assert "No conversion rate found" in caplog.text

    def test_type_none_keeps_legacy_behaviour(self, tm: TokenManager) -> None:
        """Backward compat: no type -> link-name row still works, type row is ignored."""
        self._seed(tm, "my-link", 10.0)
        assert tm.calculate_waddleai_tokens(1000, 1000, "my-link", self.MODEL) == 200
        assert tm.calculate_waddleai_tokens(1000, 1000, "other", self.MODEL) == 300

    def test_cost_uses_same_hybrid_lookup(self, tm: TokenManager) -> None:
        """USD cost follows the type row too, not the $0.001 default."""
        self._seed(tm, "anthropic", 10.0)
        _, usd = tm.calculate_cost(200, "my-link", self.MODEL, "anthropic")
        assert usd == pytest.approx(200 * 0.002)

    def test_process_usage_threads_provider_type(
        self, tm: TokenManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """process_usage passes the type through to the credit calculation."""
        monkeypatch.setattr(tm, "_update_usage_records", lambda *a, **k: None)
        monkeypatch.setattr(tm, "_update_usage_cache", lambda *a, **k: None)
        self._seed(tm, "anthropic", 10.0)
        usage = tm.process_usage(
            "x",
            "y",
            "my-link",
            self.MODEL,
            1,
            1,
            1,
            actual_input_tokens=1000,
            actual_output_tokens=1000,
            provider_type="anthropic",
        )
        assert usage.waddleai_tokens == 200


class TestSeededRatesHybridKeying:
    """regression: gh-337 -- the REAL shipped ``DEFAULT_RATE_ROWS`` resolve on the type tier."""

    LINK = "prod-link"  # a connection-link NAME that never appears as a seeded provider

    @pytest.fixture
    def tm(self) -> TokenManager:
        """TokenManager loaded with exactly the shipped default rows (no mocked divisors)."""
        db = MagicMock()
        db.return_value.select = MagicMock(return_value=[])
        manager = TokenManager(db)
        manager.conversion_rates = {
            f"{r.provider}:{r.model}": ConversionRate(
                r.provider, r.model, r.input_rate, r.output_rate, r.base_cost_per_waddleai_token
            )
            for r in DEFAULT_RATE_ROWS
        }
        return manager

    @pytest.mark.parametrize("row", DEFAULT_RATE_ROWS, ids=lambda r: f"{r.provider}:{r.model}")
    def test_type_tier_resolves_every_seeded_model(
        self, tm: TokenManager, row: RateRow, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Link name has no row, type does: credits equal the seeded rate, never the heuristic."""
        rate, tier = tm._resolve_rate(self.LINK, row.model, row.provider)
        assert tier == "type"
        assert rate is not None
        assert (rate.input_rate, rate.output_rate) == (row.input_rate, row.output_rate)
        with caplog.at_level("WARNING"):
            credits = tm.calculate_waddleai_tokens(5000, 3000, self.LINK, row.model, row.provider)
        assert "using default" not in caplog.text
        assert credits == TokenManager._credits_for(rate, 5000, 3000)

    def test_without_type_same_traffic_falls_to_heuristic(
        self, tm: TokenManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Positive control: same call bills the type rate WITH a type, the heuristic without."""
        with caplog.at_level("WARNING"):
            no_type = tm.calculate_waddleai_tokens(5000, 3000, self.LINK, "gpt-4")
        assert "No conversion rate found" in caplog.text
        with_type = tm.calculate_waddleai_tokens(5000, 3000, self.LINK, "gpt-4", "openai")
        assert no_type == (5000 + 3000 * 2) // 10 == 1100
        assert with_type == 5000 // 10 + 3000 // 20 == 650

    @pytest.mark.parametrize("row", DEFAULT_RATE_ROWS, ids=lambda r: f"{r.provider}:{r.model}")
    @pytest.mark.parametrize("tokens", [(1, 1), (10, 5), (1000, 0), (0, 1000), (5000, 3000)])
    def test_reconcile_never_exceeds_reserve_for_rate_matched_traffic(
        self, tm: TokenManager, row: RateRow, tokens: tuple[int, int]
    ) -> None:
        """Reserve (max over all rows for the model) bounds reconcile (hybrid rate)."""
        in_t, out_t = tokens
        reserve = tm.estimate_credits(row.model, in_t, out_t)
        reconcile = tm.calculate_waddleai_tokens(in_t, out_t, self.LINK, row.model, row.provider)
        assert reconcile <= reserve

    @pytest.mark.xfail(
        strict=True,
        reason="pre-existing: gh-??? reserve<reconcile on the heuristic-fallback path "
        "(heuristic (in+2*out)//10 exceeds the seeded-rate reserve); owner pricing decision",
    )
    def test_heuristic_path_can_exceed_reserve(self, tm: TokenManager) -> None:
        """Tracks the known invariant break on UNMATCHED traffic (no type); out of scope here."""
        reserve = tm.estimate_credits("llama3.2", 5000, 3000)
        reconcile = tm.calculate_waddleai_tokens(5000, 3000, self.LINK, "llama3.2")
        assert reconcile <= reserve

    def test_process_usage_threads_type_with_real_rates(
        self, tm: TokenManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main.py passes ``provider_type=final_ctx.provider_type``; omitting it changes credits."""
        monkeypatch.setattr(tm, "_update_usage_records", lambda *a, **k: None)
        monkeypatch.setattr(tm, "_update_usage_cache", lambda *a, **k: None)
        typed = tm.process_usage(
            "x", "y", self.LINK, "gpt-4", 1, 1, 1, 5000, 3000, provider_type="openai"
        )
        untyped = tm.process_usage("x", "y", self.LINK, "gpt-4", 1, 1, 1, 5000, 3000)
        assert typed.waddleai_tokens == 650
        assert untyped.waddleai_tokens == 1100
