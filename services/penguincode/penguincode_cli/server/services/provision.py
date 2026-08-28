"""Provisioning REST endpoint for code-api.

POST /api/v1/provision — Returns full client configuration based on
license tier, client platform, and GPU capabilities.
"""

import logging
from typing import Any

from quart import Blueprint, jsonify, request

from penguincode_cli.server.models.config_store import ConfigStore

logger = logging.getLogger(__name__)

provision_bp = Blueprint("provision", __name__, url_prefix="/api/v1")

# Store reference set at app startup (see rest_app.py)
_config_store: ConfigStore | None = None
_license_validator: Any = None  # Optional penguin-licensing LicenseClient


def init_provision(store: ConfigStore, license_validator: Any = None) -> None:
    """Initialise module-level references. Called once at startup."""
    global _config_store, _license_validator
    _config_store = store
    _license_validator = license_validator


# ---------------------------------------------------------------------------
# Feature gating by license tier
# ---------------------------------------------------------------------------

TIER_FEATURES = {
    "community": [
        {"name": "basic_agents", "entitled": True},
    ],
    "professional": [
        {"name": "basic_agents", "entitled": True},
        {"name": "multi_agent", "entitled": True},
        {"name": "custom_agents", "entitled": True},
        {"name": "docs_rag", "entitled": True},
    ],
    "enterprise": [
        {"name": "basic_agents", "entitled": True},
        {"name": "multi_agent", "entitled": True},
        {"name": "custom_agents", "entitled": True},
        {"name": "docs_rag", "entitled": True},
        {"name": "gpu_manager", "entitled": True},
        {"name": "multi_org", "entitled": True},
        {"name": "team_server", "entitled": True},
    ],
}

# Community tier gets only these agents
COMMUNITY_AGENTS = {"foreman", "executor", "explorer"}


def _validate_license(license_key: str | None) -> dict:
    """Validate license and return license info dict.

    Falls back to community tier if validation fails or no key provided.
    """
    if not license_key:
        return {
            "valid": False,
            "tier": "community",
            "customer": "",
            "features": TIER_FEATURES["community"],
        }

    if _license_validator is not None:
        try:
            result = _license_validator.validate(license_key)
            tier = result.get("tier", "community")
            return {
                "valid": True,
                "tier": tier,
                "customer": result.get("customer", ""),
                "features": TIER_FEATURES.get(tier, TIER_FEATURES["community"]),
            }
        except Exception:
            logger.warning("License validation failed, falling back to community tier")

    # No validator or validation failed — community tier
    return {
        "valid": False,
        "tier": "community",
        "customer": "",
        "features": TIER_FEATURES["community"],
    }


def _filter_by_tier(provision: dict, tier: str) -> dict:
    """Filter provisioning response based on license tier.

    Community tier only gets foreman + executor + explorer (3 agents).
    """
    if tier == "community":
        provision["agents"] = {k: v for k, v in provision["agents"].items() if k in COMMUNITY_AGENTS}
        # Remove non-community MCP servers
        provision["mcp_servers"] = []
        provision["plugins"] = []
        provision["github_orgs"] = []
    return provision


def _filter_models_by_gpu(models: list[dict], vram_mb: int) -> list[dict]:
    """Adjust model requirements based on client GPU VRAM."""
    if vram_mb <= 0:
        return models

    filtered = []
    for m in models:
        entry = dict(m)
        # If client VRAM is small, mark large models as not required
        if (
            vram_mb < 4096
            and "13b" in m["name"]
            or vram_mb < 8192
            and "34b" in m["name"]
            or vram_mb < 16384
            and "70b" in m["name"]
        ):
            entry["required"] = False
        filtered.append(entry)
    return filtered


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@provision_bp.route("/provision", methods=["POST"])
async def provision():
    """Return full client configuration.

    Request body:
        {
            "license_key": "...",
            "client_id": "...",
            "platform": "Linux",
            "gpu_info": {"vram_mb": 8192, "gpu_model": "RTX 3070"}
        }
    """
    assert _config_store is not None, "Config store not initialised"

    body = await request.get_json(silent=True) or {}
    license_key = body.get("license_key")
    gpu_info = body.get("gpu_info", {})
    vram_mb = gpu_info.get("vram_mb", 0)

    # 1. Validate license
    license_info = _validate_license(license_key)
    tier = license_info["tier"]

    # 2. Build full config from store
    ollama_url = await _config_store.kv_get("ollama_api_url") or "http://localhost:11434"
    response = await _config_store.build_provision_response(license_info, ollama_url)

    # 3. Filter by tier
    response = _filter_by_tier(response, tier)

    # 4. Filter models by GPU
    if vram_mb > 0:
        response["ollama"]["models"] = _filter_models_by_gpu(
            response["ollama"]["models"],
            vram_mb,
        )

    logger.info(
        "Provisioned client=%s tier=%s models=%d agents=%d",
        body.get("client_id", "unknown"),
        tier,
        len(response["ollama"]["models"]),
        len(response["agents"]),
    )
    return jsonify(response)


@provision_bp.route("/health", methods=["GET"])
async def health():
    """Simple health check for the REST API."""
    return jsonify({"status": "ok", "service": "code-api"})
