"""
Tests for verify_webhook_signature() -- the surviving piece of the former
AILB webhook module (services/management/app/api/v1/webhooks.py).

The AILB usage/health/batch ingest routes and process_usage_event() this
file used to also cover were deleted alongside the rest of the MarchProxy
AILB coupling; verify_webhook_signature() has no AILB dependency of its own
and is tested directly here.
"""

import hashlib
import hmac


class TestSignatureVerification:
    """Tests for verify_webhook_signature() function"""

    async def test_verify_signature_no_secret_configured(self, flask_app):
        """When WEBHOOK_SECRET is empty, verification REJECTS (fail closed), never skips."""
        async with flask_app.app_context():
            flask_app.config["WEBHOOK_SECRET"] = ""
            from services.management.app.api.v1.webhooks import verify_webhook_signature

            payload = b'{"test": "data"}'
            signature = "any-signature-accepted"
            result = verify_webhook_signature(payload, signature, flask_app.config["WEBHOOK_SECRET"])
            # regression: security review 2026-07-26 — empty secret must reject, not skip
            assert result is False

    async def test_verify_signature_valid(self, flask_app):
        """Valid HMAC signature returns True."""
        async with flask_app.app_context():
            secret = "test-webhook-secret-key"
            flask_app.config["WEBHOOK_SECRET"] = secret
            from services.management.app.api.v1.webhooks import verify_webhook_signature

            payload = b'{"event_id": "evt_001"}'
            expected_sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
            signature = f"sha256={expected_sig}"

            result = verify_webhook_signature(payload, signature, flask_app.config["WEBHOOK_SECRET"])
            assert result is True

    async def test_verify_signature_invalid(self, flask_app):
        """Invalid HMAC signature returns False."""
        async with flask_app.app_context():
            secret = "test-webhook-secret-key"
            flask_app.config["WEBHOOK_SECRET"] = secret
            from services.management.app.api.v1.webhooks import verify_webhook_signature

            payload = b'{"event_id": "evt_001"}'
            signature = "sha256=invalid_signature_value"

            result = verify_webhook_signature(payload, signature, flask_app.config["WEBHOOK_SECRET"])
            assert result is False

    async def test_verify_signature_wrong_payload(self, flask_app):
        """HMAC signature for different payload fails verification."""
        async with flask_app.app_context():
            secret = "test-webhook-secret-key"
            flask_app.config["WEBHOOK_SECRET"] = secret
            from services.management.app.api.v1.webhooks import verify_webhook_signature

            payload_1 = b'{"event_id": "evt_001"}'
            payload_2 = b'{"event_id": "evt_002"}'

            sig_for_payload_1 = hmac.new(secret.encode(), payload_1, hashlib.sha256).hexdigest()
            signature = f"sha256={sig_for_payload_1}"

            # Try to verify signature made for payload_1 against payload_2
            result = verify_webhook_signature(payload_2, signature, flask_app.config["WEBHOOK_SECRET"])
            assert result is False
