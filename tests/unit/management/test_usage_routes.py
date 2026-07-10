"""
Unit tests for usage tracking routes: /api/v1/usage/*
"""

from datetime import date, datetime
from typing import Dict
from unittest.mock import MagicMock

from tests.unit.management.conftest import make_select_result

# ---------------------------------------------------------------------------
# Fixtures: Mock usage records
# ---------------------------------------------------------------------------


def make_mock_token_usage(
    user_id: int = 1,
    org_id: int = 1,
    key_id: int = 1,
    waddleai_tokens: int = 1000,
    tokens_input: int = 500,
    tokens_output: int = 500,
    request_count: int = 5,
    cost_usd: float = 0.05,
    usage_date: date = None,
) -> MagicMock:
    """Create a mock token_usage record."""
    if usage_date is None:
        usage_date = date.today()
    record = MagicMock()
    record.user_id = user_id
    record.organization_id = org_id
    record.virtual_key_id = key_id
    record.waddleai_tokens = waddleai_tokens
    record.tokens_input_total = tokens_input
    record.tokens_output_total = tokens_output
    record.request_count = request_count
    record.cost_usd_total = cost_usd
    record.date = usage_date
    return record


def make_mock_usage_log(
    user_id: int = 1,
    org_id: int = 1,
    model: str = "gpt-4o",
    provider: str = "openai",
    waddleai_tokens: int = 100,
    cost_usd: float = 0.005,
    tokens_input: int = 50,
    tokens_output: int = 50,
    timestamp: datetime = None,
) -> MagicMock:
    """Create a mock usage_logs record."""
    if timestamp is None:
        timestamp = datetime.utcnow()
    record = MagicMock()
    record.user_id = user_id
    record.organization_id = org_id
    record.model_used = model
    record.provider_type = provider
    record.waddleai_tokens_used = waddleai_tokens
    record.cost_estimate_usd = cost_usd
    record.llm_tokens_input = tokens_input
    record.llm_tokens_output = tokens_output
    record.timestamp = timestamp
    return record


def make_mock_user(user_id: int = 1, username: str = "testuser") -> MagicMock:
    """Create a mock user record."""
    user = MagicMock()
    user.id = user_id
    user.username = username
    return user


def make_mock_key(key_id: int = 1, name: str = "Test Key", prefix: str = "wa-test...") -> MagicMock:
    """Create a mock virtual_key record."""
    key = MagicMock()
    key.id = key_id
    key.name = name
    key.key_prefix = prefix
    return key


# ---------------------------------------------------------------------------
# GET /api/v1/usage/summary
# ---------------------------------------------------------------------------


class TestGetUsageSummary:
    """Tests for GET /api/v1/usage/summary"""

    async def test_summary_admin_gets_all_usage(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can see summary of all token usage."""
        _today = date.today()
        monthly_start = _today.replace(day=1)

        daily_usage = make_mock_token_usage(user_id=1, org_id=1, cost_usd=0.10, usage_date=_today)
        monthly_usage = make_mock_token_usage(user_id=2, org_id=1, cost_usd=0.25, usage_date=monthly_start)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([daily_usage]),
            make_select_result([monthly_usage]),
        ]

        resp = await client.get("/api/v1/usage/summary", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "summary" in data
        assert "daily" in data["summary"]
        assert "monthly" in data["summary"]
        assert data["summary"]["daily"]["date"] == _today.isoformat()
        assert data["summary"]["daily"]["cost_usd"] == 0.10
        assert data["summary"]["monthly"]["month"] == monthly_start.isoformat()

    async def test_summary_resource_manager_filters_by_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager sees only their organization's usage."""
        org_usage = make_mock_token_usage(org_id=1, cost_usd=0.15)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([org_usage]),
            make_select_result([org_usage]),
        ]

        resp = await client.get("/api/v1/usage/summary", headers=rm_auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["summary"]["daily"]["cost_usd"] == 0.15

    async def test_summary_regular_user_filters_by_user_id(
        self, client, app_mock_db: MagicMock, user_auth_headers: Dict
    ) -> None:
        """Regular user sees only their own usage."""
        user_usage = make_mock_token_usage(user_id=2, cost_usd=0.05)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([user_usage]),
            make_select_result([user_usage]),
        ]

        resp = await client.get("/api/v1/usage/summary", headers=user_auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["summary"]["daily"]["cost_usd"] == 0.05

    async def test_summary_no_auth_returns_401(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/usage/summary")
        assert resp.status_code == 401

    async def test_summary_accumulates_multiple_records(
        self, client, app_mock_db: MagicMock, auth_headers: Dict
    ) -> None:
        """Summary sums across multiple records."""
        r1 = make_mock_token_usage(waddleai_tokens=500, cost_usd=0.05)
        r2 = make_mock_token_usage(waddleai_tokens=300, cost_usd=0.03)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([r1, r2]),
            make_select_result([r1, r2]),
        ]

        resp = await client.get("/api/v1/usage/summary", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["summary"]["daily"]["waddleai_tokens"] == 800
        assert data["summary"]["daily"]["cost_usd"] == 0.08


# ---------------------------------------------------------------------------
# GET /api/v1/usage/by-model
# ---------------------------------------------------------------------------


class TestGetUsageByModel:
    """Tests for GET /api/v1/usage/by-model"""

    async def test_by_model_groups_usage(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Usage grouped by model_used field."""
        gpt4_usage = make_mock_usage_log(model="gpt-4o", waddleai_tokens=1000, cost_usd=0.10)
        gpt35_usage = make_mock_usage_log(model="gpt-3.5-turbo", waddleai_tokens=500, cost_usd=0.01)

        app_mock_db.return_value.select.return_value = make_select_result([gpt4_usage, gpt35_usage])

        resp = await client.get("/api/v1/usage/by-model?days=30", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "by_model" in data
        assert "gpt-4o" in data["by_model"]
        assert "gpt-3.5-turbo" in data["by_model"]
        assert data["by_model"]["gpt-4o"]["tokens"] == 1000
        assert data["by_model"]["gpt-4o"]["cost_usd"] == 0.10
        assert data["period_days"] == 30

    async def test_by_model_custom_days(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Custom days parameter is reflected in response."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/usage/by-model?days=60", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["period_days"] == 60

    async def test_by_model_resource_manager_filter(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager sees only their org's model usage."""
        usage = make_mock_usage_log(org_id=1, model="gpt-4o")
        app_mock_db.return_value.select.return_value = make_select_result([usage])

        resp = await client.get("/api/v1/usage/by-model", headers=rm_auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "gpt-4o" in data["by_model"]

    async def test_by_model_user_filter(self, client, app_mock_db: MagicMock, user_auth_headers: Dict) -> None:
        """Regular user sees only their own model usage."""
        usage = make_mock_usage_log(user_id=2, model="gpt-3.5-turbo")
        app_mock_db.return_value.select.return_value = make_select_result([usage])

        resp = await client.get("/api/v1/usage/by-model", headers=user_auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "gpt-3.5-turbo" in data["by_model"]

    async def test_by_model_empty_result(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Empty result returns empty model dict."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/usage/by-model", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["by_model"] == {}


# ---------------------------------------------------------------------------
# GET /api/v1/usage/by-provider
# ---------------------------------------------------------------------------


class TestGetUsageByProvider:
    """Tests for GET /api/v1/usage/by-provider"""

    async def test_by_provider_groups_usage(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Usage grouped by provider_type field."""
        openai_usage = make_mock_usage_log(
            provider="openai", waddleai_tokens=2000, cost_usd=0.20, tokens_input=1000, tokens_output=1000
        )
        anthropic_usage = make_mock_usage_log(
            provider="anthropic", waddleai_tokens=800, cost_usd=0.08, tokens_input=400, tokens_output=400
        )

        app_mock_db.return_value.select.return_value = make_select_result([openai_usage, anthropic_usage])

        resp = await client.get("/api/v1/usage/by-provider?days=30", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "by_provider" in data
        assert "openai" in data["by_provider"]
        assert "anthropic" in data["by_provider"]
        assert data["by_provider"]["openai"]["tokens"] == 2000
        assert data["by_provider"]["openai"]["tokens_input"] == 1000
        assert data["by_provider"]["openai"]["tokens_output"] == 1000
        assert data["by_provider"]["anthropic"]["cost_usd"] == 0.08

    async def test_by_provider_custom_days(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Custom days parameter reflected in response."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/usage/by-provider?days=90", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["period_days"] == 90

    async def test_by_provider_empty_result(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Empty result returns empty provider dict."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/usage/by-provider", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["by_provider"] == {}


# ---------------------------------------------------------------------------
# GET /api/v1/usage/by-user
# ---------------------------------------------------------------------------


class TestGetUsageByUser:
    """Tests for GET /api/v1/usage/by-user"""

    async def test_by_user_admin_sees_all(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin sees usage for all users."""
        user1_usage = make_mock_token_usage(user_id=1, cost_usd=0.10)
        user2_usage = make_mock_token_usage(user_id=2, cost_usd=0.05)
        user1_record = make_mock_user(user_id=1, username="user1")
        user2_record = make_mock_user(user_id=2, username="user2")

        app_mock_db.return_value.select.side_effect = [
            make_select_result([user1_usage, user2_usage]),
            make_select_result([user1_record]),
            make_select_result([user2_record]),
        ]

        resp = await client.get("/api/v1/usage/by-user?days=30", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "by_user" in data
        assert len(data["by_user"]) == 2
        assert data["by_user"][0]["user_id"] == 1
        assert data["by_user"][1]["user_id"] == 2

    async def test_by_user_resource_manager_filters_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager sees only their org's users."""
        usage = make_mock_token_usage(user_id=1, org_id=1)
        user_rec = make_mock_user(user_id=1, username="admin")

        app_mock_db.return_value.select.side_effect = [
            make_select_result([usage]),
            make_select_result([user_rec]),
        ]

        resp = await client.get("/api/v1/usage/by-user", headers=rm_auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert len(data["by_user"]) == 1

    async def test_by_user_regular_user_forbidden(self, client, user_auth_headers: Dict) -> None:
        """Regular user cannot access by-user endpoint → 403."""
        resp = await client.get("/api/v1/usage/by-user", headers=user_auth_headers)
        assert resp.status_code == 403

    async def test_by_user_no_auth_returns_401(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/usage/by-user")
        assert resp.status_code == 401

    async def test_by_user_accumulates_per_user(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Multiple records per user are summed."""
        r1 = make_mock_token_usage(user_id=1, waddleai_tokens=500, request_count=2, cost_usd=0.05)
        r2 = make_mock_token_usage(user_id=1, waddleai_tokens=300, request_count=1, cost_usd=0.03)
        user_rec = make_mock_user(user_id=1, username="user1")

        app_mock_db.return_value.select.side_effect = [
            make_select_result([r1, r2]),
            make_select_result([user_rec]),
        ]

        resp = await client.get("/api/v1/usage/by-user", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["by_user"][0]["tokens"] == 800
        assert data["by_user"][0]["requests"] == 3
        assert data["by_user"][0]["cost_usd"] == 0.08

    async def test_by_user_unknown_user_handled(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """User not found returns 'unknown' username."""
        usage = make_mock_token_usage(user_id=999)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([usage]),
            make_select_result([]),  # User not found
        ]

        resp = await client.get("/api/v1/usage/by-user", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["by_user"][0]["username"] == "unknown"


# ---------------------------------------------------------------------------
# GET /api/v1/usage/by-key
# ---------------------------------------------------------------------------


class TestGetUsageByKey:
    """Tests for GET /api/v1/usage/by-key"""

    async def test_by_key_groups_by_virtual_key_id(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Usage grouped by virtual_key_id with key details."""
        key1_usage = make_mock_token_usage(key_id=1, waddleai_tokens=500, cost_usd=0.05)
        key2_usage = make_mock_token_usage(key_id=2, waddleai_tokens=300, cost_usd=0.03)
        key1_record = make_mock_key(key_id=1, name="Production Key")
        key2_record = make_mock_key(key_id=2, name="Testing Key")

        app_mock_db.return_value.select.side_effect = [
            make_select_result([key1_usage, key2_usage]),
            make_select_result([key1_record]),
            make_select_result([key2_record]),
        ]

        resp = await client.get("/api/v1/usage/by-key?days=30", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "by_key" in data
        assert len(data["by_key"]) == 2
        assert data["by_key"][0]["key_id"] == 1
        assert data["by_key"][0]["key_name"] == "Production Key"
        assert data["by_key"][1]["key_id"] == 2

    async def test_by_key_admin_sees_all(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin sees usage across all keys."""
        usage = make_mock_token_usage(key_id=1)
        key_rec = make_mock_key(key_id=1)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([usage]),
            make_select_result([key_rec]),
        ]

        resp = await client.get("/api/v1/usage/by-key", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert len(data["by_key"]) == 1

    async def test_by_key_resource_manager_filters_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager sees only their org's keys."""
        usage = make_mock_token_usage(key_id=1, org_id=1)
        key_rec = make_mock_key(key_id=1)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([usage]),
            make_select_result([key_rec]),
        ]

        resp = await client.get("/api/v1/usage/by-key", headers=rm_auth_headers)
        assert resp.status_code == 200

    async def test_by_key_user_filter(self, client, app_mock_db: MagicMock, user_auth_headers: Dict) -> None:
        """Regular user sees only their own keys."""
        usage = make_mock_token_usage(user_id=2, key_id=1)
        key_rec = make_mock_key(key_id=1)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([usage]),
            make_select_result([key_rec]),
        ]

        resp = await client.get("/api/v1/usage/by-key", headers=user_auth_headers)
        assert resp.status_code == 200

    async def test_by_key_no_key_id_skipped(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Records without key_id are skipped."""
        usage_with_key = make_mock_token_usage(key_id=1)
        usage_no_key = make_mock_token_usage(key_id=None)
        key_rec = make_mock_key(key_id=1)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([usage_with_key, usage_no_key]),
            make_select_result([key_rec]),
        ]

        resp = await client.get("/api/v1/usage/by-key", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert len(data["by_key"]) == 1

    async def test_by_key_unknown_key_handled(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Key not found returns 'unknown' name/prefix."""
        usage = make_mock_token_usage(key_id=999)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([usage]),
            make_select_result([]),  # Key not found
        ]

        resp = await client.get("/api/v1/usage/by-key", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["by_key"][0]["key_name"] == "unknown"
        assert data["by_key"][0]["key_prefix"] == "unknown"


# ---------------------------------------------------------------------------
# GET /api/v1/usage/cost
# ---------------------------------------------------------------------------


class TestGetCostAnalytics:
    """Tests for GET /api/v1/usage/cost"""

    async def test_cost_analytics_basic(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Cost analytics returns daily breakdown and totals."""
        today = date.today()
        r1 = make_mock_token_usage(cost_usd=0.10, usage_date=today)
        r2 = make_mock_token_usage(cost_usd=0.05, usage_date=today)

        app_mock_db.return_value.select.return_value = make_select_result([r1, r2])

        resp = await client.get("/api/v1/usage/cost?days=30", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "total_cost_usd" in data
        assert "avg_daily_cost_usd" in data
        assert "projected_monthly_cost_usd" in data
        assert "daily_cost" in data
        assert data["total_cost_usd"] == 0.15
        assert data["period_days"] == 30

    async def test_cost_analytics_daily_breakdown(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Daily costs are keyed by date ISO format."""
        date1 = date(2025, 1, 1)
        date2 = date(2025, 1, 2)
        r1 = make_mock_token_usage(cost_usd=0.10, usage_date=date1)
        r2 = make_mock_token_usage(cost_usd=0.05, usage_date=date2)

        app_mock_db.return_value.select.return_value = make_select_result([r1, r2])

        resp = await client.get("/api/v1/usage/cost", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert date1.isoformat() in data["daily_cost"]
        assert date2.isoformat() in data["daily_cost"]
        assert data["daily_cost"][date1.isoformat()] == 0.10

    async def test_cost_analytics_admin_all_usage(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin sees all cost usage."""
        usage = make_mock_token_usage(org_id=1, cost_usd=0.20)
        app_mock_db.return_value.select.return_value = make_select_result([usage])

        resp = await client.get("/api/v1/usage/cost", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["total_cost_usd"] == 0.20

    async def test_cost_analytics_resource_manager_filter(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager sees only their org's costs."""
        usage = make_mock_token_usage(org_id=1, cost_usd=0.15)
        app_mock_db.return_value.select.return_value = make_select_result([usage])

        resp = await client.get("/api/v1/usage/cost", headers=rm_auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["total_cost_usd"] == 0.15

    async def test_cost_analytics_user_filter(self, client, app_mock_db: MagicMock, user_auth_headers: Dict) -> None:
        """Regular user sees only their own costs."""
        usage = make_mock_token_usage(user_id=2, cost_usd=0.08)
        app_mock_db.return_value.select.return_value = make_select_result([usage])

        resp = await client.get("/api/v1/usage/cost", headers=user_auth_headers)
        assert resp.status_code == 200

    async def test_cost_analytics_calculates_projections(
        self, client, app_mock_db: MagicMock, auth_headers: Dict
    ) -> None:
        """Calculates average daily and projected monthly costs."""
        usage = make_mock_token_usage(cost_usd=1.00)
        app_mock_db.return_value.select.return_value = make_select_result([usage])

        resp = await client.get("/api/v1/usage/cost?days=10", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        expected_avg = 1.00 / 10
        expected_proj = expected_avg * 30
        assert abs(data["avg_daily_cost_usd"] - expected_avg) < 0.0001
        assert abs(data["projected_monthly_cost_usd"] - expected_proj) < 0.0001

    async def test_cost_analytics_empty_result(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Empty result returns zero costs."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/usage/cost", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["total_cost_usd"] == 0.0
        assert data["avg_daily_cost_usd"] == 0.0
        assert data["daily_cost"] == {}


# ---------------------------------------------------------------------------
# GET /api/v1/usage/export
# ---------------------------------------------------------------------------


class TestExportUsage:
    """Tests for GET /api/v1/usage/export"""

    async def test_export_json_format(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """JSON export returns data array with count."""
        usage = make_mock_token_usage(user_id=1, cost_usd=0.10)
        app_mock_db.return_value.select.return_value = make_select_result([usage])

        resp = await client.get("/api/v1/usage/export?format=json", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "data" in data
        assert "count" in data
        assert isinstance(data["data"], list)
        assert len(data["data"]) == 1
        assert data["count"] == 1

    async def test_export_json_includes_fields(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """JSON export includes all required fields."""
        usage = make_mock_token_usage(
            user_id=1,
            org_id=1,
            key_id=1,
            waddleai_tokens=1000,
            tokens_input=500,
            tokens_output=500,
            request_count=5,
            cost_usd=0.10,
            usage_date=date(2025, 1, 1),
        )
        app_mock_db.return_value.select.return_value = make_select_result([usage])

        resp = await client.get("/api/v1/usage/export?format=json", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        record = data["data"][0]
        assert record["user_id"] == 1
        assert record["organization_id"] == 1
        assert record["virtual_key_id"] == 1
        assert record["waddleai_tokens"] == 1000
        assert record["tokens_input"] == 500
        assert record["cost_usd"] == 0.10
        assert record["date"] == "2025-01-01"

    async def test_export_csv_format(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """CSV export returns proper CSV content."""
        usage = make_mock_token_usage(cost_usd=0.10)
        app_mock_db.return_value.select.return_value = make_select_result([usage])

        resp = await client.get("/api/v1/usage/export?format=csv", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.mimetype == "text/csv"
        assert "content-disposition" in resp.headers
        assert "date,user_id" in (await resp.get_data(as_text=True))

    async def test_export_csv_includes_headers(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """CSV has proper header row."""
        usage = make_mock_token_usage(cost_usd=0.10)
        app_mock_db.return_value.select.return_value = make_select_result([usage])

        resp = await client.get("/api/v1/usage/export?format=csv", headers=auth_headers)
        assert resp.status_code == 200
        content = await resp.get_data(as_text=True)
        lines = content.strip().split("\n")
        assert len(lines) >= 1
        # Check for at least one header field
        assert "date" in lines[0] or "user_id" in lines[0]

    async def test_export_csv_empty_returns_empty(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """CSV with no data returns empty response."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/usage/export?format=csv", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.mimetype == "text/csv"
        assert (await resp.get_data(as_text=True)) == ""

    async def test_export_default_format_json(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Default format without parameter is JSON."""
        usage = make_mock_token_usage()
        app_mock_db.return_value.select.return_value = make_select_result([usage])

        resp = await client.get("/api/v1/usage/export", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.content_type == "application/json"
        data = await resp.get_json()
        assert "data" in data

    async def test_export_admin_all_usage(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin exports all usage."""
        r1 = make_mock_token_usage(user_id=1)
        r2 = make_mock_token_usage(user_id=2)
        app_mock_db.return_value.select.return_value = make_select_result([r1, r2])

        resp = await client.get("/api/v1/usage/export?format=json", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["count"] == 2

    async def test_export_resource_manager_filter(self, client, app_mock_db: MagicMock, rm_auth_headers: Dict) -> None:
        """Resource manager exports only their org's usage."""
        usage = make_mock_token_usage(org_id=1)
        app_mock_db.return_value.select.return_value = make_select_result([usage])

        resp = await client.get("/api/v1/usage/export?format=json", headers=rm_auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["count"] == 1

    async def test_export_user_filter(self, client, app_mock_db: MagicMock, user_auth_headers: Dict) -> None:
        """Regular user exports only their own usage."""
        usage = make_mock_token_usage(user_id=2)
        app_mock_db.return_value.select.return_value = make_select_result([usage])

        resp = await client.get("/api/v1/usage/export?format=json", headers=user_auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["count"] == 1

    async def test_export_custom_days(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Custom days parameter filters results appropriately."""
        usage = make_mock_token_usage()
        app_mock_db.return_value.select.return_value = make_select_result([usage])

        resp = await client.get("/api/v1/usage/export?days=60&format=json", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["count"] == 1

    async def test_export_no_auth_returns_401(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/usage/export")
        assert resp.status_code == 401
