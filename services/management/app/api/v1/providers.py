"""
WaddleAI Management API v1 - AI Provider Management Endpoints
"""

import hashlib
import json
from datetime import datetime

from flask import current_app, jsonify, request

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth, require_role

# Supported provider types
SUPPORTED_PROVIDERS = {
    "openai": {
        "name": "OpenAI / ChatGPT",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo", "o1-preview", "o1-mini"],
        "requires_api_key": True,
        "default_endpoint": "https://api.openai.com/v1",
    },
    "anthropic": {
        "name": "Anthropic / Claude",
        "models": [
            "claude-3-5-sonnet-latest",
            "claude-3-opus-20240229",
            "claude-3-sonnet-20240229",
            "claude-3-haiku-20240307",
        ],
        "requires_api_key": True,
        "default_endpoint": "https://api.anthropic.com",
    },
    "ollama": {
        "name": "Ollama (Local)",
        "models": ["llama3.2", "llama3.1", "mistral", "mixtral", "codellama", "phi3", "qwen2.5"],
        "requires_api_key": False,
        "default_endpoint": "http://localhost:11434",
    },
    "gemini": {
        "name": "Google Gemini / Vertex AI",
        "models": ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-1.0-pro"],
        "requires_api_key": True,
        "default_endpoint": "https://generativelanguage.googleapis.com/v1",
    },
    "bedrock": {
        "name": "AWS Bedrock",
        "models": [
            "anthropic.claude-3-sonnet-20240229-v1:0",
            "anthropic.claude-3-haiku-20240307-v1:0",
            "amazon.titan-text-express-v1",
            "meta.llama3-70b-instruct-v1:0",
        ],
        "requires_api_key": True,
        "default_endpoint": "https://bedrock-runtime.us-east-1.amazonaws.com",
    },
    "azure_openai": {
        "name": "Azure OpenAI Service",
        "models": ["gpt-4", "gpt-35-turbo"],
        "requires_api_key": True,
        "default_endpoint": None,  # User must provide Azure endpoint
    },
    "cohere": {
        "name": "Cohere",
        "models": ["command-r-plus", "command-r", "command", "embed-english-v3.0"],
        "requires_api_key": True,
        "default_endpoint": "https://api.cohere.ai/v1",
    },
}


@api_v1_bp.route("/providers/types", methods=["GET"])
@require_auth
def list_provider_types():
    """List supported provider types"""
    result = []
    for provider_type, info in SUPPORTED_PROVIDERS.items():
        # Check if enterprise provider is enabled
        if provider_type == "gemini" and not current_app.config.get("ENABLE_GEMINI", True):
            continue
        if provider_type == "bedrock" and not current_app.config.get("ENABLE_BEDROCK", True):
            continue
        if provider_type == "azure_openai" and not current_app.config.get("ENABLE_AZURE_OPENAI", True):
            continue
        if provider_type == "cohere" and not current_app.config.get("ENABLE_COHERE", True):
            continue

        result.append(
            {
                "type": provider_type,
                "name": info["name"],
                "default_models": info["models"],
                "requires_api_key": info["requires_api_key"],
                "default_endpoint": info["default_endpoint"],
            }
        )

    return jsonify({"provider_types": result})


@api_v1_bp.route("/providers", methods=["GET"])
@require_auth
@require_role("admin")
def list_providers():
    """List all configured AI providers"""
    providers = db(db.ai_providers.id > 0).select()

    result = []
    for provider in providers:
        # Get sync status
        sync = db(db.marchproxy_ailb_sync.provider_id == provider.id).select().first()

        result.append(
            {
                "id": provider.id,
                "name": provider.name,
                "provider_type": provider.provider_type,
                "endpoint_url": provider.endpoint_url,
                "model_list": provider.model_list,
                "rate_limits": provider.rate_limits,
                "enabled": provider.enabled,
                "priority": provider.priority,
                "ailb_sync_enabled": provider.ailb_sync_enabled,
                "sync_status": sync.sync_status if sync else "not_synced",
                "last_synced": sync.last_synced.isoformat() if sync and sync.last_synced else None,
                "created_at": provider.created_at.isoformat() if provider.created_at else None,
            }
        )

    return jsonify({"providers": result, "total": len(result)})


@api_v1_bp.route("/providers/<int:provider_id>", methods=["GET"])
@require_auth
@require_role("admin")
def get_provider(provider_id):
    """Get provider details"""
    provider = db(db.ai_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    sync = db(db.marchproxy_ailb_sync.provider_id == provider_id).select().first()

    return jsonify(
        {
            "id": provider.id,
            "name": provider.name,
            "provider_type": provider.provider_type,
            "endpoint_url": provider.endpoint_url,
            "model_list": provider.model_list,
            "rate_limits": provider.rate_limits,
            "enabled": provider.enabled,
            "priority": provider.priority,
            "extra_config": provider.extra_config,
            "tls_config": provider.tls_config,
            "ailb_sync_enabled": provider.ailb_sync_enabled,
            "ailb_route_config": provider.ailb_route_config,
            "sync_status": {
                "status": sync.sync_status if sync else "not_synced",
                "ailb_route_id": sync.ailb_route_id if sync else None,
                "last_synced": sync.last_synced.isoformat() if sync and sync.last_synced else None,
                "sync_error": sync.sync_error if sync else None,
            },
            "created_at": provider.created_at.isoformat() if provider.created_at else None,
        }
    )


@api_v1_bp.route("/providers", methods=["POST"])
@require_auth
@require_role("admin")
def create_provider():
    """Create a new AI provider"""
    data = request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    required_fields = ["name", "provider_type", "endpoint_url"]
    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"{field} is required"}), 400

    provider_type = data["provider_type"]
    if provider_type not in SUPPORTED_PROVIDERS:
        return jsonify({"error": f"Unsupported provider type: {provider_type}"}), 400

    # Check for existing provider
    existing = db(db.ai_providers.name == data["name"]).select().first()
    if existing:
        return jsonify({"error": "Provider name already exists"}), 409

    # Validate API key requirement
    provider_info = SUPPORTED_PROVIDERS[provider_type]
    if provider_info["requires_api_key"] and not data.get("api_key"):
        return jsonify({"error": f"{provider_type} requires an API key"}), 400

    provider_id = db.ai_providers.insert(
        name=data["name"],
        provider_type=provider_type,
        endpoint_url=data["endpoint_url"],
        api_key=data.get("api_key"),
        model_list=data.get("model_list", provider_info["models"]),
        rate_limits=data.get("rate_limits", {}),
        enabled=data.get("enabled", True),
        priority=data.get("priority", 100),
        extra_config=data.get("extra_config", {}),
        tls_config=data.get("tls_config", {}),
        ailb_sync_enabled=data.get("ailb_sync_enabled", True),
        created_at=datetime.utcnow(),
    )
    db.commit()

    # Create sync record
    config_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    db.marchproxy_ailb_sync.insert(
        provider_id=provider_id, sync_status="pending", config_hash=config_hash, created_at=datetime.utcnow()
    )
    db.commit()

    return (
        jsonify(
            {
                "id": provider_id,
                "name": data["name"],
                "provider_type": provider_type,
                "sync_status": "pending",
                "message": "Provider created successfully. Use /sync endpoint to push to AILB.",
            }
        ),
        201,
    )


@api_v1_bp.route("/providers/<int:provider_id>", methods=["PUT"])
@require_auth
@require_role("admin")
def update_provider(provider_id):
    """Update provider configuration"""
    data = request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    provider = db(db.ai_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    update_fields = {}

    if "name" in data:
        existing = db((db.ai_providers.name == data["name"]) & (db.ai_providers.id != provider_id)).select().first()
        if existing:
            return jsonify({"error": "Provider name already exists"}), 409
        update_fields["name"] = data["name"]

    if "endpoint_url" in data:
        update_fields["endpoint_url"] = data["endpoint_url"]

    if "api_key" in data:
        update_fields["api_key"] = data["api_key"]

    if "model_list" in data:
        update_fields["model_list"] = data["model_list"]

    if "rate_limits" in data:
        update_fields["rate_limits"] = data["rate_limits"]

    if "enabled" in data:
        update_fields["enabled"] = data["enabled"]

    if "priority" in data:
        update_fields["priority"] = data["priority"]

    if "extra_config" in data:
        update_fields["extra_config"] = data["extra_config"]

    if "tls_config" in data:
        update_fields["tls_config"] = data["tls_config"]

    if "ailb_sync_enabled" in data:
        update_fields["ailb_sync_enabled"] = data["ailb_sync_enabled"]

    if "ailb_route_config" in data:
        update_fields["ailb_route_config"] = data["ailb_route_config"]

    if update_fields:
        db(db.ai_providers.id == provider_id).update(**update_fields)

        # Update sync status to pending
        db(db.marchproxy_ailb_sync.provider_id == provider_id).update(sync_status="pending")
        db.commit()

    return jsonify({"message": "Provider updated successfully. Re-sync required."})


@api_v1_bp.route("/providers/<int:provider_id>", methods=["DELETE"])
@require_auth
@require_role("admin")
def delete_provider(provider_id):
    """Delete provider"""
    provider = db(db.ai_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    # Mark sync as deleted
    db(db.marchproxy_ailb_sync.provider_id == provider_id).update(sync_status="deleted")

    # Soft delete by disabling
    db(db.ai_providers.id == provider_id).update(enabled=False)
    db.commit()

    return jsonify({"message": "Provider disabled successfully. Remove from AILB manually or via sync."})


@api_v1_bp.route("/providers/<int:provider_id>/test", methods=["POST"])
@require_auth
@require_role("admin")
def test_provider(provider_id):
    """Test provider connectivity"""
    provider = db(db.ai_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    # TODO: Implement actual connectivity test based on provider type
    # For now, return a mock response
    return jsonify(
        {
            "provider_id": provider_id,
            "provider_type": provider.provider_type,
            "endpoint_url": provider.endpoint_url,
            "status": "connected",
            "latency_ms": 150,
            "message": "Connection test successful",
        }
    )


@api_v1_bp.route("/providers/<int:provider_id>/sync", methods=["POST"])
@require_auth
@require_role("admin")
def sync_provider(provider_id):
    """Sync provider to MarchProxy AILB"""
    provider = db(db.ai_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    if not provider.ailb_sync_enabled:
        return jsonify({"error": "AILB sync is disabled for this provider"}), 400

    # TODO: Implement actual gRPC call to AILB
    # For now, simulate sync

    # Update sync status
    db(db.marchproxy_ailb_sync.provider_id == provider_id).update(
        sync_status="synced", last_synced=datetime.utcnow(), sync_error=None
    )
    db.commit()

    return jsonify(
        {"provider_id": provider_id, "sync_status": "synced", "message": "Provider synced to AILB successfully"}
    )


@api_v1_bp.route("/providers/<int:provider_id>/sync-status", methods=["GET"])
@require_auth
@require_role("admin")
def get_sync_status(provider_id):
    """Get provider sync status"""
    provider = db(db.ai_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    sync = db(db.marchproxy_ailb_sync.provider_id == provider_id).select().first()

    return jsonify(
        {
            "provider_id": provider_id,
            "provider_name": provider.name,
            "ailb_sync_enabled": provider.ailb_sync_enabled,
            "sync_status": sync.sync_status if sync else "not_synced",
            "ailb_route_id": sync.ailb_route_id if sync else None,
            "last_synced": sync.last_synced.isoformat() if sync and sync.last_synced else None,
            "sync_error": sync.sync_error if sync else None,
            "config_hash": sync.config_hash if sync else None,
        }
    )


@api_v1_bp.route("/providers/<int:provider_id>/models", methods=["GET"])
@require_auth
def get_provider_models(provider_id):
    """Get available models for a provider"""
    provider = db(db.ai_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    # Return configured models or default models for the provider type
    models = provider.model_list or SUPPORTED_PROVIDERS.get(provider.provider_type, {}).get("models", [])

    return jsonify({"provider_id": provider_id, "provider_type": provider.provider_type, "models": models})


# ---------------------------------------------------------------------------
# Provider Credentials Sub-Resource  (/api/v1/providers/<id>/credentials)
# ---------------------------------------------------------------------------


def _mask_key(api_key: str | None) -> str:
    """Return a masked representation of an API key — never the plaintext value."""
    if not api_key:
        return ""
    # Strip enc: prefix before masking so length reflects real key, not ciphertext
    raw = api_key[4:] if api_key.startswith("enc:") else api_key
    if len(raw) <= 8:
        return "****"
    return raw[:4] + "****" + raw[-4:]


def _credential_to_dict(cred) -> dict:
    """Serialise a provider_credentials row — api_key is NEVER returned."""
    return {
        "id": cred.id,
        "provider_id": cred.provider_id,
        "label": cred.label,
        "api_key_masked": _mask_key(cred.api_key),
        "org_id": cred.org_id,
        "account_meta": cred.account_meta,
        "weight": cred.weight,
        "enabled": cred.enabled,
        "request_count": cred.request_count,
        "token_count": cred.token_count,
        "last_used_at": cred.last_used_at.isoformat() if cred.last_used_at else None,
        "created_at": cred.created_at.isoformat() if cred.created_at else None,
    }


@api_v1_bp.route("/providers/<int:provider_id>/credentials", methods=["GET"])
@require_auth
@require_role("admin")
def list_provider_credentials(provider_id: int):
    """List all credentials for a provider. API keys are never returned in plaintext."""
    provider = db(db.ai_providers.id == provider_id).select().first()
    if not provider:
        return jsonify({"status": "error", "error": "Provider not found"}), 404

    creds = db(db.provider_credentials.provider_id == provider_id).select(orderby=db.provider_credentials.id)
    return jsonify(
        {
            "status": "success",
            "data": [_credential_to_dict(c) for c in creds],
            "meta": {
                "provider_id": provider_id,
                "total": len(creds),
                "timestamp": datetime.utcnow().isoformat() + "Z",
            },
        }
    )


@api_v1_bp.route("/providers/<int:provider_id>/credentials", methods=["POST"])
@require_auth
@require_role("admin")
def create_provider_credential(provider_id: int):
    """Add a credential to a provider's pool."""
    from shared.security.credential_encryption import encrypt_credential

    provider = db(db.ai_providers.id == provider_id).select().first()
    if not provider:
        return jsonify({"status": "error", "error": "Provider not found"}), 404

    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    label = data.get("label", "").strip()
    if not label:
        return jsonify({"status": "error", "error": "label is required"}), 400
    if len(label) > 255:
        return jsonify({"status": "error", "error": "label must be <= 255 characters"}), 400

    api_key_plain = data.get("api_key", "").strip()
    if not api_key_plain:
        provider_info = SUPPORTED_PROVIDERS.get(provider.provider_type, {})
        if provider_info.get("requires_api_key", True):
            return jsonify({"status": "error", "error": "api_key is required for this provider type"}), 400

    # Check label uniqueness within this provider
    existing = (
        db((db.provider_credentials.provider_id == provider_id) & (db.provider_credentials.label == label))
        .select()
        .first()
    )
    if existing:
        return (
            jsonify({"status": "error", "error": f"Credential with label '{label}' already exists for this provider"}),
            409,
        )

    weight = data.get("weight", 100)
    if not isinstance(weight, int) or weight < 1 or weight > 10000:
        return jsonify({"status": "error", "error": "weight must be an integer between 1 and 10000"}), 400

    encrypted_key = encrypt_credential(api_key_plain) if api_key_plain else None

    cred_id = db.provider_credentials.insert(
        provider_id=provider_id,
        label=label,
        api_key=encrypted_key,
        org_id=data.get("org_id"),
        account_meta=data.get("account_meta"),
        weight=weight,
        enabled=data.get("enabled", True),
        request_count=0,
        token_count=0,
        created_at=datetime.utcnow(),
    )
    db.commit()

    new_cred = db(db.provider_credentials.id == cred_id).select().first()
    return (
        jsonify(
            {
                "status": "success",
                "data": _credential_to_dict(new_cred),
                "meta": {
                    "action": "created",
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                },
            }
        ),
        201,
    )


@api_v1_bp.route("/providers/<int:provider_id>/credentials/<int:cred_id>", methods=["PATCH"])
@require_auth
@require_role("admin")
def update_provider_credential(provider_id: int, cred_id: int):
    """Update label, weight, enabled, org_id, account_meta, or rotate the api_key."""
    from shared.security.credential_encryption import encrypt_credential

    provider = db(db.ai_providers.id == provider_id).select().first()
    if not provider:
        return jsonify({"status": "error", "error": "Provider not found"}), 404

    cred = (
        db((db.provider_credentials.id == cred_id) & (db.provider_credentials.provider_id == provider_id))
        .select()
        .first()
    )
    if not cred:
        return jsonify({"status": "error", "error": "Credential not found"}), 404

    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    update_fields: dict = {}

    if "label" in data:
        label = data["label"].strip()
        if not label or len(label) > 255:
            return jsonify({"status": "error", "error": "label must be 1-255 characters"}), 400
        # Check uniqueness (exclude self)
        conflict = (
            db(
                (db.provider_credentials.provider_id == provider_id)
                & (db.provider_credentials.label == label)
                & (db.provider_credentials.id != cred_id)
            )
            .select()
            .first()
        )
        if conflict:
            return jsonify({"status": "error", "error": f"Label '{label}' already in use"}), 409
        update_fields["label"] = label

    if "weight" in data:
        weight = data["weight"]
        if not isinstance(weight, int) or weight < 1 or weight > 10000:
            return jsonify({"status": "error", "error": "weight must be an integer between 1 and 10000"}), 400
        update_fields["weight"] = weight

    if "enabled" in data:
        update_fields["enabled"] = bool(data["enabled"])

    if "org_id" in data:
        update_fields["org_id"] = data["org_id"]

    if "account_meta" in data:
        update_fields["account_meta"] = data["account_meta"]

    if "api_key" in data:
        new_key = data["api_key"].strip()
        if not new_key:
            return jsonify({"status": "error", "error": "api_key cannot be empty when provided"}), 400
        update_fields["api_key"] = encrypt_credential(new_key)

    if not update_fields:
        return jsonify({"status": "error", "error": "No valid fields to update"}), 400

    db(db.provider_credentials.id == cred_id).update(**update_fields)
    db.commit()

    updated = db(db.provider_credentials.id == cred_id).select().first()
    return jsonify(
        {
            "status": "success",
            "data": _credential_to_dict(updated),
            "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
        }
    )


@api_v1_bp.route("/providers/<int:provider_id>/credentials/<int:cred_id>", methods=["DELETE"])
@require_auth
@require_role("admin")
def delete_provider_credential(provider_id: int, cred_id: int):
    """Remove a credential from the pool. Requires at least one other credential to remain."""
    provider = db(db.ai_providers.id == provider_id).select().first()
    if not provider:
        return jsonify({"status": "error", "error": "Provider not found"}), 404

    cred = (
        db((db.provider_credentials.id == cred_id) & (db.provider_credentials.provider_id == provider_id))
        .select()
        .first()
    )
    if not cred:
        return jsonify({"status": "error", "error": "Credential not found"}), 404

    # Safety guard: refuse to delete the last credential
    total = len(db(db.provider_credentials.provider_id == provider_id).select())
    if total <= 1:
        return (
            jsonify(
                {
                    "status": "error",
                    "error": "Cannot delete the last credential. Add a replacement first.",
                }
            ),
            409,
        )

    db(db.provider_credentials.id == cred_id).delete()
    db.commit()

    return jsonify(
        {
            "status": "success",
            "data": {"id": cred_id},
            "meta": {
                "action": "deleted",
                "timestamp": datetime.utcnow().isoformat() + "Z",
            },
        }
    )
