"""regression: usage_tracker used `enabled is True`, which never builds a DAL expression."""

from services.management.app.services.usage_tracker import UsageTrackingService
from shared.utils.conversion_rate_store import upsert_conversion_rate


def test_regression_enabled_db_rate_is_used(real_schema_db):
    """Default for gpt-4o is (5,5) -> 400 credits; the DB row (50,50) must win -> 40."""
    upsert_conversion_rate(
        real_schema_db, provider="openai", model="gpt-4o", input_rate=50.0, output_rate=50.0
    )
    svc = UsageTrackingService(real_schema_db)
    assert svc.calculate_waddleai_tokens("openai", "gpt-4o", 1000, 1000) == 40


def test_regression_disabled_db_rate_is_ignored(real_schema_db):
    """A disabled row must fall through to the provider default (5,5) -> 400."""
    upsert_conversion_rate(
        real_schema_db,
        provider="openai",
        model="gpt-4o",
        input_rate=50.0,
        output_rate=50.0,
        enabled=False,
    )
    svc = UsageTrackingService(real_schema_db)
    assert svc.calculate_waddleai_tokens("openai", "gpt-4o", 1000, 1000) == 400
