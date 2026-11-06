"""DAC v1alpha1 Management API (§20).

The endpoint deliberately keeps plans in an application-owned short-lived
store. Deployments that run more than one Management replica must provide a
shared store through ``app.extensions['dac_plan_store']`` before enabling the
feature flag; this prevents a plan generated on one replica being applied on
another without its reviewed record.
"""

from __future__ import annotations

import asyncio
from typing import Any

from quart import Blueprint, current_app, g, jsonify, request

from shared.auth.rbac import Permission
from shared.dac.manifest import (
    DacError,
    DacPlan,
    DacPlanItem,
    DacPlanner,
    DacPlanStore,
    parse_manifest,
    redact_secrets,
)
from shared.utils.feature_flags import is_feature_enabled

from .auth import require_auth, require_scope

manifests_bp = Blueprint("manifests", __name__, url_prefix="/api/v1/manifests")
DAC_FLAG = "waddleai.configuration_as_code"


def _store() -> DacPlanStore:
    return current_app.extensions.setdefault("dac_plan_store", DacPlanStore())


def _current(resource_type: str, name: str) -> dict[str, Any] | None:
    """Allow the Management composition root to supply canonical API state.

    The default is intentionally empty: a deployment must wire its existing
    resource services before enablement, rather than silently reconciling
    against an incomplete ad-hoc database view.
    """
    lookup = current_app.extensions.get("dac_current_lookup")
    return lookup(resource_type, name) if callable(lookup) else None


def _item_dict(item: DacPlanItem) -> dict[str, Any]:
    return {
        "resource_type": item.resource_type,
        "name": item.name,
        "action": item.action,
        "desired": item.desired,
    }


def _plan_dict(plan: DacPlan) -> dict[str, Any]:
    return {
        "manifest_digest": plan.manifest_digest,
        "organization_id": plan.organization_id,
        "expires_at": plan.expires_at,
        "items": [_item_dict(item) for item in plan.items],
    }


async def _persist_reviewed_plan(
    plan: DacPlan, manifest: dict[str, Any], actor_id: int | None
) -> None:
    """Persist a redacted revision and short-lived reviewed plan when the DB is available."""
    from ... import extensions

    if extensions.db is None:
        return

    def _write() -> None:
        db = extensions.db
        if db is None:
            raise RuntimeError("database extension is not initialised")
        revision_id = db.configuration_revisions.insert(
            org_id=int(plan.organization_id),
            name=manifest["metadata"]["name"],
            manifest_digest=plan.manifest_digest,
            manifest_redacted=redact_secrets(manifest),
            created_by=actor_id,
        )
        db.manifest_apply_runs.insert(
            revision_id=revision_id,
            org_id=int(plan.organization_id),
            plan_id=plan.id,
            plan=_plan_dict(plan),
            outcomes=None,
            status="planned",
            actor_id=actor_id,
        )
        db.commit()

    await asyncio.to_thread(_write)


async def _load_reviewed_plan(plan_id: str, organization_id: str) -> DacPlan | None:
    """Rehydrate a plan from the shared DB when this process did not create it."""
    from ... import extensions

    if extensions.db is None:
        return None

    def _read() -> DacPlan | None:
        db = extensions.db
        if db is None:
            raise RuntimeError("database extension is not initialised")
        row = (
            db(
                (db.manifest_apply_runs.plan_id == plan_id)
                & (db.manifest_apply_runs.org_id == int(organization_id))
            )
            .select()
            .first()
        )
        if row is None or row.status != "planned":
            return None
        value = row.plan or {}
        try:
            return DacPlan(
                plan_id,
                value["manifest_digest"],
                str(value["organization_id"]),
                tuple(DacPlanItem(**item) for item in value["items"]),
                float(value["expires_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    return await asyncio.to_thread(_read)


async def _persist_apply_result(
    plan_id: str, organization_id: str, items: tuple[DacPlanItem, ...], status: str
) -> None:
    from ... import extensions

    if extensions.db is None:
        return

    def _write() -> None:
        db = extensions.db
        if db is None:
            raise RuntimeError("database extension is not initialised")
        db(
            (db.manifest_apply_runs.plan_id == plan_id)
            & (db.manifest_apply_runs.org_id == int(organization_id))
        ).update(status=status, outcomes=[_item_dict(item) for item in items])
        db.commit()

    await asyncio.to_thread(_write)


@manifests_bp.route("/apply", methods=["POST"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
async def apply_manifest() -> tuple:
    """Apply a previously reviewed configuration manifest plan.

    Requires the exact unexpired plan so changes are explicit and auditable.
    """
    org_id = str(g.user["organization_id"])
    if not is_feature_enabled(DAC_FLAG, distinct_id=org_id):
        return jsonify({"error": "configuration-as-code feature disabled"}), 404
    body = await request.get_json() or {}
    manifest = body.get("manifest")
    dry_run = body.get("dry_run", True)
    if not isinstance(dry_run, bool):
        return jsonify({"error": "dry_run must be boolean"}), 400
    if not isinstance(manifest, (str, bytes, dict)):
        return jsonify({"error": "manifest must be a string or object"}), 400
    try:
        if dry_run:
            parsed = parse_manifest(manifest, organization_id=org_id)
            plan = DacPlanner(_current).plan(parsed, organization_id=org_id)
            _store().put(plan)
            await _persist_reviewed_plan(plan, parsed, g.user.get("user_id"))
            return jsonify(
                {
                    "plan_id": plan.id,
                    "manifest_digest": plan.manifest_digest,
                    "expires_at": plan.expires_at,
                    "items": [_item_dict(item) for item in plan.items],
                    "dry_run": True,
                }
            ), 200
        plan_id = body.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id:
            return jsonify({"error": "apply requires a reviewed plan_id"}), 400
        if not _store().has(plan_id):  # database fallback supports multi-replica apply
            stored = await _load_reviewed_plan(plan_id, org_id)
            if stored is not None:
                _store().put(stored)
        executor = current_app.extensions.get("dac_executor")
        if not callable(executor):
            return jsonify({"error": "DAC executor is not configured"}), 503
        items = _store().apply(plan_id, organization_id=org_id, executor=executor)
        await _persist_apply_result(plan_id, org_id, items, "applied")
        return jsonify(
            {"plan_id": plan_id, "items": [_item_dict(item) for item in items], "dry_run": False}
        ), 200
    except DacError as exc:
        return jsonify({"error": str(exc)}), 400
