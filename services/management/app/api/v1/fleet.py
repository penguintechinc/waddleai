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
from quart import g, jsonify, request
from quart_schema import validate_request, validate_response

from shared.auth.rbac import Permission
from shared.fleet.azure_ai import validate_endpoint_url as _validate_azure_endpoint
from shared.fleet.base import BackendType, ManagementScope
from shared.fleet.registry import build_backend
from shared.security.credential_encryption import encrypt_credential
from shared.utils.feature_flags import is_feature_enabled

from ...extensions import db
from ...services import fleet_secrets
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
FLEET_VLLM_RAY_FLAG = "waddleai.fleet-vllm-ray"
_HYBRID_TARGETS_FEATURE = "hybrid_targets"

# Backend types requiring the two-layer Pro gate (flag AND check_feature) at
# creation -- Ollama/llama.cpp/EXO are available at any tier once fleet_v2
# is on (spec §2.4 "Deployment targets: K8s/local only" is Free-tier; Vertex
# AI/Bedrock are the Professional-gated "+ hybrid" row).
_PRO_GATED_TYPES = frozenset(
    {
        BackendType.VERTEX_AI.value,
        BackendType.BEDROCK.value,
        BackendType.AZURE_AI.value,
        BackendType.OLLAMA_CLOUD.value,
    }
)

# Public backend types. Each entry has a concrete lazy-registered backend in
# ``shared.fleet.registry``; keeping this explicit prevents an enum addition
# from accidentally creating an unusable API surface.
_VALID_TYPES = frozenset(
    {
        BackendType.OLLAMA.value,
        BackendType.LLAMACPP.value,
        BackendType.EXO.value,
        BackendType.VERTEX_AI.value,
        BackendType.BEDROCK.value,
        BackendType.AZURE_AI.value,
        BackendType.OLLAMA_CLOUD.value,
        BackendType.VLLM_RAY.value,
    }
)
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


def _vllm_ray_enabled(org_id: int) -> bool:
    """True only when both ``waddleai.fleet_v2`` and ``waddleai.fleet-vllm-ray`` are on."""
    return _fleet_v2_enabled(org_id) and is_feature_enabled(
        FLEET_VLLM_RAY_FLAG, distinct_id=str(org_id), default=False
    )


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
    """Return an org-scoped ``fleet_backends`` row.

    Another org's backend is reported as ``not_found`` -- identical to a missing id -- so the
    API is not an existence oracle across tenants.
    """
    database = _db()
    row = database(database.fleet_backends.id == backend_id).select().first()
    if row is None or row.org_id != org_id:
        return None, "not_found"
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

    if data.type == BackendType.VLLM_RAY.value and not _vllm_ray_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404

    name = (data.name or "").strip()
    backend_type = data.type
    mode = data.mode
    management_scope = data.management_scope or (
        ManagementScope.REGISTER_AND_ROUTE.value
        if backend_type == BackendType.OLLAMA_CLOUD.value
        else ManagementScope.FULL_LIFECYCLE.value
    )
    config = data.config if data.config is not None else {}
    credentials = data.credentials

    if not name or len(name) > 255:
        return _validation_error("name is required and must be <= 255 characters")
    if backend_type not in _VALID_TYPES:
        return _validation_error(f"type must be one of {sorted(_VALID_TYPES)}")
    if management_scope not in _VALID_SCOPES:
        return _validation_error(f"management_scope must be one of {sorted(_VALID_SCOPES)}")
    if (
        backend_type == BackendType.OLLAMA_CLOUD.value
        and management_scope != ManagementScope.REGISTER_AND_ROUTE.value
    ):
        return _validation_error("ollama_cloud only supports management_scope='register_and_route'")
    if not isinstance(config, dict):
        return _validation_error("config must be an object")
    if backend_type == BackendType.AZURE_AI.value and config.get("endpoint_url"):
        try:
            _validate_azure_endpoint(str(config["endpoint_url"]))
        except ValueError as exc:
            return _validation_error(str(exc))

    if backend_type == BackendType.VLLM_RAY.value:
        urls = [config.get("head_url"), config.get("serve_base_url")]
        if not all(isinstance(u, str) and u.startswith(("http://", "https://")) for u in urls):
            return _validation_error(
                "config.head_url and config.serve_base_url are required for vllm_ray "
                "(http or https URLs)"
            )

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
    """Fetch one registered fleet backend -- 404 for another org's backend and for a missing one."""
    org_id = g.user.get("organization_id")
    if not _fleet_v2_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404

    row, outcome = await asyncio.to_thread(_get_org_scoped_backend, backend_id, org_id)
    if outcome == "not_found":
        return jsonify({"status": "error", "error": "fleet backend not found"}), 404

    return {"status": "success", "data": _backend_to_dict(row), "meta": {"timestamp": _now()}}, 200


@api_v1_bp.route("/fleet/backends/<int:backend_id>", methods=["PUT"])
@require_auth
@require_scope(Permission.FLEET_ADMIN)
@validate_response(FleetBackendActionResponse, 200)
async def update_fleet_backend(backend_id: int):
    """Update a registered fleet backend's mutable fields.

    ``type`` is immutable after creation (changing it would silently
    reinterpret ``config``/``credentials_ref`` for a different backend
    class) -- delete and recreate to change type.

    Body validation runs in-handler, AFTER the flag/existence
    checks, so a malformed body against a nonexistent or foreign-org backend
    returns 404 rather than 400 -- preserving the deliberate
    existence/authz-before-input-validation order that
    tests/contract/test_management_mutations.py locks. A @validate_request
    decorator would run before the handler and invert that order
    (regression: audit-2026-09-14-wave2).
    """
    org_id = g.user.get("organization_id")
    if not _fleet_v2_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404

    row, outcome = await asyncio.to_thread(_get_org_scoped_backend, backend_id, org_id)
    if outcome == "not_found":
        return jsonify({"status": "error", "error": "fleet backend not found"}), 404

    body = (await request.get_json(silent=True)) or {}
    update_fields: dict[str, Any] = {}
    if body.get("name") is not None:
        name = str(body["name"]).strip()
        if not name or len(name) > 255:
            return _validation_error("name must be 1-255 characters")
        update_fields["name"] = name
    if body.get("mode") is not None:
        update_fields["mode"] = body["mode"]
    if body.get("management_scope") is not None:
        if body["management_scope"] not in _VALID_SCOPES:
            return _validation_error(f"management_scope must be one of {sorted(_VALID_SCOPES)}")
        update_fields["management_scope"] = body["management_scope"]
    if body.get("config") is not None:
        if not isinstance(body["config"], dict):
            return _validation_error("config must be an object")
        if row.type == BackendType.AZURE_AI.value and body["config"].get("endpoint_url"):
            try:
                _validate_azure_endpoint(str(body["config"]["endpoint_url"]))
            except ValueError as exc:
                return _validation_error(str(exc))
        update_fields["config"] = body["config"]
    if body.get("credentials"):
        update_fields["credentials_ref"] = encrypt_credential(body["credentials"])
    if body.get("status") is not None:
        if body["status"] not in _VALID_STATUSES:
            return _validation_error(_STATUS_ERROR)
        update_fields["status"] = body["status"]

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


# ---------------------------------------------------------------------------
# `/api/v1/fleet/secrets` -- cluster key list + rotation (AM13, AM15; values never returned)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FleetSecretItem:
    """A rotatable cluster secret: identity and fingerprint only, never the value."""

    name: str
    source: str
    fingerprint: str | None
    rotated_at: str | None


@dataclass(slots=True)
class FleetSecretListResponse:
    """Response body for GET /api/v1/fleet/secrets."""

    status: str
    data: list[FleetSecretItem]
    meta: TimestampMeta


@dataclass(slots=True)
class FleetSecretResponse:
    """Response body for POST /api/v1/fleet/secrets/<name>/rotate."""

    status: str
    data: FleetSecretItem
    meta: ActionMeta


_ROTATION_COOLDOWN_S = 300
_ROTATION_AUDIT_PREFIX = "fleet.secret.rotated/"


def _is_system_operator() -> bool:
    """True for the platform operator: ``system:config`` scope AND the configured system org.

    Cluster-level fleet secrets (Ray token, Ray TLS) are shared by every tenant, so they are NOT
    ``fleet:admin`` territory. ``system:config`` is admin-exclusive in this repo's RBAC, but every
    org's admin holds it, so the token must also belong to the reserved ``system`` tenant, mapped
    to an org id by ``WADDLEAI_SYSTEM_ORG_ID``. Unset or malformed -> nobody qualifies.
    """
    user = getattr(g, "user", None) or {}
    if Permission.SYSTEM_CONFIG.value not in set(user.get("scope") or []):
        return False
    configured = os.environ.get("WADDLEAI_SYSTEM_ORG_ID", "").strip()
    return configured.isdigit() and user.get("organization_id") == int(configured)


def _rotation_audit_rows(name: str) -> Any:
    """Audit rows of ``name``'s rotations, newest first (claims and completed rotations)."""
    database = _db()
    table = database.audit_log
    return database(table.path == f"{_ROTATION_AUDIT_PREFIX}{name}").select(
        orderby=[~table.created_at, ~table.id]
    )


def _rotated_at(name: str) -> str | None:
    """Time of the last successful rotation, from the audit trail (None when never rotated)."""
    for row in _rotation_audit_rows(name):
        if row.status_code == 200 and row.created_at:
            return row.created_at.isoformat()
    return None


def _claim_rotation(name: str, old_fp: str | None) -> tuple[int | None, int]:
    """Reserve a rotation slot: ``(audit_row_id, 0)`` or ``(None, retry_after_seconds)``.

    The claim row is written BEFORE the rotation so concurrent requests see it; it records the
    actor and the old fingerprint, and is completed (new fingerprint) or removed on failure.
    """
    now = datetime.utcnow()
    for row in _rotation_audit_rows(name):
        age = (now - row.created_at).total_seconds() if row.created_at else None
        if age is not None and age < _ROTATION_COOLDOWN_S:
            return None, max(1, int(_ROTATION_COOLDOWN_S - age))
        break
    database = _db()
    row_id = database.audit_log.insert(
        method="POST",
        path=f"{_ROTATION_AUDIT_PREFIX}{name}",
        resource_id=f"{name}:{old_fp}->pending",
        status_code=202,
        user_id=g.user.get("user_id"),
        organization_id=g.user.get("organization_id"),
        created_at=now,
    )
    database.commit()
    return int(row_id), 0


def _finish_rotation(row_id: int, name: str, old_fp: str | None, new_fp: str | None) -> None:
    """Complete a claimed rotation's audit row with the new fingerprint."""
    database = _db()
    database(database.audit_log.id == row_id).update(
        resource_id=f"{name}:{old_fp}->{new_fp}", status_code=200
    )
    database.commit()


def _release_rotation(row_id: int) -> None:
    """Drop a claim whose rotation failed, so a retry is not locked out."""
    database = _db()
    database(database.audit_log.id == row_id).delete()
    database.commit()


@api_v1_bp.route("/fleet/secrets", methods=["GET"])
@require_auth
@validate_response(FleetSecretListResponse, 200)
async def list_fleet_secrets():
    """List rotatable cluster secrets (platform operator only; never the values)."""
    if not _is_system_operator():
        return jsonify({"error": "Insufficient permissions"}), 403
    org_id = g.user.get("organization_id")
    if not _vllm_ray_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404
    infos = await fleet_secrets.list_secrets()
    items = []
    for info in infos:
        rotated = await asyncio.to_thread(_rotated_at, info.name)
        items.append(
            {
                "name": info.name,
                "source": info.source,
                "fingerprint": info.fingerprint,
                "rotated_at": rotated,
            }
        )
    return {"status": "success", "data": items, "meta": {"timestamp": _now()}}, 200


@api_v1_bp.route("/fleet/secrets/<name>/rotate", methods=["POST"])
@require_auth
@validate_response(FleetSecretResponse, 200)
async def rotate_fleet_secret(name: str):
    """Rotate a generated cluster secret and roll the Ray pods (platform operator only).

    An org admin is 403 (these keys are shared by all tenants); an externally managed key is 409;
    at most one rotation per secret per 5 minutes (429); every rotation is audit-logged with the
    actor and the fingerprint change, never a value.
    """
    if not _is_system_operator():
        return jsonify({"error": "Insufficient permissions"}), 403
    org_id = g.user.get("organization_id")
    if not _vllm_ray_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404
    if name not in fleet_secrets.SECRET_NAMES:
        return jsonify({"status": "error", "error": "unknown secret"}), 404
    if fleet_secrets.secret_source(name) == "external":
        return (
            jsonify(
                {"status": "error", "error": "managed externally", "reason": "managed externally"}
            ),
            409,
        )
    current = next((i for i in await fleet_secrets.list_secrets() if i.name == name), None)
    old_fp = current.fingerprint if current else None
    row_id, retry_after = await asyncio.to_thread(_claim_rotation, name, old_fp)
    if row_id is None:
        response = jsonify(
            {"status": "error", "error": "rotated too recently", "retry_after_s": retry_after}
        )
        response.headers["Retry-After"] = str(retry_after)
        return response, 429
    try:
        info = await fleet_secrets.rotate_secret(name)
    except fleet_secrets.ManagedExternally:
        await asyncio.to_thread(_release_rotation, row_id)
        return jsonify({"status": "error", "error": "managed externally"}), 409
    except fleet_secrets.SecretWriteFailed as exc:
        await asyncio.to_thread(_release_rotation, row_id)
        if exc.partially_applied:
            logger.error("fleet.secret.rotation_partial name=%s: restart pending", name)
            return (
                jsonify(
                    {
                        "status": "error",
                        "error": "rotated, restart pending, retry",
                        "reason": "restart pending",
                    }
                ),
                502,
            )
        return (
            jsonify(
                {"status": "error", "error": "secret write failed", "reason": "secret write failed"}
            ),
            502,
        )
    await asyncio.to_thread(_finish_rotation, row_id, name, old_fp, info.fingerprint)
    logger.info(
        "fleet.secret.rotated name=%s actor=%s fingerprint_old=%s fingerprint_new=%s",
        info.name,
        g.user.get("user_id"),
        old_fp,
        info.fingerprint,
    )
    return {
        "status": "success",
        "data": {
            "name": info.name,
            "source": info.source,
            "fingerprint": info.fingerprint,
            "rotated_at": _now(),
        },
        "meta": {"action": "rotated", "timestamp": _now()},
    }, 200
