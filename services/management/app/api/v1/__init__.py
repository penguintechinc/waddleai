"""WaddleAI Management API v1."""

from quart import Blueprint

api_v1_bp = Blueprint("api_v1", __name__)

# Import all route modules to register them
from . import (
    auth,
    cache_configs,
    cilium,
    keys,
    llamacpp,
    memory_config,
    ollama,
    ollama_models,
    organizations,
    providers,
    quotas,
    usage,
    users,
    webhooks,
)
