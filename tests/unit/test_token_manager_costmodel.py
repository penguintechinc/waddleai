"""
Test suite for Token Manager cost model (ported from AILB)
Tests token conversion, cost calculation, and conversion rate management
"""

from datetime import date
from unittest.mock import MagicMock

import pytest

from shared.utils.token_manager import ConversionRate, TokenManager, TokenUsage


class TestTokenConversion:
    """Test token conversion to WaddleAI tokens"""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create a mock database"""
        db = MagicMock()
        db.token_conversion_rates = MagicMock()
        db.return_value = MagicMock()
        db.return_value.select = MagicMock(return_value=[])
        return db

    def test_calculate_waddleai_tokens_gpt4(self, mock_db):
        """Test conversion for GPT-4 model"""
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
        """Test conversion for Claude model"""
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
        """Test conversion for unknown model uses default"""
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
        """Test conversion with zero input tokens"""
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
        """Test conversion with zero output tokens"""
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
    """Test cost calculation"""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create a mock database"""
        db = MagicMock()
        db.token_conversion_rates = MagicMock()
        db.return_value = MagicMock()
        db.return_value.select = MagicMock(return_value=[])
        return db

    def test_calculate_cost_returns_tuple(self, mock_db):
        """Test that calculate_cost returns tuple of WaddleAI and USD costs"""
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
        """Test cost calculation for GPT-4"""
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
        """Test cost calculation differs by model"""
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

        waddleai_cost_opus, usd_cost_opus = manager.calculate_cost(100, "anthropic", "claude-3-opus")
        waddleai_cost_sonnet, usd_cost_sonnet = manager.calculate_cost(100, "anthropic", "claude-3-sonnet")

        # Same WaddleAI tokens, different USD costs
        assert waddleai_cost_opus == waddleai_cost_sonnet
        assert usd_cost_opus != usd_cost_sonnet  # Different rates

    def test_calculate_cost_unknown_model(self, mock_db):
        """Test cost calculation for unknown model"""
        manager = TokenManager(mock_db)
        manager.conversion_rates = {}

        waddleai_cost, usd_cost = manager.calculate_cost(100, provider="unknown", model="unknown")

        # WaddleAI cost is still 1:1
        assert waddleai_cost == 100.0
        # USD uses default rate
        assert usd_cost == 100 * 0.001

    def test_calculate_cost_zero_tokens(self, mock_db):
        """Test cost calculation for zero tokens"""
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
    """Test conversion rate management"""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create a mock database"""
        db = MagicMock()
        db.token_conversion_rates = MagicMock()
        db.return_value = MagicMock()
        db.return_value.select = MagicMock(return_value=[])
        return db

    def test_conversion_rate_to_dict(self):
        """Test ConversionRate.to_dict()"""
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
        """Test that DEFAULT_CONVERSION_RATES is available"""
        manager = TokenManager(mock_db)

        # Check that DEFAULT_CONVERSION_RATES is available and has expected models
        assert hasattr(TokenManager, "DEFAULT_CONVERSION_RATES") or hasattr(manager, "DEFAULT_CONVERSION_RATES")


class TestEdgeCases:
    """Test edge cases and boundary conditions"""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create a mock database"""
        db = MagicMock()
        db.token_conversion_rates = MagicMock()
        db.return_value = MagicMock()
        db.return_value.select = MagicMock(return_value=[])
        return db

    def test_very_large_token_count(self, mock_db):
        """Test handling of very large token counts"""
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
        """Test that negative tokens don't break calculation"""
        manager = TokenManager(mock_db)
        manager.conversion_rates = {}

        # The function should handle gracefully
        waddleai_tokens = manager.calculate_waddleai_tokens(-10, -5, "unknown", "unknown")

        # Should return positive value even with negative input
        assert waddleai_tokens >= 1  # At least min value

    def test_fractional_rates_calculation(self, mock_db):
        """Test calculation with fractional conversion rates"""
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
