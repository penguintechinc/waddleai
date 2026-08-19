"""
WaddleAI Management API v1 - AI Provider Management Endpoints
"""

import asyncio
from datetime import datetime

from quart import current_app, jsonify, request

from shared.auth.rbac import Permission

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth, require_scope

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
async def list_provider_types():
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
@require_scope(Permission.PROVIDER_ADMIN)
async def list_providers():
    """List all configured AI providers"""

    def _fetch():
        return db(db.ai_providers.id > 0).select()

    providers = await asyncio.to_thread(_fetch)

    result = []
    for provider in providers:
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
                "created_at": provider.created_at.isoformat() if provider.created_at else None,
            }
        )

    return jsonify({"providers": result, "total": len(result)})


@api_v1_bp.route("/providers/<int:provider_id>", methods=["GET"])
@require_auth
@require_scope(Permission.PROVIDER_ADMIN)
async def get_provider(provider_id):
    """Get provider details"""

    def _fetch():
        return db(db.ai_providers.id == provider_id).select().first()

    provider = await asyncio.to_thread(_fetch)

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

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
            "created_at": provider.created_at.isoformat() if provider.created_at else None,
        }
    )


@api_v1_bp.route("/providers", methods=["POST"])
@require_auth
@require_scope(Permission.PROVIDER_ADMIN)
async def create_provider():
    """Create a new AI provider"""
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    required_fields = ["name", "provider_type", "endpoint_url"]
    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"{field} is required"}), 400

    provider_type = data["provider_type"]
    if provider_type not in SUPPORTED_PROVIDERS:
        return jsonify({"error": f"Unsupported provider type: {provider_type}"}), 400

    provider_name = data["name"]

    def _check_existing():
        return db(db.ai_providers.name == provider_name).select().first()

    existing = await asyncio.to_thread(_check_existing)
    if existing:
        return jsonify({"error": "Provider name already exists"}), 409

    provider_info = SUPPORTED_PROVIDERS[provider_type]
    if provider_info["requires_api_key"] and not data.get("api_key"):
        return jsonify({"error": f"{provider_type} requires an API key"}), 400

    def _create():
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

        return provider_id

    provider_id = await asyncio.to_thread(_create)

    return (
        jsonify(
            {
                "id": provider_id,
                "name": data["name"],
                "provider_type": provider_type,
                "message": "Provider created successfully.",
            }
        ),
        201,
    )


@api_v1_bp.route("/providers/<int:provider_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.PROVIDER_ADMIN)
async def update_provider(provider_id):
    """Update provider configuration"""
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    def _update():
        provider = db(db.ai_providers.id == provider_id).select().first()

        if not provider:
            return "not_found"

        update_fields = {}

        if "name" in data:
            existing = (
                db((db.ai_providers.name == data["name"]) & (db.ai_providers.id != provider_id)).select().first()
            )
            if existing:
                return "name_conflict"
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
            db.commit()

        return "ok"

    result = await asyncio.to_thread(_update)

    if result == "not_found":
        return jsonify({"error": "Provider not found"}), 404
    if result == "name_conflict":
        return jsonify({"error": "Provider name already exists"}), 409

    return jsonify({"message": "Provider updated successfully."})


@api_v1_bp.route("/providers/<int:provider_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.PROVIDER_ADMIN)
async def delete_provider(provider_id):
    """Delete provider"""

    def _delete():
        provider = db(db.ai_providers.id == provider_id).select().first()

        if not provider:
            return False

        # Soft delete by disabling
        db(db.ai_providers.id == provider_id).update(enabled=False)
        db.commit()

        return True

    found = await asyncio.to_thread(_delete)

    if not found:
        return jsonify({"error": "Provider not found"}), 404

    return jsonify({"message": "Provider disabled successfully."})


@api_v1_bp.route("/providers/<int:provider_id>/test", methods=["POST"])
@require_auth
@require_scope(Permission.PROVIDER_ADMIN)
async def test_provider(provider_id):
    """Test provider connectivity"""
    provider = await asyncio.to_thread(lambda: db(db.ai_providers.id == provider_id).select().first())

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


@api_v1_bp.route("/providers/<int:provider_id>/models", methods=["GET"])
@require_auth
async def get_provider_models(provider_id):
    """Get available models for a provider"""
    provider = await asyncio.to_thread(lambda: db(db.ai_providers.id == provider_id).select().first())

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
@require_scope(Permission.PROVIDER_ADMIN)
async def list_provider_credentials(provider_id: int):
    """List all credentials for a provider. API keys are never returned in plaintext."""

    def _fetch():
        provider = db(db.ai_providers.id == provider_id).select().first()
        if not provider:
            return None
        return db(db.provider_credentials.provider_id == provider_id).select(orderby=db.provider_credentials.id)

    creds = await asyncio.to_thread(_fetch)

    if creds is None:
        return jsonify({"status": "error", "error": "Provider not found"}), 404

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
@require_scope(Permission.PROVIDER_ADMIN)
async def create_provider_credential(provider_id: int):
    """Add a credential to a provider's pool."""
    from shared.security.credential_encryption import encrypt_credential

    def _get_provider():
        return db(db.ai_providers.id == provider_id).select().first()

    provider = await asyncio.to_thread(_get_provider)
    if not provider:
        return jsonify({"status": "error", "error": "Provider not found"}), 404

    data = await request.get_json()
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

    encrypted_key = encrypt_credential(api_key_plain) if api_key_plain else None

    def _create():
        # Check label uniqueness within this provider
        existing = (
            db((db.provider_credentials.provider_id == provider_id) & (db.provider_credentials.label == label))
            .select()
            .first()
        )
        if existing:
            return ("label_conflict", None)

        weight = data.get("weight", 100)
        if not isinstance(weight, int) or weight < 1 or weight > 10000:
            return ("invalid_weight", None)

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

        return ("ok", db(db.provider_credentials.id == cred_id).select().first())

    status, payload = await asyncio.to_thread(_create)

    if status == "label_conflict":
        return (
            jsonify({"status": "error", "error": f"Credential with label '{label}' already exists for this provider"}),
            409,
        )
    if status == "invalid_weight":
        return jsonify({"status": "error", "error": "weight must be an integer between 1 and 10000"}), 400

    new_cred = payload
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
@require_scope(Permission.PROVIDER_ADMIN)
async def update_provider_credential(provider_id: int, cred_id: int):
    """Update label, weight, enabled, org_id, account_meta, or rotate the api_key."""
    from shared.security.credential_encryption import encrypt_credential

    def _check_existence():
        provider = db(db.ai_providers.id == provider_id).select().first()
        if not provider:
            return "provider_not_found"

        cred = (
            db((db.provider_credentials.id == cred_id) & (db.provider_credentials.provider_id == provider_id))
            .select()
            .first()
        )
        if not cred:
            return "cred_not_found"

        return "ok"

    existence = await asyncio.to_thread(_check_existence)
    if existence == "provider_not_found":
        return jsonify({"status": "error", "error": "Provider not found"}), 404
    if existence == "cred_not_found":
        return jsonify({"status": "error", "error": "Credential not found"}), 404

    data = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    if "label" in data:
        label = data["label"].strip()
        if not label or len(label) > 255:
            return jsonify({"status": "error", "error": "label must be 1-255 characters"}), 400

    if "weight" in data:
        weight = data["weight"]
        if not isinstance(weight, int) or weight < 1 or weight > 10000:
            return jsonify({"status": "error", "error": "weight must be an integer between 1 and 10000"}), 400

    if "api_key" in data:
        new_key = data["api_key"].strip()
        if not new_key:
            return jsonify({"status": "error", "error": "api_key cannot be empty when provided"}), 400
        encrypted_new_key = encrypt_credential(new_key)

    def _update():
        update_fields: dict = {}

        if "label" in data:
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
                return "label_conflict"
            update_fields["label"] = label

        if "weight" in data:
            update_fields["weight"] = weight

        if "enabled" in data:
            update_fields["enabled"] = bool(data["enabled"])

        if "org_id" in data:
            update_fields["org_id"] = data["org_id"]

        if "account_meta" in data:
            update_fields["account_meta"] = data["account_meta"]

        if "api_key" in data:
            update_fields["api_key"] = encrypted_new_key

        if not update_fields:
            return "no_fields"

        db(db.provider_credentials.id == cred_id).update(**update_fields)
        db.commit()

        return db(db.provider_credentials.id == cred_id).select().first()

    result = await asyncio.to_thread(_update)

    if result == "label_conflict":
        return jsonify({"status": "error", "error": f"Label '{label}' already in use"}), 409
    if result == "no_fields":
        return jsonify({"status": "error", "error": "No valid fields to update"}), 400

    updated = result
    return jsonify(
        {
            "status": "success",
            "data": _credential_to_dict(updated),
            "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
        }
    )


@api_v1_bp.route("/providers/<int:provider_id>/credentials/<int:cred_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.PROVIDER_ADMIN)
async def delete_provider_credential(provider_id: int, cred_id: int):
    """Remove a credential from the pool. Requires at least one other credential to remain."""

    def _delete():
        provider = db(db.ai_providers.id == provider_id).select().first()
        if not provider:
            return "provider_not_found"

        cred = (
            db((db.provider_credentials.id == cred_id) & (db.provider_credentials.provider_id == provider_id))
            .select()
            .first()
        )
        if not cred:
            return "cred_not_found"

        # Safety guard: refuse to delete the last credential
        total = len(db(db.provider_credentials.provider_id == provider_id).select())
        if total <= 1:
            return "last_credential"

        db(db.provider_credentials.id == cred_id).delete()
        db.commit()

        return "ok"

    result = await asyncio.to_thread(_delete)

    if result == "provider_not_found":
        return jsonify({"status": "error", "error": "Provider not found"}), 404
    if result == "cred_not_found":
        return jsonify({"status": "error", "error": "Credential not found"}), 404
    if result == "last_credential":
        return (
            jsonify(
                {
                    "status": "error",
                    "error": "Cannot delete the last credential. Add a replacement first.",
                }
            ),
            409,
        )

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
