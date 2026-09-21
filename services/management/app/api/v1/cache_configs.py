"""WaddleAI Management API v1 - Response Cache Configuration Endpoints.

CRUD for `cache_configs` (spec §6.4), the key > org > global precedence
table consumed at request time by shared.cache.config.CacheConfigResolver.
Writes invalidate the resolver's Valkey hot-path entry for the affected
scope so proxy reads never serve a stale config past the write.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from penguin_dal.db import DB
from quart import g, jsonify, request

from shared.auth.rbac import Permission
from shared.cache.config import scope_cache_key

from ...extensions import db, redis_client
from . import api_v1_bp
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)

_VALID_SCOPE_TYPES = {"global", "org", "key"}


def _db() -> DB:
    """Return the process-wide penguin-dal handle, narrowed away from ``None``.

    ``extensions.db`` is declared ``DB | None`` because it starts unset before
    ``init_db()`` runs at startup; every route below only executes after that
    point, so this narrows the type for mypy without adding any reachable
    failure mode (mirrors the same helper in ``model_aliases.py``/``fleet.py``).
    """
    if db is None:
        raise RuntimeError("database not initialized")
    return db


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "scope_type": row.scope_type,
        "scope_ref": row.scope_ref,
        "exact_enabled": row.exact_enabled,
        "semantic_enabled": row.semantic_enabled,
        "semantic_threshold": row.semantic_threshold,
        "ttl_seconds": row.ttl_seconds,
        "max_entry_kb": row.max_entry_kb,
        "anthropic_cache_control": row.anthropic_cache_control,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _validate_payload(data: dict[str, Any], partial: bool = False) -> str | None:
    """Returns an error message, or None if the payload is valid."""
    if not partial or "scope_type" in data:
        if data.get("scope_type") not in _VALID_SCOPE_TYPES:
            return f"scope_type must be one of {sorted(_VALID_SCOPE_TYPES)}"
    scope_type = data.get("scope_type")
    if scope_type == "global" and data.get("scope_ref") is not None:
        return "scope_ref must be null for scope_type='global'"
    if scope_type in ("org", "key") and not data.get("scope_ref"):
        return "scope_ref is required for scope_type='org'/'key'"

    if "semantic_threshold" in data and data["semantic_threshold"] is not None:
        threshold = data["semantic_threshold"]
        if not isinstance(threshold, (int, float)) or not (0.5 <= threshold <= 1.0):
            return "semantic_threshold must be between 0.5 and 1.0"

    if "ttl_seconds" in data and data["ttl_seconds"] is not None:
        if not isinstance(data["ttl_seconds"], int) or data["ttl_seconds"] <= 0:
            return "ttl_seconds must be a positive integer"

    if "max_entry_kb" in data and data["max_entry_kb"] is not None:
        if not isinstance(data["max_entry_kb"], int) or data["max_entry_kb"] <= 0:
            return "max_entry_kb must be a positive integer"

    return None


async def _invalidate_scope(scope_type: str, scope_ref: str | None) -> None:
    """Bust the resolver's Valkey hot-path entry for one scope after a write."""
    if redis_client is None:
        return
    key = scope_cache_key(scope_type, scope_ref)
    try:
        await asyncio.to_thread(redis_client.delete, key)
    except Exception as exc:  # pragma: no cover - Valkey unavailability must not fail the write
        logger.warning("cache_configs: failed to invalidate %s: %s", key, exc)


def _visible_query(user_role: str, user_org_id: int | None) -> Any:
    """Admin sees every row; everyone else sees global rows plus their own org's row.

    regression: audit-2026-09-14 -- the two read routes previously carried
    no tenant filter at all, so any authenticated user enumerated every
    organization's cache config. Mirrors the write path's ownership model
    (`_authorize_scope_write`) and the identical visibility helper in
    model_access_policies.py.

    Key-scoped rows are admin-only on read: `scope_ref` holds a
    `virtual_keys.id`, so resolving their owning org needs a per-row lookup
    the query builder cannot express as a join here. Same trade-off, and
    same rationale, as model_access_policies._visible_query.
    """
    table = _db().cache_configs
    if user_role == "admin":
        return table.id > 0
    query = table.scope_type == "global"
    if user_org_id is not None:
        query |= (table.scope_type == "org") & (table.scope_ref == str(user_org_id))
    return query


def _row_visible_to(row: Any, user_role: str, user_org_id: int | None) -> bool:
    """Re-check one row's visibility in Python, immediately before it is serialized.

    Deliberately redundant with `_visible_query`: the SQL filter is the
    primary control, this is the response-side guard that keeps another
    tenant's row from being serialized even if the query is later widened
    or bypassed (which is exactly the regression audit-2026-09-14 found).
    """
    if user_role == "admin":
        return True
    if row.scope_type == "global":
        return True
    return (
        row.scope_type == "org"
        and user_org_id is not None
        and str(row.scope_ref) == str(user_org_id)
    )


def _authorize_scope_write(scope_type: str, scope_ref: str | None, verb: str) -> tuple | None:
    """Return a (jsonify, 403) tuple if the caller may not write/delete this scope, else None.

    Global rows require admin; org rows require admin or ownership of that org.
    """
    role = g.user.get("role")
    if scope_type == "global" and role != "admin":
        error_msg = f"Only admin may {verb} global cache config"
        return jsonify({"status": "error", "error": error_msg}), 403
    if scope_type == "org" and role != "admin" and scope_ref != str(g.user.get("organization_id")):
        error_msg = f"Cannot {verb} another organization's cache config"
        return jsonify({"status": "error", "error": error_msg}), 403
    return None


@api_v1_bp.route("/cache-configs", methods=["GET"])
@require_auth
async def list_cache_configs() -> tuple:
    """List cache configs, optionally filtered by scope_type/scope_ref."""
    scope_type = request.args.get("scope_type")
    scope_ref = request.args.get("scope_ref")
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    def _fetch():
        query = _visible_query(user_role, user_org_id)
        if scope_type:
            query &= _db().cache_configs.scope_type == scope_type
        if scope_ref is not None:
            query &= _db().cache_configs.scope_ref == scope_ref
        return _db()(query).select(orderby=_db().cache_configs.id)

    rows = await asyncio.to_thread(_fetch)
    visible = [r for r in rows if _row_visible_to(r, user_role, user_org_id)]
    return jsonify({"status": "success", "data": [_row_to_dict(r) for r in visible]}), 200


@api_v1_bp.route("/cache-configs/<int:config_id>", methods=["GET"])
@require_auth
async def get_cache_config(config_id: int) -> tuple:
    """Get a single cache config row by ID."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    def _fetch_one():
        query = _visible_query(user_role, user_org_id) & (_db().cache_configs.id == config_id)
        return _db()(query).select().first()

    row = await asyncio.to_thread(_fetch_one)
    # A row outside the caller's tenant is reported as absent rather than
    # forbidden, so this route cannot be used to enumerate which config ids
    # exist in other organizations (matches model_access_policies.py).
    if not row or not _row_visible_to(row, user_role, user_org_id):
        return jsonify({"status": "error", "error": "Cache config not found"}), 404
    return jsonify({"status": "success", "data": _row_to_dict(row)}), 200


@api_v1_bp.route("/cache-configs", methods=["POST"])
@require_auth
@require_scope(Permission.CACHE_CONFIG_WRITE)
async def create_cache_config() -> tuple:
    """Create a new cache config row for a scope. 409 if the scope already has one."""
    data: dict[str, Any] | None = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    error = _validate_payload(data)
    if error:
        return jsonify({"status": "error", "error": error}), 400

    scope_type = data["scope_type"]
    scope_ref = data.get("scope_ref")

    auth_error = _authorize_scope_write(scope_type, scope_ref, verb="write")
    if auth_error:
        return auth_error

    def _create():
        scope_query = (_db().cache_configs.scope_type == scope_type) & (
            _db().cache_configs.scope_ref == scope_ref
        )
        existing = _db()(scope_query).select().first()
        if existing:
            return "conflict", existing

        new_id = _db().cache_configs.insert(
            scope_type=scope_type,
            scope_ref=scope_ref,
            exact_enabled=data.get("exact_enabled", True),
            semantic_enabled=data.get("semantic_enabled", False),
            semantic_threshold=data.get("semantic_threshold", 0.95),
            ttl_seconds=data.get("ttl_seconds", 86400),
            max_entry_kb=data.get("max_entry_kb", 256),
            anthropic_cache_control=data.get("anthropic_cache_control", True),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        _db().commit()
        return "created", _db()(_db().cache_configs.id == new_id).select().first()

    action, row = await asyncio.to_thread(_create)

    if action == "conflict":
        error_body = {
            "status": "error",
            "error": "A cache config already exists for this scope",
            "data": _row_to_dict(row),
        }
        return jsonify(error_body), 409

    await _invalidate_scope(scope_type, scope_ref)
    return jsonify({"status": "success", "data": _row_to_dict(row)}), 201


@api_v1_bp.route("/cache-configs/<int:config_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.CACHE_CONFIG_WRITE)
async def update_cache_config(config_id: int) -> tuple:
    """Update an existing cache config row."""
    data: dict[str, Any] | None = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    existing = await asyncio.to_thread(
        lambda: _db()(_db().cache_configs.id == config_id).select().first()
    )
    if not existing:
        return jsonify({"status": "error", "error": "Cache config not found"}), 404

    error = _validate_payload(data, partial=True)
    if error:
        return jsonify({"status": "error", "error": error}), 400

    scope_type = existing.scope_type
    scope_ref = existing.scope_ref
    auth_error = _authorize_scope_write(scope_type, scope_ref, verb="write")
    if auth_error:
        return auth_error

    allowed_fields = (
        "exact_enabled",
        "semantic_enabled",
        "semantic_threshold",
        "ttl_seconds",
        "max_entry_kb",
        "anthropic_cache_control",
    )
    update_fields = {f: data[f] for f in allowed_fields if f in data}
    update_fields["updated_at"] = datetime.utcnow()

    def _update():
        _db()(_db().cache_configs.id == config_id).update(**update_fields)
        _db().commit()
        return _db()(_db().cache_configs.id == config_id).select().first()

    row = await asyncio.to_thread(_update)
    await _invalidate_scope(scope_type, scope_ref)
    return jsonify({"status": "success", "data": _row_to_dict(row)}), 200


@api_v1_bp.route("/cache-configs/<int:config_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.CACHE_CONFIG_WRITE)
async def delete_cache_config(config_id: int) -> tuple:
    """Delete a cache config row (falls back to the next-broader scope)."""
    existing = await asyncio.to_thread(
        lambda: _db()(_db().cache_configs.id == config_id).select().first()
    )
    if not existing:
        return jsonify({"status": "error", "error": "Cache config not found"}), 404

    scope_type = existing.scope_type
    scope_ref = existing.scope_ref
    auth_error = _authorize_scope_write(scope_type, scope_ref, verb="delete")
    if auth_error:
        return auth_error

    def _delete():
        _db()(_db().cache_configs.id == config_id).delete()
        _db().commit()

    await asyncio.to_thread(_delete)
    await _invalidate_scope(scope_type, scope_ref)
    return jsonify({"status": "success", "data": {"id": config_id, "deleted": True}}), 200
