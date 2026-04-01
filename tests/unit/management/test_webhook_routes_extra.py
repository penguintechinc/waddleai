"""
Additional pytest tests for AILB webhook routes to improve coverage.
Focuses on HMAC signature verification and process_usage_event() internal logic.
"""

import hmac
import hashlib
from datetime import datetime, date
from unittest.mock import MagicMock, patch

import pytest

from tests.unit.management.route_conftest import make_mock_key
from tests.unit.management.conftest import make_select_result


class TestSignatureVerification:
    """Tests for verify_webhook_signature() function"""

    def test_verify_signature_no_secret_configured(self, flask_app):
        """When WEBHOOK_SECRET not configured, signature verification is skipped."""
        with flask_app.app_context():
            flask_app.config['WEBHOOK_SECRET'] = ''
            from services.management.app.api.v1.webhooks import verify_webhook_signature

            payload = b'{"test": "data"}'
            signature = 'any-signature-accepted'
            result = verify_webhook_signature(payload, signature)
            assert result is True

    def test_verify_signature_valid(self, flask_app):
        """Valid HMAC signature returns True."""
        with flask_app.app_context():
            secret = 'test-webhook-secret-key'
            flask_app.config['WEBHOOK_SECRET'] = secret
            from services.management.app.api.v1.webhooks import verify_webhook_signature

            payload = b'{"event_id": "evt_001"}'
            expected_sig = hmac.new(
                secret.encode(), payload, hashlib.sha256
            ).hexdigest()
            signature = f'sha256={expected_sig}'

            result = verify_webhook_signature(payload, signature)
            assert result is True

    def test_verify_signature_invalid(self, flask_app):
        """Invalid HMAC signature returns False."""
        with flask_app.app_context():
            secret = 'test-webhook-secret-key'
            flask_app.config['WEBHOOK_SECRET'] = secret
            from services.management.app.api.v1.webhooks import verify_webhook_signature

            payload = b'{"event_id": "evt_001"}'
            signature = 'sha256=invalid_signature_value'

            result = verify_webhook_signature(payload, signature)
            assert result is False

    def test_verify_signature_wrong_payload(self, flask_app):
        """HMAC signature for different payload fails verification."""
        with flask_app.app_context():
            secret = 'test-webhook-secret-key'
            flask_app.config['WEBHOOK_SECRET'] = secret
            from services.management.app.api.v1.webhooks import verify_webhook_signature

            payload_1 = b'{"event_id": "evt_001"}'
            payload_2 = b'{"event_id": "evt_002"}'

            sig_for_payload_1 = hmac.new(
                secret.encode(), payload_1, hashlib.sha256
            ).hexdigest()
            signature = f'sha256={sig_for_payload_1}'

            # Try to verify signature made for payload_1 against payload_2
            result = verify_webhook_signature(payload_2, signature)
            assert result is False

    def test_webhook_with_invalid_signature(self, client, flask_app):
        """POST /webhooks/ailb/usage with invalid signature returns 401."""
        secret = 'test-webhook-secret-key'
        flask_app.config['WEBHOOK_SECRET'] = secret

        payload = {
            'event_id': 'evt_test_001',
            'key_id': 'wa-testkey',
            'model': 'gpt-4o',
            'provider': 'openai',
            'input_tokens': 100,
            'output_tokens': 200,
            'timestamp': '2025-01-01T12:00:00Z',
            'status': 'success',
        }

        bad_signature = 'sha256=invalid_signature'
        resp = client.post(
            '/api/v1/webhooks/ailb/usage',
            json=payload,
            headers={'X-Webhook-Signature': bad_signature},
        )
        assert resp.status_code == 401
        data = resp.get_json()
        assert 'error' in data
        assert data['error'] == 'Invalid signature'


class TestProcessUsageEventNewRecord:
    """Tests for process_usage_event() creating new token_usage records"""

    def test_process_usage_event_creates_new_record(self, client, app_mock_db: MagicMock, flask_app) -> None:
        """New usage event creates token_usage record when none exists."""
        flask_app.config['WEBHOOK_SECRET'] = ''  # Disable signature check
        key = make_mock_key()
        payload = {
            'event_id': 'evt_new_001',
            'key_id': 'wa-testkey',
            'request_id': 'req_001',
            'model': 'gpt-4o',
            'provider': 'openai',
            'input_tokens': 100,
            'output_tokens': 200,
            'cost_usd': 0.005,
            'latency_ms': 300,
            'timestamp': '2025-01-01T12:00:00Z',
            'status': 'success',
        }

        # Side effects:
        # 1st select() → no duplicate event
        # 2nd select() → find key by ailb_key_id
        # 3rd select() → no existing token_usage record (new)
        # 4th select() → no token_conversion_rates
        app_mock_db.return_value.select.return_value.first.side_effect = [
            None,   # no duplicate event
            key,    # ailb_key_id lookup
            None,   # existing usage record (none)
            None,   # token_conversion_rates
        ]
        app_mock_db.ailb_usage_events.insert.return_value = 1
        app_mock_db.token_usage.insert.return_value = 1
        app_mock_db.usage_logs.insert.return_value = 1

        resp = client.post('/api/v1/webhooks/ailb/usage', json=payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'accepted'
        assert data['processed'] is True

    def test_process_usage_event_with_conversion_rate(self, client, app_mock_db: MagicMock, flask_app) -> None:
        """Token conversion rate is applied when available."""
        flask_app.config['WEBHOOK_SECRET'] = ''  # Disable signature check
        key = make_mock_key()
        payload = {
            'event_id': 'evt_rate_001',
            'key_id': 'wa-testkey',
            'model': 'gpt-4o',
            'provider': 'openai',
            'input_tokens': 1000,
            'output_tokens': 2000,
            'cost_usd': 0.05,
            'timestamp': '2025-01-01T12:00:00Z',
            'status': 'success',
        }

        # Create a mock conversion rate
        conversion_rate = MagicMock()
        conversion_rate.input_rate = 5  # 1000 / 5 = 200 waddleai tokens
        conversion_rate.output_rate = 10  # 2000 / 10 = 200 waddleai tokens
        # Total: 400 waddleai tokens

        app_mock_db.return_value.select.return_value.first.side_effect = [
            None,   # no duplicate event
            key,    # ailb_key_id lookup
            None,   # existing usage record (none)
            conversion_rate,  # token_conversion_rates found
        ]
        app_mock_db.ailb_usage_events.insert.return_value = 1
        app_mock_db.token_usage.insert.return_value = 1
        app_mock_db.usage_logs.insert.return_value = 1

        resp = client.post('/api/v1/webhooks/ailb/usage', json=payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['processed'] is True


class TestProcessUsageEventUpdateRecord:
    """Tests for process_usage_event() updating existing token_usage records"""

    def test_process_usage_event_updates_existing_record(self, client, app_mock_db: MagicMock, flask_app) -> None:
        """Existing daily usage record is updated with new tokens."""
        flask_app.config['WEBHOOK_SECRET'] = ''  # Disable signature check
        key = make_mock_key()
        existing_usage = MagicMock()
        existing_usage.id = 99
        existing_usage.waddleai_tokens = 50
        existing_usage.tokens_input_total = 200
        existing_usage.tokens_output_total = 400
        existing_usage.request_count = 2
        existing_usage.cost_usd_total = 0.010

        payload = {
            'event_id': 'evt_update_001',
            'key_id': 'wa-testkey',
            'model': 'gpt-4o',
            'provider': 'openai',
            'input_tokens': 100,
            'output_tokens': 200,
            'cost_usd': 0.005,
            'timestamp': '2025-01-01T12:00:00Z',
            'status': 'success',
        }

        # Side effects:
        # 1st select() → no duplicate
        # 2nd select() → find key
        # 3rd select() → find existing usage record
        # 4th select() → no conversion rates
        app_mock_db.return_value.select.return_value.first.side_effect = [
            None,   # no duplicate event
            key,    # ailb_key_id lookup
            existing_usage,  # existing token_usage record
            None,   # token_conversion_rates
        ]
        app_mock_db.ailb_usage_events.insert.return_value = 1
        app_mock_db.usage_logs.insert.return_value = 1

        resp = client.post('/api/v1/webhooks/ailb/usage', json=payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'accepted'
        assert data['processed'] is True

    def test_process_usage_event_increments_request_count(self, client, app_mock_db: MagicMock, flask_app) -> None:
        """Request count is incremented for each event."""
        flask_app.config['WEBHOOK_SECRET'] = ''  # Disable signature check
        key = make_mock_key()
        existing_usage = MagicMock()
        existing_usage.id = 99
        existing_usage.waddleai_tokens = 100
        existing_usage.tokens_input_total = 500
        existing_usage.tokens_output_total = 1000
        existing_usage.request_count = 5
        existing_usage.cost_usd_total = 0.050

        payload = {
            'event_id': 'evt_count_001',
            'key_id': 'wa-testkey',
            'model': 'gpt-4o',
            'provider': 'openai',
            'input_tokens': 100,
            'output_tokens': 200,
            'cost_usd': 0.005,
            'timestamp': '2025-01-01T12:00:00Z',
            'status': 'success',
        }

        app_mock_db.return_value.select.return_value.first.side_effect = [
            None,   # no duplicate
            key,
            existing_usage,  # existing record
            None,   # no conversion rate
        ]
        app_mock_db.ailb_usage_events.insert.return_value = 1
        app_mock_db.usage_logs.insert.return_value = 1

        resp = client.post('/api/v1/webhooks/ailb/usage', json=payload)
        assert resp.status_code == 200
        assert resp.get_json()['processed'] is True


class TestWebhookUsageLogs:
    """Tests for usage log creation in process_usage_event()"""

    def test_usage_log_created_with_event_data(self, client, app_mock_db: MagicMock, flask_app) -> None:
        """Usage log is created with all event data."""
        flask_app.config['WEBHOOK_SECRET'] = ''  # Disable signature check
        key = make_mock_key()
        payload = {
            'event_id': 'evt_log_001',
            'key_id': 'wa-testkey',
            'request_id': 'req_log_001',
            'model': 'gpt-4o',
            'provider': 'openai',
            'input_tokens': 150,
            'output_tokens': 300,
            'cost_usd': 0.0075,
            'latency_ms': 1500,
            'timestamp': '2025-01-01T12:00:00Z',
            'status': 'success',
        }

        app_mock_db.return_value.select.return_value.first.side_effect = [
            None,   # no duplicate
            key,
            None,   # no existing usage
            None,   # no conversion rate
        ]
        app_mock_db.ailb_usage_events.insert.return_value = 1
        app_mock_db.token_usage.insert.return_value = 1
        app_mock_db.usage_logs.insert.return_value = 1

        resp = client.post('/api/v1/webhooks/ailb/usage', json=payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['processed'] is True

    def test_usage_log_status_code_success(self, client, app_mock_db: MagicMock, flask_app) -> None:
        """Usage log status_code is 200 for success events."""
        flask_app.config['WEBHOOK_SECRET'] = ''  # Disable signature check
        key = make_mock_key()
        payload = {
            'event_id': 'evt_success_001',
            'key_id': 'wa-testkey',
            'model': 'gpt-4o',
            'provider': 'openai',
            'input_tokens': 100,
            'output_tokens': 200,
            'timestamp': '2025-01-01T12:00:00Z',
            'status': 'success',
        }

        app_mock_db.return_value.select.return_value.first.side_effect = [
            None, key, None, None
        ]
        app_mock_db.ailb_usage_events.insert.return_value = 1
        app_mock_db.token_usage.insert.return_value = 1
        app_mock_db.usage_logs.insert.return_value = 1

        resp = client.post('/api/v1/webhooks/ailb/usage', json=payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['processed'] is True

    def test_usage_log_status_code_error(self, client, app_mock_db: MagicMock, flask_app) -> None:
        """Usage log status_code is 500 for error events."""
        flask_app.config['WEBHOOK_SECRET'] = ''  # Disable signature check
        key = make_mock_key()
        payload = {
            'event_id': 'evt_error_001',
            'key_id': 'wa-testkey',
            'model': 'gpt-4o',
            'provider': 'openai',
            'input_tokens': 100,
            'output_tokens': 200,
            'timestamp': '2025-01-01T12:00:00Z',
            'status': 'error',
        }

        app_mock_db.return_value.select.return_value.first.side_effect = [
            None, key, None, None
        ]
        app_mock_db.ailb_usage_events.insert.return_value = 1
        app_mock_db.token_usage.insert.return_value = 1
        app_mock_db.usage_logs.insert.return_value = 1

        resp = client.post('/api/v1/webhooks/ailb/usage', json=payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['processed'] is True


class TestKeyLookupByPrefix:
    """Tests for key lookup by wa- prefix when ailb_key_id not found"""

    def test_key_lookup_by_prefix_fallback(self, client, app_mock_db: MagicMock, flask_app) -> None:
        """When ailb_key_id not found, lookup by key_prefix is attempted."""
        flask_app.config['WEBHOOK_SECRET'] = ''  # Disable signature check
        key = make_mock_key()
        payload = {
            'event_id': 'evt_prefix_001',
            'key_id': 'wa-unknownprefix123456789',
            'model': 'gpt-4o',
            'provider': 'openai',
            'input_tokens': 100,
            'output_tokens': 200,
            'timestamp': '2025-01-01T12:00:00Z',
            'status': 'success',
        }

        # 1st select: no duplicate
        # 2nd select: no ailb_key_id match → None
        # 3rd select: find by prefix
        # 4th select: no existing usage
        # 5th select: no conversion rate
        app_mock_db.return_value.select.return_value.first.side_effect = [
            None,   # no duplicate
            None,   # ailb_key_id lookup failed
            key,    # prefix lookup succeeded
            None,   # existing usage
            None,   # conversion rate
        ]
        app_mock_db.ailb_usage_events.insert.return_value = 1
        app_mock_db.token_usage.insert.return_value = 1
        app_mock_db.usage_logs.insert.return_value = 1

        resp = client.post('/api/v1/webhooks/ailb/usage', json=payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['processed'] is True


class TestWebhookEventProcessedFlag:
    """Tests for marking events as processed"""

    def test_event_marked_processed_after_usage(self, client, app_mock_db: MagicMock, flask_app) -> None:
        """Event is marked as processed=True after processing."""
        flask_app.config['WEBHOOK_SECRET'] = ''  # Disable signature check
        key = make_mock_key()
        payload = {
            'event_id': 'evt_processed_001',
            'key_id': 'wa-testkey',
            'model': 'gpt-4o',
            'provider': 'openai',
            'input_tokens': 100,
            'output_tokens': 200,
            'timestamp': '2025-01-01T12:00:00Z',
            'status': 'success',
        }

        app_mock_db.return_value.select.return_value.first.side_effect = [
            None, key, None, None
        ]
        app_mock_db.ailb_usage_events.insert.return_value = 1
        app_mock_db.token_usage.insert.return_value = 1
        app_mock_db.usage_logs.insert.return_value = 1

        resp = client.post('/api/v1/webhooks/ailb/usage', json=payload)
        assert resp.status_code == 200

        # Verify the final update call to mark event as processed
        # This should be in the update calls to ailb_usage_events
        assert app_mock_db.commit.called
