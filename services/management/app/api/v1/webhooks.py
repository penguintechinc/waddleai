"""WaddleAI Management API v1 - Webhook Signature Verification

The AILB usage/health/batch ingest routes formerly hosted here were
deleted alongside the rest of the MarchProxy AILB coupling (nothing writes
to the legacy AILB usage-events table anymore, ahead of it being dropped
by migration 007). ``verify_webhook_signature`` is generic HMAC
verification logic with no AILB dependency of its own and survives for
any future webhook route.
"""

import hashlib
import hmac


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify an HMAC-SHA256 webhook signature.

    Rejects (returns False) if no secret is configured — never skips verification.
    """
    if not secret:
        return False  # REJECT: verification requires a secret (never skip)

    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    return hmac.compare_digest(f"sha256={expected}", signature)
