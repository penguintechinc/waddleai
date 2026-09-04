"""WaddleAI Management API v1 -- provider-destination + BYOK-credential CRUD (failover spec §4).

Two-layer Enterprise gate (``waddleai.provider_failover`` flag -> 404 when off;
``waddleai_provider_failover`` entitlement -> 403; fail-closed). Org from the validated
JWT only; cross-org requires PROVIDER_ADMIN (else 403 on mismatch). Rows addressed by id
outside the resolved org resolve to 404 (IDOR-safe). Ownership enforced at write (422);
<=5 enabled destinations per (org, model). Credential material is Fernet-encrypted and
never returned/logged -- responses carry masked labels only (per provider type).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from penguin_dal.db import DB
from quart import g, jsonify, request
from quart_schema import security_scheme, tag, validate_request, validate_response

from shared.auth.rbac import Permission
from shared.routing.failover_gate import FAILOVER_FLAG_KEY, FAILOVER_LICENSE_FEATURE
from shared.utils.feature_flags import is_feature_enabled

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth, require_scope
from .providers import _mask_key

logger = logging.getLogger(__name__)

_BEARER_AUTH: list[dict[str, list[str]]] = [{"bearerAuth": []}]
MAX_DESTINATIONS_PER_MODEL = 5
_BEARER_TYPES = frozenset(
    {"openai", "anthropic", "gemini", "xai", "azure_openai", "cohere", "llamacpp"}
)
_license_client: Any = None

_CREATE_ERRORS = {
    "provider_not_found": ("Provider not found", 422),
    "provider_disabled": ("Provider is disabled", 422),
    "credential_not_found": ("credential not found", 422),
    "priority_conflict": ("priority already in use for this model", 409),
    "cap_exceeded": (f"at most {MAX_DESTINATIONS_PER_MODEL} enabled destinations per model", 422),
}
_UPDATE_ERRORS = {
    "not_found": ("Destination not found", 404),
    "credential_not_found": ("credential not found", 422),
    "priority_conflict": ("priority already in use for this model", 409),
    "cap_exceeded": (f"at most {MAX_DESTINATIONS_PER_MODEL} enabled destinations per model", 422),
    "no_fields": ("No valid fields to update", 400),
}


def _get_license_client() -> Any:
    """Lazily construct the shared penguin_licensing client (product must be 'waddleai')."""
    global _license_client
    if _license_client is None:
        from penguin_licensing import LicenseClient

        _license_client = LicenseClient(
            license_key=os.environ.get("LICENSE_KEY", ""),
            product="waddleai",
            base_url=os.environ.get("LICENSE_SERVER_URL", "https://license.penguintech.io"),
        )
    return _license_client


def _require_db() -> DB:
    """Return the initialised DAL handle for use inside a ``to_thread`` closure.

    ``db`` is typed ``DB | None`` at import time (``extensions.py``), but is
    always non-None by the time a request reaches a route handler --
    ``init_extensions`` runs before blueprints are registered. Narrows the
    type for mypy and fails loudly, rather than silently operating on
    ``None``, if that startup invariant is ever violated.
    """
    if db is None:
        raise RuntimeError("database not initialised")
    return db


async def _gate(org_id: int | None) -> tuple | None:
    """404 when the flag is off, 403 when unentitled, else None. Fail-closed."""
    distinct_id = str(org_id or "server")
    if not is_feature_enabled(FAILOVER_FLAG_KEY, distinct_id=distinct_id, default=False):
        return _err("not_found", 404)

    def _check() -> bool:
        try:
            return bool(_get_license_client().check_feature(FAILOVER_LICENSE_FEATURE))
        except Exception as exc:  # pragma: no cover - defensive, license I/O failure
            logger.warning("routing_destinations: entitlement check failed: %s", exc)
            return False

    if not await asyncio.to_thread(_check):
        return _err(
            "Provider failover requires an Enterprise entitlement (waddleai_provider_failover)",
            403,
        )
    return None


def _has_provider_admin(g_user: dict) -> bool:
    """Whether the caller's token scope grants cross-org access via PROVIDER_ADMIN (S1)."""
    return Permission.PROVIDER_ADMIN.value in set(g_user.get("scope") or [])


def _resolve_org(
    g_user: dict, requested_org: int | None, has_provider_admin: bool
) -> tuple[int | None, int | None]:
    """Resolve the effective org; cross-org needs PROVIDER_ADMIN (else 403). Returns (org, err)."""
    raw_token_org = g_user.get("organization_id")
    if raw_token_org is None:
        # Tenant claim missing from an otherwise-authenticated token (S1) --
        # never call int(None); surface the same 403 the mismatch path uses.
        return None, 403
    token_org = int(raw_token_org)
    if requested_org is None or int(requested_org) == token_org:
        return token_org, None
    if has_provider_admin:
        return int(requested_org), None
    return None, 403


def _err(message: str, status: int) -> tuple:
    """Shorthand for a ``{"status": "error", "error": message}`` JSON response."""
    return jsonify({"status": "error", "error": message}), status


def _org_mismatch_response() -> tuple:
    """403 body for a cross-org ``organization_id`` override without PROVIDER_ADMIN (S1)."""
    return _err("organization_id does not match token", 403)


def _mask_material(provider_type: str, stored: str | None, cred_id: int | None = None) -> str:
    """Decrypt then mask credential material per provider type (S4).

    Management holds the encryption key (same ``decrypt_credential`` helper
    the proxy uses at dispatch time) -- masking the stored Fernet ciphertext
    directly would mask meaningless bytes, not the real key. Bearer types
    mask the plaintext via ``_mask_key``; bedrock parses the plaintext JSON
    and shows only a masked ``aws_access_key_id``, never the secret or the
    raw JSON. Any decrypt failure (wrong/missing key, corrupt ciphertext)
    degrades to a fixed placeholder; only ``cred_id`` is logged, never
    material.
    """
    if not stored:
        return ""

    from shared.security.credential_encryption import decrypt_credential

    try:
        plaintext = decrypt_credential(stored)
    except Exception as exc:
        logger.warning(
            "routing_destinations: credential decrypt failed for masking (cred_id=%s): %s",
            cred_id,
            exc,
        )
        return "****"

    if provider_type == "bedrock":
        try:
            parsed = json.loads(plaintext)
        except (ValueError, TypeError):
            return "****"
        akid = parsed.get("aws_access_key_id") if isinstance(parsed, dict) else None
        return f"aws_access_key_id={_mask_key(akid)}" if akid else "****"
    return _mask_key(plaintext)


def _validate_ownership(cred_row: Any, dest_provider_id: int, org_id: int) -> str | None:
    """S2 write invariant: same provider AND (platform OR same-org) credential; else an error."""
    cred_provider = cred_row["provider_id"] if isinstance(cred_row, dict) else cred_row.provider_id
    owner = cred_row["owner_org_id"] if isinstance(cred_row, dict) else cred_row.owner_org_id
    if cred_provider != dest_provider_id:
        return "credential.provider_id must match the destination's provider_id"
    if owner is not None and int(owner) != int(org_id):
        return "credential is owned by another org"
    return None


def _validate_material(provider_type: str, material: str) -> str | None:
    """Validate BYOK credential material by provider type (create-time, S2); None on success."""
    if provider_type == "bedrock":
        try:
            parsed = json.loads(material)
        except (ValueError, TypeError):
            return "material must be a JSON object with aws_access_key_id/aws_secret_access_key"
        if (
            not isinstance(parsed, dict)
            or not parsed.get("aws_access_key_id")
            or not parsed.get("aws_secret_access_key")
        ):
            return "material must include aws_access_key_id and aws_secret_access_key"
        return None
    if provider_type in _BEARER_TYPES:
        if not material or not material.strip():
            return "material is required"
        return None
    accepted = ", ".join((*sorted(_BEARER_TYPES), "bedrock"))
    return f"unsupported provider type '{provider_type}'; accepted: {accepted}"


def _count_enabled_sync(org_id: int, model: str) -> int:
    """Sync core of the enabled-destination count -- safe to call from within a to_thread body."""
    database = _require_db()
    return database(
        (database.model_destinations.organization_id == org_id)
        & (database.model_destinations.model == model)
        & (database.model_destinations.enabled == True)  # noqa: E712
    ).count()


async def _count_enabled(org_id: int, model: str) -> int:
    """Count enabled destinations for (org, model) -- for the <=5 cap (S7)."""
    return await asyncio.to_thread(_count_enabled_sync, org_id, model)


def _destination_to_dict(row: Any, credential_label: str | None) -> dict[str, Any]:
    """Serialise a model_destinations row -- credential material is never included."""
    return {
        "id": row.id,
        "organization_id": row.organization_id,
        "model": row.model,
        "priority": row.priority,
        "provider_id": row.provider_id,
        "credential_id": row.credential_id,
        "credential_label": credential_label,
        "provider_model_id": row.provider_model_id,
        "region": row.region,
        "timeout_seconds": row.timeout_seconds,
        "enabled": row.enabled,
    }


# ---------------------------------------------------------------------------
# OpenAPI request/response DTOs
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DestinationDTO:
    """One destination row for API responses -- never a credential secret."""

    id: int
    organization_id: int
    model: str
    priority: int
    provider_id: int
    credential_id: int | None
    credential_label: str | None
    provider_model_id: str | None
    region: str | None
    timeout_seconds: int | None
    enabled: bool


@dataclass(slots=True)
class DestinationListResponse:
    """GET /routing/destinations body."""

    destinations: list[DestinationDTO]
    total: int


@dataclass(slots=True)
class CreateDestinationRequest:
    """Request body for POST /api/v1/routing/destinations."""

    model: str | None = None
    priority: int | None = None
    provider_id: int | None = None
    credential_id: int | None = None
    provider_model_id: str | None = None
    region: str | None = None
    timeout_seconds: int | None = None
    enabled: bool | None = None
    organization_id: int | None = None


@dataclass(slots=True)
class UpdateDestinationRequest:
    """Request body for PATCH /api/v1/routing/destinations/<id> (partial update)."""

    priority: int | None = None
    enabled: bool | None = None
    provider_model_id: str | None = None
    region: str | None = None
    timeout_seconds: int | None = None
    credential_id: int | None = None


@dataclass(slots=True)
class DeletedDestinationResponse:
    """Response body for a successful destination deletion."""

    id: int
    deleted: bool


# ---------------------------------------------------------------------------
# Destinations: GET / POST /routing/destinations, PATCH / DELETE .../<id>
# ---------------------------------------------------------------------------


@api_v1_bp.route("/routing/destinations", methods=["GET"])
@tag(["Routing", "Destinations"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(DestinationListResponse, 200)
async def list_destinations():
    """List this org's model_destinations rows, optionally filtered by ?model=."""
    org_id = g.user.get("organization_id")
    gate_error = await _gate(org_id)
    if gate_error:
        return gate_error

    requested_org = request.args.get("organization_id", type=int)
    resolved_org, err = _resolve_org(g.user, requested_org, _has_provider_admin(g.user))
    if err:
        return _org_mismatch_response()

    model_filter = request.args.get("model")

    def _fetch():
        database = _require_db()
        query = database.model_destinations.organization_id == resolved_org
        if model_filter:
            query &= database.model_destinations.model == model_filter
        order = (database.model_destinations.model, database.model_destinations.priority)
        rows = database(query).select(orderby=order)
        labels: dict[int, str | None] = {}
        for row in rows:
            if row.credential_id and row.credential_id not in labels:
                cred = (
                    database(database.provider_credentials.id == row.credential_id).select().first()
                )
                labels[row.credential_id] = cred.label if cred else None
        return rows, labels

    rows, labels = await asyncio.to_thread(_fetch)
    destinations = [_destination_to_dict(r, labels.get(r.credential_id)) for r in rows]

    return {"destinations": destinations, "total": len(destinations)}


@api_v1_bp.route("/routing/destinations", methods=["POST"])
@tag(["Routing", "Destinations"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.MODEL_DESTINATION_WRITE)
@validate_response(DestinationDTO, 201)
@validate_request(CreateDestinationRequest)
async def create_destination(data: CreateDestinationRequest):
    """Create a model_destinations row (spec §3.2: ownership, <=5 enabled, provider enabled)."""
    org_id = g.user.get("organization_id")
    gate_error = await _gate(org_id)
    if gate_error:
        return gate_error

    resolved_org, err = _resolve_org(g.user, data.organization_id, _has_provider_admin(g.user))
    if err:
        return _org_mismatch_response()

    model = (data.model or "").strip()
    if not model:
        return _err("model is required", 400)
    if data.provider_id is None:
        return _err("provider_id is required", 400)
    priority = data.priority if data.priority is not None else 0
    if not isinstance(priority, int) or priority < 0:
        return _err("priority must be an integer >= 0", 400)
    timeout_seconds = data.timeout_seconds
    if timeout_seconds is not None and not (1 <= timeout_seconds <= 600):
        return _err("timeout_seconds must be between 1 and 600", 400)
    enabled = data.enabled if data.enabled is not None else True
    provider_id = data.provider_id
    credential_id = data.credential_id

    def _create():
        database = _require_db()
        provider = database(database.ai_providers.id == provider_id).select().first()
        if not provider:
            return "provider_not_found", None
        if not provider.enabled:
            return "provider_disabled", None

        if credential_id is not None:
            cred = database(database.provider_credentials.id == credential_id).select().first()
            if not cred:
                return "credential_not_found", None
            error = _validate_ownership(cred, provider_id, resolved_org)
            if error:
                return "ownership", error

        conflict = (
            database(
                (database.model_destinations.organization_id == resolved_org)
                & (database.model_destinations.model == model)
                & (database.model_destinations.priority == priority)
            )
            .select()
            .first()
        )
        if conflict:
            return "priority_conflict", None

        if enabled and _count_enabled_sync(resolved_org, model) >= MAX_DESTINATIONS_PER_MODEL:
            return "cap_exceeded", None

        now = datetime.utcnow()
        new_id = database.model_destinations.insert(
            organization_id=resolved_org,
            model=model,
            priority=priority,
            provider_id=provider_id,
            credential_id=credential_id,
            provider_model_id=data.provider_model_id,
            region=data.region,
            timeout_seconds=timeout_seconds,
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        database.commit()
        return "ok", database(database.model_destinations.id == new_id).select().first()

    outcome, payload = await asyncio.to_thread(_create)

    if outcome == "ownership":
        return _err(payload, 422)
    if outcome in _CREATE_ERRORS:
        message, status = _CREATE_ERRORS[outcome]
        return _err(message, status)

    row = payload
    label = None
    if row.credential_id:

        def _fetch_label() -> str | None:
            database = _require_db()
            cred = database(database.provider_credentials.id == row.credential_id).select().first()
            return cred.label if cred else None

        label = await asyncio.to_thread(_fetch_label)
    return _destination_to_dict(row, label), 201


@api_v1_bp.route("/routing/destinations/<int:destination_id>", methods=["PATCH"])
@tag(["Routing", "Destinations"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.MODEL_DESTINATION_WRITE)
@validate_response(DestinationDTO, 200)
@validate_request(UpdateDestinationRequest)
async def update_destination(destination_id: int, data: UpdateDestinationRequest):
    """Update priority/enabled/provider_model_id/region/timeout/credential on a destination."""
    org_id = g.user.get("organization_id")
    gate_error = await _gate(org_id)
    if gate_error:
        return gate_error

    requested_org = request.args.get("organization_id", type=int)
    resolved_org, err = _resolve_org(g.user, requested_org, _has_provider_admin(g.user))
    if err:
        return _org_mismatch_response()

    if data.priority is not None and data.priority < 0:
        return _err("priority must be an integer >= 0", 400)
    if data.timeout_seconds is not None and not (1 <= data.timeout_seconds <= 600):
        return _err("timeout_seconds must be between 1 and 600", 400)

    def _update():
        database = _require_db()
        existing = (
            database(
                (database.model_destinations.id == destination_id)
                & (database.model_destinations.organization_id == resolved_org)
            )
            .select()
            .first()
        )
        if not existing:
            return "not_found", None

        update_fields: dict[str, Any] = {}

        if data.credential_id is not None:
            cred = database(database.provider_credentials.id == data.credential_id).select().first()
            if not cred:
                return "credential_not_found", None
            error = _validate_ownership(cred, existing.provider_id, resolved_org)
            if error:
                return "ownership", error
            update_fields["credential_id"] = data.credential_id

        if data.priority is not None and data.priority != existing.priority:
            conflict = (
                database(
                    (database.model_destinations.organization_id == resolved_org)
                    & (database.model_destinations.model == existing.model)
                    & (database.model_destinations.priority == data.priority)
                    & (database.model_destinations.id != destination_id)
                )
                .select()
                .first()
            )
            if conflict:
                return "priority_conflict", None
            update_fields["priority"] = data.priority

        if data.enabled is not None:
            if data.enabled and not existing.enabled:
                if _count_enabled_sync(resolved_org, existing.model) >= MAX_DESTINATIONS_PER_MODEL:
                    return "cap_exceeded", None
            update_fields["enabled"] = data.enabled

        if data.provider_model_id is not None:
            update_fields["provider_model_id"] = data.provider_model_id
        if data.region is not None:
            update_fields["region"] = data.region
        if data.timeout_seconds is not None:
            update_fields["timeout_seconds"] = data.timeout_seconds

        if not update_fields:
            return "no_fields", None

        update_fields["updated_at"] = datetime.utcnow()
        database(database.model_destinations.id == destination_id).update(**update_fields)
        database.commit()
        return "ok", database(database.model_destinations.id == destination_id).select().first()

    outcome, result = await asyncio.to_thread(_update)

    if outcome == "ownership":
        return _err(result, 422)
    if outcome in _UPDATE_ERRORS:
        message, status = _UPDATE_ERRORS[outcome]
        return _err(message, status)

    row = result
    label = None
    if row.credential_id:

        def _fetch_label() -> str | None:
            database = _require_db()
            cred = database(database.provider_credentials.id == row.credential_id).select().first()
            return cred.label if cred else None

        label = await asyncio.to_thread(_fetch_label)
    return _destination_to_dict(row, label)


@api_v1_bp.route("/routing/destinations/<int:destination_id>", methods=["DELETE"])
@tag(["Routing", "Destinations"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.MODEL_DESTINATION_DELETE)
@validate_response(DeletedDestinationResponse, 200)
async def delete_destination(destination_id: int):
    """Delete a model_destinations row (org-scoped, IDOR-safe 404)."""
    org_id = g.user.get("organization_id")
    gate_error = await _gate(org_id)
    if gate_error:
        return gate_error

    requested_org = request.args.get("organization_id", type=int)
    resolved_org, err = _resolve_org(g.user, requested_org, _has_provider_admin(g.user))
    if err:
        return _org_mismatch_response()

    def _delete():
        database = _require_db()
        row = (
            database(
                (database.model_destinations.id == destination_id)
                & (database.model_destinations.organization_id == resolved_org)
            )
            .select()
            .first()
        )
        if not row:
            return False
        database(database.model_destinations.id == destination_id).delete()
        database.commit()
        return True

    found = await asyncio.to_thread(_delete)
    if not found:
        return _err("Destination not found", 404)

    return {"id": destination_id, "deleted": True}
