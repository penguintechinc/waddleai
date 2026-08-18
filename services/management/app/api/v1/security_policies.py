"""WaddleAI Management API v1 - Security Policies (§8.9).

CRUD for the security_policies table (§8.1 global->org->model->tool scope
chain) plus a resolution-preview endpoint. Every write invalidates the
PolicyResolver's Valkey cache -- resolved policies must never serve a stale
value after an admin changes a scope's config.

Also hosts §8.6 bypass-grant management (create/list/revoke) for
`security_bypass_grants` -- org-scoped (resource_manager limited to their
own org's subjects, matching the organizations.py precedent; admin is
platform-wide), every mutation audit-logged, every grant requires an
`expires_at` (no indefinite bypass via this API).

WebUI toggle matrix / bypass-grant view are tracked separately (React
scope, out of scope for this pass).
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from quart import Blueprint, g, jsonify, request

from ... import extensions as _ext
from ...extensions import db
from .auth import require_auth, require_role

logger = logging.getLogger(__name__)

security_policies_bp = Blueprint(
    "security_policies", __name__, url_prefix="/api/v1/security-policies"
)

_SCOPE_TYPES = ("global", "org", "model", "tool")
_DIRECTIONS = ("input", "output", "both")
_CONFIGURABLE_FIELDS = (
    "tier1_enabled",
    "tier2_enabled",
    "tier3_enabled",
    "tier4_enabled",
    "tier4_model",
    "intent_classifier_enabled",
    "intent_categories",
    "block_action",
    "fail_mode",
    "on_unclassifiable",
    "auditor_timeout_ms",
    "latency_budget_ms",
    "sample_rate",
    "upstream_filters",
)


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a penguin-dal security_policies row to an explicit response schema.

    Scoped to exactly the columns callers need -- never `**row.as_dict()` /
    raw model serialization (house rule: every response goes through an
    explicit schema, not a raw ORM object).
    """
    data: dict[str, Any] = {
        "id": row.id,
        "scope_type": row.scope_type,
        "scope_ref": row.scope_ref,
        "direction": row.direction,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    for field_name in _CONFIGURABLE_FIELDS:
        data[field_name] = getattr(row, field_name, None)
    return data


def _get_resolver() -> Any:
    """Build a PolicyResolver against the current db/Valkey connections."""
    from shared.security.policy_resolver import create_policy_resolver

    return create_policy_resolver(db, getattr(_ext, "redis_client", None))


@security_policies_bp.route("/", methods=["GET"])
@require_auth
async def list_policies() -> tuple:
    """List security policies, optionally filtered by scope_type/scope_ref."""
    scope_type: str | None = request.args.get("scope_type")
    scope_ref: str | None = request.args.get("scope_ref")

    def _fetch():
        query = db.security_policies.id > 0
        if scope_type:
            query &= db.security_policies.scope_type == scope_type
        if scope_ref is not None:
            query &= db.security_policies.scope_ref == scope_ref
        return db(query).select(orderby=db.security_policies.id)

    rows = await asyncio.to_thread(_fetch)
    policies: list[dict[str, Any]] = [_row_to_dict(r) for r in rows]

    return (
        jsonify(
            {
                "status": "success",
                "data": policies,
                "meta": {"total": len(policies), "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


@security_policies_bp.route("/resolve", methods=["GET"])
@require_auth
async def resolve_policy() -> tuple:
    """Resolution-preview: what policy applies to org X + model Y + tool Z."""
    org_id: str | None = request.args.get("org")
    model: str | None = request.args.get("model")
    tool: str | None = request.args.get("tool")
    direction: str = request.args.get("direction", "both")

    if direction not in _DIRECTIONS:
        return jsonify({"status": "error", "error": f"direction must be one of {_DIRECTIONS}"}), 400

    resolver = _get_resolver()
    resolved = await resolver.resolve(org_id, model, tool, direction=direction)

    return (
        jsonify(
            {
                "status": "success",
                "data": {
                    "tier1_enabled": resolved.tier1_enabled,
                    "tier2_enabled": resolved.tier2_enabled,
                    "tier3_enabled": resolved.tier3_enabled,
                    "tier4_enabled": resolved.tier4_enabled,
                    "tier4_model": resolved.tier4_model,
                    "intent_classifier_enabled": resolved.intent_classifier_enabled,
                    "intent_categories": list(resolved.intent_categories),
                    "block_action": resolved.block_action,
                    "fail_mode": resolved.fail_mode,
                    "on_unclassifiable": resolved.on_unclassifiable,
                    "auditor_timeout_ms": resolved.auditor_timeout_ms,
                    "latency_budget_ms": resolved.latency_budget_ms,
                    "sample_rate": resolved.sample_rate,
                    "upstream_filters": resolved.upstream_filters,
                },
                "meta": {
                    "org": org_id,
                    "model": model,
                    "tool": tool,
                    "direction": direction,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                },
            }
        ),
        200,
    )


@security_policies_bp.route("/", methods=["POST"])
@require_auth
@require_role("admin")
async def create_or_upsert_policy() -> tuple:
    """Create or upsert a security policy by (scope_type, scope_ref, direction)."""
    data: dict[str, Any] | None = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    scope_type: str = data.get("scope_type", "")
    scope_ref: str | None = data.get("scope_ref")
    direction: str = data.get("direction", "both")

    if scope_type not in _SCOPE_TYPES:
        error = f"scope_type must be one of {_SCOPE_TYPES}"
        return jsonify({"status": "error", "error": error}), 400
    if scope_type == "global" and scope_ref is not None:
        return jsonify({"status": "error", "error": "global scope must not set scope_ref"}), 400
    if scope_type != "global" and not scope_ref:
        error = "scope_ref is required for non-global scope"
        return jsonify({"status": "error", "error": error}), 400
    if direction not in _DIRECTIONS:
        return jsonify({"status": "error", "error": f"direction must be one of {_DIRECTIONS}"}), 400

    update_fields: dict[str, Any] = {k: data[k] for k in _CONFIGURABLE_FIELDS if k in data}

    def _upsert():
        existing = (
            db(
                (db.security_policies.scope_type == scope_type)
                & (db.security_policies.scope_ref == scope_ref)
                & (db.security_policies.direction == direction)
            )
            .select()
            .first()
        )
        if existing:
            db(db.security_policies.id == existing.id).update(
                updated_at=datetime.utcnow(), **update_fields
            )
            db.commit()
            row = db(db.security_policies.id == existing.id).select().first()
            return "updated", row

        new_id: int = db.security_policies.insert(
            scope_type=scope_type,
            scope_ref=scope_ref,
            direction=direction,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            **update_fields,
        )
        db.commit()
        row = db(db.security_policies.id == new_id).select().first()
        return "created", row

    action, row = await asyncio.to_thread(_upsert)

    resolver = _get_resolver()
    await resolver.invalidate(scope_type, scope_ref)

    return (
        jsonify(
            {
                "status": "success",
                "data": _row_to_dict(row),
                "meta": {"action": action, "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200 if action == "updated" else 201,
    )


@security_policies_bp.route("/<int:policy_id>", methods=["PUT"])
@require_auth
@require_role("admin")
async def update_policy(policy_id: int) -> tuple:
    """Update selected fields of an existing security policy."""
    data: dict[str, Any] | None = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    update_fields: dict[str, Any] = {k: data[k] for k in _CONFIGURABLE_FIELDS if k in data}
    if not update_fields:
        return jsonify({"status": "error", "error": "No valid fields to update"}), 400

    def _update():
        row = db(db.security_policies.id == policy_id).select().first()
        if not row:
            return None, None
        db(db.security_policies.id == policy_id).update(
            updated_at=datetime.utcnow(), **update_fields
        )
        db.commit()
        updated_row = db(db.security_policies.id == policy_id).select().first()
        return updated_row.scope_type, updated_row

    scope_type, row = await asyncio.to_thread(_update)
    if row is None:
        return jsonify({"status": "error", "error": "Security policy not found"}), 404

    resolver = _get_resolver()
    await resolver.invalidate(scope_type, row.scope_ref)

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


@security_policies_bp.route("/<int:policy_id>", methods=["DELETE"])
@require_auth
@require_role("admin")
async def delete_policy(policy_id: int) -> tuple:
    """Delete a security policy by ID."""

    def _delete():
        row = db(db.security_policies.id == policy_id).select().first()
        if not row:
            return None
        scope = (row.scope_type, row.scope_ref)
        db(db.security_policies.id == policy_id).delete()
        db.commit()
        return scope

    scope = await asyncio.to_thread(_delete)
    if scope is None:
        return jsonify({"status": "error", "error": "Security policy not found"}), 404

    resolver = _get_resolver()
    await resolver.invalidate(scope[0], scope[1])

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


# ---------------------------------------------------------------------------
# Bypass grants (§8.6) -- create/list/revoke security_bypass_grants
# ---------------------------------------------------------------------------

_SUBJECT_TYPES = ("user", "vkey")
_MODES = ("shadow", "skip")


def _grant_to_dict(row: Any) -> dict[str, Any]:
    """Explicit response schema for a security_bypass_grants row."""
    return {
        "id": row.id,
        "subject_type": row.subject_type,
        "subject_ref": row.subject_ref,
        "mode": row.mode,
        "scope_narrow": row.scope_narrow,
        "include_upstream": row.include_upstream,
        "granted_by": row.granted_by,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _resolve_subject_org(subject_type: str, subject_ref: str) -> int | None:
    """Look up the organization a bypass-grant subject belongs to.

    security_bypass_grants carries no organization_id column of its own
    (see migration 011) -- org scoping is enforced here by joining out to
    the subject's own record instead.
    """
    if subject_type == "user":
        row = db(db.users.id == subject_ref).select().first()
    else:
        row = db(db.virtual_keys.id == subject_ref).select().first()
    return row.organization_id if row else None


def _org_scope_allowed(user_role: str | None, user_org_id: Any, subject_org_id: Any) -> bool:
    """Admin is platform-wide; every other role is limited to its own org's subjects."""
    if user_role == "admin":
        return True
    return subject_org_id is not None and str(subject_org_id) == str(user_org_id)


@security_policies_bp.route("/bypass-grants", methods=["GET"])
@require_auth
@require_role("admin", "resource_manager")
async def list_bypass_grants() -> tuple:
    """List bypass grants, org-scoped for non-admin roles."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    subject_type: str | None = request.args.get("subject_type")

    def _fetch():
        query = db.security_bypass_grants.id > 0
        if subject_type:
            query &= db.security_bypass_grants.subject_type == subject_type
        rows = db(query).select(orderby=db.security_bypass_grants.id)
        if user_role == "admin":
            return rows
        return [
            r
            for r in rows
            if _org_scope_allowed(
                user_role, user_org_id, _resolve_subject_org(r.subject_type, r.subject_ref)
            )
        ]

    rows = await asyncio.to_thread(_fetch)
    grants: list[dict[str, Any]] = [_grant_to_dict(r) for r in rows]

    return (
        jsonify(
            {
                "status": "success",
                "data": grants,
                "meta": {"total": len(grants), "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


@security_policies_bp.route("/bypass-grants", methods=["POST"])
@require_auth
@require_role("admin", "resource_manager")
async def create_bypass_grant() -> tuple:
    """Create a bypass grant. Requires an explicit expires_at -- no indefinite bypass."""
    data: dict[str, Any] | None = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    subject_type: str = data.get("subject_type", "")
    subject_ref: str = str(data.get("subject_ref", ""))
    mode: str = data.get("mode", "shadow")
    expires_at_raw: str | None = data.get("expires_at")

    if subject_type not in _SUBJECT_TYPES:
        error = f"subject_type must be one of {_SUBJECT_TYPES}"
        return jsonify({"status": "error", "error": error}), 400
    if not subject_ref:
        return jsonify({"status": "error", "error": "subject_ref is required"}), 400
    if mode not in _MODES:
        return jsonify({"status": "error", "error": f"mode must be one of {_MODES}"}), 400
    if not expires_at_raw:
        error = "expires_at is required (no indefinite grants)"
        return jsonify({"status": "error", "error": error}), 400
    try:
        expires_at = datetime.fromisoformat(expires_at_raw)
    except ValueError:
        return jsonify({"status": "error", "error": "expires_at must be ISO-8601"}), 400
    if expires_at <= datetime.utcnow():
        return jsonify({"status": "error", "error": "expires_at must be in the future"}), 400

    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    granted_by = g.user.get("user_id") or g.user.get("id")

    def _create():
        subject_org_id = _resolve_subject_org(subject_type, subject_ref)
        if not _org_scope_allowed(user_role, user_org_id, subject_org_id):
            return "forbidden", None

        new_id: int = db.security_bypass_grants.insert(
            subject_type=subject_type,
            subject_ref=subject_ref,
            mode=mode,
            scope_narrow=data.get("scope_narrow"),
            include_upstream=bool(data.get("include_upstream", False)),
            granted_by=granted_by,
            expires_at=expires_at,
            created_at=datetime.utcnow(),
        )
        db.commit()
        row = db(db.security_bypass_grants.id == new_id).select().first()
        return "created", row

    result, row = await asyncio.to_thread(_create)
    if result == "forbidden":
        return jsonify({"status": "error", "error": "subject is outside your organization"}), 403

    logger.warning(
        "BypassGrant created: id=%s subject=%s:%s mode=%s granted_by=%s expires_at=%s",
        row.id,
        subject_type,
        subject_ref,
        mode,
        granted_by,
        expires_at.isoformat(),
    )

    return (
        jsonify(
            {
                "status": "success",
                "data": _grant_to_dict(row),
                "meta": {"action": "created", "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        201,
    )


@security_policies_bp.route("/bypass-grants/<int:grant_id>", methods=["DELETE"])
@require_auth
@require_role("admin", "resource_manager")
async def revoke_bypass_grant(grant_id: int) -> tuple:
    """Revoke (delete) a bypass grant, org-scoped for non-admin roles."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    revoked_by = g.user.get("user_id") or g.user.get("id")

    def _revoke():
        row = db(db.security_bypass_grants.id == grant_id).select().first()
        if not row:
            return "not_found", None
        subject_org_id = _resolve_subject_org(row.subject_type, row.subject_ref)
        if not _org_scope_allowed(user_role, user_org_id, subject_org_id):
            return "forbidden", None
        subject = (row.subject_type, row.subject_ref)
        db(db.security_bypass_grants.id == grant_id).delete()
        db.commit()
        return "revoked", subject

    result, subject = await asyncio.to_thread(_revoke)
    if result == "not_found":
        return jsonify({"status": "error", "error": "Bypass grant not found"}), 404
    if result == "forbidden":
        return jsonify({"status": "error", "error": "subject is outside your organization"}), 403

    logger.warning(
        "BypassGrant revoked: id=%s subject=%s:%s revoked_by=%s",
        grant_id,
        subject[0],
        subject[1],
        revoked_by,
    )

    return (
        jsonify(
            {
                "status": "success",
                "data": {"id": grant_id},
                "meta": {"action": "revoked", "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )
