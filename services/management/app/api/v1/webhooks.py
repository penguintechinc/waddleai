"""
WaddleAI Management API v1 - AILB Webhook Endpoints
Receives usage events and health updates from MarchProxy AILB
"""

import asyncio
import hashlib
import hmac
from datetime import date, datetime

from quart import current_app, jsonify, request

from ...extensions import db
from . import api_v1_bp


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """
    Verify webhook signature from AILB.

    Rejects (returns False) if no secret is configured — never skips verification.
    """
    if not secret:
        return False  # REJECT: verification requires a secret (never skip)

    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    return hmac.compare_digest(f"sha256={expected}", signature)


@api_v1_bp.route("/webhooks/ailb/usage", methods=["POST"])
async def handle_usage_webhook():
    """
    Receive usage events from MarchProxy AILB after each request.

    Expected payload:
    {
        "event_id": "evt_abc123",
        "key_id": "wa-xxx",
        "request_id": "req_xyz",
        "model": "gpt-4",
        "provider": "openai",
        "input_tokens": 150,
        "output_tokens": 300,
        "cost_usd": 0.0135,
        "latency_ms": 1250,
        "timestamp": "2025-01-12T10:30:00Z",
        "status": "success",
        "error_message": null
    }
    """
    if not current_app.config.get("ENABLE_USAGE_WEBHOOKS", True):
        return jsonify({"error": "Webhooks disabled"}), 403

    secret = current_app.config.get("WEBHOOK_SECRET", "")

    # Verify signature
    signature = request.headers.get("X-Webhook-Signature", "")
    raw_body = await request.data
    if not verify_webhook_signature(raw_body, signature, secret):
        return jsonify({"error": "Invalid signature"}), 401

    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    event_id = data.get("event_id")
    if not event_id:
        return jsonify({"error": "event_id required"}), 400

    def _process():
        # Check for duplicate event
        existing = db(db.ailb_usage_events.event_id == event_id).select().first()
        if existing:
            return "duplicate"

        # Find virtual key by AILB key ID or key prefix
        key_id = data.get("key_id", "")
        virtual_key = None

        if key_id:
            # Try to find by ailb_key_id first
            virtual_key = db(db.virtual_keys.ailb_key_id == key_id).select().first()

            # If not found, try by key prefix
            if not virtual_key and key_id.startswith("wa-"):
                virtual_key = db(db.virtual_keys.key_prefix.like(f"{key_id[:12]}%")).select().first()

        # Store raw event
        db.ailb_usage_events.insert(
            event_id=event_id,
            virtual_key_id=virtual_key.id if virtual_key else None,
            ailb_key_id=key_id,
            request_id=data.get("request_id"),
            model=data.get("model"),
            provider=data.get("provider"),
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            cost_usd=data.get("cost_usd", 0),
            latency_ms=data.get("latency_ms"),
            status=data.get("status", "unknown"),
            error_message=data.get("error_message"),
            timestamp=(
                datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
                if data.get("timestamp")
                else datetime.utcnow()
            ),
            processed=False,
            created_at=datetime.utcnow(),
        )

        # Process usage if key found
        if virtual_key:
            process_usage_event(virtual_key, data)

        db.commit()

        return "accepted" if virtual_key else "accepted_unprocessed"

    result = await asyncio.to_thread(_process)

    if result == "duplicate":
        return jsonify({"status": "duplicate", "message": "Event already processed"})

    return jsonify({"status": "accepted", "event_id": event_id, "processed": result == "accepted"})


def process_usage_event(virtual_key, event_data: dict):
    """Process usage event and update tracking tables"""
    today = date.today()

    # Get or create daily usage record
    usage_record = (
        db((db.token_usage.virtual_key_id == virtual_key.id) & (db.token_usage.date == today)).select().first()
    )

    input_tokens = event_data.get("input_tokens", 0)
    output_tokens = event_data.get("output_tokens", 0)
    cost_usd = event_data.get("cost_usd", 0)

    # Calculate WaddleAI tokens using conversion rates
    provider = event_data.get("provider", "unknown")
    model = event_data.get("model", "unknown")

    waddleai_tokens = calculate_waddleai_tokens(provider, model, input_tokens, output_tokens)

    if usage_record:
        # Update existing record
        db(db.token_usage.id == usage_record.id).update(
            waddleai_tokens=(usage_record.waddleai_tokens or 0) + waddleai_tokens,
            tokens_input_total=(usage_record.tokens_input_total or 0) + input_tokens,
            tokens_output_total=(usage_record.tokens_output_total or 0) + output_tokens,
            request_count=(usage_record.request_count or 0) + 1,
            cost_usd_total=(usage_record.cost_usd_total or 0) + cost_usd,
            last_updated=datetime.utcnow(),
        )
    else:
        # Create new record
        db.token_usage.insert(
            virtual_key_id=virtual_key.id,
            user_id=virtual_key.user_id,
            organization_id=virtual_key.organization_id,
            date=today,
            waddleai_tokens=waddleai_tokens,
            tokens_input_total=input_tokens,
            tokens_output_total=output_tokens,
            request_count=1,
            cost_usd_total=cost_usd,
            last_updated=datetime.utcnow(),
        )

    # Update key last_used timestamp
    db(db.virtual_keys.id == virtual_key.id).update(last_used=datetime.utcnow())

    # Create detailed usage log
    db.usage_logs.insert(
        timestamp=datetime.utcnow(),
        virtual_key_id=virtual_key.id,
        user_id=virtual_key.user_id,
        organization_id=virtual_key.organization_id,
        request_hash=event_data.get("request_id"),
        waddleai_tokens_used=waddleai_tokens,
        llm_tokens_input=input_tokens,
        llm_tokens_output=output_tokens,
        llm_tokens_total=input_tokens + output_tokens,
        response_time=event_data.get("latency_ms", 0) / 1000.0 if event_data.get("latency_ms") else None,
        status_code=200 if event_data.get("status") == "success" else 500,
        model_used=model,
        provider_type=provider,
        cost_estimate_waddleai=waddleai_tokens * 0.001,  # Default rate
        cost_estimate_usd=cost_usd,
    )

    # Mark event as processed
    db(db.ailb_usage_events.event_id == event_data.get("event_id")).update(processed=True)


def calculate_waddleai_tokens(provider: str, model: str, input_tokens: int, output_tokens: int) -> int:
    """Calculate WaddleAI normalized tokens from LLM tokens"""
    # Look up conversion rate
    rate = (
        db(
            (db.token_conversion_rates.provider == provider)
            & (db.token_conversion_rates.model == model)
            & (db.token_conversion_rates.enabled is True)
        )
        .select()
        .first()
    )

    if rate:
        input_rate = rate.input_rate or 10
        output_rate = rate.output_rate or 10
    else:
        # Default rates
        input_rate = 10
        output_rate = 10

    waddleai_tokens = int(input_tokens / input_rate) + int(output_tokens / output_rate)
    return waddleai_tokens


@api_v1_bp.route("/webhooks/ailb/health", methods=["POST"])
async def handle_health_webhook():
    """
    Receive health status updates from MarchProxy AILB.

    Expected payload:
    {
        "instance_id": "ailb-001",
        "status": "healthy",
        "providers": {
            "openai": {"status": "healthy", "latency_ms": 150},
            "anthropic": {"status": "healthy", "latency_ms": 120}
        },
        "timestamp": "2025-01-12T10:30:00Z"
    }
    """
    if not current_app.config.get("ENABLE_USAGE_WEBHOOKS", True):
        return jsonify({"error": "Webhooks disabled"}), 403

    secret = current_app.config.get("WEBHOOK_SECRET", "")

    # Verify signature
    signature = request.headers.get("X-Webhook-Signature", "")
    raw_body = await request.data
    if not verify_webhook_signature(raw_body, signature, secret):
        return jsonify({"error": "Invalid signature"}), 401

    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    # Store health status (could be used for dashboard/monitoring)
    # For now, just acknowledge
    current_app.logger.info(f"AILB health update: {data}")

    return jsonify({"status": "accepted", "instance_id": data.get("instance_id")})


@api_v1_bp.route("/webhooks/ailb/batch", methods=["POST"])
async def handle_batch_webhook():
    """
    Receive batch of usage events from MarchProxy AILB.
    More efficient for high-volume scenarios.

    Expected payload:
    {
        "events": [
            {
                "event_id": "evt_1",
                "key_id": "wa-xxx",
                ...
            },
            ...
        ]
    }
    """
    if not current_app.config.get("ENABLE_USAGE_WEBHOOKS", True):
        return jsonify({"error": "Webhooks disabled"}), 403

    secret = current_app.config.get("WEBHOOK_SECRET", "")

    # Verify signature
    signature = request.headers.get("X-Webhook-Signature", "")
    raw_body = await request.data
    if not verify_webhook_signature(raw_body, signature, secret):
        return jsonify({"error": "Invalid signature"}), 401

    data = await request.get_json()

    if not data or "events" not in data:
        return jsonify({"error": "events array required"}), 400

    events = data["events"]
    logger = current_app.logger

    def _process_batch():
        results = {"accepted": 0, "duplicates": 0, "errors": 0}

        for event_data in events:
            event_id = event_data.get("event_id")
            if not event_id:
                results["errors"] += 1
                continue

            # Check for duplicate
            existing = db(db.ailb_usage_events.event_id == event_id).select().first()
            if existing:
                results["duplicates"] += 1
                continue

            try:
                # Find virtual key
                key_id = event_data.get("key_id", "")
                virtual_key = None

                if key_id:
                    virtual_key = db(db.virtual_keys.ailb_key_id == key_id).select().first()
                    if not virtual_key and key_id.startswith("wa-"):
                        virtual_key = db(db.virtual_keys.key_prefix.like(f"{key_id[:12]}%")).select().first()

                # Store event
                db.ailb_usage_events.insert(
                    event_id=event_id,
                    virtual_key_id=virtual_key.id if virtual_key else None,
                    ailb_key_id=key_id,
                    request_id=event_data.get("request_id"),
                    model=event_data.get("model"),
                    provider=event_data.get("provider"),
                    input_tokens=event_data.get("input_tokens", 0),
                    output_tokens=event_data.get("output_tokens", 0),
                    cost_usd=event_data.get("cost_usd", 0),
                    latency_ms=event_data.get("latency_ms"),
                    status=event_data.get("status", "unknown"),
                    error_message=event_data.get("error_message"),
                    timestamp=(
                        datetime.fromisoformat(event_data["timestamp"].replace("Z", "+00:00"))
                        if event_data.get("timestamp")
                        else datetime.utcnow()
                    ),
                    processed=False,
                    created_at=datetime.utcnow(),
                )

                if virtual_key:
                    process_usage_event(virtual_key, event_data)

                results["accepted"] += 1

            except Exception as e:
                logger.error(f"Error processing event {event_id}: {e}")
                results["errors"] += 1

        db.commit()

        return results

    results = await asyncio.to_thread(_process_batch)

    return jsonify({"status": "completed", "results": results})
