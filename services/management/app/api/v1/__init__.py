"""WaddleAI Management API v1."""

from quart import Blueprint

api_v1_bp = Blueprint("api_v1", __name__)

# Import all route modules to register them
from . import (
    ailb,
    ailb_memory,
    auth,
    cache_configs,
    cilium,
    keys,
    llamacpp,
    ollama,
    ollama_models,
    organizations,
    providers,
    quotas,
    usage,
    users,
    webhooks,
)
