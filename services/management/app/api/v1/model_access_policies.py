"""WaddleAI Management API v1 - Model Access Policy Endpoints (design spec §8).

CRUD for ``model_access_policies``: per-tenant block rules that keep a
client-supplied model pattern (exact id or glob, e.g. ``claude-opus-5*``)
from being routed to for a given org, or a narrower user/key scope inside
it. Admin surface for ``shared.security.model_access.ModelAccessPolicyResolver``.

Enterprise-tier, two-layer gated -- the ``waddleai.model_access_policy``
PostHog flag AND a ``model_access_policy`` license entitlement, mirroring
``fleet.py``'s ``hybrid_targets`` gate (spec §8): flag off hides the entire
surface (404, matching ``fleet.py``'s flag-off shape); flag on but
unentitled refuses with a tier-named 403.

Every row is a block/deny rule (no allow-carve-out mode in this build --
see ``shared/security/model_access.py`` module docstring). Proxy-side
enforcement (wiring the resolver into ``RoutingStage``/`` /v1/models``
filtering) is a separate follow-up branch; this module only owns the CRUD
surface.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any

from quart import Blueprint, g, jsonify, request

from shared.auth.rbac import Permission
from shared.utils.feature_flags import is_feature_enabled

from ...extensions import db
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)

model_access_policies_bp = Blueprint(
    "model_access_policies", __name__, url_prefix="/api/v1/routing/access-policies"
)

MODEL_ACCESS_POLICY_FLAG = "waddleai.model_access_policy"
_MODEL_ACCESS_POLICY_FEATURE = "model_access_policy"

_VALID_SCOPE_TYPES = frozenset({"global", "org", "user", "key"})
_VALID_ACTIONS = frozenset({"reject", "reroute"})

_UPDATABLE_FIELDS = ("model_pattern", "action", "fallback_model", "reason", "enabled")

_license_client: Any = None


def _get_license_client() -> Any:
    """Lazily construct the shared ``penguin_licensing.LicenseClient``.

    ``product`` must be ``"waddleai"`` -- the SDK's own default is
    ``"elder"`` and would silently check entitlements for the wrong
    product.
    """
    global _license_client
    if _license_client is None:
        from penguin_licensing import LicenseClient

        _license_client = LicenseClient(
            license_key=os.environ.get("LICENSE_KEY", ""),
            product="waddleai",
            base_url=os.environ.get("LICENSE_SERVER_URL", "https://license.penguintech.io"),
        )
    return _license_client


def _flag_enabled(org_id: int | None) -> bool:
    """Evaluate the ``waddleai.model_access_policy`` PostHog flag for this org."""
    return is_feature_enabled(MODEL_ACCESS_POLICY_FLAG, distinct_id=str(org_id or "server"))


async def _entitled() -> bool:
    """Two-layer gate's license-entitlement half -- fail-closed on any I/O error."""

    def _check() -> bool:
        try:
            return bool(_get_license_client().check_feature(_MODEL_ACCESS_POLICY_FEATURE))
        except Exception as exc:  # pragma: no cover - defensive, license I/O failure
            logger.warning("model_access_policies: entitlement check failed: %s", exc)
            return False

    return await asyncio.to_thread(_check)


async def _gate(org_id: int | None) -> tuple | None:
    """Return a `(jsonify, status)` tuple if the caller may not use this surface, else None."""
    if not _flag_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404
    if not await _entitled():
        return (
            jsonify(
                {
                    "status": "error",
                    "error": (
                        "Model access policies require an Enterprise license entitlement "
                        "(model_access_policy)"
                    ),
                }
            ),
            403,
        )
    return None


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a penguin-dal ``model_access_policies`` row into a serializable dict."""
    return {
        "id": row.id,
        "scope_type": row.scope_type,
        "scope_ref": row.scope_ref,
        "model_pattern": row.model_pattern,
        "action": row.action,
        "fallback_model": row.fallback_model,
        "reason": row.reason,
        "enabled": row.enabled,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _validate_scope(scope_type: Any, scope_ref: Any) -> str | None:
    """Validate the (immutable, create-time-only) scope columns."""
    if scope_type not in _VALID_SCOPE_TYPES:
        return f"scope_type must be one of {sorted(_VALID_SCOPE_TYPES)}"
    if scope_type == "global" and scope_ref is not None:
        return "scope_ref must be null for scope_type='global'"
    if scope_type != "global" and not scope_ref:
        return "scope_ref is required for scope_type='org'/'user'/'key'"
    return None


def _validate_rule(model_pattern: Any, action: Any, fallback_model: Any) -> str | None:
    """Validate the rule fields shared by create and update."""
    if not model_pattern or not isinstance(model_pattern, str) or not model_pattern.strip():
        return "model_pattern is required and must be non-empty"
    if action not in _VALID_ACTIONS:
        return f"action must be one of {sorted(_VALID_ACTIONS)}"
    if action == "reroute" and not fallback_model:
        return "fallback_model is required when action='reroute'"
    return None


def _visible_query(user_role: str, user_org_id: int | None, user_id: int | None) -> Any:
    """Admin sees every row; everyone else sees global + their own org/user scoped rows.

    Key-scoped rows are visible to admin only in list/get -- resolving
    ``virtual_keys`` ownership for the *visibility query* (as opposed to a
    single write's tenant check, see `_target_org_for_scope`) would need a
    join penguin-dal's query builder doesn't do here. The write path below
    still enforces true tenant isolation for key-scoped rows via an
    explicit per-row lookup.
    """
    table = db.model_access_policies
    if user_role == "admin":
        return table.id > 0
    query = table.scope_type == "global"
    query |= (table.scope_type == "org") & (table.scope_ref == str(user_org_id))
    if user_id is not None:
        query |= (table.scope_type == "user") & (table.scope_ref == str(user_id))
    return query


def _target_org_for_scope(scope_type: str, scope_ref: str) -> int | None:
    """Resolve the owning org_id for a scope_ref, for write-time tenant isolation."""
    if scope_type == "org":
        try:
            return int(scope_ref)
        except (TypeError, ValueError):
            return None
    if scope_type == "user":
        row = db(db.users.id == scope_ref).select().first()
        return row.organization_id if row else None
    if scope_type == "key":
        row = db(db.virtual_keys.id == scope_ref).select().first()
        return row.organization_id if row else None
    return None  # scope_type == "global"


def _can_write(
    user_role: str, user_org_id: int | None, scope_type: str, scope_ref: str | None
) -> bool:
    """Admin may write any row; resource_manager only rows whose resolved org is their own."""
    if user_role == "admin":
        return True
    if user_role != "resource_manager" or scope_type == "global":
        return False
    target_org = _target_org_for_scope(scope_type, scope_ref)
    return target_org is not None and target_org == user_org_id


@model_access_policies_bp.route("/", methods=["GET"])
@require_auth
async def list_access_policies() -> tuple:
    """List visible model_access_policies rows, optionally filtered by scope_type."""
    org_id = g.user.get("organization_id")
    gate_error = await _gate(org_id)
    if gate_error:
        return gate_error

    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    scope_type = request.args.get("scope_type")

    def _fetch():
        query = _visible_query(user_role, org_id, user_id)
        if scope_type:
            query &= db.model_access_policies.scope_type == scope_type
        return db(query).select(orderby=db.model_access_policies.id)

    rows = await asyncio.to_thread(_fetch)
    entries = [_row_to_dict(r) for r in rows]

    return (
        jsonify(
            {
                "status": "success",
                "data": entries,
                "meta": {"total": len(entries), "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


@model_access_policies_bp.route("/<int:policy_id>", methods=["GET"])
@require_auth
async def get_access_policy(policy_id: int) -> tuple:
    """Get a single model_access_policies row by ID (org-visibility scoped)."""
    org_id = g.user.get("organization_id")
    gate_error = await _gate(org_id)
    if gate_error:
        return gate_error

    user_role = g.user.get("role")
    user_id = g.user.get("user_id")

    def _fetch_one():
        query = _visible_query(user_role, org_id, user_id) & (
            db.model_access_policies.id == policy_id
        )
        return db(query).select().first()

    row = await asyncio.to_thread(_fetch_one)
    if not row:
        return jsonify({"status": "error", "error": "Policy not found"}), 404

    return (
        jsonify(
            {
                "status": "success",
                "data": _row_to_dict(row),
                "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


@model_access_policies_bp.route("/", methods=["POST"])
@require_auth
@require_scope(Permission.MODEL_ACCESS_POLICY_WRITE)
async def create_access_policy() -> tuple:
    """Create a model_access_policies row."""
    org_id = g.user.get("organization_id")
    gate_error = await _gate(org_id)
    if gate_error:
        return gate_error

    data: dict[str, Any] | None = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    scope_type = data.get("scope_type")
    scope_ref = data.get("scope_ref")
    error = _validate_scope(scope_type, scope_ref)
    if error:
        return jsonify({"status": "error", "error": error}), 400

    action = data.get("action", "reject")
    error = _validate_rule(data.get("model_pattern"), action, data.get("fallback_model"))
    if error:
        return jsonify({"status": "error", "error": error}), 400

    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    user_id = g.user.get("user_id")

    def _create():
        if not _can_write(user_role, user_org_id, scope_type, scope_ref):
            return "forbidden", None

        new_id = db.model_access_policies.insert(
            scope_type=scope_type,
            scope_ref=scope_ref,
            model_pattern=data["model_pattern"],
            action=action,
            fallback_model=data.get("fallback_model"),
            reason=data.get("reason"),
            enabled=data.get("enabled", True),
            created_by=user_id,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.commit()
        return "created", db(db.model_access_policies.id == new_id).select().first()

    outcome, row = await asyncio.to_thread(_create)

    if outcome == "forbidden":
        return jsonify({"status": "error", "error": "Access denied for this scope"}), 403

    return (
        jsonify(
            {
                "status": "success",
                "data": _row_to_dict(row),
                "meta": {"action": "created", "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        201,
    )


@model_access_policies_bp.route("/<int:policy_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.MODEL_ACCESS_POLICY_WRITE)
async def update_access_policy(policy_id: int) -> tuple:
    """Update a model_access_policies row. scope_type/scope_ref are immutable after creation."""
    org_id = g.user.get("organization_id")
    gate_error = await _gate(org_id)
    if gate_error:
        return gate_error

    data: dict[str, Any] | None = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    update_fields: dict[str, Any] = {f: data[f] for f in _UPDATABLE_FIELDS if f in data}
    if not update_fields:
        return jsonify({"status": "error", "error": "No valid fields to update"}), 400

    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    def _update():
        existing = db(db.model_access_policies.id == policy_id).select().first()
        if not existing:
            return "not_found", None
        if not _can_write(user_role, user_org_id, existing.scope_type, existing.scope_ref):
            return "forbidden", None

        merged_pattern = update_fields.get("model_pattern", existing.model_pattern)
        merged_action = update_fields.get("action", existing.action)
        merged_fallback = update_fields.get("fallback_model", existing.fallback_model)
        error = _validate_rule(merged_pattern, merged_action, merged_fallback)
        if error:
            return "invalid", error

        db(db.model_access_policies.id == policy_id).update(
            **update_fields, updated_at=datetime.utcnow()
        )
        db.commit()
        return "ok", db(db.model_access_policies.id == policy_id).select().first()

    outcome, result = await asyncio.to_thread(_update)

    if outcome == "not_found":
        return jsonify({"status": "error", "error": "Policy not found"}), 404
    if outcome == "forbidden":
        return jsonify({"status": "error", "error": "Access denied"}), 403
    if outcome == "invalid":
        return jsonify({"status": "error", "error": result}), 400

    return (
        jsonify(
            {
                "status": "success",
                "data": _row_to_dict(result),
                "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


@model_access_policies_bp.route("/<int:policy_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.MODEL_ACCESS_POLICY_DELETE)
async def delete_access_policy(policy_id: int) -> tuple:
    """Delete a model_access_policies row by ID (admin only)."""
    org_id = g.user.get("organization_id")
    gate_error = await _gate(org_id)
    if gate_error:
        return gate_error

    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    def _delete():
        row = db(db.model_access_policies.id == policy_id).select().first()
        if not row:
            return "not_found"
        if not _can_write(user_role, user_org_id, row.scope_type, row.scope_ref):
            return "forbidden"
        db(db.model_access_policies.id == policy_id).delete()
        db.commit()
        return "ok"

    outcome = await asyncio.to_thread(_delete)
    if outcome == "not_found":
        return jsonify({"status": "error", "error": "Policy not found"}), 404
    if outcome == "forbidden":
        return jsonify({"status": "error", "error": "Access denied"}), 403

    return (
        jsonify(
            {
                "status": "success",
                "data": {"id": policy_id},
                "meta": {"action": "deleted", "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )
