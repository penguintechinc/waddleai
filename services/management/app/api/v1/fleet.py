"""WaddleAI Management API v1 - Inference Fleet Backend Registry (spec §10.1/§10.4).

``fleet_backends`` CRUD (org-scoped, admin-gated), `management_scope`
selection at creation, and status/health surfaced via the
``InferenceFleetBackend`` interface (``shared.fleet.registry.build_backend``)
rather than a bespoke per-type health check. `vertex_ai`/`bedrock` creation
is two-layer Pro-gated (spec §14.6): the `waddleai.fleet_v2` PostHog flag
*and* `LicenseClient.check_feature("hybrid_targets")` -- Free/`community`
never sees either backend type even with the flag on. Existing
`ollama.py`/`llamacpp.py` deployment routes are untouched and stay
byte-compatible; this module only owns the new `fleet_backends` registry
table, not deployment lifecycle.

Follows the same conventions as ``integrations.py`` (§11.4): `{"status",
"data", "meta"}` response envelope, secrets never echoed,
``encrypt_credential``/``decrypt_credential`` for anything sensitive,
admin-role CRUD, thread-offloaded penguin-dal calls, every route org-scoped
from ``g.user["organization_id"]``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from penguin_dal.db import DB
from quart import g, jsonify
from quart_schema import validate_request, validate_response

from shared.auth.rbac import Permission
from shared.fleet.base import BackendType, ManagementScope
from shared.fleet.registry import build_backend
from shared.security.credential_encryption import encrypt_credential
from shared.utils.feature_flags import is_feature_enabled

from ...extensions import db
from . import api_v1_bp
from ._pagination import PageRequest
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)


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


FLEET_V2_FLAG = "waddleai.fleet_v2"
_HYBRID_TARGETS_FEATURE = "hybrid_targets"

# Backend types requiring the two-layer Pro gate (flag AND check_feature) at
# creation -- Ollama/llama.cpp/EXO are available at any tier once fleet_v2
# is on (spec §2.4 "Deployment targets: K8s/local only" is Free-tier; Vertex
# AI/Bedrock are the Professional-gated "+ hybrid" row).
_PRO_GATED_TYPES = frozenset({BackendType.VERTEX_AI.value, BackendType.BEDROCK.value})

_VALID_TYPES = frozenset(t.value for t in BackendType)
_VALID_SCOPES = frozenset(s.value for s in ManagementScope)
_VALID_STATUSES = frozenset({"pending", "active", "disabled", "error"})
_STATUS_ERROR = "status must be one of ['pending', 'active', 'disabled', 'error']"

_license_client: Any = None


# ---------------------------------------------------------------------------
# OpenAPI request/response models (audit-2026-09-14-wave2).
#
# The `{"status", "data", "meta"}` envelope is preserved exactly; each model
# lists the precise field set its handler returns so quart-schema cannot
# silently drop one (a dropped `credentials_ref`/`data` field breaks clients),
# and the masked-credential guarantee is pinned by a response-schema test.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FleetBackend:
    """A ``fleet_backends`` row, exactly as ``_backend_to_dict`` serialises it.

    ``credentials_ref`` is the masked reference only -- the plaintext
    credential is never a field on this model.
    """

    id: int
    org_id: int
    name: str
    type: str
    mode: str | None
    management_scope: str
    config: dict[str, Any]
    credentials_ref: str | None
    status: str | None
    created_at: str | None
    updated_at: str | None


@dataclass(slots=True)
class ListMeta:
    """Envelope ``meta`` for the list endpoint -- carries the pagination window."""

    total: int
    page: int
    limit: int
    timestamp: str


@dataclass(slots=True)
class ActionMeta:
    """Envelope ``meta`` for a mutating action (create/update/delete)."""

    action: str
    timestamp: str


@dataclass(slots=True)
class TimestampMeta:
    """Envelope ``meta`` carrying only a timestamp (read/health responses)."""

    timestamp: str


@dataclass(slots=True)
class FleetBackendListResponse:
    """Response body for GET /api/v1/fleet/backends."""

    status: str
    data: list[FleetBackend]
    meta: ListMeta


@dataclass(slots=True)
class FleetBackendResponse:
    """Response body for GET/POST/PUT of a single fleet backend."""

    status: str
    data: FleetBackend
    meta: TimestampMeta


@dataclass(slots=True)
class FleetBackendActionResponse:
    """Response body for POST (create) / PUT (update) with an action meta."""

    status: str
    data: FleetBackend
    meta: ActionMeta


@dataclass(slots=True)
class DeletedBackendRef:
    """The ``data`` payload of a delete response -- just the removed id."""

    id: int


@dataclass(slots=True)
class DeleteFleetBackendResponse:
    """Response body for DELETE /api/v1/fleet/backends/<id>."""

    status: str
    data: DeletedBackendRef
    meta: ActionMeta


@dataclass(slots=True)
class FleetHealthData:
    """The ``data`` payload of a backend health response."""

    backend_id: int
    healthy: bool
    node_count: int
    detail: dict[str, Any]


@dataclass(slots=True)
class FleetBackendHealthResponse:
    """Response body for GET /api/v1/fleet/backends/<id>/health."""

    status: str
    data: FleetHealthData
    meta: TimestampMeta


@dataclass(slots=True)
class CreateFleetBackendRequest:
    """Request body for POST /api/v1/fleet/backends. Every field optional."""

    name: str | None = None
    type: str | None = None
    mode: str | None = None
    management_scope: str | None = None
    config: dict[str, Any] | None = field(default=None)
    credentials: str | None = None


@dataclass(slots=True)
class UpdateFleetBackendRequest:
    """Request body for PUT /api/v1/fleet/backends/<id>. Every field a partial.

    ``type`` is intentionally absent -- it is immutable after creation.
    """

    name: str | None = None
    mode: str | None = None
    management_scope: str | None = None
    config: dict[str, Any] | None = field(default=None)
    credentials: str | None = None
    status: str | None = None


def _get_license_client() -> Any:
    """Lazily construct the shared ``penguin_licensing.LicenseClient`` (spec §14.6).

    ``product`` must be ``"waddleai"`` -- the SDK's own default is
    ``"elder"`` and would silently check entitlements for the wrong
    product. Also wires this deployment's public host (see
    ``shared.licensing.domain_bypass``) so a managed PenguinTech domain
    bypasses license checks entirely, once the pinned SDK version supports it.
    """
    global _license_client
    if _license_client is None:
        from penguin_licensing import LicenseClient

        from shared.licensing.domain_bypass import apply_deployment_host

        _license_client = LicenseClient(
            license_key=os.environ.get("LICENSE_KEY", ""),
            product="waddleai",
            base_url=os.environ.get("LICENSE_SERVER_URL", "https://license.penguintech.io"),
        )
        apply_deployment_host(_license_client)
    return _license_client


def _fleet_v2_enabled(org_id: int) -> bool:
    return is_feature_enabled(FLEET_V2_FLAG, distinct_id=str(org_id), default=False)


async def _hybrid_targets_entitled() -> bool:
    """Two-layer gate's entitlement half -- fail-closed on any license-client error."""

    def _check():
        try:
            return bool(_get_license_client().check_feature(_HYBRID_TARGETS_FEATURE))
        except Exception as exc:  # pragma: no cover - defensive, license I/O failure
            logger.warning("fleet.py: hybrid_targets entitlement check failed: %s", exc)
            return False

    return await asyncio.to_thread(_check)


def _mask_secret(value: str | None) -> str | None:
    """Return a masked representation of a stored credential -- never plaintext."""
    if not value:
        return value
    raw = value[4:] if value.startswith("enc:") else value
    if len(raw) <= 8:
        return "****"
    return raw[:4] + "****" + raw[-4:]


def _backend_to_dict(row: Any) -> dict[str, Any]:
    """Serialize a ``fleet_backends`` row -- ``credentials_ref`` is always masked."""
    return {
        "id": row.id,
        "org_id": row.org_id,
        "name": row.name,
        "type": row.type,
        "mode": row.mode,
        "management_scope": row.management_scope,
        "config": row.config or {},
        "credentials_ref": _mask_secret(row.credentials_ref),
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _now() -> str:
    """UTC timestamp string used across every envelope ``meta`` block."""
    return datetime.utcnow().isoformat() + "Z"


def _validation_error(detail: str) -> tuple[Any, int]:
    return jsonify({"status": "error", "error": detail}), 400


def _get_org_scoped_backend(backend_id: int, org_id: int) -> tuple[Any, str]:
    """Return a ``fleet_backends`` row, distinguishing "not found" from "wrong org"."""
    database = _db()
    row = database(database.fleet_backends.id == backend_id).select().first()
    if row is None:
        return None, "not_found"
    if row.org_id != org_id:
        return None, "forbidden"
    return row, "ok"


# ---------------------------------------------------------------------------
# `/api/v1/fleet/backends` CRUD (admin only, org-scoped, flag-gated)
# ---------------------------------------------------------------------------


@api_v1_bp.route("/fleet/backends", methods=["GET"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(FleetBackendListResponse, 200)
async def list_fleet_backends():
    """List this org's registered inference fleet backends (bounded page)."""
    org_id = g.user.get("organization_id")
    if not _fleet_v2_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404

    page = PageRequest.from_request()

    def _fetch():
        query = db.fleet_backends.org_id == org_id
        total = db(query).count()
        rows = db(query).select(limitby=page.limitby, orderby=db.fleet_backends.id)
        return total, list(rows)

    total, rows = await asyncio.to_thread(_fetch)
    return {
        "status": "success",
        "data": [_backend_to_dict(r) for r in rows],
        "meta": {"total": total, "page": page.page, "limit": page.limit, "timestamp": _now()},
    }, 200


@api_v1_bp.route("/fleet/backends", methods=["POST"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(FleetBackendActionResponse, 201)
@validate_request(CreateFleetBackendRequest)
async def create_fleet_backend(data: CreateFleetBackendRequest):
    """Register a new inference fleet backend for this org.

    `vertex_ai`/`bedrock` additionally require the `hybrid_targets`
    license entitlement (two-layer gate, spec §14.6) -- rejected with a
    tier-named 403 even when `waddleai.fleet_v2` is on.
    """
    org_id = g.user.get("organization_id")
    if not _fleet_v2_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404

    name = (data.name or "").strip()
    backend_type = data.type
    mode = data.mode
    management_scope = data.management_scope or ManagementScope.FULL_LIFECYCLE.value
    config = data.config if data.config is not None else {}
    credentials = data.credentials

    if not name or len(name) > 255:
        return _validation_error("name is required and must be <= 255 characters")
    if backend_type not in _VALID_TYPES:
        return _validation_error(f"type must be one of {sorted(_VALID_TYPES)}")
    if management_scope not in _VALID_SCOPES:
        return _validation_error(f"management_scope must be one of {sorted(_VALID_SCOPES)}")
    if not isinstance(config, dict):
        return _validation_error("config must be an object")

    if backend_type in _PRO_GATED_TYPES and not await _hybrid_targets_entitled():
        return (
            jsonify(
                {
                    "status": "error",
                    "error": (
                        f"'{backend_type}' fleet backends require a Professional (or higher) "
                        "license entitlement (hybrid_targets)"
                    ),
                }
            ),
            403,
        )

    credentials_ref = encrypt_credential(credentials) if credentials else None

    def _create():
        existing = (
            db((db.fleet_backends.org_id == org_id) & (db.fleet_backends.name == name))
            .select()
            .first()
        )
        if existing:
            return "name_conflict", None

        backend_id = db.fleet_backends.insert(
            org_id=org_id,
            name=name,
            type=backend_type,
            mode=mode,
            management_scope=management_scope,
            config=config,
            credentials_ref=credentials_ref,
            status="pending",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.commit()
        return "ok", db(db.fleet_backends.id == backend_id).select().first()

    status, row = await asyncio.to_thread(_create)
    if status == "name_conflict":
        return (
            jsonify(
                {
                    "status": "error",
                    "error": f"a fleet backend named '{name}' already exists for this org",
                }
            ),
            409,
        )

    return {
        "status": "success",
        "data": _backend_to_dict(row),
        "meta": {"action": "created", "timestamp": _now()},
    }, 201


@api_v1_bp.route("/fleet/backends/<int:backend_id>", methods=["GET"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(FleetBackendResponse, 200)
async def get_fleet_backend(backend_id: int):
    """Fetch one registered fleet backend -- 403 across orgs, 404 if it never existed."""
    org_id = g.user.get("organization_id")
    if not _fleet_v2_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404

    row, outcome = await asyncio.to_thread(_get_org_scoped_backend, backend_id, org_id)
    if outcome == "not_found":
        return jsonify({"status": "error", "error": "fleet backend not found"}), 404
    if outcome == "forbidden":
        return jsonify({"status": "error", "error": "forbidden"}), 403

    return {"status": "success", "data": _backend_to_dict(row), "meta": {"timestamp": _now()}}, 200


@api_v1_bp.route("/fleet/backends/<int:backend_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(FleetBackendActionResponse, 200)
@validate_request(UpdateFleetBackendRequest)
async def update_fleet_backend(backend_id: int, data: UpdateFleetBackendRequest):
    """Update a registered fleet backend's mutable fields.

    ``type`` is immutable after creation (changing it would silently
    reinterpret ``config``/``credentials_ref`` for a different backend
    class) -- delete and recreate to change type.
    """
    org_id = g.user.get("organization_id")
    if not _fleet_v2_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404

    row, outcome = await asyncio.to_thread(_get_org_scoped_backend, backend_id, org_id)
    if outcome == "not_found":
        return jsonify({"status": "error", "error": "fleet backend not found"}), 404
    if outcome == "forbidden":
        return jsonify({"status": "error", "error": "forbidden"}), 403

    update_fields: dict[str, Any] = {}
    if data.name is not None:
        name = data.name.strip()
        if not name or len(name) > 255:
            return _validation_error("name must be 1-255 characters")
        update_fields["name"] = name
    if data.mode is not None:
        update_fields["mode"] = data.mode
    if data.management_scope is not None:
        if data.management_scope not in _VALID_SCOPES:
            return _validation_error(f"management_scope must be one of {sorted(_VALID_SCOPES)}")
        update_fields["management_scope"] = data.management_scope
    if data.config is not None:
        if not isinstance(data.config, dict):
            return _validation_error("config must be an object")
        update_fields["config"] = data.config
    if data.credentials:
        update_fields["credentials_ref"] = encrypt_credential(data.credentials)
    if data.status is not None:
        if data.status not in _VALID_STATUSES:
            return _validation_error(_STATUS_ERROR)
        update_fields["status"] = data.status

    if update_fields:
        update_fields["updated_at"] = datetime.utcnow()

    def _update():
        if update_fields:
            db(db.fleet_backends.id == backend_id).update(**update_fields)
            db.commit()
        return db(db.fleet_backends.id == backend_id).select().first()

    updated = await asyncio.to_thread(_update)
    return {
        "status": "success",
        "data": _backend_to_dict(updated),
        "meta": {"action": "updated", "timestamp": _now()},
    }, 200


@api_v1_bp.route("/fleet/backends/<int:backend_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(DeleteFleetBackendResponse, 200)
async def delete_fleet_backend(backend_id: int):
    """Delete a registered fleet backend (deployment rows keep their FK, set NULL)."""
    org_id = g.user.get("organization_id")
    if not _fleet_v2_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404

    row, outcome = await asyncio.to_thread(_get_org_scoped_backend, backend_id, org_id)
    if outcome == "not_found":
        return jsonify({"status": "error", "error": "fleet backend not found"}), 404
    if outcome == "forbidden":
        return jsonify({"status": "error", "error": "forbidden"}), 403

    def _delete():
        db(db.fleet_backends.id == backend_id).delete()
        db.commit()

    await asyncio.to_thread(_delete)
    return {
        "status": "success",
        "data": {"id": backend_id},
        "meta": {"action": "deleted", "timestamp": _now()},
    }, 200


@api_v1_bp.route("/fleet/backends/<int:backend_id>/health", methods=["GET"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(FleetBackendHealthResponse, 200)
async def check_fleet_backend_health(backend_id: int):
    """Health-check a registered backend through the ``InferenceFleetBackend`` interface.

    Constructed fresh via ``shared.fleet.registry.build_backend`` (the
    single chokepoint every backend type shares) rather than a bespoke
    per-type health check -- this route works unmodified for any future
    backend type the registry supports.
    """
    org_id = g.user.get("organization_id")
    if not _fleet_v2_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404

    row, outcome = await asyncio.to_thread(_get_org_scoped_backend, backend_id, org_id)
    if outcome == "not_found":
        return jsonify({"status": "error", "error": "fleet backend not found"}), 404
    if outcome == "forbidden":
        return jsonify({"status": "error", "error": "forbidden"}), 403

    try:
        backend = build_backend(db, row)
        health = await backend.health()
    except Exception as exc:
        logger.warning("fleet backend health check failed for id=%s: %s", backend_id, exc)
        return {
            "status": "success",
            "data": {
                "backend_id": backend_id,
                "healthy": False,
                "node_count": 0,
                "detail": {"error": str(exc)},
            },
            "meta": {"timestamp": _now()},
        }, 200

    return {
        "status": "success",
        "data": {
            "backend_id": health.backend_id,
            "healthy": health.healthy,
            "node_count": health.node_count,
            "detail": health.detail,
        },
        "meta": {"timestamp": _now()},
    }, 200
