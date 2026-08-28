"""Admin CRUD REST endpoints for code-api configuration.

All endpoints require JWT authentication (admin role).
Uses the ConfigStore for persistence.
"""

import functools
import logging

import jwt as pyjwt
from quart import Blueprint, jsonify, request

from penguincode_cli.server.models.config_store import ConfigStore

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/api/v1")

_config_store: ConfigStore | None = None
_jwt_secret: str = ""


def init_admin(store: ConfigStore, jwt_secret: str) -> None:
    """Initialise module-level references. Called once at startup."""
    global _config_store, _jwt_secret
    _config_store = store
    _jwt_secret = jwt_secret


# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------


def require_admin(fn):
    """Decorator that requires a valid JWT with admin scope."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Missing Authorization header"}), 401
        token = auth[7:]
        try:
            claims = pyjwt.decode(token, _jwt_secret, algorithms=["HS256"])
            if "admin" not in claims.get("scopes", []):
                return jsonify({"error": "Admin scope required"}), 403
        except pyjwt.ExpiredSignatureError:
            return jsonify({"error": "Token expired"}), 401
        except pyjwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401
        return await fn(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Generic CRUD helper
# ---------------------------------------------------------------------------


def _crud_routes(
    entity: str,
    key_field: str,
    list_fn: str,
    get_fn: str,
    upsert_fn: str,
    delete_fn: str,
):
    """Register GET (list) and PUT (upsert) for an entity type."""

    @admin_bp.route(f"/{entity}", methods=["GET"], endpoint=f"list_{entity}")
    @require_admin
    async def list_all():
        assert _config_store is not None
        items = await getattr(_config_store, list_fn)()
        return jsonify(items)

    @admin_bp.route(f"/{entity}", methods=["PUT"], endpoint=f"upsert_{entity}")
    @require_admin
    async def upsert():
        assert _config_store is not None
        body = await request.get_json(silent=True)
        if not body or key_field not in body:
            return jsonify({"error": f"Missing required field: {key_field}"}), 400
        await getattr(_config_store, upsert_fn)(body)
        return jsonify({"ok": True, key_field: body[key_field]})

    @admin_bp.route(
        f"/{entity}/<key>",
        methods=["DELETE"],
        endpoint=f"delete_{entity}",
    )
    @require_admin
    async def delete(key: str):
        assert _config_store is not None
        deleted = await getattr(_config_store, delete_fn)(key)
        if not deleted:
            return jsonify({"error": "Not found"}), 404
        return jsonify({"ok": True})

    @admin_bp.route(
        f"/{entity}/<key>",
        methods=["GET"],
        endpoint=f"get_{entity}",
    )
    @require_admin
    async def get_one(key: str):
        assert _config_store is not None
        item = await getattr(_config_store, get_fn)(key)
        if item is None:
            return jsonify({"error": "Not found"}), 404
        return jsonify(item)


# Register CRUD routes for each entity type
_crud_routes("models", "name", "list_models", "get_model", "upsert_model", "delete_model")
_crud_routes("agents", "name", "list_agents", "get_agent", "upsert_agent", "delete_agent")
_crud_routes("mcp-servers", "name", "list_mcp_servers", "get_mcp_server", "upsert_mcp_server", "delete_mcp_server")
_crud_routes("plugins", "name", "list_plugins", "get_plugin", "upsert_plugin", "delete_plugin")
_crud_routes("skills", "name", "list_skills", "get_skill", "upsert_skill", "delete_skill")
_crud_routes("tools", "name", "list_tools", "get_tool", "upsert_tool", "delete_tool")
_crud_routes("github-orgs", "org", "list_github_orgs", "get_github_org", "upsert_github_org", "delete_github_org")


# ---------------------------------------------------------------------------
# Instructions and permissions (different shape, hand-coded)
# ---------------------------------------------------------------------------


@admin_bp.route("/instructions", methods=["GET"])
@require_admin
async def list_instructions():
    assert _config_store is not None
    paths = await _config_store.list_instructions()
    return jsonify(paths)


@admin_bp.route("/instructions", methods=["PUT"])
@require_admin
async def add_instruction():
    assert _config_store is not None
    body = await request.get_json(silent=True)
    if not body or "path" not in body:
        return jsonify({"error": "Missing required field: path"}), 400
    await _config_store.add_instruction(body["path"])
    return jsonify({"ok": True})


@admin_bp.route("/instructions/<path:path>", methods=["DELETE"])
@require_admin
async def remove_instruction(path: str):
    assert _config_store is not None
    removed = await _config_store.remove_instruction(path)
    if not removed:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


@admin_bp.route("/permissions", methods=["GET"])
@require_admin
async def list_permissions():
    assert _config_store is not None
    perms = await _config_store.list_permissions()
    return jsonify(perms)


@admin_bp.route("/permissions", methods=["PUT"])
@require_admin
async def set_permission():
    assert _config_store is not None
    body = await request.get_json(silent=True)
    if not body or "pattern" not in body or "policy" not in body:
        return jsonify({"error": "Missing pattern and/or policy"}), 400
    await _config_store.set_permission(body["pattern"], body["policy"])
    return jsonify({"ok": True})
