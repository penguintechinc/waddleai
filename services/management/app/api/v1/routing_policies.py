"""WaddleAI Management API v1 - Routing Policy Endpoints (spec §7.1, §7.3).

CRUD for ``routing_policies``: one row per organization (mode, escalation
threshold/target, ``classifier_prompt``, de-escalation, sensitivity routing,
budget-pressure toggle, provider failover). Admin surface for
``shared.routing.PolicyResolver``.

``classifier_prompt`` absorbs the legacy Valkey ``routing:instructions``
natural-language routing UX (spec §7.6) -- there is no separate
"instructions" endpoint anymore; set it here.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from penguin_dal.db import DB
from quart import Blueprint, g, jsonify
from quart_schema import validate_request, validate_response

from shared.auth.rbac import Permission

from ...extensions import db, redis_client
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)

routing_policies_bp = Blueprint("routing_policies", __name__, url_prefix="/api/v1/routing/policies")


# ---------------------------------------------------------------------------
# OpenAPI request/response models (audit-2026-09-14). Request fields Optional
# so the handler's own enum validation stays authoritative; response models
# mirror EXACTLY the keys each handler returns.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UpsertPolicyRequest:
    """Request body for PUT /api/v1/routing/policies/<org>. Every field is a partial update."""

    mode: str | None = None
    escalation_threshold: int | None = None
    escalation_target: str | None = None
    classifier_prompt: str | None = None
    de_escalation: str | None = None
    idle_reset_minutes: int | None = None
    sensitivity_routing: str | None = None
    budget_pressure_enabled: bool | None = None
    provider_failover: str | None = None


@dataclass(slots=True)
class PolicyRow:
    """A routing_policies row (or engine defaults) -- mirrors ``_row_to_dict`` exactly.

    ``id`` is nullable because the get-defaults path returns a synthetic row
    with no persisted id yet.
    """

    id: int | None
    organization_id: int
    mode: str
    escalation_threshold: Any
    escalation_target: Any
    classifier_prompt: str | None
    de_escalation: str
    idle_reset_minutes: Any
    sensitivity_routing: str
    budget_pressure_enabled: Any
    provider_failover: str
    created_at: str | None
    updated_at: str | None


@dataclass(slots=True)
class PolicyGetMeta:
    """``meta`` for the get response -- whether the row was defaulted, plus timestamp."""

    defaulted: bool
    timestamp: str


@dataclass(slots=True)
class PolicyActionMeta:
    """``meta`` for the upsert response -- action verb plus timestamp."""

    action: str
    timestamp: str


@dataclass(slots=True)
class PolicyDeleteMeta:
    """``meta`` for the delete response -- action verb plus timestamp."""

    action: str
    timestamp: str


@dataclass(slots=True)
class PolicyDeletedRef:
    """``data`` for a delete response -- the org whose policy was reset."""

    organization_id: int


@dataclass(slots=True)
class PolicyGetResponse:
    """Response body for GET /api/v1/routing/policies/<org>."""

    status: str
    data: PolicyRow
    meta: PolicyGetMeta


@dataclass(slots=True)
class PolicyWriteResponse:
    """Response body for a successful PUT (create/update)."""

    status: str
    data: PolicyRow
    meta: PolicyActionMeta


@dataclass(slots=True)
class PolicyDeleteResponse:
    """Response body for a successful DELETE."""

    status: str
    data: PolicyDeletedRef
    meta: PolicyDeleteMeta


_VALID_MODES = frozenset({"local_only", "local_first", "commercial_only", "cost", "latency"})
_VALID_DE_ESCALATION = frozenset({"never", "idle_reset"})  # "task_detect" deferred, spec §7.3/§14.1
_VALID_SENSITIVITY = frozenset({"local_only", "redact_then_any", "ignore"})
_VALID_PROVIDER_FAILOVER = frozenset({"off", "same_class"})

_WRITABLE_FIELDS = (
    "mode",
    "escalation_threshold",
    "escalation_target",
    "classifier_prompt",
    "de_escalation",
    "idle_reset_minutes",
    "sensitivity_routing",
    "budget_pressure_enabled",
    "provider_failover",
)


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a penguin-dal routing_policies row into a serializable dict."""
    return {
        "id": row.id,
        "organization_id": row.organization_id,
        "mode": row.mode,
        "escalation_threshold": row.escalation_threshold,
        "escalation_target": row.escalation_target,
        "classifier_prompt": row.classifier_prompt,
        "de_escalation": row.de_escalation,
        "idle_reset_minutes": row.idle_reset_minutes,
        "sensitivity_routing": row.sensitivity_routing,
        "budget_pressure_enabled": row.budget_pressure_enabled,
        "provider_failover": row.provider_failover,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _validate_fields(data: dict[str, Any]) -> str | None:
    """Return an error message for the first invalid enum field, else None."""
    if "mode" in data and data["mode"] not in _VALID_MODES:
        return f"mode must be one of {sorted(_VALID_MODES)}"
    if "de_escalation" in data and data["de_escalation"] not in _VALID_DE_ESCALATION:
        return (
            "de_escalation must be 'never' or 'idle_reset' "
            "('task_detect' is deferred, spec §7.3/§14.1)"
        )
    if "sensitivity_routing" in data and data["sensitivity_routing"] not in _VALID_SENSITIVITY:
        return f"sensitivity_routing must be one of {sorted(_VALID_SENSITIVITY)}"
    if "provider_failover" in data and data["provider_failover"] not in _VALID_PROVIDER_FAILOVER:
        return f"provider_failover must be one of {sorted(_VALID_PROVIDER_FAILOVER)}"
    return None


def _db() -> DB:
    """Return the process-wide penguin-dal handle, narrowed away from ``None``.

    ``extensions.db`` is declared ``DB | None`` because it starts unset
    before ``init_db()`` runs at startup; every route below only executes
    after that point, so this narrows the type for mypy without adding any
    reachable failure mode.
    """
    if db is None:
        raise RuntimeError("database not initialized")
    return db


def _has_scope(perm: Permission) -> bool:
    """True when the caller's OIDC ``scope`` claim carries ``perm``.

    Authoritative ``scope`` claim only, never the ``role`` claim (house
    scope-only policy, see ``auth.require_scope``).

    audit-2026-09-14-wave2: the ``role == "admin"`` cross-org bypass in
    ``_can_access`` (read/write any org's policy) is now the admin-only
    ``routing_policy:admin`` scope. Identical for a fresh admin token; an
    in-flight admin JWT gains it on next login (<=1h TTL), API-key admins
    immediately.
    """
    user = getattr(g, "user", None) or {}
    return perm.value in set(user.get("scope") or [])


def _can_access(can_admin: bool, user_org_id: int | None, target_org_id: int) -> bool:
    """Admin (routing_policy:admin) manages any org's policy; everyone else only their own."""
    return can_admin or target_org_id == user_org_id


async def _invalidate_policy_cache(org_id: int) -> None:
    """Best-effort Valkey cache invalidation via the shared PolicyResolver."""
    if redis_client is None:
        return
    try:
        from shared.routing.policy import PolicyResolver

        await PolicyResolver(db=None, valkey=redis_client).invalidate(org_id)
    except Exception as exc:  # pragma: no cover - defensive, cache-only failure
        logger.warning("routing_policies: cache invalidation failed: %s", exc)


@routing_policies_bp.route("/<int:organization_id>", methods=["GET"])
@require_auth
@validate_response(PolicyGetResponse, 200)
async def get_policy(organization_id: int) -> tuple:
    """Get an org's routing policy, or engine defaults if no row exists yet."""
    can_admin = _has_scope(Permission.ROUTING_POLICY_ADMIN)
    user_org_id = g.user.get("organization_id")
    if not _can_access(can_admin, user_org_id, organization_id):
        return jsonify({"status": "error", "error": "Access denied"}), 403

    def _fetch():
        database = _db()
        return (
            database(database.routing_policies.organization_id == organization_id).select().first()
        )

    row = await asyncio.to_thread(_fetch)
    if not row:
        from shared.routing.policy import RoutingPolicyConfig

        defaults = RoutingPolicyConfig()
        return (
            {
                "status": "success",
                "data": {
                    "organization_id": organization_id,
                    "id": None,
                    "mode": defaults.mode,
                    "escalation_threshold": defaults.escalation_threshold,
                    "escalation_target": defaults.escalation_target,
                    "classifier_prompt": defaults.classifier_prompt,
                    "de_escalation": defaults.de_escalation,
                    "idle_reset_minutes": defaults.idle_reset_minutes,
                    "sensitivity_routing": defaults.sensitivity_routing,
                    "budget_pressure_enabled": defaults.budget_pressure_enabled,
                    "provider_failover": defaults.provider_failover,
                    # created_at/updated_at have no persisted row yet; declared
                    # here so the response field set matches the stored-row path.
                    "created_at": None,
                    "updated_at": None,
                },
                "meta": {"defaulted": True, "timestamp": datetime.utcnow().isoformat() + "Z"},
            },
            200,
        )

    return (
        {
            "status": "success",
            "data": _row_to_dict(row),
            "meta": {"defaulted": False, "timestamp": datetime.utcnow().isoformat() + "Z"},
        },
        200,
    )


@routing_policies_bp.route("/<int:organization_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.ROUTING_POLICY_WRITE)
@validate_response(PolicyWriteResponse, 200)
@validate_response(PolicyWriteResponse, 201)
@validate_request(UpsertPolicyRequest)
async def upsert_policy(organization_id: int, data: UpsertPolicyRequest) -> tuple:
    """Create or update an org's routing policy (upsert on organization_id)."""
    can_admin = _has_scope(Permission.ROUTING_POLICY_ADMIN)
    user_org_id = g.user.get("organization_id")
    if not _can_access(can_admin, user_org_id, organization_id):
        return jsonify({"status": "error", "error": "Access denied"}), 403

    update_fields: dict[str, Any] = {
        f: getattr(data, f) for f in _WRITABLE_FIELDS if getattr(data, f) is not None
    }
    if not update_fields:
        return jsonify({"status": "error", "error": "No valid fields to update"}), 400

    error = _validate_fields(update_fields)
    if error:
        return jsonify({"status": "error", "error": error}), 400

    def _upsert():
        existing = db(db.routing_policies.organization_id == organization_id).select().first()
        if existing:
            db(db.routing_policies.id == existing.id).update(
                **update_fields, updated_at=datetime.utcnow()
            )
            db.commit()
            return "updated", db(db.routing_policies.id == existing.id).select().first()

        new_id = db.routing_policies.insert(
            organization_id=organization_id,
            **update_fields,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.commit()
        return "created", db(db.routing_policies.id == new_id).select().first()

    action, row = await asyncio.to_thread(_upsert)
    await _invalidate_policy_cache(organization_id)

    return (
        {
            "status": "success",
            "data": _row_to_dict(row),
            "meta": {"action": action, "timestamp": datetime.utcnow().isoformat() + "Z"},
        },
        200 if action == "updated" else 201,
    )


@routing_policies_bp.route("/<int:organization_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.ROUTING_POLICY_DELETE)
@validate_response(PolicyDeleteResponse, 200)
async def delete_policy(organization_id: int) -> tuple:
    """Delete an org's routing policy row (admin only) -- resets it to engine defaults."""

    def _delete():
        row = db(db.routing_policies.organization_id == organization_id).select().first()
        if not row:
            return "not_found"
        db(db.routing_policies.organization_id == organization_id).delete()
        db.commit()
        return "ok"

    result = await asyncio.to_thread(_delete)
    if result == "not_found":
        return jsonify({"status": "error", "error": "Policy not found"}), 404

    await _invalidate_policy_cache(organization_id)

    return (
        {
            "status": "success",
            "data": {"organization_id": organization_id},
            "meta": {"action": "deleted", "timestamp": datetime.utcnow().isoformat() + "Z"},
        },
        200,
    )
