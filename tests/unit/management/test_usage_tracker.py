"""
Unit tests for usage tracking service
"""

from datetime import date

from services.management.app.services.usage_tracker import (
    DailyUsage,
    QuotaInfo,
    QuotaStatus,
    UsageEvent,
    UsageStats,
    UsageTrackingService,
)


class MockDB:
    """Mock database for testing"""

    def __init__(self):
        self.data = {
            "virtual_keys": [],
            "ailb_usage_events": [],
            "token_usage": [],
            "usage_logs": [],
            "token_conversion_rates": [],
            "users": [],
            "organizations": [],
        }
        self._committed = False

    def __call__(self, query):
        return MockQuery(self)

    def __getattr__(self, name):
        if name in self.data:
            return MockTable(name, self)
        return object.__getattribute__(self, name)

    def commit(self):
        self._committed = True


class MockTable:
    """Mock database table"""

    def __init__(self, name, db):
        self.name = name
        self.db = db

    def insert(self, **kwargs):
        record = {"id": len(self.db.data[self.name]) + 1, **kwargs}
        self.db.data[self.name].append(record)
        return record["id"]

    def __getattr__(self, name):
        return MockField(self.name, name)


class MockField:
    """Mock database field"""

    def __init__(self, table, field):
        self.table = table
        self.field = field

    def __eq__(self, other):
        return self

    def __ne__(self, other):
        return self

    def __ge__(self, other):
        return self

    def __gt__(self, other):
        return self

    def __lt__(self, other):
        return self

    def __le__(self, other):
        return self

    def __and__(self, other):
        return self

    def __rand__(self, other):
        return self

    def __or__(self, other):
        return self

    def __ror__(self, other):
        return self

    def like(self, pattern):
        return self

    def __hash__(self):
        return id(self)


class MockQuery:
    """Mock database query"""

    def __init__(self, db):
        self.db = db

    def select(self):
        return MockResultSet([])

    def first(self):
        return None

    def update(self, **kwargs):
        return 0


class MockResultSet:
    """Mock result set"""

    def __init__(self, data):
        self._data = data

    def first(self):
        return self._data[0] if self._data else None

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)


class TestUsageEvent:
    """Test UsageEvent dataclass"""

    def test_create_usage_event(self):
        """Test creating a usage event"""
        event = UsageEvent(
            event_id="evt_123",
            key_id="wa-test",
            request_id="req_456",
            model="gpt-4o",
            provider="openai",
            input_tokens=100,
            output_tokens=200,
            cost_usd=0.01,
            latency_ms=500,
            status="success",
        )
        assert event.event_id == "evt_123"
        assert event.model == "gpt-4o"
        assert event.input_tokens == 100
        assert event.output_tokens == 200
        assert event.timestamp is not None

    def test_usage_event_with_error(self):
        """Test creating a usage event with error"""
        event = UsageEvent(
            event_id="evt_err",
            key_id="wa-test",
            request_id="req_789",
            model="gpt-4o",
            provider="openai",
            input_tokens=100,
            output_tokens=0,
            cost_usd=0.0,
            latency_ms=100,
            status="error",
            error_message="Rate limited",
        )
        assert event.status == "error"
        assert event.error_message == "Rate limited"


class TestDailyUsage:
    """Test DailyUsage dataclass"""

    def test_create_daily_usage(self):
        """Test creating daily usage"""
        usage = DailyUsage(date=date.today(), key_id=1, waddleai_tokens=1000, request_count=50)
        assert usage.waddleai_tokens == 1000
        assert usage.request_count == 50

    def test_daily_usage_defaults(self):
        """Test daily usage defaults"""
        usage = DailyUsage(date=date.today())
        assert usage.waddleai_tokens == 0
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.request_count == 0
        assert usage.cost_usd == 0.0


class TestUsageStats:
    """Test UsageStats dataclass"""

    def test_usage_stats_defaults(self):
        """Test usage stats defaults"""
        stats = UsageStats()
        assert stats.total_tokens == 0
        assert stats.request_count == 0
        assert stats.by_model == {}
        assert stats.by_provider == {}


class TestQuotaInfo:
    """Test QuotaInfo dataclass"""

    def test_quota_info_ok(self):
        """Test OK quota status"""
        info = QuotaInfo(status=QuotaStatus.OK, limit=10000, used=5000, remaining=5000, percentage=50.0)
        assert info.status == QuotaStatus.OK
        assert info.percentage == 50.0

    def test_quota_info_exceeded(self):
        """Test exceeded quota status"""
        info = QuotaInfo(status=QuotaStatus.EXCEEDED, limit=10000, used=10000, remaining=0, percentage=100.0)
        assert info.status == QuotaStatus.EXCEEDED


class TestUsageTrackingService:
    """Test UsageTrackingService"""

    def test_init(self):
        """Test service initialization"""
        db = MockDB()
        service = UsageTrackingService(db)
        assert service.db == db

    def test_calculate_waddleai_tokens_openai_gpt4(self):
        """Test token calculation for GPT-4"""
        db = MockDB()
        service = UsageTrackingService(db)

        # GPT-4 has 5:1 conversion rate
        tokens = service.calculate_waddleai_tokens("openai", "gpt-4o", 100, 100)
        # 100/5 + 100/5 = 40
        assert tokens == 40

    def test_calculate_waddleai_tokens_openai_gpt35(self):
        """Test token calculation for GPT-3.5"""
        db = MockDB()
        service = UsageTrackingService(db)

        # GPT-3.5 has 15:1 conversion rate
        tokens = service.calculate_waddleai_tokens("openai", "gpt-3.5-turbo", 150, 150)
        # 150/15 + 150/15 = 20
        assert tokens == 20

    def test_calculate_waddleai_tokens_anthropic_opus(self):
        """Test token calculation for Claude Opus"""
        db = MockDB()
        service = UsageTrackingService(db)

        # Opus has 3:1 conversion rate (expensive)
        tokens = service.calculate_waddleai_tokens("anthropic", "claude-3-opus-20240229", 90, 90)
        # 90/3 + 90/3 = 60
        assert tokens == 60

    def test_calculate_waddleai_tokens_anthropic_haiku(self):
        """Test token calculation for Claude Haiku"""
        db = MockDB()
        service = UsageTrackingService(db)

        # Haiku has 20:1 conversion rate (cheap)
        tokens = service.calculate_waddleai_tokens("anthropic", "claude-3-haiku-20240307", 200, 200)
        # 200/20 + 200/20 = 20
        assert tokens == 20

    def test_calculate_waddleai_tokens_ollama(self):
        """Test token calculation for Ollama (local)"""
        db = MockDB()
        service = UsageTrackingService(db)

        # Ollama has 100:1 conversion rate (free/cheap)
        tokens = service.calculate_waddleai_tokens("ollama", "llama3.2", 1000, 1000)
        # 1000/100 + 1000/100 = 20
        assert tokens == 20

    def test_get_default_rates_openai(self):
        """Test default rates for OpenAI"""
        db = MockDB()
        service = UsageTrackingService(db)

        input_rate, output_rate = service._get_default_rates("openai", "gpt-4o")
        assert input_rate == 5
        assert output_rate == 5

        input_rate, output_rate = service._get_default_rates("openai", "gpt-3.5-turbo")
        assert input_rate == 15
        assert output_rate == 15

    def test_get_default_rates_anthropic(self):
        """Test default rates for Anthropic"""
        db = MockDB()
        service = UsageTrackingService(db)

        input_rate, output_rate = service._get_default_rates("anthropic", "claude-3-opus-20240229")
        assert input_rate == 3

        input_rate, output_rate = service._get_default_rates("anthropic", "claude-3-sonnet-20240229")
        assert input_rate == 8

        input_rate, output_rate = service._get_default_rates("anthropic", "claude-3-haiku-20240307")
        assert input_rate == 20

    def test_get_default_rates_unknown_provider(self):
        """Test default rates for unknown provider"""
        db = MockDB()
        service = UsageTrackingService(db)

        input_rate, output_rate = service._get_default_rates("unknown", "model")
        assert input_rate == 10
        assert output_rate == 10


class TestQuotaChecking:
    """Test quota checking functionality"""

    def test_quota_status_enum(self):
        """Test QuotaStatus enum values"""
        assert QuotaStatus.OK == "ok"
        assert QuotaStatus.WARNING == "warning"
        assert QuotaStatus.EXCEEDED == "exceeded"
        assert QuotaStatus.DISABLED == "disabled"
