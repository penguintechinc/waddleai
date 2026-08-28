"""Comprehensive unit tests for UsageTrackingService."""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from services.management.app.services.usage_tracker import (
    DailyUsage,
    QuotaInfo,
    QuotaStatus,
    UsageEvent,
    UsageStats,
    UsageTrackingService,
)
from tests.unit.management.conftest import _make_mock_db


@pytest.fixture
def mock_db():
    """Create a mock database connection that supports PyDAL-style queries."""
    return _make_mock_db()


@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    return MagicMock()


@pytest.fixture
def tracker(mock_db, mock_redis):
    """Create a UsageTrackingService instance with mocked dependencies."""
    return UsageTrackingService(mock_db, mock_redis)


def make_usage_event(**kwargs):
    """Helper to create UsageEvent instances with defaults."""
    defaults = {
        "event_id": "evt-001",
        "key_id": "wa-testkey",
        "request_id": "req-001",
        "model": "gpt-4",
        "provider": "openai",
        "input_tokens": 100,
        "output_tokens": 200,
        "cost_usd": 0.005,
        "latency_ms": 300,
        "status": "success",
    }
    defaults.update(kwargs)
    return UsageEvent(**defaults)


class TestUsageEventDataclass:
    """Test UsageEvent creation and timestamp handling."""

    def test_usage_event_creation(self):
        """Test creating a UsageEvent with all fields."""
        event = make_usage_event(
            event_id="evt-123",
            key_id="wa-key123",
            model="gpt-4o",
            input_tokens=150,
            output_tokens=250,
        )

        assert event.event_id == "evt-123"
        assert event.key_id == "wa-key123"
        assert event.model == "gpt-4o"
        assert event.input_tokens == 150
        assert event.output_tokens == 250

    def test_usage_event_default_timestamp(self):
        """Test UsageEvent gets default timestamp if not provided."""
        before = datetime.utcnow()
        event = make_usage_event()
        after = datetime.utcnow()

        assert event.timestamp is not None
        assert before <= event.timestamp <= after

    def test_usage_event_custom_timestamp(self):
        """Test UsageEvent accepts custom timestamp."""
        custom_time = datetime(2025, 1, 15, 12, 0, 0)
        event = make_usage_event(timestamp=custom_time)

        assert event.timestamp == custom_time


class TestCalculateWaddleaiTokens:
    """Test token conversion calculation."""

    def test_calculate_tokens_with_cached_rates(self, tracker):
        """Test conversion uses cached rates when available."""
        # Pre-populate cache
        cache_key = "openai:gpt-4"
        tracker._conversion_rates_cache[cache_key] = {
            "input_rate": 5.0,
            "output_rate": 5.0,
            "expires": datetime.utcnow() + timedelta(seconds=300),
        }

        result = tracker.calculate_waddleai_tokens("openai", "gpt-4", 100, 200)

        # 100 / 5 + 200 / 5 = 20 + 40 = 60
        assert result == 60
        # DB should not be queried when cache hit
        tracker.db.assert_not_called()

    def test_calculate_tokens_expired_cache(self, tracker, mock_db):
        """Test conversion fetches from DB when cache expired."""
        cache_key = "openai:gpt-4"
        tracker._conversion_rates_cache[cache_key] = {
            "input_rate": 5.0,
            "output_rate": 5.0,
            "expires": datetime.utcnow() - timedelta(seconds=1),  # Expired
        }

        # Mock DB query to return a rate
        mock_rate = MagicMock()
        mock_rate.input_rate = 10
        mock_rate.output_rate = 10
        mock_db.return_value.select.return_value.first.return_value = mock_rate

        result = tracker.calculate_waddleai_tokens("openai", "gpt-4", 100, 200)

        # 100 / 10 + 200 / 10 = 10 + 20 = 30
        assert result == 30
        # Cache should be updated
        assert tracker._conversion_rates_cache[cache_key]["input_rate"] == 10

    def test_calculate_tokens_db_lookup_found(self, tracker, mock_db):
        """Test conversion looks up from DB when no cache."""
        mock_rate = MagicMock()
        mock_rate.input_rate = 8
        mock_rate.output_rate = 8
        mock_select = MagicMock()
        mock_select.first.return_value = mock_rate
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        result = tracker.calculate_waddleai_tokens("anthropic", "claude-opus", 80, 160)

        # 80 / 8 + 160 / 8 = 10 + 20 = 30
        assert result == 30

    def test_calculate_tokens_no_rate_found_defaults(self, tracker, mock_db):
        """Test conversion uses default rates when no DB match."""
        mock_select = MagicMock()
        mock_select.first.return_value = None  # No rate found
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        # GPT-4 defaults to (5, 5)
        result = tracker.calculate_waddleai_tokens("openai", "gpt-4", 100, 200)

        # 100 / 5 + 200 / 5 = 20 + 40 = 60
        assert result == 60

    def test_calculate_tokens_gpt35_cheaper(self, tracker, mock_db):
        """Test GPT-3.5 has higher conversion rate (cheaper)."""
        mock_select = MagicMock()
        mock_select.first.return_value = None
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        # GPT-3.5 defaults to (15, 15)
        result = tracker.calculate_waddleai_tokens("openai", "gpt-3.5-turbo", 100, 200)

        # 100 / 15 + 200 / 15 ≈ 6 + 13 = 19 (integer division)
        assert result == 19

    def test_calculate_tokens_o1_more_expensive(self, tracker, mock_db):
        """Test o1 has lower conversion rate (more expensive)."""
        mock_select = MagicMock()
        mock_select.first.return_value = None
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        # o1 defaults to (3, 3)
        result = tracker.calculate_waddleai_tokens("openai", "o1", 100, 200)

        # 100 / 3 + 200 / 3 ≈ 33 + 66 = 99
        assert result == 99

    def test_calculate_tokens_anthropic_opus(self, tracker, mock_db):
        """Test Anthropic Opus conversion."""
        mock_select = MagicMock()
        mock_select.first.return_value = None
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        # Opus defaults to (3, 3)
        result = tracker.calculate_waddleai_tokens("anthropic", "claude-opus", 100, 200)
        assert result == 99

    def test_calculate_tokens_anthropic_sonnet(self, tracker, mock_db):
        """Test Anthropic Sonnet conversion."""
        mock_select = MagicMock()
        mock_select.first.return_value = None
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        # Sonnet defaults to (8, 8)
        result = tracker.calculate_waddleai_tokens("anthropic", "claude-sonnet", 100, 200)

        # 100 / 8 + 200 / 8 = 12 + 25 = 37
        assert result == 37

    def test_calculate_tokens_anthropic_haiku(self, tracker, mock_db):
        """Test Anthropic Haiku conversion (cheapest)."""
        mock_select = MagicMock()
        mock_select.first.return_value = None
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        # Haiku defaults to (20, 20)
        result = tracker.calculate_waddleai_tokens("anthropic", "claude-haiku", 100, 200)

        # 100 / 20 + 200 / 20 = 5 + 10 = 15
        assert result == 15

    def test_calculate_tokens_ollama(self, tracker, mock_db):
        """Test Ollama (local) conversion rate."""
        mock_select = MagicMock()
        mock_select.first.return_value = None
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        # Ollama defaults to (100, 100) — effectively free
        result = tracker.calculate_waddleai_tokens("ollama", "llama2", 1000, 2000)

        # 1000 / 100 + 2000 / 100 = 10 + 20 = 30
        assert result == 30

    def test_calculate_tokens_cache_stores_result(self, tracker, mock_db):
        """Test cache stores lookup result."""
        mock_rate = MagicMock()
        mock_rate.input_rate = 5
        mock_rate.output_rate = 5
        mock_select = MagicMock()
        mock_select.first.return_value = mock_rate
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        cache_key = "openai:gpt-4"
        assert cache_key not in tracker._conversion_rates_cache

        tracker.calculate_waddleai_tokens("openai", "gpt-4", 100, 200)

        assert cache_key in tracker._conversion_rates_cache
        assert tracker._conversion_rates_cache[cache_key]["input_rate"] == 5


class TestRecordUsage:
    """Test recording usage events."""

    def test_record_usage_with_virtual_key(self, tracker, mock_db):
        """Test recording usage with existing virtual key."""
        event = make_usage_event(key_id="wa-key123")

        # Mock virtual key lookup - return key on first call, None for usage check
        mock_key = MagicMock()
        mock_key.id = 1
        mock_key.user_id = 10
        mock_key.organization_id = 5

        mock_select = MagicMock()
        # key lookup -> mock_key; calculate_waddleai_tokens's conversion-rate
        # lookup -> None (falls back to defaults); usage lookup -> None (insert path)
        mock_select.first.side_effect = [mock_key, None, None]
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        result = tracker.record_usage(event)

        assert result is True
        # The AILB raw-event insert was dropped by migration 007 -- the
        # aggregate write into token_usage/usage_logs is the whole effect now.
        mock_db.token_usage.insert.assert_called_once()
        mock_db.usage_logs.insert.assert_called_once()

    def test_record_usage_without_virtual_key(self, tracker, mock_db):
        """Unresolvable key_id records nothing.

        There is no raw-event fallback table anymore (ailb_usage_events was
        dropped with no successor) -- an unresolved key means the event is
        dropped, not silently attributed to nobody.
        """
        event = make_usage_event(key_id="wa-unknown")

        # Mock virtual key not found
        mock_select = MagicMock()
        mock_select.first.return_value = None  # No key found
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        result = tracker.record_usage(event)

        assert result is False
        mock_db.token_usage.insert.assert_not_called()
        mock_db.usage_logs.insert.assert_not_called()

    def test_record_usage_calls_calculate_waddleai_tokens(self, tracker, mock_db):
        """Test record_usage calls token conversion once a virtual key resolves."""
        event = make_usage_event(model="gpt-4o")

        mock_key = MagicMock()
        mock_key.id = 1
        mock_key.user_id = 10
        mock_key.organization_id = 5

        mock_select = MagicMock()
        mock_select.first.side_effect = [mock_key, None]  # key lookup, usage lookup
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        with patch.object(tracker, "calculate_waddleai_tokens", return_value=50) as mock_calc:
            tracker.record_usage(event)

            mock_calc.assert_called_once_with("openai", "gpt-4o", 100, 200)

    def test_record_usage_updates_existing_daily_usage(self, tracker, mock_db):
        """Test updating existing daily usage record."""
        event = make_usage_event()

        mock_key = MagicMock()
        mock_key.id = 1
        mock_key.user_id = 10
        mock_key.organization_id = 5

        # First call: virtual key lookup
        # Second call: token_usage lookup (existing record)
        mock_existing_usage = MagicMock()
        mock_existing_usage.id = 100
        mock_existing_usage.waddleai_tokens = 30
        mock_existing_usage.tokens_input_total = 50
        mock_existing_usage.tokens_output_total = 100
        mock_existing_usage.request_count = 2
        mock_existing_usage.cost_usd_total = 0.002

        mock_select = MagicMock()
        mock_select.first.side_effect = [mock_key, mock_existing_usage, None]
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        with patch.object(tracker, "calculate_waddleai_tokens", return_value=50):
            result = tracker.record_usage(event)

        # Verify the operation succeeded
        assert result is True
        # Verify token_usage.insert was NOT called (since we're updating existing)
        mock_db.token_usage.insert.assert_not_called()

    def test_record_usage_inserts_new_daily_usage(self, tracker, mock_db):
        """Test inserting new daily usage record."""
        event = make_usage_event()

        mock_key = MagicMock()
        mock_key.id = 1
        mock_key.user_id = 10
        mock_key.organization_id = 5

        mock_select = MagicMock()
        mock_select.first.side_effect = [
            mock_key,
            None,
            None,
        ]  # Key found, no usage record, no vkey update
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        with patch.object(tracker, "calculate_waddleai_tokens", return_value=50):
            tracker.record_usage(event)

        # Verify insert was called
        mock_db.token_usage.insert.assert_called_once()

    def test_record_usage_creates_usage_log(self, tracker, mock_db):
        """Test creating usage_logs entry."""
        event = make_usage_event()

        mock_key = MagicMock()
        mock_key.id = 1
        mock_key.user_id = 10
        mock_key.organization_id = 5

        mock_select = MagicMock()
        mock_select.first.side_effect = [
            mock_key,
            None,
            None,
        ]  # Key found, no usage record, no vkey update
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        with patch.object(tracker, "calculate_waddleai_tokens", return_value=50):
            tracker.record_usage(event)

        # Verify usage_logs insert was called
        mock_db.usage_logs.insert.assert_called_once()


class TestCheckQuota:
    """Test quota checking."""

    def test_check_quota_key_not_found(self, tracker, mock_db):
        """Test quota check when key doesn't exist."""
        mock_select = MagicMock()
        mock_select.first.return_value = None
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        allowed, quota_info = tracker.check_quota(999)

        assert allowed is False
        assert quota_info.status == QuotaStatus.DISABLED

    def test_check_quota_key_disabled(self, tracker, mock_db):
        """Test quota check when key is disabled."""
        mock_key = MagicMock()
        mock_key.enabled = False
        mock_select = MagicMock()
        mock_select.first.return_value = mock_key
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        allowed, quota_info = tracker.check_quota(1)

        assert allowed is False
        assert quota_info.status == QuotaStatus.DISABLED

    def test_check_quota_no_limit_ok(self, tracker, mock_db):
        """Test quota check with no limit set."""
        mock_key = MagicMock()
        mock_key.enabled = True
        mock_key.budget_limit_daily = None
        mock_key.budget_limit_monthly = None

        mock_select = MagicMock()
        mock_select.first.return_value = mock_key
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        allowed, quota_info = tracker.check_quota(1)

        assert allowed is True
        assert quota_info.status == QuotaStatus.OK

    def test_check_quota_daily_limit_exceeded(self, tracker, mock_db):
        """Test quota check when daily limit exceeded."""
        mock_key = MagicMock()
        mock_key.enabled = True
        mock_key.budget_limit_daily = 10.0
        mock_key.budget_limit_monthly = None

        mock_usage = MagicMock()
        mock_usage.cost_usd_total = 15.0  # Exceeds daily limit

        mock_select = MagicMock()
        mock_select.first.side_effect = [mock_key, mock_usage]
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        allowed, quota_info = tracker.check_quota(1)

        assert allowed is False
        assert quota_info.status == QuotaStatus.EXCEEDED
        assert quota_info.limit == 10.0
        assert quota_info.used == 15.0
        assert quota_info.remaining == 0

    def test_check_quota_monthly_limit_exceeded(self, tracker, mock_db):
        """Test quota check when monthly limit exceeded."""
        mock_key = MagicMock()
        mock_key.enabled = True
        mock_key.budget_limit_daily = None
        mock_key.budget_limit_monthly = 100.0

        mock_usage1 = MagicMock()
        mock_usage1.cost_usd_total = 60.0
        mock_usage2 = MagicMock()
        mock_usage2.cost_usd_total = 50.0

        mock_select = MagicMock()
        mock_select.first.return_value = mock_key
        mock_select.__iter__.return_value = iter([mock_usage1, mock_usage2])
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        allowed, quota_info = tracker.check_quota(1)

        assert allowed is False
        assert quota_info.status == QuotaStatus.EXCEEDED
        assert quota_info.limit == 100.0

    def test_check_quota_warning_80_percent(self, tracker, mock_db):
        """Test quota check returns WARNING over 80% usage."""
        mock_key = MagicMock()
        mock_key.enabled = True
        mock_key.budget_limit_daily = None
        mock_key.budget_limit_monthly = 100.0

        mock_usage1 = MagicMock()
        mock_usage1.cost_usd_total = 81.0  # >80% of limit

        mock_select = MagicMock()
        # first() calls: key lookup, daily usage check
        mock_select.first.side_effect = [mock_key, None]
        mock_select.__iter__ = MagicMock(side_effect=lambda: iter([mock_usage1]))
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        allowed, quota_info = tracker.check_quota(1)

        assert allowed is True
        assert quota_info.status == QuotaStatus.WARNING
        assert quota_info.percentage == 81.0

    def test_check_quota_ok_under_80_percent(self, tracker, mock_db):
        """Test quota check returns OK when under 80% usage."""
        mock_key = MagicMock()
        mock_key.enabled = True
        mock_key.budget_limit_daily = None
        mock_key.budget_limit_monthly = 100.0

        mock_usage1 = MagicMock()
        mock_usage1.cost_usd_total = 50.0  # 50% of limit

        mock_select = MagicMock()
        mock_select.first.return_value = mock_key
        mock_select.__iter__ = MagicMock(side_effect=lambda: iter([mock_usage1]))
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        allowed, quota_info = tracker.check_quota(1)

        assert allowed is True
        assert quota_info.status == QuotaStatus.OK
        assert quota_info.percentage == 50.0


class TestCheckUserQuota:
    """Test user-level quota checking."""

    def test_check_user_quota_user_not_found(self, tracker, mock_db):
        """Test user quota check when user doesn't exist."""
        mock_select = MagicMock()
        mock_select.first.return_value = None
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        allowed, quota_info = tracker.check_user_quota(999)

        assert allowed is False
        assert quota_info.status == QuotaStatus.DISABLED

    def test_check_user_quota_no_quota_set(self, tracker, mock_db):
        """Test user quota check when no quota is set."""
        mock_user = MagicMock()
        mock_user.token_quota_daily = None
        mock_user.token_quota_monthly = None

        mock_select = MagicMock()
        mock_select.first.return_value = mock_user
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        allowed, quota_info = tracker.check_user_quota(1)

        assert allowed is True
        assert quota_info.status == QuotaStatus.OK

    def test_check_user_quota_monthly_exceeded(self, tracker, mock_db):
        """Test user quota check when monthly quota exceeded."""
        mock_user = MagicMock()
        mock_user.token_quota_daily = None
        mock_user.token_quota_monthly = 10000

        mock_usage1 = MagicMock()
        mock_usage1.waddleai_tokens = 6000
        mock_usage2 = MagicMock()
        mock_usage2.waddleai_tokens = 5000  # Total 11000 > 10000

        mock_select = MagicMock()
        mock_select.first.return_value = mock_user
        mock_select.__iter__ = MagicMock(side_effect=lambda: iter([mock_usage1, mock_usage2]))
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        allowed, quota_info = tracker.check_user_quota(1)

        assert allowed is False
        assert quota_info.status == QuotaStatus.EXCEEDED

    def test_check_user_quota_warning_at_80_percent(self, tracker, mock_db):
        """Test user quota check returns WARNING over 80% usage."""
        mock_user = MagicMock()
        mock_user.token_quota_daily = None
        mock_user.token_quota_monthly = 10000

        mock_usage1 = MagicMock()
        mock_usage1.waddleai_tokens = 8100  # >80% of limit

        mock_select = MagicMock()
        # first() returns user, then no daily usage
        mock_select.first.side_effect = [mock_user, None]
        mock_select.__iter__ = MagicMock(side_effect=lambda: iter([mock_usage1]))
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        allowed, quota_info = tracker.check_user_quota(1)

        assert allowed is True
        assert quota_info.status == QuotaStatus.WARNING
        assert quota_info.percentage == 81.0


class TestCheckOrgQuota:
    """Test organization-level quota checking."""

    def test_check_org_quota_org_not_found(self, tracker, mock_db):
        """Test org quota check when org doesn't exist."""
        mock_select = MagicMock()
        mock_select.first.return_value = None
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        allowed, quota_info = tracker.check_org_quota(999)

        assert allowed is False
        assert quota_info.status == QuotaStatus.DISABLED

    def test_check_org_quota_no_quota_set(self, tracker, mock_db):
        """Test org quota check when no quota is set."""
        mock_org = MagicMock()
        mock_org.token_quota_monthly = None

        mock_select = MagicMock()
        mock_select.first.return_value = mock_org
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        allowed, quota_info = tracker.check_org_quota(1)

        assert allowed is True
        assert quota_info.status == QuotaStatus.OK

    def test_check_org_quota_exceeded(self, tracker, mock_db):
        """Test org quota check when quota exceeded."""
        mock_org = MagicMock()
        mock_org.token_quota_monthly = 100000

        mock_usage1 = MagicMock()
        mock_usage1.waddleai_tokens = 60000
        mock_usage2 = MagicMock()
        mock_usage2.waddleai_tokens = 50000  # Total 110000 > 100000

        mock_select = MagicMock()
        mock_select.first.return_value = mock_org
        mock_select.__iter__ = MagicMock(side_effect=lambda: iter([mock_usage1, mock_usage2]))
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        allowed, quota_info = tracker.check_org_quota(1)

        assert allowed is False
        assert quota_info.status == QuotaStatus.EXCEEDED

    def test_check_org_quota_warning(self, tracker, mock_db):
        """Test org quota check returns WARNING over 80% usage."""
        mock_org = MagicMock()
        mock_org.token_quota_monthly = 100000

        mock_usage1 = MagicMock()
        mock_usage1.waddleai_tokens = 81000  # >80% of limit

        mock_select = MagicMock()
        # first() returns org (check_org_quota only calls first() once for org lookup)
        mock_select.first.return_value = mock_org
        mock_select.__iter__ = MagicMock(side_effect=lambda: iter([mock_usage1]))
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        allowed, quota_info = tracker.check_org_quota(1)

        assert allowed is True
        assert quota_info.status == QuotaStatus.WARNING
        assert quota_info.percentage == 81.0


class TestAggregateDailyUsage:
    """Test daily usage aggregation."""

    def test_aggregate_daily_usage_found(self, tracker, mock_db):
        """Test aggregating existing daily usage."""
        today = date.today()

        mock_usage = MagicMock()
        mock_usage.waddleai_tokens = 100
        mock_usage.tokens_input_total = 500
        mock_usage.tokens_output_total = 1000
        mock_usage.request_count = 5
        mock_usage.cost_usd_total = 0.15
        mock_usage.user_id = 10
        mock_usage.organization_id = 5

        mock_select = MagicMock()
        mock_select.first.return_value = mock_usage
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        result = tracker.aggregate_daily_usage(1, today)

        assert isinstance(result, DailyUsage)
        assert result.date == today
        assert result.key_id == 1
        assert result.waddleai_tokens == 100
        assert result.input_tokens == 500
        assert result.output_tokens == 1000
        assert result.request_count == 5
        assert result.cost_usd == 0.15

    def test_aggregate_daily_usage_not_found(self, tracker, mock_db):
        """Test aggregating when no usage record exists."""
        today = date.today()

        mock_select = MagicMock()
        mock_select.first.return_value = None
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        result = tracker.aggregate_daily_usage(1, today)

        assert isinstance(result, DailyUsage)
        assert result.date == today
        assert result.key_id == 1
        assert result.waddleai_tokens == 0
        assert result.request_count == 0

    def test_aggregate_daily_usage_null_fields(self, tracker, mock_db):
        """Test aggregating with null numeric fields."""
        today = date.today()

        mock_usage = MagicMock()
        mock_usage.waddleai_tokens = None
        mock_usage.tokens_input_total = None
        mock_usage.tokens_output_total = None
        mock_usage.request_count = None
        mock_usage.cost_usd_total = None
        mock_usage.user_id = 10
        mock_usage.organization_id = 5

        mock_select = MagicMock()
        mock_select.first.return_value = mock_usage
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        result = tracker.aggregate_daily_usage(1, today)

        # Null values should default to 0
        assert result.waddleai_tokens == 0
        assert result.input_tokens == 0
        assert result.output_tokens == 0
        assert result.request_count == 0
        assert result.cost_usd == 0


class TestGetUsageStats:
    """Test usage statistics generation."""

    def test_get_usage_stats_by_key(self, tracker, mock_db):
        """Test getting stats filtered by key_id."""
        mock_usage1 = MagicMock()
        mock_usage1.waddleai_tokens = 100
        mock_usage1.tokens_input_total = 500
        mock_usage1.tokens_output_total = 1000
        mock_usage1.request_count = 5
        mock_usage1.cost_usd_total = 0.15
        mock_usage1.date = date.today()

        mock_select = MagicMock()
        mock_select.__iter__.return_value = iter([mock_usage1])
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        result = tracker.get_usage_stats(key_id=1)

        assert isinstance(result, UsageStats)
        assert result.total_tokens == 100
        assert result.input_tokens == 500
        assert result.output_tokens == 1000
        assert result.request_count == 5
        assert result.cost_usd == 0.15

    def test_get_usage_stats_by_user(self, tracker, mock_db):
        """Test getting stats filtered by user_id."""
        mock_usage = MagicMock()
        mock_usage.waddleai_tokens = 50
        mock_usage.tokens_input_total = 250
        mock_usage.tokens_output_total = 500
        mock_usage.request_count = 3
        mock_usage.cost_usd_total = 0.05
        mock_usage.date = date.today()

        mock_select = MagicMock()
        mock_select.__iter__.return_value = iter([mock_usage])
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        result = tracker.get_usage_stats(user_id=10)

        assert result.total_tokens == 50
        assert result.request_count == 3

    def test_get_usage_stats_by_org(self, tracker, mock_db):
        """Test getting stats filtered by organization_id."""
        mock_select = MagicMock()
        mock_select.__iter__.return_value = iter([])
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        result = tracker.get_usage_stats(organization_id=5)

        assert result.total_tokens == 0
        assert result.request_count == 0

    def test_get_usage_stats_multiple_days(self, tracker, mock_db):
        """Test aggregating stats across multiple days."""
        mock_usage1 = MagicMock()
        mock_usage1.waddleai_tokens = 100
        mock_usage1.tokens_input_total = 500
        mock_usage1.tokens_output_total = 1000
        mock_usage1.request_count = 5
        mock_usage1.cost_usd_total = 0.15
        mock_usage1.date = date.today()

        mock_usage2 = MagicMock()
        mock_usage2.waddleai_tokens = 50
        mock_usage2.tokens_input_total = 250
        mock_usage2.tokens_output_total = 500
        mock_usage2.request_count = 3
        mock_usage2.cost_usd_total = 0.05
        mock_usage2.date = date.today() - timedelta(days=1)

        mock_select = MagicMock()
        mock_select.__iter__.return_value = iter([mock_usage1, mock_usage2])
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        result = tracker.get_usage_stats(key_id=1, days=30)

        assert result.total_tokens == 150  # 100 + 50
        assert result.request_count == 8  # 5 + 3
        assert result.cost_usd == 0.20  # 0.15 + 0.05

    def test_get_usage_stats_by_day_grouping(self, tracker, mock_db):
        """Test stats grouped by day."""
        today = date.today()
        yesterday = today - timedelta(days=1)

        mock_usage1 = MagicMock()
        mock_usage1.waddleai_tokens = 100
        mock_usage1.tokens_input_total = 0
        mock_usage1.tokens_output_total = 0
        mock_usage1.request_count = 0
        mock_usage1.cost_usd_total = 0
        mock_usage1.date = today

        mock_usage2 = MagicMock()
        mock_usage2.waddleai_tokens = 50
        mock_usage2.tokens_input_total = 0
        mock_usage2.tokens_output_total = 0
        mock_usage2.request_count = 0
        mock_usage2.cost_usd_total = 0
        mock_usage2.date = yesterday

        mock_select = MagicMock()
        mock_select.__iter__.return_value = iter([mock_usage1, mock_usage2])
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        result = tracker.get_usage_stats(key_id=1)

        assert today.isoformat() in result.by_day
        assert result.by_day[today.isoformat()] == 100
        assert yesterday.isoformat() in result.by_day
        assert result.by_day[yesterday.isoformat()] == 50

    def test_get_usage_stats_empty(self, tracker, mock_db):
        """Test getting stats when no usage records exist."""
        mock_select = MagicMock()
        mock_select.__iter__.return_value = iter([])
        mock_db.return_value = mock_select
        mock_db.return_value.select.return_value = mock_select

        result = tracker.get_usage_stats(key_id=999)

        assert result.total_tokens == 0
        assert result.request_count == 0
        assert result.by_day == {}


class TestDataclassDefaults:
    """Test dataclass default values."""

    def test_daily_usage_defaults(self):
        """Test DailyUsage default values."""
        today = date.today()
        usage = DailyUsage(date=today, key_id=1)

        assert usage.date == today
        assert usage.key_id == 1
        assert usage.waddleai_tokens == 0
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.request_count == 0
        assert usage.cost_usd == 0.0

    def test_usage_stats_defaults(self):
        """Test UsageStats default values."""
        stats = UsageStats()

        assert stats.total_tokens == 0
        assert stats.input_tokens == 0
        assert stats.output_tokens == 0
        assert stats.request_count == 0
        assert stats.cost_usd == 0.0
        assert stats.by_model == {}
        assert stats.by_provider == {}
        assert stats.by_day == {}

    def test_quota_info_defaults(self):
        """Test QuotaInfo default values."""
        info = QuotaInfo(status=QuotaStatus.OK)

        assert info.status == QuotaStatus.OK
        assert info.limit is None
        assert info.used == 0
        assert info.remaining is None
        assert info.percentage == 0.0
        assert info.resets_at is None
