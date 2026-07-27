"""
Unit tests for AILB webhook routes: /api/v1/webhooks/ailb/*
"""

from unittest.mock import MagicMock, patch

import pytest

from tests.unit.management.conftest import make_select_result
from tests.unit.management.route_conftest import make_mock_key


@pytest.fixture(autouse=True)
def _bypass_webhook_signature():
    """Bypass HMAC verification for these endpoint-logic tests.

    Signature behavior (incl. fail-closed on an empty secret) is covered in
    test_webhook_routes_extra.py::TestSignatureVerification.
    """
    with patch("services.management.app.api.v1.webhooks.verify_webhook_signature", return_value=True):
        yield

# ---------------------------------------------------------------------------
# POST /api/v1/webhooks/ailb/usage
# ---------------------------------------------------------------------------


class TestUsageWebhook:
    """Tests for POST /api/v1/webhooks/ailb/usage"""

    def _valid_payload(self) -> dict:
        return {
            "event_id": "evt_test_001",
            "key_id": "wa-testkey",
            "request_id": "req_test_001",
            "model": "gpt-4o",
            "provider": "openai",
            "input_tokens": 100,
            "output_tokens": 200,
            "cost_usd": 0.005,
            "latency_ms": 300,
            "timestamp": "2025-01-01T12:00:00Z",
            "status": "success",
        }

    async def test_usage_webhook_success_no_key(self, client, app_mock_db: MagicMock) -> None:
        """New event with unknown key is accepted and stored."""
        # No existing event → None; no virtual key match → None
        app_mock_db.return_value.select.return_value.first.return_value = None
        app_mock_db.return_value.select.return_value = make_select_result([])
        app_mock_db.ailb_usage_events.insert.return_value = 1

        resp = await client.post(
            "/api/v1/webhooks/ailb/usage",
            json=self._valid_payload(),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["status"] == "accepted"
        assert data["processed"] is False

    async def test_usage_webhook_success_with_key(self, client, app_mock_db: MagicMock) -> None:
        """Known key event is accepted and processed."""
        key = make_mock_key()
        # Duplicate check → None; ailb_key_id lookup → key
        app_mock_db.return_value.select.return_value.first.side_effect = [
            None,  # no duplicate event
            key,  # ailb_key_id lookup
            None,  # existing usage record for process_usage_event
            None,  # token_conversion_rates lookup
        ]
        app_mock_db.return_value.select.return_value = make_select_result([])
        app_mock_db.ailb_usage_events.insert.return_value = 2
        app_mock_db.token_usage.insert.return_value = 1
        app_mock_db.usage_logs.insert.return_value = 1

        resp = await client.post(
            "/api/v1/webhooks/ailb/usage",
            json=self._valid_payload(),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["status"] == "accepted"

    async def test_usage_webhook_duplicate_event(self, client, app_mock_db: MagicMock) -> None:
        """Duplicate event_id returns duplicate status."""
        existing_event = MagicMock()
        app_mock_db.return_value.select.return_value.first.return_value = existing_event

        resp = await client.post(
            "/api/v1/webhooks/ailb/usage",
            json=self._valid_payload(),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["status"] == "duplicate"

    async def test_usage_webhook_missing_event_id(self, client) -> None:
        """Missing event_id returns 400."""
        payload = self._valid_payload()
        del payload["event_id"]

        resp = await client.post("/api/v1/webhooks/ailb/usage", json=payload)
        assert resp.status_code == 400

    async def test_usage_webhook_no_body(self, client) -> None:
        """Empty body returns 400."""
        resp = await client.post(
            "/api/v1/webhooks/ailb/usage",
            data="",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    async def test_usage_webhook_disabled(self, client, flask_app) -> None:
        """Webhooks disabled returns 403."""
        original = flask_app.config.get("ENABLE_USAGE_WEBHOOKS", True)
        flask_app.config["ENABLE_USAGE_WEBHOOKS"] = False
        try:
            resp = await client.post(
                "/api/v1/webhooks/ailb/usage",
                json=self._valid_payload(),
            )
            assert resp.status_code == 403
        finally:
            flask_app.config["ENABLE_USAGE_WEBHOOKS"] = original


# ---------------------------------------------------------------------------
# POST /api/v1/webhooks/ailb/health
# ---------------------------------------------------------------------------


class TestHealthWebhook:
    """Tests for POST /api/v1/webhooks/ailb/health"""

    def _health_payload(self) -> dict:
        return {
            "instance_id": "ailb-001",
            "status": "healthy",
            "providers": {
                "openai": {"status": "healthy", "latency_ms": 150},
            },
            "timestamp": "2025-01-01T12:00:00Z",
        }

    async def test_health_webhook_success(self, client) -> None:
        """Valid health update returns accepted."""
        resp = await client.post(
            "/api/v1/webhooks/ailb/health",
            json=self._health_payload(),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["status"] == "accepted"
        assert data["instance_id"] == "ailb-001"

    async def test_health_webhook_no_body(self, client) -> None:
        """Empty body returns 400."""
        resp = await client.post(
            "/api/v1/webhooks/ailb/health",
            data="",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    async def test_health_webhook_disabled(self, client, flask_app) -> None:
        """Webhooks disabled returns 403."""
        original = flask_app.config.get("ENABLE_USAGE_WEBHOOKS", True)
        flask_app.config["ENABLE_USAGE_WEBHOOKS"] = False
        try:
            resp = await client.post(
                "/api/v1/webhooks/ailb/health",
                json=self._health_payload(),
            )
            assert resp.status_code == 403
        finally:
            flask_app.config["ENABLE_USAGE_WEBHOOKS"] = original


# ---------------------------------------------------------------------------
# POST /api/v1/webhooks/ailb/batch
# ---------------------------------------------------------------------------


class TestBatchWebhook:
    """Tests for POST /api/v1/webhooks/ailb/batch"""

    def _batch_payload(self) -> dict:
        return {
            "events": [
                {
                    "event_id": "evt_batch_001",
                    "key_id": "wa-testkey",
                    "model": "gpt-4o",
                    "provider": "openai",
                    "input_tokens": 50,
                    "output_tokens": 100,
                    "cost_usd": 0.002,
                    "timestamp": "2025-01-01T12:00:00Z",
                    "status": "success",
                },
                {
                    "event_id": "evt_batch_002",
                    "key_id": "wa-anotherkey",
                    "model": "gpt-3.5-turbo",
                    "provider": "openai",
                    "input_tokens": 20,
                    "output_tokens": 50,
                    "cost_usd": 0.0001,
                    "timestamp": "2025-01-01T12:01:00Z",
                    "status": "success",
                },
            ]
        }

    async def test_batch_webhook_success(self, client, app_mock_db: MagicMock) -> None:
        """Valid batch of events returns completed status."""
        # Both events: no duplicate, no matching key
        app_mock_db.return_value.select.return_value.first.return_value = None
        app_mock_db.return_value.select.return_value = make_select_result([])
        app_mock_db.ailb_usage_events.insert.return_value = 1

        resp = await client.post(
            "/api/v1/webhooks/ailb/batch",
            json=self._batch_payload(),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["status"] == "completed"
        assert "results" in data
        assert data["results"]["accepted"] == 2

    async def test_batch_webhook_no_events_key(self, client) -> None:
        """Missing events array returns 400."""
        resp = await client.post(
            "/api/v1/webhooks/ailb/batch",
            json={"not_events": []},
        )
        assert resp.status_code == 400

    async def test_batch_webhook_no_body(self, client) -> None:
        """Empty body returns 400."""
        resp = await client.post(
            "/api/v1/webhooks/ailb/batch",
            data="",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    async def test_batch_webhook_with_duplicates(self, client, app_mock_db: MagicMock) -> None:
        """Duplicate events in batch are counted separately."""
        existing_event = MagicMock()
        empty = make_select_result([])
        # evt_batch_001: dup check → found (duplicate); evt_batch_002: dup check → None, key lookup → None
        dup_sel = make_select_result([existing_event])
        app_mock_db.return_value.select.side_effect = [dup_sel, empty, empty, empty, empty]
        app_mock_db.ailb_usage_events.insert.return_value = 1

        resp = await client.post(
            "/api/v1/webhooks/ailb/batch",
            json=self._batch_payload(),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["results"]["duplicates"] == 1
        assert data["results"]["accepted"] == 1

    async def test_batch_webhook_disabled(self, client, flask_app) -> None:
        """Webhooks disabled returns 403."""
        original = flask_app.config.get("ENABLE_USAGE_WEBHOOKS", True)
        flask_app.config["ENABLE_USAGE_WEBHOOKS"] = False
        try:
            resp = await client.post(
                "/api/v1/webhooks/ailb/batch",
                json=self._batch_payload(),
            )
            assert resp.status_code == 403
        finally:
            flask_app.config["ENABLE_USAGE_WEBHOOKS"] = original
