"""WaddleAI Management API v1 - Response Cache Configuration Endpoints.

CRUD for `cache_configs` (spec §6.4), the key > org > global precedence
table consumed at request time by shared.cache.config.CacheConfigResolver.
Writes invalidate the resolver's Valkey hot-path entry for the affected
scope so proxy reads never serve a stale config past the write.
"""

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from penguin_dal.db import DB
from quart import g, jsonify, request
from quart_schema import validate_request, validate_response

from shared.auth.rbac import Permission
from shared.cache.config import scope_cache_key

from ...extensions import db, redis_client
from . import api_v1_bp
from ._pagination import PageRequest
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)

_VALID_SCOPE_TYPES = {"global", "org", "key"}


# ---------------------------------------------------------------------------
# Request/response schemas (quart-schema). Request-body fields are optional
# partial updates -- ``None`` means "not supplied", matching the
# ``data.get(key, default)`` presence semantics ``_validate_payload`` and the
# insert/update handlers used before the routes were given a typed schema.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheConfigCreateRequest:
    """Request body for POST /api/v1/cache-configs."""

    scope_type: str | None = None
    scope_ref: str | None = None
    exact_enabled: bool | None = None
    semantic_enabled: bool | None = None
    semantic_threshold: float | None = None
    ttl_seconds: int | None = None
    max_entry_kb: int | None = None
    anthropic_cache_control: bool | None = None


@dataclass(slots=True)
class CacheConfigUpdateRequest:
    """Request body for PUT /api/v1/cache-configs/<id>. Every field is optional."""

    exact_enabled: bool | None = None
    semantic_enabled: bool | None = None
    semantic_threshold: float | None = None
    ttl_seconds: int | None = None
    max_entry_kb: int | None = None
    anthropic_cache_control: bool | None = None


@dataclass(slots=True)
class CacheConfigItem:
    """A single cache_configs row as serialized to callers (see ``_row_to_dict``)."""

    id: int
    scope_type: str
    scope_ref: str | None
    exact_enabled: bool
    semantic_enabled: bool
    semantic_threshold: float
    ttl_seconds: int
    max_entry_kb: int
    anthropic_cache_control: bool
    created_at: str | None
    updated_at: str | None


@dataclass(slots=True)
class PaginationMeta:
    """The pagination window echoed back on a list response."""

    page: int
    limit: int
    total: int | None
    pages: int | None


@dataclass(slots=True)
class CacheConfigEnvelope:
    """Single-row success envelope for get/create/update."""

    status: str
    data: CacheConfigItem


@dataclass(slots=True)
class CacheConfigListEnvelope:
    """List success envelope for GET /api/v1/cache-configs."""

    status: str
    data: list[CacheConfigItem]
    pagination: PaginationMeta


@dataclass(slots=True)
class CacheConfigDeleted:
    """The ``data`` payload of a successful delete."""

    id: int
    deleted: bool


@dataclass(slots=True)
class CacheConfigDeleteEnvelope:
    """Success envelope for DELETE /api/v1/cache-configs/<id>."""

    status: str
    data: CacheConfigDeleted


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


def _provided(data: object) -> dict[str, Any]:
    """Return only the request-body fields the caller actually supplied.

    ``@validate_request`` fills every unset field with ``None``; stripping
    those reproduces the ``"key" in data`` presence semantics the handlers
    and ``_validate_payload`` relied on before the routes carried a schema,
    so a partial update still touches only the columns named in the body.
    """
    return {k: v for k, v in asdict(data).items() if v is not None}  # type: ignore[call-overload]


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


def _has_scope(perm: Permission) -> bool:
    """True when the caller's OIDC ``scope`` claim carries ``perm``.

    Authoritative ``scope`` claim only, never the ``role`` claim (house
    scope-only policy, see ``auth.require_scope``). MUST be called from the
    request context, not a DB worker thread: the read handlers compute the
    admin-capability boolean here and capture it in their thread closures.

    audit-2026-09-14-wave2: replaces the ``role == "admin"`` cross-org
    read/write bypass with the admin-only ``cache_config:admin`` scope.
    Identical for a fresh admin token; an in-flight admin JWT gains it on
    next login (<=1h TTL), API-key admins immediately.
    """
    user = getattr(g, "user", None) or {}
    return perm.value in set(user.get("scope") or [])


def _visible_query(can_admin: bool, user_org_id: int | None) -> Any:
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
    if can_admin:
        return table.id > 0
    query = table.scope_type == "global"
    if user_org_id is not None:
        query |= (table.scope_type == "org") & (table.scope_ref == str(user_org_id))
    return query


def _row_visible_to(row: Any, can_admin: bool, user_org_id: int | None) -> bool:
    """Re-check one row's visibility in Python, immediately before it is serialized.

    Deliberately redundant with `_visible_query`: the SQL filter is the
    primary control, this is the response-side guard that keeps another
    tenant's row from being serialized even if the query is later widened
    or bypassed (which is exactly the regression audit-2026-09-14 found).
    """
    if can_admin:
        return True
    if row.scope_type == "global":
        return True
    return (
        row.scope_type == "org"
        and user_org_id is not None
        and str(row.scope_ref) == str(user_org_id)
    )


def _forbidden(message: str) -> tuple:
    """Build the standard 403 body for a refused cache-config write."""
    return jsonify({"status": "error", "error": message}), 403


def _org_for_key_scope_ref(scope_ref: str | None) -> int | None:
    """Resolve a key-scoped ``scope_ref`` to the organization owning that virtual key.

    ``cache_configs.scope_ref`` stores ``str(virtual_keys.id)`` for
    key-scoped rows, so ownership has to be looked up rather than read off
    the row. Returns ``None`` when the key does not exist, which callers
    must treat as "cannot authorize" -- never as "unconstrained".
    """
    if not scope_ref:
        return None
    database = _db()
    row = database(database.virtual_keys.id == scope_ref).select().first()
    return row.organization_id if row else None


async def _authorize_scope_write(scope_type: str, scope_ref: str | None, verb: str) -> tuple | None:
    """Return a (jsonify, 403) tuple if the caller may not write/delete this scope, else None.

    Admin may write any scope. Otherwise: global rows are admin-only, org
    rows require ownership of that org, and key rows require the virtual
    key to belong to the caller's org. Any other ``scope_type`` is refused.

    regression: audit-2026-09-14 -- this function used to branch only on
    "global" and "org" and then `return None` (allow) for everything else.
    "key" is a valid scope type (see `_validate_payload`), so a caller
    submitting ``scope_type="key"`` with another organization's virtual-key
    id passed authorization untouched and could create, update or delete
    that tenant's response-cache behaviour. The fall-through default is now
    deny: a scope_type this function does not explicitly authorize is
    refused rather than allowed.

    audit-2026-09-14-wave2: the cross-tenant/global bypass now keys on the
    admin-only ``cache_config:admin`` scope, minted for exactly this in
    rbac.py. ``CACHE_CONFIG_WRITE`` (the scope the route already requires)
    could not be reused -- it is held by BOTH admin and resource_manager, so
    keying the bypass on it would let resource_manager write any org's and
    the global config, breaking tenant isolation and the #239 exhaustiveness
    fix. Behaviour is identical for a fresh admin token; an in-flight admin
    JWT gains ``cache_config:admin`` on next login (<=1h TTL), API-key admins
    immediately.
    """
    if _has_scope(Permission.CACHE_CONFIG_ADMIN):
        return None

    if scope_type == "global":
        return _forbidden(f"Only admin may {verb} global cache config")

    if scope_type == "org":
        if scope_ref != str(g.user.get("organization_id")):
            return _forbidden(f"Cannot {verb} another organization's cache config")
        return None

    if scope_type == "key":
        key_org_id = await asyncio.to_thread(_org_for_key_scope_ref, scope_ref)
        # An unresolvable key is refused too: "key not found" must not be
        # the same outcome as "key is mine".
        if key_org_id is None or key_org_id != g.user.get("organization_id"):
            return _forbidden(f"Cannot {verb} another organization's cache config")
        return None

    return _forbidden(f"Cannot {verb} cache config for unrecognized scope type")


@api_v1_bp.route("/cache-configs", methods=["GET"])
@require_auth
@validate_response(CacheConfigListEnvelope, 200)
async def list_cache_configs() -> tuple:
    """List cache configs, optionally filtered by scope_type/scope_ref, paginated."""
    scope_type = request.args.get("scope_type")
    scope_ref = request.args.get("scope_ref")
    can_admin = _has_scope(Permission.CACHE_CONFIG_ADMIN)
    user_org_id = g.user.get("organization_id")
    page = PageRequest.from_request()

    def _fetch():
        query = _visible_query(can_admin, user_org_id)
        if scope_type:
            query &= _db().cache_configs.scope_type == scope_type
        if scope_ref is not None:
            query &= _db().cache_configs.scope_ref == scope_ref
        return _db()(query).select(limitby=page.limitby, orderby=_db().cache_configs.id)

    rows = await asyncio.to_thread(_fetch)
    visible = [r for r in rows if _row_visible_to(r, can_admin, user_org_id)]
    return {
        "status": "success",
        "data": [_row_to_dict(r) for r in visible],
        **page.meta(),
    }, 200


@api_v1_bp.route("/cache-configs/<int:config_id>", methods=["GET"])
@require_auth
@validate_response(CacheConfigEnvelope, 200)
async def get_cache_config(config_id: int) -> tuple:
    """Get a single cache config row by ID."""
    can_admin = _has_scope(Permission.CACHE_CONFIG_ADMIN)
    user_org_id = g.user.get("organization_id")

    def _fetch_one():
        query = _visible_query(can_admin, user_org_id) & (_db().cache_configs.id == config_id)
        return _db()(query).select().first()

    row = await asyncio.to_thread(_fetch_one)
    # A row outside the caller's tenant is reported as absent rather than
    # forbidden, so this route cannot be used to enumerate which config ids
    # exist in other organizations (matches model_access_policies.py).
    if not row or not _row_visible_to(row, can_admin, user_org_id):
        return jsonify({"status": "error", "error": "Cache config not found"}), 404
    return {"status": "success", "data": _row_to_dict(row)}, 200


@api_v1_bp.route("/cache-configs", methods=["POST"])
@require_auth
@require_scope(Permission.CACHE_CONFIG_WRITE)
@validate_response(CacheConfigEnvelope, 201)
@validate_request(CacheConfigCreateRequest)
async def create_cache_config(data: CacheConfigCreateRequest) -> tuple:
    """Create a new cache config row for a scope. 409 if the scope already has one."""
    payload = _provided(data)
    if not payload:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    error = _validate_payload(payload)
    if error:
        return jsonify({"status": "error", "error": error}), 400

    scope_type = payload["scope_type"]
    scope_ref = payload.get("scope_ref")

    auth_error = await _authorize_scope_write(scope_type, scope_ref, verb="write")
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
            exact_enabled=payload.get("exact_enabled", True),
            semantic_enabled=payload.get("semantic_enabled", False),
            semantic_threshold=payload.get("semantic_threshold", 0.95),
            ttl_seconds=payload.get("ttl_seconds", 86400),
            max_entry_kb=payload.get("max_entry_kb", 256),
            anthropic_cache_control=payload.get("anthropic_cache_control", True),
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
    return {"status": "success", "data": _row_to_dict(row)}, 201


@api_v1_bp.route("/cache-configs/<int:config_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.CACHE_CONFIG_WRITE)
@validate_response(CacheConfigEnvelope, 200)
@validate_request(CacheConfigUpdateRequest)
async def update_cache_config(config_id: int, data: CacheConfigUpdateRequest) -> tuple:
    """Update an existing cache config row."""
    payload = _provided(data)
    if not payload:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    existing = await asyncio.to_thread(
        lambda: _db()(_db().cache_configs.id == config_id).select().first()
    )
    if not existing:
        return jsonify({"status": "error", "error": "Cache config not found"}), 404

    error = _validate_payload(payload, partial=True)
    if error:
        return jsonify({"status": "error", "error": error}), 400

    scope_type = existing.scope_type
    scope_ref = existing.scope_ref
    auth_error = await _authorize_scope_write(scope_type, scope_ref, verb="write")
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
    update_fields = {f: payload[f] for f in allowed_fields if f in payload}
    update_fields["updated_at"] = datetime.utcnow()

    def _update():
        _db()(_db().cache_configs.id == config_id).update(**update_fields)
        _db().commit()
        return _db()(_db().cache_configs.id == config_id).select().first()

    row = await asyncio.to_thread(_update)
    await _invalidate_scope(scope_type, scope_ref)
    return {"status": "success", "data": _row_to_dict(row)}, 200


@api_v1_bp.route("/cache-configs/<int:config_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.CACHE_CONFIG_WRITE)
@validate_response(CacheConfigDeleteEnvelope, 200)
async def delete_cache_config(config_id: int) -> tuple:
    """Delete a cache config row (falls back to the next-broader scope)."""
    existing = await asyncio.to_thread(
        lambda: _db()(_db().cache_configs.id == config_id).select().first()
    )
    if not existing:
        return jsonify({"status": "error", "error": "Cache config not found"}), 404

    scope_type = existing.scope_type
    scope_ref = existing.scope_ref
    auth_error = await _authorize_scope_write(scope_type, scope_ref, verb="delete")
    if auth_error:
        return auth_error

    def _delete():
        _db()(_db().cache_configs.id == config_id).delete()
        _db().commit()

    await asyncio.to_thread(_delete)
    await _invalidate_scope(scope_type, scope_ref)
    return {"status": "success", "data": {"id": config_id, "deleted": True}}, 200
