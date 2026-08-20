"""
WaddleAI Management API v1 - Virtual Key Management Endpoints
"""

import asyncio
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from passlib.hash import bcrypt
from quart import g, jsonify, request
from quart_schema import security_scheme, tag, validate_request, validate_response

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth

_BEARER_AUTH = [{"bearerAuth": []}]


# ---------------------------------------------------------------------------
# OpenAPI request/response models.
#
# Request models make every field Optional with the same default the
# handler's own `data.get(...)` calls already used, so quart-schema's
# automatic validation never fires where the handler's own presence/value
# checks (and their exact error messages) used to be the only gate. See
# auth.py for the same rationale, applied first.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CreateKeyRequest:
    """Request body for POST /api/v1/keys."""

    name: str | None = None
    user_id: int | None = None
    organization_id: int | None = None
    expires_days: int | None = 365
    allowed_models: list[str] | None = None
    allowed_providers: list[str] | None = None
    budget_limit_daily: float | None = None
    budget_limit_monthly: float | None = None
    tpm_limit: int | None = 10000
    rpm_limit: int | None = 60


@dataclass(slots=True)
class UpdateKeyRequest:
    """Request body for PUT /api/v1/keys/<key_id>. Every field is a partial update."""

    name: str | None = None
    allowed_models: list[str] | None = None
    allowed_providers: list[str] | None = None
    budget_limit_daily: float | None = None
    budget_limit_monthly: float | None = None
    tpm_limit: int | None = None
    rpm_limit: int | None = None
    enabled: bool | None = None
    expires_at: str | None = None


@dataclass(slots=True)
class MessageResponse:
    """Generic `{"message": str}` envelope used by several key endpoints."""

    message: str


@dataclass(slots=True)
class KeySummary:
    """Virtual key summary -- never includes the raw secret, only its prefix."""

    id: int
    name: str
    key_prefix: str
    user_id: int
    organization_id: int
    allowed_models: list[str] | None
    allowed_providers: list[str] | None
    budget_limit_daily: float | None
    budget_limit_monthly: float | None
    tpm_limit: int | None
    rpm_limit: int | None
    enabled: bool
    expires_at: str | None
    last_used: str | None
    created_at: str | None


@dataclass(slots=True)
class KeyListResponse:
    """Response body for GET /api/v1/keys."""

    keys: list[KeySummary]
    total: int


@dataclass(slots=True)
class KeyDetailUsage:
    """Aggregate usage embedded in the single-key detail response."""

    daily_tokens: int
    monthly_tokens: int
    monthly_cost_usd: float


@dataclass(slots=True)
class KeyDetailResponse:
    """Response body for GET /api/v1/keys/<key_id>."""

    id: int
    name: str
    key_prefix: str
    user_id: int
    organization_id: int
    allowed_models: list[str] | None
    allowed_providers: list[str] | None
    budget_limit_daily: float | None
    budget_limit_monthly: float | None
    tpm_limit: int | None
    rpm_limit: int | None
    enabled: bool
    expires_at: str | None
    last_used: str | None
    created_at: str | None
    usage: KeyDetailUsage


@dataclass(slots=True)
class CreateKeyResponse:
    """Response body for a successful POST /api/v1/keys.

    The plaintext `api_key` is only ever returned here and on rotate --
    it is not recoverable afterwards, only `key_prefix` is persisted for display.
    """

    id: int
    name: str
    api_key: str
    key_prefix: str
    expires_at: str | None
    message: str


@dataclass(slots=True)
class RotateKeyResponse:
    """Response body for POST /api/v1/keys/<key_id>/rotate."""

    id: int
    api_key: str
    key_prefix: str
    message: str


@dataclass(slots=True)
class KeyUsageDailyEntry:
    """A single day's usage row within the key-usage response."""

    date: str
    waddleai_tokens: int | None
    tokens_input: int | None
    tokens_output: int | None
    request_count: int | None
    cost_usd: float | None


@dataclass(slots=True)
class KeyUsageTotals:
    """Aggregate totals across the requested usage window."""

    waddleai_tokens: int
    requests: int
    cost_usd: float


@dataclass(slots=True)
class KeyUsageResponse:
    """Response body for GET /api/v1/keys/<key_id>/usage."""

    key_id: int
    key_name: str
    period_days: int
    totals: KeyUsageTotals
    daily_usage: list[KeyUsageDailyEntry]


@api_v1_bp.route("/keys", methods=["GET"])
@tag(["Keys"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(KeyListResponse, 200)
async def list_keys():
    """List virtual keys based on user role"""
    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    def _fetch():
        if user_role == "admin":
            return db(db.virtual_keys.id > 0).select()
        elif user_role == "resource_manager":
            return db(db.virtual_keys.organization_id == org_id).select()
        else:
            return db(db.virtual_keys.user_id == user_id).select()

    keys = await asyncio.to_thread(_fetch)

    result = []
    for key in keys:
        result.append(
            {
                "id": key.id,
                "name": key.name,
                "key_prefix": key.key_prefix,
                "user_id": key.user_id,
                "organization_id": key.organization_id,
                "allowed_models": key.allowed_models,
                "allowed_providers": key.allowed_providers,
                "budget_limit_daily": key.budget_limit_daily,
                "budget_limit_monthly": key.budget_limit_monthly,
                "tpm_limit": key.tpm_limit,
                "rpm_limit": key.rpm_limit,
                "enabled": key.enabled,
                "expires_at": key.expires_at.isoformat() if key.expires_at else None,
                "last_used": key.last_used.isoformat() if key.last_used else None,
                "created_at": key.created_at.isoformat() if key.created_at else None,
            }
        )

    return {"keys": result, "total": len(result)}


@api_v1_bp.route("/keys/<int:key_id>", methods=["GET"])
@tag(["Keys"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(KeyDetailResponse, 200)
async def get_key(key_id):
    """Get virtual key details"""
    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    key = await asyncio.to_thread(lambda: db(db.virtual_keys.id == key_id).select().first())

    if not key:
        return jsonify({"error": "Key not found"}), 404

    # Permission check
    if user_role not in ["admin"]:
        if user_role == "resource_manager" and key.organization_id != org_id:
            return jsonify({"error": "Access denied"}), 403
        elif user_role not in ["resource_manager"] and key.user_id != user_id:
            return jsonify({"error": "Access denied"}), 403

    # Get usage stats
    from datetime import date

    today = date.today()
    month_start = today.replace(day=1)

    def _fetch_usage():
        daily = db((db.token_usage.virtual_key_id == key_id) & (db.token_usage.date == today)).select().first()
        monthly = db((db.token_usage.virtual_key_id == key_id) & (db.token_usage.date >= month_start)).select()
        return daily, monthly

    daily_usage, monthly_usage = await asyncio.to_thread(_fetch_usage)

    return {
        "id": key.id,
        "name": key.name,
        "key_prefix": key.key_prefix,
        "user_id": key.user_id,
        "organization_id": key.organization_id,
        "allowed_models": key.allowed_models,
        "allowed_providers": key.allowed_providers,
        "budget_limit_daily": key.budget_limit_daily,
        "budget_limit_monthly": key.budget_limit_monthly,
        "tpm_limit": key.tpm_limit,
        "rpm_limit": key.rpm_limit,
        "enabled": key.enabled,
        "expires_at": key.expires_at.isoformat() if key.expires_at else None,
        "last_used": key.last_used.isoformat() if key.last_used else None,
        "created_at": key.created_at.isoformat() if key.created_at else None,
        "usage": {
            "daily_tokens": daily_usage.waddleai_tokens if daily_usage else 0,
            "monthly_tokens": sum(u.waddleai_tokens or 0 for u in monthly_usage),
            "monthly_cost_usd": sum(u.cost_usd_total or 0 for u in monthly_usage),
        },
    }


@api_v1_bp.route("/keys", methods=["POST"])
@tag(["Keys"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(CreateKeyResponse, 201)
@validate_request(CreateKeyRequest)
async def create_key(data: CreateKeyRequest):
    """Create a new virtual key"""
    if data.name is None:
        return jsonify({"error": "name is required"}), 400

    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    # Determine target user and organization
    target_user_id = data.user_id if data.user_id is not None else user_id
    target_org_id = data.organization_id if data.organization_id is not None else org_id

    # Permission check
    if target_user_id != user_id or target_org_id != org_id:
        if user_role not in ["admin", "resource_manager"]:
            return jsonify({"error": "Cannot create keys for other users"}), 403
        if user_role == "resource_manager" and target_org_id != org_id:
            return jsonify({"error": "Cannot create keys for other organizations"}), 403

    # Vuln A fix: when creating a key for ANOTHER user, validate the target
    # exists, belongs to target_org_id, and has a role no higher than the
    # caller's. Creating a key for oneself needs no such lookup (the caller is
    # authenticated and clearly exists).
    if target_user_id != user_id:
        def _validate_target_user() -> tuple[object, int]:
            """Fetch target user and validate org membership + role hierarchy."""
            target = db(db.users.id == target_user_id).select().first()
            if not target:
                return None, 404
            if target.organization_id != target_org_id:
                return None, 403
            # Non-admin can only create keys for users with role <= their own
            if user_role != "admin" and target.role == "admin":
                return None, 403
            return target, 200

        _, status_code = await asyncio.to_thread(_validate_target_user)
        if status_code == 404:
            return jsonify({"error": "Target user not found"}), 404
        if status_code != 200:
            return jsonify({"error": "Cannot create key for target user"}), 403

    # Generate API key
    key_secret = secrets.token_urlsafe(32)
    api_key = f"wa-{key_secret}"
    key_prefix = f"wa-{key_secret[:8]}..."

    # Default expiration (1 year)
    expires_days = data.expires_days
    expires_at = datetime.utcnow() + timedelta(days=expires_days) if expires_days else None

    def _insert():
        new_key_id = db.virtual_keys.insert(
            user_id=target_user_id,
            organization_id=target_org_id,
            name=data.name,
            key_prefix=key_prefix,
            key_hash=bcrypt.hash(api_key),
            allowed_models=data.allowed_models,
            allowed_providers=data.allowed_providers,
            budget_limit_daily=data.budget_limit_daily,
            budget_limit_monthly=data.budget_limit_monthly,
            tpm_limit=data.tpm_limit,
            rpm_limit=data.rpm_limit,
            enabled=True,
            expires_at=expires_at,
            created_at=datetime.utcnow(),
        )
        db.commit()
        return new_key_id

    key_id = await asyncio.to_thread(_insert)

    return {
        "id": key_id,
        "name": data.name,
        "api_key": api_key,
        "key_prefix": key_prefix,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "message": "Key created successfully. Save the api_key - it will not be shown again.",
    }, 201


@api_v1_bp.route("/keys/<int:key_id>", methods=["PUT"])
@tag(["Keys"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
@validate_request(UpdateKeyRequest)
async def update_key(key_id, data: UpdateKeyRequest):
    """Update virtual key"""
    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    key = await asyncio.to_thread(lambda: db(db.virtual_keys.id == key_id).select().first())

    if not key:
        return jsonify({"error": "Key not found"}), 404

    # Permission check
    if user_role not in ["admin"]:
        if user_role == "resource_manager" and key.organization_id != org_id:
            return jsonify({"error": "Access denied"}), 403
        elif user_role not in ["resource_manager"] and key.user_id != user_id:
            return jsonify({"error": "Access denied"}), 403

    update_fields = {}

    if data.name is not None:
        update_fields["name"] = data.name

    if data.allowed_models is not None:
        update_fields["allowed_models"] = data.allowed_models

    if data.allowed_providers is not None:
        update_fields["allowed_providers"] = data.allowed_providers

    if data.budget_limit_daily is not None:
        update_fields["budget_limit_daily"] = data.budget_limit_daily

    if data.budget_limit_monthly is not None:
        update_fields["budget_limit_monthly"] = data.budget_limit_monthly

    if data.tpm_limit is not None:
        update_fields["tpm_limit"] = data.tpm_limit

    if data.rpm_limit is not None:
        update_fields["rpm_limit"] = data.rpm_limit

    if data.enabled is not None:
        update_fields["enabled"] = data.enabled

    if data.expires_at is not None:
        if data.expires_at:
            update_fields["expires_at"] = datetime.fromisoformat(data.expires_at.replace("Z", "+00:00"))
        else:
            update_fields["expires_at"] = None

    if update_fields:

        def _update():
            db(db.virtual_keys.id == key_id).update(**update_fields)
            db.commit()

        await asyncio.to_thread(_update)

    return {"message": "Key updated successfully."}


@api_v1_bp.route("/keys/<int:key_id>", methods=["DELETE"])
@tag(["Keys"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
async def delete_key(key_id):
    """Revoke/delete virtual key"""
    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    key = await asyncio.to_thread(lambda: db(db.virtual_keys.id == key_id).select().first())

    if not key:
        return jsonify({"error": "Key not found"}), 404

    # Permission check
    if user_role not in ["admin"]:
        if user_role == "resource_manager" and key.organization_id != org_id:
            return jsonify({"error": "Access denied"}), 403
        elif user_role not in ["resource_manager"] and key.user_id != user_id:
            return jsonify({"error": "Access denied"}), 403

    # Soft delete by disabling
    def _disable():
        db(db.virtual_keys.id == key_id).update(enabled=False)
        db.commit()

    await asyncio.to_thread(_disable)

    return {"message": "Key revoked successfully"}


@api_v1_bp.route("/keys/<int:key_id>/rotate", methods=["POST"])
@tag(["Keys"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(RotateKeyResponse, 200)
async def rotate_key(key_id):
    """Rotate key secret"""
    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    key = await asyncio.to_thread(lambda: db(db.virtual_keys.id == key_id).select().first())

    if not key:
        return jsonify({"error": "Key not found"}), 404

    # Permission check
    if user_role not in ["admin"]:
        if user_role == "resource_manager" and key.organization_id != org_id:
            return jsonify({"error": "Access denied"}), 403
        elif user_role not in ["resource_manager"] and key.user_id != user_id:
            return jsonify({"error": "Access denied"}), 403

    # Generate new key
    key_secret = secrets.token_urlsafe(32)
    new_api_key = f"wa-{key_secret}"
    key_prefix = f"wa-{key_secret[:8]}..."

    def _rotate():
        db(db.virtual_keys.id == key_id).update(key_hash=bcrypt.hash(new_api_key), key_prefix=key_prefix)
        db.commit()

    await asyncio.to_thread(_rotate)

    return {
        "id": key_id,
        "api_key": new_api_key,
        "key_prefix": key_prefix,
        "message": "Key rotated successfully. Save the new api_key - it will not be shown again.",
    }


@api_v1_bp.route("/keys/<int:key_id>/usage", methods=["GET"])
@tag(["Keys"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(KeyUsageResponse, 200)
async def get_key_usage(key_id):
    """Get usage statistics for a key"""
    user_role = g.user.get("role")
    user_id = g.user.get("user_id")
    org_id = g.user.get("organization_id")

    key = await asyncio.to_thread(lambda: db(db.virtual_keys.id == key_id).select().first())

    if not key:
        return jsonify({"error": "Key not found"}), 404

    # Permission check — Vuln B fix: always scope to caller's org, never skip for reporter
    if user_role == "admin":
        # Admin can access any key
        pass
    elif user_role == "resource_manager":
        if key.organization_id != org_id:
            return jsonify({"error": "Access denied"}), 403
    else:  # user or reporter or any other role
        if key.user_id != user_id:
            return jsonify({"error": "Access denied"}), 403

    from datetime import date, timedelta

    days = request.args.get("days", 30, type=int)
    start_date = date.today() - timedelta(days=days)

    usage_records = await asyncio.to_thread(
        lambda: db((db.token_usage.virtual_key_id == key_id) & (db.token_usage.date >= start_date)).select(
            orderby=db.token_usage.date
        )
    )

    daily_usage = []
    for record in usage_records:
        daily_usage.append(
            {
                "date": record.date.isoformat(),
                "waddleai_tokens": record.waddleai_tokens,
                "tokens_input": record.tokens_input_total,
                "tokens_output": record.tokens_output_total,
                "request_count": record.request_count,
                "cost_usd": record.cost_usd_total,
            }
        )

    # Calculate totals
    total_tokens = sum(r.waddleai_tokens or 0 for r in usage_records)
    total_requests = sum(r.request_count or 0 for r in usage_records)
    total_cost = sum(r.cost_usd_total or 0 for r in usage_records)

    return {
        "key_id": key_id,
        "key_name": key.name,
        "period_days": days,
        "totals": {"waddleai_tokens": total_tokens, "requests": total_requests, "cost_usd": total_cost},
        "daily_usage": daily_usage,
    }
