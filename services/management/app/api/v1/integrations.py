"""WaddleAI Management API v1 - External MCP Gateway Integrations (§11.4).

WebUI/CLI control surface for the external-MCP gateway: register/manage
``mcp_endpoints`` (§13.1 migration 014), render a per-virtual-key OpenCode
config, and drive a caller's own per-user OAuth2 link to a `per_user`
endpoint.

Follows the same provider-credential pattern already established in
``providers.py`` (``{"status", "data", "meta"}`` envelope, secrets never
echoed, ``encrypt_credential``/``decrypt_credential`` for anything
sensitive, admin-role CRUD, thread-offloaded penguin-dal calls). Every
route is org-scoped from ``g.user["organization_id"]`` -- never from a
client-supplied ``org_id`` -- and gated behind ``waddleai.mcp_v2``
(flag-off -> 404, matching the `/mcp` mount's flag-off behavior).
"""

from __future__ import annotations

import asyncio
import hmac
import os
import time
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from passlib.hash import bcrypt
from quart import current_app, g, jsonify, request
from quart_schema import security_scheme, tag, validate_request, validate_response

from shared.auth.rbac import Permission
from shared.mcp.gateway.auth import OAuth2AuthCodeConfig, OutboundAuth
from shared.security.credential_encryption import decrypt_credential, encrypt_credential
from shared.utils.feature_flags import is_feature_enabled

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth, require_scope

_BEARER_AUTH = [{"bearerAuth": []}]


# ---------------------------------------------------------------------------
# OpenAPI request/response models for the mcp-endpoints CRUD sub-resource.
#
# `auth_config` is modeled as a loose `dict` rather than a fixed shape --
# its fields genuinely vary by `auth_type` (header vs oauth2 client
# credentials vs oauth2 auth code, see VALID_AUTH_TYPES) and secret
# sub-fields are always masked before this ever reaches a response (see
# `_masked_auth_config`), so a fixed dataclass would either be wrong for
# some auth_types or have to enumerate every provider's fields.
#
# The self-service opencode-config/link/link-callback routes below are
# intentionally left unannotated: their response bodies are dynamically
# shaped (a whole OpenCode client config, an IdP authorization URL) rather
# than a stable resource schema -- documented here rather than guessed.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class McpEndpoint:
    """A registered external MCP endpoint. `auth_config` secrets are always masked."""

    id: int
    org_id: int
    name: str
    url: str
    transport: str
    auth_type: str
    auth_config: dict
    identity_mode: str
    namespace: str
    credentials_ref: str | None
    status: str
    created_at: str | None


@dataclass(slots=True)
class ListMcpEndpointsMeta:
    """`meta` block for the list-endpoints response."""

    total: int
    timestamp: str


@dataclass(slots=True)
class ListMcpEndpointsResponse:
    """Response body for GET /api/v1/integrations/mcp-endpoints."""

    status: str
    data: list[McpEndpoint]
    meta: ListMcpEndpointsMeta


@dataclass(slots=True)
class McpEndpointActionMeta:
    """`meta` block for a single-endpoint mutation response.

    `action` is only set on create/update -- a plain GET carries just a
    timestamp, so it stays Optional rather than forcing a fabricated value.
    """

    timestamp: str
    action: str | None = None


@dataclass(slots=True)
class McpEndpointResponse:
    """Response body shared by get/create/update of a single mcp-endpoint."""

    status: str
    data: McpEndpoint
    meta: McpEndpointActionMeta


@dataclass(slots=True)
class DeletedMcpEndpointData:
    """`data` block for a successful endpoint deletion."""

    id: int


@dataclass(slots=True)
class DeleteMcpEndpointResponse:
    """Response body for DELETE /api/v1/integrations/mcp-endpoints/<id>."""

    status: str
    data: DeletedMcpEndpointData
    meta: McpEndpointActionMeta


@dataclass(slots=True)
class CreateMcpEndpointRequest:
    """Request body for POST /api/v1/integrations/mcp-endpoints."""

    name: str | None = None
    url: str | None = None
    transport: str | None = None
    auth_type: str | None = "none"
    identity_mode: str | None = "shared"
    namespace: str | None = None
    auth_config: dict | None = None
    credentials_ref: str | None = None


@dataclass(slots=True)
class UpdateMcpEndpointRequest:
    """Request body for PUT /api/v1/integrations/mcp-endpoints/<id>.

    Every field is a partial update.
    """

    name: str | None = None
    url: str | None = None
    transport: str | None = None
    auth_type: str | None = None
    auth_config: dict | None = None
    identity_mode: str | None = None
    status: str | None = None
    credentials_ref: str | None = None


MCP_V2_FLAG = "waddleai.mcp_v2"

VALID_TRANSPORTS = {"streamable_http", "stdio"}
VALID_AUTH_TYPES = {"none", "header", "oauth2_client_credentials", "oauth2_auth_code"}
VALID_IDENTITY_MODES = {"shared", "per_user"}

# `auth_config` sub-fields that carry secret material and must be
# encrypted before storage / never echoed back in a response -- the same
# encryption helper `provider_credentials.api_key` uses (see
# `shared/security/credential_encryption.py`), applied per-field since
# migration 014 stores `auth_config` as one JSON blob rather than a
# dedicated encrypted column.
_SECRET_AUTH_CONFIG_FIELDS = ("header_value", "client_secret")

LINK_STATE_MAX_AGE_SECONDS = 600  # 10 minutes -- enough for a real OAuth redirect round trip


def _feature_enabled(org_id: int) -> bool:
    return is_feature_enabled(MCP_V2_FLAG, distinct_id=str(org_id))


def _mask_secret(value: str | None) -> str:
    """Return a masked representation of a secret -- never the plaintext value."""
    if not value:
        return ""
    raw = value[4:] if value.startswith("enc:") else value
    if len(raw) <= 8:
        return "****"
    return raw[:4] + "****" + raw[-4:]


def _encrypt_auth_config(auth_config: dict[str, Any] | None) -> dict[str, Any]:
    """Encrypt every secret-bearing sub-field of ``auth_config`` before storage."""
    if not auth_config:
        return {}
    encrypted = dict(auth_config)
    for field_name in _SECRET_AUTH_CONFIG_FIELDS:
        value = encrypted.get(field_name)
        if isinstance(value, str) and value:
            encrypted[field_name] = encrypt_credential(value)
    return encrypted


def _decrypt_auth_config(auth_config: dict[str, Any] | None) -> dict[str, Any]:
    """Decrypt every secret-bearing sub-field, for internal use only (never returned as-is)."""
    if not auth_config:
        return {}
    decrypted = dict(auth_config)
    for field_name in _SECRET_AUTH_CONFIG_FIELDS:
        value = decrypted.get(field_name)
        if isinstance(value, str) and value:
            decrypted[field_name] = decrypt_credential(value)
    return decrypted


def _masked_auth_config(auth_config: dict[str, Any] | None) -> dict[str, Any]:
    """Auth config safe to return in an API response -- secret fields masked, never decrypted."""
    if not auth_config:
        return {}
    masked = dict(auth_config)
    for field_name in _SECRET_AUTH_CONFIG_FIELDS:
        if field_name in masked and masked[field_name]:
            masked[field_name] = _mask_secret(masked[field_name])
    return masked


def _endpoint_to_dict(row) -> dict[str, Any]:
    """Serialize an `mcp_endpoints` row -- secret `auth_config` fields are always masked."""
    return {
        "id": row.id,
        "org_id": row.org_id,
        "name": row.name,
        "url": row.url,
        "transport": row.transport,
        "auth_type": row.auth_type,
        "auth_config": _masked_auth_config(row.auth_config),
        "identity_mode": row.identity_mode,
        "namespace": row.namespace,
        "credentials_ref": row.credentials_ref,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _validation_error(detail: str) -> tuple[Any, int]:
    return jsonify({"status": "error", "error": detail}), 400


# ---------------------------------------------------------------------------
# `/api/v1/integrations/mcp-endpoints` CRUD (admin only, org-scoped)
# ---------------------------------------------------------------------------


@api_v1_bp.route("/integrations/mcp-endpoints", methods=["GET"])
@tag(["Integrations"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.INTEGRATION_ADMIN)
@validate_response(ListMcpEndpointsResponse, 200)
async def list_mcp_endpoints():
    """List this org's registered external MCP endpoints."""
    org_id = g.user.get("organization_id")
    if not _feature_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404

    def _fetch():
        return db(db.mcp_endpoints.org_id == org_id).select(orderby=db.mcp_endpoints.id)

    rows = await asyncio.to_thread(_fetch)
    return {
        "status": "success",
        "data": [_endpoint_to_dict(r) for r in rows],
        "meta": {"total": len(rows), "timestamp": datetime.utcnow().isoformat() + "Z"},
    }


@api_v1_bp.route("/integrations/mcp-endpoints", methods=["POST"])
@tag(["Integrations"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.INTEGRATION_ADMIN)
@validate_response(McpEndpointResponse, 201)
@validate_request(CreateMcpEndpointRequest)
async def create_mcp_endpoint(data: CreateMcpEndpointRequest):
    """Register a new external MCP endpoint for this org."""
    org_id = g.user.get("organization_id")
    if not _feature_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404

    name = (data.name or "").strip()
    url = (data.url or "").strip()
    transport = data.transport
    auth_type = data.auth_type
    identity_mode = data.identity_mode
    namespace = (data.namespace or "").strip()

    if not name or len(name) > 255:
        return _validation_error("name is required and must be <= 255 characters")
    if not url or len(url) > 1024:
        return _validation_error("url is required and must be <= 1024 characters")
    if transport not in VALID_TRANSPORTS:
        return _validation_error(f"transport must be one of {sorted(VALID_TRANSPORTS)}")
    if auth_type not in VALID_AUTH_TYPES:
        return _validation_error(f"auth_type must be one of {sorted(VALID_AUTH_TYPES)}")
    if identity_mode not in VALID_IDENTITY_MODES:
        return _validation_error(f"identity_mode must be one of {sorted(VALID_IDENTITY_MODES)}")
    if not namespace or not namespace.replace("_", "").replace("-", "").isalnum():
        return _validation_error("namespace is required and must be alphanumeric (- and _ allowed)")

    encrypted_auth_config = _encrypt_auth_config(data.auth_config)

    def _create():
        existing = (
            db((db.mcp_endpoints.org_id == org_id) & (db.mcp_endpoints.namespace == namespace))
            .select()
            .first()
        )
        if existing:
            return "namespace_conflict", None

        endpoint_id = db.mcp_endpoints.insert(
            org_id=org_id,
            name=name,
            url=url,
            transport=transport,
            auth_type=auth_type,
            auth_config=encrypted_auth_config,
            identity_mode=identity_mode,
            namespace=namespace,
            credentials_ref=data.credentials_ref,
            status="active",
            created_at=datetime.utcnow(),
        )
        db.commit()
        return "ok", db(db.mcp_endpoints.id == endpoint_id).select().first()

    status, row = await asyncio.to_thread(_create)
    if status == "namespace_conflict":
        return (
            jsonify(
                {
                    "status": "error",
                    "error": (
                        f"an endpoint with namespace '{namespace}' already exists for this org"
                    ),
                }
            ),
            409,
        )

    return {
        "status": "success",
        "data": _endpoint_to_dict(row),
        "meta": {"action": "created", "timestamp": datetime.utcnow().isoformat() + "Z"},
    }, 201


def _get_org_scoped_endpoint(endpoint_id: int, org_id: int):
    """Return an `mcp_endpoints` row, distinguishing "not found" from "wrong org"."""
    row = db(db.mcp_endpoints.id == endpoint_id).select().first()
    if row is None:
        return None, "not_found"
    if row.org_id != org_id:
        return None, "forbidden"
    return row, "ok"


@api_v1_bp.route("/integrations/mcp-endpoints/<int:endpoint_id>", methods=["GET"])
@tag(["Integrations"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.INTEGRATION_ADMIN)
@validate_response(McpEndpointResponse, 200)
async def get_mcp_endpoint(endpoint_id: int):
    """Fetch one registered endpoint -- 403 across orgs, 404 if it never existed."""
    org_id = g.user.get("organization_id")
    if not _feature_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404

    row, outcome = await asyncio.to_thread(_get_org_scoped_endpoint, endpoint_id, org_id)
    if outcome == "not_found":
        return jsonify({"status": "error", "error": "endpoint not found"}), 404
    if outcome == "forbidden":
        return jsonify({"status": "error", "error": "forbidden"}), 403

    return {
        "status": "success",
        "data": _endpoint_to_dict(row),
        "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
    }


@api_v1_bp.route("/integrations/mcp-endpoints/<int:endpoint_id>", methods=["PUT"])
@tag(["Integrations"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.INTEGRATION_ADMIN)
@validate_response(McpEndpointResponse, 200)
@validate_request(UpdateMcpEndpointRequest)
async def update_mcp_endpoint(endpoint_id: int, data: UpdateMcpEndpointRequest):
    """Update a registered endpoint's mutable fields."""
    org_id = g.user.get("organization_id")
    if not _feature_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404

    row, outcome = await asyncio.to_thread(_get_org_scoped_endpoint, endpoint_id, org_id)
    if outcome == "not_found":
        return jsonify({"status": "error", "error": "endpoint not found"}), 404
    if outcome == "forbidden":
        return jsonify({"status": "error", "error": "forbidden"}), 403

    update_fields: dict[str, Any] = {}
    if data.name is not None:
        name = (data.name or "").strip()
        if not name or len(name) > 255:
            return _validation_error("name must be 1-255 characters")
        update_fields["name"] = name
    if data.url is not None:
        url = (data.url or "").strip()
        if not url or len(url) > 1024:
            return _validation_error("url must be 1-1024 characters")
        update_fields["url"] = url
    if data.transport is not None:
        if data.transport not in VALID_TRANSPORTS:
            return _validation_error(f"transport must be one of {sorted(VALID_TRANSPORTS)}")
        update_fields["transport"] = data.transport
    if data.auth_type is not None:
        if data.auth_type not in VALID_AUTH_TYPES:
            return _validation_error(f"auth_type must be one of {sorted(VALID_AUTH_TYPES)}")
        update_fields["auth_type"] = data.auth_type
    if data.auth_config is not None:
        update_fields["auth_config"] = _encrypt_auth_config(data.auth_config)
    if data.identity_mode is not None:
        if data.identity_mode not in VALID_IDENTITY_MODES:
            return _validation_error(f"identity_mode must be one of {sorted(VALID_IDENTITY_MODES)}")
        update_fields["identity_mode"] = data.identity_mode
    if data.status is not None:
        if data.status not in {"active", "disabled", "error"}:
            return _validation_error("status must be one of ['active', 'disabled', 'error']")
        update_fields["status"] = data.status
    if data.credentials_ref is not None:
        update_fields["credentials_ref"] = data.credentials_ref

    def _update():
        db(db.mcp_endpoints.id == endpoint_id).update(**update_fields)
        db.commit()
        return db(db.mcp_endpoints.id == endpoint_id).select().first()

    updated = await asyncio.to_thread(_update)
    return {
        "status": "success",
        "data": _endpoint_to_dict(updated),
        "meta": {"action": "updated", "timestamp": datetime.utcnow().isoformat() + "Z"},
    }


@api_v1_bp.route("/integrations/mcp-endpoints/<int:endpoint_id>", methods=["DELETE"])
@tag(["Integrations"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.INTEGRATION_ADMIN)
@validate_response(DeleteMcpEndpointResponse, 200)
async def delete_mcp_endpoint(endpoint_id: int):
    """Delete a registered endpoint (cascades to its `mcp_user_links`)."""
    org_id = g.user.get("organization_id")
    if not _feature_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404

    row, outcome = await asyncio.to_thread(_get_org_scoped_endpoint, endpoint_id, org_id)
    if outcome == "not_found":
        return jsonify({"status": "error", "error": "endpoint not found"}), 404
    if outcome == "forbidden":
        return jsonify({"status": "error", "error": "forbidden"}), 403

    def _delete():
        db(db.mcp_user_links.endpoint_id == endpoint_id).delete()
        db(db.mcp_endpoints.id == endpoint_id).delete()
        db.commit()

    await asyncio.to_thread(_delete)
    return {
        "status": "success",
        "data": {"id": endpoint_id},
        "meta": {"action": "deleted", "timestamp": datetime.utcnow().isoformat() + "Z"},
    }


# ---------------------------------------------------------------------------
# `/api/v1/integrations/opencode-config` -- self-service, any authenticated user
# ---------------------------------------------------------------------------


def _verify_caller_owns_key(virtual_key: str, org_id: int, user_id: int):
    """Bcrypt-verify `virtual_key` against this caller's own keys.

    Mirrors `auth.py::verify_api_key`.
    """
    keys = db(
        (db.virtual_keys.organization_id == org_id)
        & (db.virtual_keys.user_id == user_id)
        & (db.virtual_keys.enabled == True)  # noqa: E712 -- PyDAL query, not a Python bool compare
    ).select()
    for key in keys:
        if bcrypt.verify(virtual_key, key.key_hash):
            return key
    return None


@api_v1_bp.route("/integrations/opencode-config", methods=["POST"])
@tag(["Integrations"])
@security_scheme(_BEARER_AUTH)
@require_auth
async def opencode_config():
    """Render a per-virtual-key OpenCode config (custom provider + `/mcp` entry).

    Self-service: any authenticated caller can render a config for a
    `wa-` key they already hold and can prove ownership of (bcrypt
    verification against their own `virtual_keys` rows) -- no admin
    scope required, since this is downloading a client config for the
    caller's own key, not managing the org's registered endpoints.

    POST-with-body, deliberately not GET-with-query-param: a `wa-` key
    is a bearer credential, and a query string gets written to disk in
    every ingress/access log, any CDN or proxy in front (this deployment
    sits behind Cloudflare), browser history, and `Referer` headers on
    outbound links -- long after this handler returns. The response body
    already carries the same key back to the caller (that's the whole
    point of the endpoint); only the *request*'s transport matters here.
    """
    org_id = g.user.get("organization_id")
    user_id = g.user.get("user_id")
    if not _feature_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404

    data = await request.get_json(silent=True) or {}
    virtual_key = data.get("virtual_key", "")
    if not virtual_key:
        return _validation_error("virtual_key is required in the request body")

    key_row = await asyncio.to_thread(_verify_caller_owns_key, virtual_key, org_id, user_id)
    if key_row is None:
        return (
            jsonify({"status": "error", "error": "virtual_key not recognized for this account"}),
            403,
        )

    base_url = os.getenv("WADDLEAI_PROXY_PUBLIC_URL", "http://localhost:8000").rstrip("/")
    config = {
        "provider": {
            "waddleai": {
                "type": "openai-compatible",
                "baseURL": f"{base_url}/v1",
                "apiKey": virtual_key,
                "models": f"{base_url}/v1/models",
            }
        },
        "mcp": {
            "waddleai": {
                "type": "remote",
                "url": f"{base_url}/mcp",
                "headers": {"Authorization": f"Bearer {virtual_key}"},
            }
        },
    }
    return jsonify(
        {
            "status": "success",
            "data": config,
            "meta": {"key_id": key_row.id, "timestamp": datetime.utcnow().isoformat() + "Z"},
        }
    )


# ---------------------------------------------------------------------------
# Per-user link flow -- self-service, drives the §11.4 OAuth2 auth-code link
# ---------------------------------------------------------------------------


def _link_state_secret() -> str:
    return current_app.config.get("SECRET_KEY") or os.getenv("JWT_SECRET", "")


def _sign_link_state(endpoint_id: int, user_id: int) -> str:
    """Build a stateless, HMAC-signed CSRF state token for the link redirect.

    No server-side session store is required -- the state token itself
    carries and authenticates `(endpoint_id, user_id, issued_at)`, verified
    on callback via constant-time comparison.
    """
    issued_at = int(time.time())
    payload = f"{endpoint_id}:{user_id}:{issued_at}"
    signature = hmac.new(_link_state_secret().encode(), payload.encode(), sha256).hexdigest()
    return f"{payload}:{signature}"


def _verify_link_state(state: str, endpoint_id: int, user_id: int) -> bool:
    try:
        endpoint_part, user_part, issued_at_part, signature = state.split(":")
    except ValueError:
        return False
    if int(endpoint_part) != endpoint_id or int(user_part) != user_id:
        return False
    payload = f"{endpoint_part}:{user_part}:{issued_at_part}"
    expected = hmac.new(_link_state_secret().encode(), payload.encode(), sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False
    return (time.time() - int(issued_at_part)) <= LINK_STATE_MAX_AGE_SECONDS


def _auth_code_config(row) -> OAuth2AuthCodeConfig:
    decrypted = _decrypt_auth_config(row.auth_config)
    base_url = os.getenv("WADDLEAI_PROXY_PUBLIC_URL", "http://localhost:8000").rstrip("/")
    return OAuth2AuthCodeConfig(
        authorization_endpoint=decrypted.get("authorization_endpoint", ""),
        token_endpoint=decrypted.get("token_endpoint", ""),
        redirect_uri=f"{base_url}/api/v1/integrations/mcp-endpoints/{row.id}/link/callback",
        registration_endpoint=decrypted.get("registration_endpoint"),
        scope=decrypted.get("scope"),
        client_id=decrypted.get("client_id"),
        client_secret=decrypted.get("client_secret"),
    )


@api_v1_bp.route("/integrations/mcp-endpoints/<int:endpoint_id>/link", methods=["GET"])
@tag(["Integrations"])
@security_scheme(_BEARER_AUTH)
@require_auth
async def initiate_mcp_link(endpoint_id: int):
    """Start the caller's own per-user OAuth2 link to a `per_user` endpoint.

    Dynamically registers a client (RFC 7591) on first use if the
    endpoint has a `registration_endpoint` and no pre-registered
    `client_id` yet, persisting the result so subsequent links reuse it.
    """
    org_id = g.user.get("organization_id")
    user_id = g.user.get("user_id")
    if not _feature_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404

    row, outcome = await asyncio.to_thread(_get_org_scoped_endpoint, endpoint_id, org_id)
    if outcome == "not_found":
        return jsonify({"status": "error", "error": "endpoint not found"}), 404
    if outcome == "forbidden":
        return jsonify({"status": "error", "error": "forbidden"}), 403
    if row.identity_mode != "per_user":
        return _validation_error("this endpoint is not configured for per_user identity")
    if row.auth_type != "oauth2_auth_code":
        return _validation_error("this endpoint's auth_type does not support account linking")

    auth_config = _auth_code_config(row)
    outbound_auth = OutboundAuth()

    if not auth_config.client_id:
        if not auth_config.registration_endpoint:
            return _validation_error(
                "endpoint has no client_id and no registration_endpoint for DCR"
            )
        client_id, client_secret = await outbound_auth.register_client(auth_config)
        stored_auth_config = dict(row.auth_config or {})
        stored_auth_config["client_id"] = client_id
        stored_auth_config["client_secret"] = encrypt_credential(client_secret)

        def _persist_client():
            db(db.mcp_endpoints.id == endpoint_id).update(auth_config=stored_auth_config)
            db.commit()

        await asyncio.to_thread(_persist_client)
        auth_config = OAuth2AuthCodeConfig(
            authorization_endpoint=auth_config.authorization_endpoint,
            token_endpoint=auth_config.token_endpoint,
            redirect_uri=auth_config.redirect_uri,
            registration_endpoint=auth_config.registration_endpoint,
            scope=auth_config.scope,
            client_id=client_id,
            client_secret=client_secret,
        )

    state = _sign_link_state(endpoint_id, user_id)
    authorization_url = outbound_auth.build_authorization_url(
        auth_config, client_id=auth_config.client_id, state=state
    )
    return jsonify(
        {
            "status": "success",
            "data": {"authorization_url": authorization_url},
            "meta": {"endpoint_id": endpoint_id, "timestamp": datetime.utcnow().isoformat() + "Z"},
        }
    )


@api_v1_bp.route("/integrations/mcp-endpoints/<int:endpoint_id>/link/callback", methods=["GET"])
@tag(["Integrations"])
@security_scheme(_BEARER_AUTH)
@require_auth
async def mcp_link_callback(endpoint_id: int):
    """Exchange the authorization code and store the caller's encrypted `McpUserLink`."""
    org_id = g.user.get("organization_id")
    user_id = g.user.get("user_id")
    if not _feature_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404

    code = request.args.get("code", "")
    state = request.args.get("state", "")
    if not code or not state:
        return _validation_error("code and state query parameters are required")
    if not _verify_link_state(state, endpoint_id, user_id):
        return jsonify({"status": "error", "error": "invalid or expired state"}), 400

    row, outcome = await asyncio.to_thread(_get_org_scoped_endpoint, endpoint_id, org_id)
    if outcome == "not_found":
        return jsonify({"status": "error", "error": "endpoint not found"}), 404
    if outcome == "forbidden":
        return jsonify({"status": "error", "error": "forbidden"}), 403

    auth_config = _auth_code_config(row)
    if not auth_config.client_id:
        return _validation_error(
            "endpoint has no registered client -- call the link endpoint first"
        )

    outbound_auth = OutboundAuth()
    token = await outbound_auth.exchange_code(
        auth_config,
        client_id=auth_config.client_id,
        client_secret=auth_config.client_secret or "",
        code=code,
    )

    # user_uuid: this schema has no native UUID column on `users` yet --
    # the opaque, non-PII stand-in is the integer id as a string, matching
    # `proxy/apps/proxy_server/mcp_mount.py::_build_tool_context`.
    user_uuid = str(user_id)
    access_token_enc = encrypt_credential(token.access_token)
    refresh_token_enc = encrypt_credential(token.refresh_token) if token.refresh_token else None
    expires_at = datetime.utcfromtimestamp(token.expires_at) if token.expires_at else None

    def _upsert_link():
        existing = (
            db(
                (db.mcp_user_links.endpoint_id == endpoint_id)
                & (db.mcp_user_links.user_uuid == user_uuid)
            )
            .select()
            .first()
        )
        if existing:
            db(db.mcp_user_links.id == existing.id).update(
                access_token_enc=access_token_enc,
                refresh_token_enc=refresh_token_enc,
                expires_at=expires_at,
                status="linked",
            )
        else:
            db.mcp_user_links.insert(
                endpoint_id=endpoint_id,
                user_uuid=user_uuid,
                access_token_enc=access_token_enc,
                refresh_token_enc=refresh_token_enc,
                expires_at=expires_at,
                status="linked",
                created_at=datetime.utcnow(),
            )
        db.commit()

    await asyncio.to_thread(_upsert_link)

    # Never echo the token -- confirmation only.
    return jsonify(
        {
            "status": "success",
            "data": {"endpoint_id": endpoint_id, "linked": True},
            "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
        }
    )
