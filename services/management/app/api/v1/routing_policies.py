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
from datetime import datetime
from typing import Any

from quart import Blueprint, g, jsonify, request

from ...extensions import db, redis_client
from .auth import require_auth, require_role

logger = logging.getLogger(__name__)

routing_policies_bp = Blueprint("routing_policies", __name__, url_prefix="/api/v1/routing/policies")

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


def _can_access(user_role: str, user_org_id: int | None, target_org_id: int) -> bool:
    """Admin manages any org's policy; everyone else only their own."""
    return user_role == "admin" or target_org_id == user_org_id


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
async def get_policy(organization_id: int) -> tuple:
    """Get an org's routing policy, or engine defaults if no row exists yet."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    if not _can_access(user_role, user_org_id, organization_id):
        return jsonify({"status": "error", "error": "Access denied"}), 403

    row = await asyncio.to_thread(
        lambda: db(db.routing_policies.organization_id == organization_id).select().first()
    )
    if not row:
        from shared.routing.policy import RoutingPolicyConfig

        defaults = RoutingPolicyConfig()
        return (
            jsonify(
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
                    },
                    "meta": {"defaulted": True, "timestamp": datetime.utcnow().isoformat() + "Z"},
                }
            ),
            200,
        )

    return (
        jsonify(
            {
                "status": "success",
                "data": _row_to_dict(row),
                "meta": {"defaulted": False, "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )


@routing_policies_bp.route("/<int:organization_id>", methods=["PUT"])
@require_auth
@require_role("admin", "resource_manager")
async def upsert_policy(organization_id: int) -> tuple:
    """Create or update an org's routing policy (upsert on organization_id)."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    if not _can_access(user_role, user_org_id, organization_id):
        return jsonify({"status": "error", "error": "Access denied"}), 403

    data: dict[str, Any] | None = await request.get_json()
    if not data:
        return jsonify({"status": "error", "error": "Request body required"}), 400

    error = _validate_fields(data)
    if error:
        return jsonify({"status": "error", "error": error}), 400

    update_fields: dict[str, Any] = {f: data[f] for f in _WRITABLE_FIELDS if f in data}
    if not update_fields:
        return jsonify({"status": "error", "error": "No valid fields to update"}), 400

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
        jsonify(
            {
                "status": "success",
                "data": _row_to_dict(row),
                "meta": {"action": action, "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200 if action == "updated" else 201,
    )


@routing_policies_bp.route("/<int:organization_id>", methods=["DELETE"])
@require_auth
@require_role("admin")
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
        jsonify(
            {
                "status": "success",
                "data": {"organization_id": organization_id},
                "meta": {"action": "deleted", "timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )
