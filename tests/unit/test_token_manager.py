"""Unit tests for token management system."""

from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest

try:
    from shared.utils.token_manager import (
        TokenManager,
        TokenUsage,
        WaddleAITokenCalculator,
        create_token_manager,
    )
except ImportError as e:
    pytest.skip(
        f"Skipping: shared.utils.token_manager not available ({e})", allow_module_level=True
    )


class TestWaddleAITokenCalculator:
    """Test WaddleAI token calculation."""

    def test_init(self):
        """Test calculator initialization."""
        calc = WaddleAITokenCalculator()
        assert len(calc.provider_rates) > 0
        assert "openai" in calc.provider_rates
        assert "anthropic" in calc.provider_rates
        assert "ollama" in calc.provider_rates

    def test_calculate_tokens_openai_gpt4(self):
        """Test token calculation for OpenAI GPT-4."""
        calc = WaddleAITokenCalculator()

        waddleai_tokens = calc.calculate_tokens(
            provider="openai", model="gpt-4", input_tokens=100, output_tokens=50
        )

        # GPT-4 should be expensive (high rate)
        assert waddleai_tokens > 150  # Should be more than raw tokens
        assert isinstance(waddleai_tokens, int)

    def test_calculate_tokens_openai_gpt35(self):
        """Test token calculation for OpenAI GPT-3.5."""
        calc = WaddleAITokenCalculator()

        waddleai_tokens = calc.calculate_tokens(
            provider="openai", model="gpt-3.5-turbo", input_tokens=100, output_tokens=50
        )

        # GPT-3.5 should be cheaper than GPT-4
        gpt4_tokens = calc.calculate_tokens("openai", "gpt-4", 100, 50)
        assert waddleai_tokens < gpt4_tokens

    def test_calculate_tokens_anthropic(self):
        """Test token calculation for Anthropic."""
        calc = WaddleAITokenCalculator()

        waddleai_tokens = calc.calculate_tokens(
            provider="anthropic", model="claude-3-opus-20240229", input_tokens=100, output_tokens=50
        )

        assert waddleai_tokens > 0
        assert isinstance(waddleai_tokens, int)

    def test_calculate_tokens_ollama(self):
        """Test token calculation for Ollama (free local)."""
        calc = WaddleAITokenCalculator()

        waddleai_tokens = calc.calculate_tokens(
            provider="ollama", model="llama2", input_tokens=100, output_tokens=50
        )

        # Ollama should be free/very cheap
        assert waddleai_tokens >= 0
        assert waddleai_tokens < 50  # Should be much less than raw tokens

    def test_calculate_tokens_unknown_provider(self):
        """Test token calculation for unknown provider."""
        calc = WaddleAITokenCalculator()

        waddleai_tokens = calc.calculate_tokens(
            provider="unknown", model="unknown-model", input_tokens=100, output_tokens=50
        )

        # Should use default rate
        assert waddleai_tokens > 0
        assert isinstance(waddleai_tokens, int)

    def test_get_rate_for_provider_model(self):
        """Test getting rate for specific provider/model."""
        calc = WaddleAITokenCalculator()

        # Test specific model rates
        gpt4_rate = calc._get_rate_for_provider_model("openai", "gpt-4")
        gpt35_rate = calc._get_rate_for_provider_model("openai", "gpt-3.5-turbo")

        assert gpt4_rate > gpt35_rate  # GPT-4 should be more expensive

        # Test default provider rate
        unknown_rate = calc._get_rate_for_provider_model("openai", "unknown-model")
        assert unknown_rate > 0

    def test_edge_cases(self):
        """Test edge cases."""
        calc = WaddleAITokenCalculator()

        # Zero tokens
        result = calc.calculate_tokens("openai", "gpt-4", 0, 0)
        assert result == 0

        # Negative tokens (should be treated as 0)
        result = calc.calculate_tokens("openai", "gpt-4", -10, -5)
        assert result == 0


class TestTokenUsage:
    """Test TokenUsage dataclass."""

    def test_token_usage_creation(self):
        """Test TokenUsage creation."""
        usage = TokenUsage(
            waddleai_tokens=100,
            llm_tokens_input=80,
            llm_tokens_output=40,
            provider="openai",
            model="gpt-4",
            cost_usd=0.002,
        )

        assert usage.waddleai_tokens == 100
        assert usage.llm_tokens_input == 80
        assert usage.llm_tokens_output == 40
        assert usage.provider == "openai"
        assert usage.model == "gpt-4"
        assert usage.cost_usd == 0.002

    def test_token_usage_defaults(self):
        """Test TokenUsage default values."""
        usage = TokenUsage(
            waddleai_tokens=100,
            llm_tokens_input=80,
            llm_tokens_output=40,
            provider="openai",
            model="gpt-4",
        )

        assert usage.cost_usd == 0.0  # Default value


class TestTokenManager:
    """Test TokenManager class."""

    def test_init(self, mock_db):
        """Test TokenManager initialization."""
        manager = TokenManager(mock_db)
        assert manager.db == mock_db
        assert isinstance(manager.calculator, WaddleAITokenCalculator)

    @patch("shared.utils.token_manager.tiktoken.encoding_for_model")
    def test_count_tokens(self, mock_encoding, mock_db):
        """Test token counting."""
        # Mock tiktoken encoder
        mock_encoder = Mock()
        mock_encoder.encode.return_value = [1, 2, 3, 4, 5]  # 5 tokens
        mock_encoding.return_value = mock_encoder

        manager = TokenManager(mock_db)
        token_count = manager._count_tokens("Hello world", "gpt-4")

        assert token_count == 5
        mock_encoder.encode.assert_called_once_with("Hello world")

    @patch("shared.utils.token_manager.tiktoken.encoding_for_model")
    def test_count_tokens_fallback(self, mock_encoding, mock_db):
        """Test token counting fallback."""
        # Mock encoding failure
        mock_encoding.side_effect = Exception("Encoding failed")

        manager = TokenManager(mock_db)
        token_count = manager._count_tokens("Hello world", "gpt-4")

        # Should fall back to character/4 estimation
        assert token_count == len("Hello world") // 4

    def test_process_usage_with_actual_tokens(self, mock_db):
        """Test processing usage with actual token counts."""
        manager = TokenManager(mock_db)

        # Mock database insert
        mock_db.token_usage = Mock()
        mock_db.token_usage.insert = Mock(return_value=1)

        usage = manager.process_usage(
            input_text="Hello world",
            output_text="Hi there!",
            provider="openai",
            model="gpt-4",
            api_key_id=1,
            user_id=1,
            organization_id=1,
            actual_input_tokens=5,
            actual_output_tokens=3,
        )

        assert isinstance(usage, TokenUsage)
        assert usage.llm_tokens_input == 5
        assert usage.llm_tokens_output == 3
        assert usage.waddleai_tokens > 0
        assert usage.provider == "openai"
        assert usage.model == "gpt-4"

        # Verify database insertion
        mock_db.token_usage.insert.assert_called_once()

    @patch("shared.utils.token_manager.tiktoken.encoding_for_model")
    def test_process_usage_estimated_tokens(self, mock_encoding, mock_db):
        """Test processing usage with estimated tokens."""
        # Mock tiktoken encoder
        mock_encoder = Mock()
        mock_encoder.encode.side_effect = [[1, 2, 3], [1, 2]]  # 3 input, 2 output tokens
        mock_encoding.return_value = mock_encoder

        manager = TokenManager(mock_db)

        # Mock database insert
        mock_db.token_usage = Mock()
        mock_db.token_usage.insert = Mock(return_value=1)

        usage = manager.process_usage(
            input_text="Hello",
            output_text="Hi",
            provider="openai",
            model="gpt-4",
            api_key_id=1,
            user_id=1,
            organization_id=1,
        )

        assert usage.llm_tokens_input == 3
        assert usage.llm_tokens_output == 2
        assert usage.waddleai_tokens > 0

    def test_check_quota_under_limit(self, mock_db):
        """Test quota check when under limit."""
        manager = TokenManager(mock_db)

        # Mock quota queries
        mock_db.token_usage = Mock()
        mock_db.token_usage.waddleai_tokens = Mock()
        mock_db.token_usage.created_at = Mock()

        # Mock return values for daily and monthly usage
        mock_db.return_value = Mock()
        mock_db.return_value.sum = Mock(return_value=Mock())
        mock_db.return_value.sum.return_value = 5000  # Under daily limit

        # Mock API key data
        mock_api_key = Mock()
        mock_api_key.organization_id = 1
        mock_db.api_keys = Mock()
        mock_db.return_value.select = Mock(return_value=[mock_api_key])

        # Mock organization data
        mock_org = Mock()
        mock_org.token_quota_daily = 10000
        mock_org.token_quota_monthly = 300000
        mock_db.organizations = Mock()

        # Setup different return values for different calls
        def side_effect(*args, **kwargs):
            mock_result = Mock()
            if hasattr(args[0], "id"):  # API key query
                mock_result.select.return_value = [mock_api_key]
            else:  # Organization query
                mock_result.select.return_value = [mock_org]
            return mock_result

        mock_db.side_effect = side_effect

        quota_ok, quota_info = manager.check_quota(1)

        assert quota_ok is True
        assert "daily" in quota_info
        assert "monthly" in quota_info

    def test_get_usage_stats(self, mock_db):
        """Test getting usage statistics."""
        manager = TokenManager(mock_db)

        # Mock usage records
        mock_usage_records = []
        for i in range(5):
            record = Mock()
            record.waddleai_tokens = 100
            record.created_at = datetime.utcnow() - timedelta(days=i)
            record.provider = "openai"
            record.model = "gpt-4"
            mock_usage_records.append(record)

        mock_db.token_usage = Mock()
        mock_db.token_usage.created_at = Mock()
        mock_db.return_value = Mock()
        mock_db.return_value.select = Mock(return_value=mock_usage_records)

        stats = manager.get_usage_stats(api_key_id=1, days=7)

        assert "total_tokens" in stats
        assert "total_requests" in stats
        assert "daily_usage" in stats
        assert "provider_breakdown" in stats
        assert stats["total_tokens"] == 500  # 5 records * 100 tokens each
        assert stats["total_requests"] == 5

    def test_get_usage_stats_no_data(self, mock_db):
        """Test getting usage statistics with no data."""
        manager = TokenManager(mock_db)

        # Mock empty result
        mock_db.token_usage = Mock()
        mock_db.token_usage.created_at = Mock()
        mock_db.return_value = Mock()
        mock_db.return_value.select = Mock(return_value=[])

        stats = manager.get_usage_stats(api_key_id=1, days=7)

        assert stats["total_tokens"] == 0
        assert stats["total_requests"] == 0
        assert stats["daily_usage"] == {}
        assert stats["provider_breakdown"] == {}


class TestTokenManagerFactory:
    """Test token manager factory function."""

    def test_create_token_manager(self, mock_db):
        """Test creating token manager."""
        manager = create_token_manager(mock_db)

        assert isinstance(manager, TokenManager)
        assert manager.db == mock_db
        assert isinstance(manager.calculator, WaddleAITokenCalculator)
