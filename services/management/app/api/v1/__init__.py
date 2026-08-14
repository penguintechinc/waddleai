"""WaddleAI Management API v1."""

from quart import Blueprint

api_v1_bp = Blueprint("api_v1", __name__)

# Import all route modules to register them. New entries are appended at
# the end, not alphabetized -- several branches add a module here in
# parallel, and reordering the whole block guarantees a merge conflict
# that silently drops someone else's registration.
from . import (  # noqa: I001 -- append-only order, see comment above
    ailb,
    ailb_memory,
    auth,
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
    integrations,
)
