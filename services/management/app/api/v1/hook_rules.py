"""WaddleAI Management API v1 - Agent Hooks Admin (§18.3/§18.4).

Admin CRUD for the declarative `hook_rules`, `hook_denylist_entries`
(Tier-1 canonical-list additions), and `hook_configs` (Tier-2 opt-in +
telemetry capture) tables `shared.security.hooks_engine` evaluates
against. Attaches routes to the shared `hooks_bp` (see `hooks.py`) so
`/api/v1/hooks/*` stays one blueprint; uses the house `{"status","data",
"meta"}` envelope, unlike the adapter-contract routes in `hooks.py`.

Scoping is a hard boundary (coordinator directive, §18.4): `admin` is
platform-wide and may author `scope_type='global'` rows; `resource_manager`
is force-scoped to their own org (`scope_type='org'`,
`scope_ref=str(their org)`) on every write, sees global rows read-only, and
can never read/write another org's rows. This mirrors the
`security_policies.py` bypass-grants `_org_scope_allowed` precedent, but
checks the *scope itself* rather than resolving an implied org from a
subject -- there is no subject here, the scope IS the org.

Every write invalidates the corresponding resolver's Valkey cache -- same
rule as `security_policies.py`: a resolved value must never serve stale
after an admin changes it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from quart import g, jsonify
from quart_schema import validate_request, validate_response

from shared.auth.rbac import Permission
from shared.security.hooks_config import HookConfigResolver, PenguinDALHookConfigStore
from shared.security.hooks_denylist import HookDenylistResolver, PenguinDALHookDenylistStore
from shared.security.hooks_rules import HookRulesResolver, PenguinDALHookRulesStore

from ... import extensions as _ext
from ...extensions import db
from ._pagination import PageRequest
from .auth import require_auth, require_scope
from .hooks import HOOK_ECOSYSTEMS, HOOK_EVENTS, hooks_bp

_SCOPE_TYPES = ("global", "org")
_DECISIONS = ("allow", "deny", "ask")
_FAIL_MODES = ("open", "closed")


# ---------------------------------------------------------------------------
# quart-schema request/response models (audit-2026-09-14 wave2).
#
# Request models are deliberately loose (every field Optional): shape/type
# validation is quart-schema's job, but the *business* rules (scope_type in
# _SCOPE_TYPES, decision in _DECISIONS, reason required, force-scoping a
# resource_manager to their own org) stay in the handlers so the §18.4
# tenant-isolation boundary and the existing 400 error bodies are preserved.
#
# Response models pin the EXACT field set each route already emits -- the
# `{status, data, meta}` envelope -- so a future edit that accidentally
# serialized a raw row (or dropped a field) fails the response-schema test
# instead of silently over-/under-exposing.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CreateHookRuleRequest:
    """Body for POST /rules. Business validation stays in the handler."""

    decision: str | None = None
    reason: str | None = None
    scope_type: str | None = None
    scope_ref: str | None = None
    ecosystem: str | None = None
    event: str | None = None
    tool_name_pattern: str | None = None
    match_pattern: str | None = None
    enabled: bool | None = None
    priority: int | None = None


@dataclass(slots=True)
class UpdateHookRuleRequest:
    """Body for PUT /rules/<id>. Only non-null fields are applied; scope is immutable."""

    ecosystem: str | None = None
    event: str | None = None
    tool_name_pattern: str | None = None
    match_pattern: str | None = None
    decision: str | None = None
    reason: str | None = None
    enabled: bool | None = None
    priority: int | None = None


@dataclass(slots=True)
class CreateDenylistEntryRequest:
    """Body for POST /denylist. Additive only."""

    pattern: str | None = None
    reason: str | None = None
    scope_type: str | None = None
    scope_ref: str | None = None
    enabled: bool | None = None


@dataclass(slots=True)
class UpsertHookConfigRequest:
    """Body for POST /configs (upsert by scope)."""

    scope_type: str | None = None
    scope_ref: str | None = None
    remote_eval_enabled: bool | None = None
    remote_eval_timeout_ms: int | None = None
    remote_eval_fail_mode: str | None = None
    capture_raw_payloads: bool | None = None


@dataclass(slots=True)
class HookRuleModel:
    """Exact response schema for one hook_rules row (mirrors `_rule_to_dict`)."""

    id: int
    scope_type: str
    scope_ref: str | None
    ecosystem: str | None
    event: str | None
    tool_name_pattern: str | None
    match_pattern: str | None
    decision: str
    reason: str | None
    enabled: bool
    priority: int
    created_by: int | None
    created_at: str | None
    updated_at: str | None


@dataclass(slots=True)
class DenylistEntryModel:
    """Exact response schema for one hook_denylist_entries row."""

    id: int
    scope_type: str
    scope_ref: str | None
    pattern: str
    reason: str | None
    enabled: bool
    created_by: int | None
    created_at: str | None


@dataclass(slots=True)
class HookConfigModel:
    """Exact response schema for one hook_configs row."""

    id: int
    scope_type: str
    scope_ref: str | None
    remote_eval_enabled: bool | None
    remote_eval_timeout_ms: int | None
    remote_eval_fail_mode: str | None
    capture_raw_payloads: bool | None
    updated_at: str | None


@dataclass(slots=True)
class IdModel:
    """`data` block for a delete response."""

    id: int


@dataclass(slots=True)
class ListMeta:
    """`meta` for a paginated list response: count + timestamp + pagination window."""

    total: int
    timestamp: str
    pagination: dict[str, Any]


@dataclass(slots=True)
class ActionMeta:
    """`meta` for a create/delete/upsert response."""

    action: str
    timestamp: str


@dataclass(slots=True)
class TimestampMeta:
    """`meta` for an update response (timestamp only)."""

    timestamp: str


@dataclass(slots=True)
class HookRuleListResponse:
    """Response body for GET /rules."""

    status: str
    data: list[HookRuleModel]
    meta: ListMeta


@dataclass(slots=True)
class HookRuleCreateResponse:
    """Response body for POST /rules."""

    status: str
    data: HookRuleModel
    meta: ActionMeta


@dataclass(slots=True)
class HookRuleUpdateResponse:
    """Response body for PUT /rules/<id>."""

    status: str
    data: HookRuleModel
    meta: TimestampMeta


@dataclass(slots=True)
class DeleteResponse:
    """Response body for a delete (rule or denylist entry)."""

    status: str
    data: IdModel
    meta: ActionMeta


@dataclass(slots=True)
class DenylistListResponse:
    """Response body for GET /denylist."""

    status: str
    data: list[DenylistEntryModel]
    meta: ListMeta


@dataclass(slots=True)
class DenylistCreateResponse:
    """Response body for POST /denylist."""

    status: str
    data: DenylistEntryModel
    meta: ActionMeta


@dataclass(slots=True)
class HookConfigListResponse:
    """Response body for GET /configs."""

    status: str
    data: list[HookConfigModel]
    meta: ListMeta


@dataclass(slots=True)
class HookConfigUpsertResponse:
    """Response body for POST /configs (200 update / 201 create)."""

    status: str
    data: HookConfigModel
    meta: ActionMeta


def _get_redis() -> Any:
    return getattr(_ext, "redis_client", None)


def _actor_forced_scope(user_role: str | None, user_org_id: Any) -> tuple[str, str | None] | None:
    """(scope_type, scope_ref) a non-admin actor is forced onto; None for admin (no override)."""
    if user_role == "admin":
        return None
    return ("org", str(user_org_id))


def scope_readable(
    user_role: str | None, user_org_id: Any, row_scope_type: str, row_scope_ref: str | None
) -> bool:
    """Admin sees everything; resource_manager sees global rows (read-only) + their own org."""
    if user_role == "admin":
        return True
    if row_scope_type == "global":
        return True
    return row_scope_type == "org" and row_scope_ref == str(user_org_id)


def _write_scope_allowed(
    user_role: str | None, user_org_id: Any, row_scope_type: str, row_scope_ref: str | None
) -> bool:
    """Only admin may touch global rows; resource_manager limited to their own org's rows."""
    if user_role == "admin":
        return True
    return row_scope_type == "org" and row_scope_ref == str(user_org_id)


# ---------------------------------------------------------------------------
# hook_rules
# ---------------------------------------------------------------------------


def _rule_to_dict(row: Any) -> dict[str, Any]:
    """Explicit response schema for a hook_rules row -- never raw model serialization."""
    return {
        "id": row.id,
        "scope_type": row.scope_type,
        "scope_ref": row.scope_ref,
        "ecosystem": row.ecosystem,
        "event": row.event,
        "tool_name_pattern": row.tool_name_pattern,
        "match_pattern": row.match_pattern,
        "decision": row.decision,
        "reason": row.reason,
        "enabled": row.enabled,
        "priority": row.priority,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@hooks_bp.route("/rules", methods=["GET"])
@require_auth
@require_scope(Permission.HOOK_RULE_ADMIN)
@validate_response(HookRuleListResponse, 200)
async def list_hook_rules() -> tuple:
    """List hook_rules -- admin sees all; resource_manager sees global (read-only) + own org."""
    page = PageRequest.from_request()
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    def _fetch():
        rows = db(db.hook_rules.id > 0).select(limitby=page.limitby, orderby=db.hook_rules.id)
        return [
            r for r in rows if scope_readable(user_role, user_org_id, r.scope_type, r.scope_ref)
        ]

    rows = await asyncio.to_thread(_fetch)
    rules = [_rule_to_dict(r) for r in rows]
    return (
        {
            "status": "success",
            "data": rules,
            "meta": {
                "total": len(rules),
                "timestamp": datetime.utcnow().isoformat() + "Z",
                **page.meta(),
            },
        },
        200,
    )


@hooks_bp.route("/rules", methods=["POST"])
@require_auth
@require_scope(Permission.HOOK_RULE_ADMIN)
@validate_response(HookRuleCreateResponse, 201)
@validate_request(CreateHookRuleRequest)
async def create_hook_rule(data: CreateHookRuleRequest) -> tuple:
    """Create a hook_rule. resource_manager is force-scoped to their own org, always."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    user_id = g.user.get("user_id") or g.user.get("id")

    forced = _actor_forced_scope(user_role, user_org_id)
    if forced is not None:
        scope_type, scope_ref = forced
    else:
        scope_type = data.scope_type or ""
        scope_ref = data.scope_ref
        if scope_type not in _SCOPE_TYPES:
            error = f"scope_type must be one of {_SCOPE_TYPES}"
            return jsonify({"status": "error", "error": error}), 400
        if scope_type == "global" and scope_ref is not None:
            return jsonify({"status": "error", "error": "global scope must not set scope_ref"}), 400
        if scope_type == "org" and not scope_ref:
            return jsonify({"status": "error", "error": "scope_ref is required for org scope"}), 400

    decision = data.decision or ""
    reason = data.reason or ""
    if decision not in _DECISIONS:
        return jsonify({"status": "error", "error": f"decision must be one of {_DECISIONS}"}), 400
    if not reason:
        return jsonify({"status": "error", "error": "reason is required"}), 400

    ecosystem = data.ecosystem
    if ecosystem is not None and ecosystem not in HOOK_ECOSYSTEMS:
        error = f"ecosystem must be one of {HOOK_ECOSYSTEMS} or null"
        return jsonify({"status": "error", "error": error}), 400
    event = data.event
    if event is not None and event not in HOOK_EVENTS:
        error = f"event must be one of {HOOK_EVENTS} or null"
        return jsonify({"status": "error", "error": error}), 400

    def _create():
        new_id: int = db.hook_rules.insert(
            scope_type=scope_type,
            scope_ref=scope_ref,
            ecosystem=ecosystem,
            event=event,
            tool_name_pattern=data.tool_name_pattern,
            match_pattern=data.match_pattern,
            decision=decision,
            reason=reason,
            enabled=True if data.enabled is None else bool(data.enabled),
            priority=100 if data.priority is None else int(data.priority),
            created_by=user_id,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.commit()
        return db(db.hook_rules.id == new_id).select().first()

    row = await asyncio.to_thread(_create)
    await HookRulesResolver(PenguinDALHookRulesStore(db), _get_redis()).invalidate()

    return (
        {
            "status": "success",
            "data": _rule_to_dict(row),
            "meta": {"action": "created", "timestamp": datetime.utcnow().isoformat() + "Z"},
        },
        201,
    )


@hooks_bp.route("/rules/<int:rule_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.HOOK_RULE_ADMIN)
@validate_response(HookRuleUpdateResponse, 200)
@validate_request(UpdateHookRuleRequest)
async def update_hook_rule(rule_id: int, data: UpdateHookRuleRequest) -> tuple:
    """Update a hook_rule -- scope-checked against the row's CURRENT scope, not the request body."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    if data.decision is not None and data.decision not in _DECISIONS:
        return jsonify({"status": "error", "error": f"decision must be one of {_DECISIONS}"}), 400

    update_fields = {
        k: v
        for k, v in {
            "ecosystem": data.ecosystem,
            "event": data.event,
            "tool_name_pattern": data.tool_name_pattern,
            "match_pattern": data.match_pattern,
            "decision": data.decision,
            "reason": data.reason,
            "enabled": data.enabled,
            "priority": data.priority,
        }.items()
        if v is not None
    }
    # scope_type/scope_ref are immutable via update -- reassigning a rule's
    # scope is a delete+recreate so the write-scope check above always
    # matches what's actually persisted.

    def _update():
        row = db(db.hook_rules.id == rule_id).select().first()
        if row is None:
            return "not_found", None
        if not _write_scope_allowed(user_role, user_org_id, row.scope_type, row.scope_ref):
            return "forbidden", None
        db(db.hook_rules.id == rule_id).update(updated_at=datetime.utcnow(), **update_fields)
        db.commit()
        return "updated", db(db.hook_rules.id == rule_id).select().first()

    result, row = await asyncio.to_thread(_update)
    if result == "not_found":
        return jsonify({"status": "error", "error": "hook_rule not found"}), 404
    if result == "forbidden":
        return jsonify({"status": "error", "error": "rule is outside your organization"}), 403

    await HookRulesResolver(PenguinDALHookRulesStore(db), _get_redis()).invalidate()

    return (
        {
            "status": "success",
            "data": _rule_to_dict(row),
            "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
        },
        200,
    )


@hooks_bp.route("/rules/<int:rule_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.HOOK_RULE_ADMIN)
@validate_response(DeleteResponse, 200)
async def delete_hook_rule(rule_id: int) -> tuple:
    """Delete a hook_rule -- scope-checked against the row's current scope."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    def _delete():
        row = db(db.hook_rules.id == rule_id).select().first()
        if row is None:
            return "not_found"
        if not _write_scope_allowed(user_role, user_org_id, row.scope_type, row.scope_ref):
            return "forbidden"
        db(db.hook_rules.id == rule_id).delete()
        db.commit()
        return "deleted"

    result = await asyncio.to_thread(_delete)
    if result == "not_found":
        return jsonify({"status": "error", "error": "hook_rule not found"}), 404
    if result == "forbidden":
        return jsonify({"status": "error", "error": "rule is outside your organization"}), 403

    await HookRulesResolver(PenguinDALHookRulesStore(db), _get_redis()).invalidate()

    return (
        {
            "status": "success",
            "data": {"id": rule_id},
            "meta": {"action": "deleted", "timestamp": datetime.utcnow().isoformat() + "Z"},
        },
        200,
    )


# ---------------------------------------------------------------------------
# hook_denylist_entries (Tier-1 canonical-list ADDITIONS only)
# ---------------------------------------------------------------------------


def _denylist_entry_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "scope_type": row.scope_type,
        "scope_ref": row.scope_ref,
        "pattern": row.pattern,
        "reason": row.reason,
        "enabled": row.enabled,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@hooks_bp.route("/denylist", methods=["GET"])
@require_auth
@require_scope(Permission.HOOK_RULE_ADMIN)
@validate_response(DenylistListResponse, 200)
async def list_hook_denylist_entries() -> tuple:
    """List admin-added denylist entries -- the builtin seed list is not a DB row.

    See `shared.security.hooks_denylist.BUILTIN_DENYLIST_PATTERNS` for the
    always-on floor every org gets regardless of what is (or isn't) in this
    table.
    """
    page = PageRequest.from_request()
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    def _fetch():
        rows = db(db.hook_denylist_entries.id > 0).select(
            limitby=page.limitby, orderby=db.hook_denylist_entries.id
        )
        return [
            r for r in rows if scope_readable(user_role, user_org_id, r.scope_type, r.scope_ref)
        ]

    rows = await asyncio.to_thread(_fetch)
    entries = [_denylist_entry_to_dict(r) for r in rows]
    return (
        {
            "status": "success",
            "data": entries,
            "meta": {
                "total": len(entries),
                "timestamp": datetime.utcnow().isoformat() + "Z",
                **page.meta(),
            },
        },
        200,
    )


@hooks_bp.route("/denylist", methods=["POST"])
@require_auth
@require_scope(Permission.HOOK_RULE_ADMIN)
@validate_response(DenylistCreateResponse, 201)
@validate_request(CreateDenylistEntryRequest)
async def create_hook_denylist_entry(data: CreateDenylistEntryRequest) -> tuple:
    """Add a Tier-1 denylist entry. Additive only -- there is no update/replace endpoint.

    resource_manager is force-scoped to their own org; only `admin` may add
    a `scope_type='global'` (deployment-wide) entry.
    """
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    user_id = g.user.get("user_id") or g.user.get("id")

    forced = _actor_forced_scope(user_role, user_org_id)
    if forced is not None:
        scope_type, scope_ref = forced
    else:
        scope_type = data.scope_type or ""
        scope_ref = data.scope_ref
        if scope_type not in _SCOPE_TYPES:
            error = f"scope_type must be one of {_SCOPE_TYPES}"
            return jsonify({"status": "error", "error": error}), 400
        if scope_type == "global" and scope_ref is not None:
            return jsonify({"status": "error", "error": "global scope must not set scope_ref"}), 400
        if scope_type == "org" and not scope_ref:
            return jsonify({"status": "error", "error": "scope_ref is required for org scope"}), 400

    pattern = data.pattern or ""
    if not pattern:
        return jsonify({"status": "error", "error": "pattern is required"}), 400

    def _create():
        new_id: int = db.hook_denylist_entries.insert(
            scope_type=scope_type,
            scope_ref=scope_ref,
            pattern=pattern,
            reason=data.reason,
            enabled=True if data.enabled is None else bool(data.enabled),
            created_by=user_id,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.commit()
        return db(db.hook_denylist_entries.id == new_id).select().first()

    row = await asyncio.to_thread(_create)
    await HookDenylistResolver(PenguinDALHookDenylistStore(db), _get_redis()).invalidate()

    return (
        {
            "status": "success",
            "data": _denylist_entry_to_dict(row),
            "meta": {"action": "created", "timestamp": datetime.utcnow().isoformat() + "Z"},
        },
        201,
    )


@hooks_bp.route("/denylist/<int:entry_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.HOOK_RULE_ADMIN)
@validate_response(DeleteResponse, 200)
async def delete_hook_denylist_entry(entry_id: int) -> tuple:
    """Delete an admin-added denylist entry. Never touches the builtin seed list."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    def _delete():
        row = db(db.hook_denylist_entries.id == entry_id).select().first()
        if row is None:
            return "not_found"
        if not _write_scope_allowed(user_role, user_org_id, row.scope_type, row.scope_ref):
            return "forbidden"
        db(db.hook_denylist_entries.id == entry_id).delete()
        db.commit()
        return "deleted"

    result = await asyncio.to_thread(_delete)
    if result == "not_found":
        return jsonify({"status": "error", "error": "denylist entry not found"}), 404
    if result == "forbidden":
        return jsonify({"status": "error", "error": "entry is outside your organization"}), 403

    await HookDenylistResolver(PenguinDALHookDenylistStore(db), _get_redis()).invalidate()

    return (
        {
            "status": "success",
            "data": {"id": entry_id},
            "meta": {"action": "deleted", "timestamp": datetime.utcnow().isoformat() + "Z"},
        },
        200,
    )


# ---------------------------------------------------------------------------
# hook_configs (Tier-2 opt-in + telemetry-capture opt-in, per scope)
# ---------------------------------------------------------------------------


def _config_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "scope_type": row.scope_type,
        "scope_ref": row.scope_ref,
        "remote_eval_enabled": row.remote_eval_enabled,
        "remote_eval_timeout_ms": row.remote_eval_timeout_ms,
        "remote_eval_fail_mode": row.remote_eval_fail_mode,
        "capture_raw_payloads": row.capture_raw_payloads,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@hooks_bp.route("/configs", methods=["GET"])
@require_auth
@require_scope(Permission.HOOK_RULE_ADMIN)
@validate_response(HookConfigListResponse, 200)
async def list_hook_configs() -> tuple:
    """List hook_configs rows -- admin sees all; resource_manager sees global + own org."""
    page = PageRequest.from_request()
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    def _fetch():
        rows = db(db.hook_configs.id > 0).select(limitby=page.limitby, orderby=db.hook_configs.id)
        return [
            r for r in rows if scope_readable(user_role, user_org_id, r.scope_type, r.scope_ref)
        ]

    rows = await asyncio.to_thread(_fetch)
    configs = [_config_to_dict(r) for r in rows]
    return (
        {
            "status": "success",
            "data": configs,
            "meta": {
                "total": len(configs),
                "timestamp": datetime.utcnow().isoformat() + "Z",
                **page.meta(),
            },
        },
        200,
    )


@hooks_bp.route("/configs", methods=["POST"])
@require_auth
@require_scope(Permission.HOOK_RULE_ADMIN)
@validate_response(HookConfigUpsertResponse, 200)
@validate_response(HookConfigUpsertResponse, 201)
@validate_request(UpsertHookConfigRequest)
async def upsert_hook_config(data: UpsertHookConfigRequest) -> tuple:
    """Create or update the one hook_configs row for a scope (upsert by scope_type/scope_ref).

    resource_manager is force-scoped to their own org, always -- they
    cannot set the deployment-wide (`global`) Tier-2/telemetry defaults.
    """
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")

    forced = _actor_forced_scope(user_role, user_org_id)
    if forced is not None:
        scope_type, scope_ref = forced
    else:
        scope_type = data.scope_type or ""
        scope_ref = data.scope_ref
        if scope_type not in _SCOPE_TYPES:
            error = f"scope_type must be one of {_SCOPE_TYPES}"
            return jsonify({"status": "error", "error": error}), 400
        if scope_type == "global" and scope_ref is not None:
            return jsonify({"status": "error", "error": "global scope must not set scope_ref"}), 400
        if scope_type == "org" and not scope_ref:
            return jsonify({"status": "error", "error": "scope_ref is required for org scope"}), 400

    if data.remote_eval_fail_mode is not None and data.remote_eval_fail_mode not in _FAIL_MODES:
        error = f"remote_eval_fail_mode must be one of {_FAIL_MODES}"
        return jsonify({"status": "error", "error": error}), 400

    update_fields = {
        k: v
        for k, v in {
            "remote_eval_enabled": data.remote_eval_enabled,
            "remote_eval_timeout_ms": data.remote_eval_timeout_ms,
            "remote_eval_fail_mode": data.remote_eval_fail_mode,
            "capture_raw_payloads": data.capture_raw_payloads,
        }.items()
        if v is not None
    }

    def _upsert():
        existing = (
            db(
                (db.hook_configs.scope_type == scope_type)
                & (db.hook_configs.scope_ref == scope_ref)
            )
            .select()
            .first()
        )
        if existing:
            db(db.hook_configs.id == existing.id).update(
                updated_at=datetime.utcnow(), **update_fields
            )
            db.commit()
            return "updated", db(db.hook_configs.id == existing.id).select().first()

        new_id: int = db.hook_configs.insert(
            scope_type=scope_type,
            scope_ref=scope_ref,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            **update_fields,
        )
        db.commit()
        return "created", db(db.hook_configs.id == new_id).select().first()

    action, row = await asyncio.to_thread(_upsert)
    await HookConfigResolver(PenguinDALHookConfigStore(db), _get_redis()).invalidate()

    return (
        {
            "status": "success",
            "data": _config_to_dict(row),
            "meta": {"action": action, "timestamp": datetime.utcnow().isoformat() + "Z"},
        },
        200 if action == "updated" else 201,
    )
