"""WaddleAI Management API v1 -- tenant-owned BYOK provider-credential CRUD (failover spec §4).

Split out of ``routing_destinations.py`` to keep both modules under the house
25,000-char limit; shares that module's gate, org resolution, and masking
helpers rather than duplicating them. Same two-layer Enterprise gate,
IDOR-safe 404s, and S4 masking rules -- see that module's docstring.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from quart import g, request
from quart_schema import security_scheme, tag, validate_request, validate_response

from shared.auth.rbac import Permission

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth, require_scope
from .routing_destinations import (
    _BEARER_AUTH,
    _err,
    _gate,
    _has_provider_admin,
    _mask_material,
    _org_mismatch_response,
    _resolve_org,
    _validate_material,
)


def _credential_to_dict(row: Any, provider_type: str) -> dict[str, Any]:
    """Serialise a provider_credentials row -- api_key is NEVER returned, only its mask (S4)."""
    return {
        "id": row.id,
        "provider_id": row.provider_id,
        "label": row.label,
        "api_key_masked": _mask_material(provider_type, row.api_key),
        "owner_org_id": row.owner_org_id,
        "enabled": row.enabled,
    }


@dataclass(slots=True)
class DestinationCredentialDTO:
    """A BYOK provider_credentials row -- api_key_masked only, never plaintext."""

    id: int
    provider_id: int
    label: str
    api_key_masked: str
    owner_org_id: int | None
    enabled: bool


@dataclass(slots=True)
class DestinationCredentialListResponse:
    """GET /routing/destination-credentials body."""

    credentials: list[DestinationCredentialDTO]
    total: int


@dataclass(slots=True)
class CreateDestinationCredentialRequest:
    """Request body for POST /api/v1/routing/destination-credentials."""

    provider_id: int | None = None
    label: str | None = None
    material: str | None = None
    enabled: bool | None = None
    organization_id: int | None = None


@dataclass(slots=True)
class DeletedDestinationCredentialResponse:
    """Response body for a successful destination-credential deletion."""

    id: int
    deleted: bool


@api_v1_bp.route("/routing/destination-credentials", methods=["GET"])
@tag(["Routing", "Destination Credentials"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(DestinationCredentialListResponse, 200)
async def list_destination_credentials():
    """List this org's BYOK provider_credentials rows -- api_key_masked only (S4)."""
    org_id = g.user.get("organization_id")
    gate_error = await _gate(org_id)
    if gate_error:
        return gate_error

    requested_org = request.args.get("organization_id", type=int)
    resolved_org, err = _resolve_org(g.user, requested_org, _has_provider_admin(g.user))
    if err:
        return _org_mismatch_response()

    def _fetch():
        rows = db(db.provider_credentials.owner_org_id == resolved_org).select(
            orderby=db.provider_credentials.id
        )
        types: dict[int, str] = {}
        for row in rows:
            if row.provider_id not in types:
                provider = db(db.ai_providers.id == row.provider_id).select().first()
                types[row.provider_id] = provider.provider_type if provider else "openai"
        return rows, types

    rows, types = await asyncio.to_thread(_fetch)
    credentials = [_credential_to_dict(r, types.get(r.provider_id, "openai")) for r in rows]

    return {"credentials": credentials, "total": len(credentials)}


@api_v1_bp.route("/routing/destination-credentials", methods=["POST"])
@tag(["Routing", "Destination Credentials"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.MODEL_DESTINATION_WRITE)
@validate_response(DestinationCredentialDTO, 201)
@validate_request(CreateDestinationCredentialRequest)
async def create_destination_credential(data: CreateDestinationCredentialRequest):
    """Create a tenant-owned BYOK credential -- Fernet-encrypted, material validated by type."""
    from shared.security.credential_encryption import encrypt_credential

    org_id = g.user.get("organization_id")
    gate_error = await _gate(org_id)
    if gate_error:
        return gate_error

    resolved_org, err = _resolve_org(g.user, data.organization_id, _has_provider_admin(g.user))
    if err:
        return _org_mismatch_response()

    label = (data.label or "").strip()
    if not label:
        return _err("label is required", 400)
    if len(label) > 255:
        return _err("label must be <= 255 characters", 400)
    if data.provider_id is None:
        return _err("provider_id is required", 400)
    material = data.material or ""
    provider_id = data.provider_id
    enabled = data.enabled if data.enabled is not None else True

    def _create():
        provider = db(db.ai_providers.id == provider_id).select().first()
        if not provider:
            return "provider_not_found", None

        error = _validate_material(provider.provider_type, material)
        if error:
            return "invalid_material", error

        now = datetime.utcnow()
        cred_id = db.provider_credentials.insert(
            provider_id=provider_id,
            label=label,
            api_key=encrypt_credential(material),
            org_id=None,
            account_meta=None,
            weight=100,
            enabled=enabled,
            request_count=0,
            token_count=0,
            owner_org_id=resolved_org,
            created_at=now,
            updated_at=now,
        )
        db.commit()
        created = db(db.provider_credentials.id == cred_id).select().first()
        return "ok", (created, provider.provider_type)

    outcome, payload = await asyncio.to_thread(_create)

    if outcome == "provider_not_found":
        return _err("Provider not found", 422)
    if outcome == "invalid_material":
        return _err(payload, 400)

    row, provider_type = payload
    return _credential_to_dict(row, provider_type), 201


@api_v1_bp.route("/routing/destination-credentials/<int:credential_id>", methods=["DELETE"])
@tag(["Routing", "Destination Credentials"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.MODEL_DESTINATION_DELETE)
@validate_response(DeletedDestinationCredentialResponse, 200)
async def delete_destination_credential(credential_id: int):
    """Delete a BYOK credential (org-scoped, IDOR-safe 404). Referencing destinations SET NULL."""
    org_id = g.user.get("organization_id")
    gate_error = await _gate(org_id)
    if gate_error:
        return gate_error

    requested_org = request.args.get("organization_id", type=int)
    resolved_org, err = _resolve_org(g.user, requested_org, _has_provider_admin(g.user))
    if err:
        return _org_mismatch_response()

    def _delete():
        row = (
            db(
                (db.provider_credentials.id == credential_id)
                & (db.provider_credentials.owner_org_id == resolved_org)
            )
            .select()
            .first()
        )
        if not row:
            return False
        db(db.provider_credentials.id == credential_id).delete()
        db.commit()
        return True

    found = await asyncio.to_thread(_delete)
    if not found:
        return _err("Credential not found", 404)

    return {"id": credential_id, "deleted": True}
