"""WaddleAI Management API v1 - Cilium Policy Status & Reconcile Endpoints.

Exposes the control-plane state of the Cilium reconciler
(services/management/app/services/cilium_policy.py): CRD capability
detection, feature-flag state, and the most recent reconcile outcome, plus
an admin-triggered on-demand reconcile. Both endpoints are read-only with
respect to WaddleAI's own data — they only ever write Cilium CRDs, never
enter the AIProxy request path (spec §3.3).
"""

import asyncio
import logging

from quart import jsonify

from shared.auth.rbac import Permission

from ...extensions import db
from ...services.cilium_policy import (
    CiliumPolicyReconciler,
    cilium_capabilities,
    get_last_status,
    is_native_rate_limit_enabled,
)
from . import api_v1_bp
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)


@api_v1_bp.route("/cilium/status", methods=["GET"])
@require_auth
@require_scope(Permission.CILIUM_ADMIN)
async def cilium_status():
    """Report Cilium CRD capabilities + the most recent reconcile outcome (admin only).

    Always 200 (never 500) even when Cilium CRDs are entirely absent from the
    cluster — that is a normal, expected state on non-Cilium clusters (§12.3),
    not a server error.
    """
    caps = await asyncio.to_thread(cilium_capabilities)
    flag_enabled = await asyncio.to_thread(is_native_rate_limit_enabled)
    last = get_last_status()

    last_reconcile = (
        {
            "applied": last.applied,
            "skipped": last.skipped,
            "reason": last.reason,
            "degraded": last.degraded,
        }
        if last is not None
        else None
    )

    return jsonify(
        {
            "capabilities": caps,
            "flag_enabled": flag_enabled,
            "last_reconcile": last_reconcile,
            "applied": last.applied if last is not None else [],
            "degraded": last.degraded if last is not None else False,
        }
    )


@api_v1_bp.route("/cilium/reconcile", methods=["POST"])
@require_auth
@require_scope(Permission.CILIUM_ADMIN)
async def cilium_reconcile():
    """Trigger an on-demand reconcile and return the resulting status (admin only)."""
    reconciler = CiliumPolicyReconciler(db)
    status = await asyncio.to_thread(reconciler.reconcile)

    return (
        jsonify(
            {
                "applied": status.applied,
                "skipped": status.skipped,
                "reason": status.reason,
                "degraded": status.degraded,
            }
        ),
        202,
    )
