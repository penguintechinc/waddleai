"""Unit tests for GET /api/v1/usage/cache-stats (spec §6.4)."""

from datetime import date
from unittest.mock import MagicMock

from tests.unit.management.conftest import make_select_result


def make_cache_usage_row(
    org_id: int = 1,
    vkey_id: int = 1,
    cache_status: str = "exact",
    tokens_saved: int = 100,
    request_count: int = 5,
    tokens_input: int = 50,
    tokens_output: int = 50,
    cost_usd_cents: int = 10,
) -> MagicMock:
    """Make cache usage row."""
    row = MagicMock()
    row.organization_id = org_id
    row.virtual_key_id = vkey_id
    row.cache_status = cache_status
    row.tokens_saved = tokens_saved
    row.request_count = request_count
    row.tokens_input_total = tokens_input
    row.tokens_output_total = tokens_output
    row.cost_usd_total = cost_usd_cents
    row.date = date.today()
    return row


class TestCacheStatsHappyPath:
    """Tests for cache stats happy path."""

    async def test_mixed_cache_status_rows_report_per_layer_counts(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Mixed cache status rows report per layer counts."""
        rows = [
            make_cache_usage_row(cache_status="exact", request_count=10, tokens_saved=1000),
            make_cache_usage_row(cache_status="semantic", request_count=5, tokens_saved=500),
            make_cache_usage_row(cache_status="miss", request_count=20, tokens_saved=0),
        ]
        app_mock_db.return_value.select.side_effect = [make_select_result(rows)]

        resp = await client.get("/api/v1/usage/cache-stats", headers=auth_headers)
        assert resp.status_code == 200
        data = (await resp.get_json())["data"]

        assert data["by_layer"]["exact"] == 10
        assert data["by_layer"]["semantic"] == 5
        assert data["by_layer"]["miss"] == 20
        assert data["total_requests"] == 35
        assert data["tokens_saved_total"] == 1500
        assert data["hit_rate"] == round(15 / 35, 4)

    async def test_empty_window_returns_zeroes_not_error(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Empty window returns zeroes not error."""
        app_mock_db.return_value.select.side_effect = [make_select_result([])]

        resp = await client.get("/api/v1/usage/cache-stats", headers=auth_headers)
        assert resp.status_code == 200
        data = (await resp.get_json())["data"]
        assert data["total_requests"] == 0
        assert data["hit_rate"] == 0.0
        assert data["tokens_saved_total"] == 0
        assert data["usd_saved_estimate"] == 0

    async def test_filters_by_org_and_virtual_key_query_params(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Filters by org and virtual key query params."""
        rows = [make_cache_usage_row(org_id=2, vkey_id=9, cache_status="upstream", request_count=3)]
        app_mock_db.return_value.select.side_effect = [make_select_result(rows)]

        resp = await client.get(
            "/api/v1/usage/cache-stats?org_id=2&virtual_key_id=9&window=7", headers=auth_headers
        )
        assert resp.status_code == 200
        data = (await resp.get_json())["data"]
        assert data["window_days"] == 7
        assert data["organization_id"] == 2
        assert data["virtual_key_id"] == 9
        assert data["by_layer"]["upstream"] == 3

    async def test_usd_saved_estimate_computed_from_blended_rate(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        # 100 total tokens costing 100 cents -> 1 cent/token; 50 tokens saved -> 50 cents -> $0.50
        """Usd saved estimate computed from blended rate."""
        rows = [
            make_cache_usage_row(
                cache_status="exact",
                tokens_saved=50,
                tokens_input=50,
                tokens_output=50,
                cost_usd_cents=100,
            )
        ]
        app_mock_db.return_value.select.side_effect = [make_select_result(rows)]

        resp = await client.get("/api/v1/usage/cache-stats", headers=auth_headers)
        data = (await resp.get_json())["data"]
        assert data["usd_saved_estimate"] == 0.5


class TestCacheStatsOrgScoping:
    """Tests for cache stats org scoping."""

    async def test_resource_manager_cannot_query_another_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """Resource manager cannot query another org."""
        resp = await client.get("/api/v1/usage/cache-stats?org_id=999", headers=rm_auth_headers)
        assert resp.status_code == 403

    async def test_resource_manager_defaults_to_own_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """Resource manager defaults to own org."""
        rows = [make_cache_usage_row(org_id=1, cache_status="exact", request_count=2)]
        app_mock_db.return_value.select.side_effect = [make_select_result(rows)]

        resp = await client.get("/api/v1/usage/cache-stats", headers=rm_auth_headers)
        assert resp.status_code == 200
        data = (await resp.get_json())["data"]
        assert data["organization_id"] == 1

    async def test_admin_can_query_any_org(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin can query any org."""
        rows = [make_cache_usage_row(org_id=42, cache_status="exact", request_count=1)]
        app_mock_db.return_value.select.side_effect = [make_select_result(rows)]

        resp = await client.get("/api/v1/usage/cache-stats?org_id=42", headers=auth_headers)
        assert resp.status_code == 200
