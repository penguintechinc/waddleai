"""WaddleAI Management API v1 -- Proxy API-key revoke/disable.

Manages the ``api_keys`` table that ``RBACManager.authenticate_api_key``
(``shared/auth/rbac.py``) verifies on the proxy's data-plane auth hot path --
a distinct table from the legacy ``virtual_keys`` CRUD in ``keys.py``
(different credential format: ``wa-{key_id}-{secret}`` here vs. a bare
``wa-{secret}`` there).

Exists so disabling/deleting a key here can invalidate the proxy's
Valkey-backed auth-lookup cache (``proxy/apps/proxy_server/auth_cache.py``,
release-audit-2026-10-02 O7-a/O11) instead of leaving a revoked key usable
for up to its cache TTL (``PROXY_AUTH_CACHE_TTL_SECONDS``, default 60s) --
that TTL is the documented worst-case staleness bound when the Valkey
invalidation below cannot itself reach Valkey (best-effort, logged, never
blocks or rolls back the DB write).
"""

import asyncio
import logging
from dataclasses import dataclass

from quart import g, jsonify
from quart_schema import security_scheme, tag, validate_response

from shared.auth.rbac import Permission, auth_cache_key

from ... import extensions as _ext
from ...extensions import db
from . import api_v1_bp
from .auth import require_auth

logger = logging.getLogger(__name__)

_BEARER_AUTH: list[dict[str, list[str]]] = [{"bearerAuth": []}]


@dataclass(slots=True)
class MessageResponse:
    """Generic ``{"message": str}`` envelope, matching keys.py's own."""

    message: str


def _has_scope(perm: Permission) -> bool:
    """True when the caller's OIDC ``scope`` claim carries ``perm`` (scope-only authz policy)."""
    user = getattr(g, "user", None) or {}
    return perm.value in set(user.get("scope") or [])


async def _invalidate_auth_cache(key_id: str) -> None:
    """Best-effort Valkey delete of the proxy's cached lookup for ``key_id``.

    Reads ``extensions.redis_client`` at call time (not via a module-level
    ``from ... import redis_client``, which would bind the pre-``init_cache()``
    ``None`` forever) so this works once the app has actually started. Never
    raises: a missing/unreachable cache just means the proxy's own TTL is the
    staleness bound instead of an immediate cutover.
    """
    redis_client = getattr(_ext, "redis_client", None)
    if redis_client is None:
        logger.warning(
            "proxy_keys: no cache client configured; auth-cache invalidation for "
            "key_id=%s skipped (bounded by the proxy's cache TTL instead)",
            key_id,
        )
        return
    try:
        await asyncio.to_thread(redis_client.delete, auth_cache_key(key_id))
    except Exception as exc:  # pragma: no cover - Valkey unavailability must not fail the write
        logger.warning("proxy_keys: failed to invalidate auth cache for key_id=%s: %s", key_id, exc)


@api_v1_bp.route("/proxy-keys/<string:key_id>", methods=["DELETE"])
@tag(["Keys"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
async def revoke_proxy_api_key(key_id: str):
    """Disable the proxy ``api_keys`` row identified by its non-secret ``key_id``.

    Soft-delete (``enabled=False``), mirroring ``keys.py``'s own revoke
    semantics for the legacy virtual-key table, then invalidates the
    proxy's auth-lookup cache so the key stops working well before its
    cache TTL would otherwise expire.
    """
    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    def _fetch():
        return db(db.api_keys.key_id == key_id).select().first()

    key = await asyncio.to_thread(_fetch)
    if not key:
        return jsonify({"error": "Key not found"}), 404

    # Permission check -- scope-only cross-org/cross-user bypass, per the
    # house scope-only authz policy (never a role-name check). Mirrors
    # keys.py's delete_key: holding apikey:delete lets a caller revoke any
    # key; everyone else falls through to the org/owner check below.
    if not _has_scope(Permission.APIKEY_DELETE):
        if user_role == "resource_manager" and key.organization_id != org_id:
            return jsonify({"error": "Access denied"}), 403
        if user_role != "resource_manager" and key.user_id != user_id:
            return jsonify({"error": "Access denied"}), 403

    def _disable():
        db(db.api_keys.id == key.id).update(enabled=False)
        db.commit()

    await asyncio.to_thread(_disable)
    await _invalidate_auth_cache(key_id)

    return {"message": "API key revoked successfully"}
