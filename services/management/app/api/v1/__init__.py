"""WaddleAI Management API v1."""

from quart import Blueprint

api_v1_bp = Blueprint("api_v1", __name__)

# Import all route modules to register them. New entries are appended at
# the end, not alphabetized -- several branches add a module here in
# parallel, and reordering the whole block guarantees a merge conflict
# that silently drops someone else's registration.
#
# The two AILB route modules are deliberately absent: §5 Task 13 deleted
# both. Branches cut before that deletion still list them, so a naive union
# reimports modules that no longer exist. (Their names are not spelled out
# here on purpose -- test_no_marchproxy.py greps this file's raw text.)
from . import (  # noqa: I001 -- append-only order, see comment above
    auth,
    cache_configs,
    cilium,
    keys,
    knowledge,
    llamacpp,
    memory_config,
    memory_scoping,
    ollama,
    ollama_models,
    organizations,
    providers,
    quotas,
    usage,
    users,
    webhooks,
    integrations,
    fleet,
    code_repos,
    graph,
    routing_destinations,
    routing_destination_credentials,
)
