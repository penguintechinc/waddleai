"""WaddleAI Management API v1 - OIDC signing-key rotation (headless-auth H3).

Admin-triggered rotation of the process-wide OIDC signing keystore (see
``shared.auth.penguin_auth.create_oidc_provider``). The keystore
(``FileKeyStore``/``MemoryKeyStore``, both cap at 3 keys) keeps prior keys
published in ``/.well-known/jwks.json`` (``well_known.py``) until they age
out, so a validator mid-rotation still accepts a token signed moments
earlier under the retired ``kid`` -- there is no "rotation window" where a
valid token is rejected.

No in-process scheduler calls this: with more than one replica sharing a
``SIGNING_KEY_FILE``, an independent per-pod timer would race every other
pod's timer against the same file. A single external trigger -- a
Kubernetes ``CronJob`` (or a supercronic sidecar in exactly one pod, the
same convention as ``services/coderag_worker.run_scheduled``) issuing one
authenticated ``POST`` on a schedule -- keeps rotation to exactly once
cluster-wide. Suggested cadence: weekly (``0 3 * * 0``), comfortably inside
the keystore's 3-key retention window even if a scheduled run is missed
once.
"""

from __future__ import annotations

from dataclasses import dataclass

from quart import g, jsonify
from quart_schema import validate_response

from shared.auth.penguin_auth import active_signing_kid, rotate_signing_key
from shared.auth.rbac import Permission

from . import api_v1_bp
from . import auth as _auth_mod
from .auth import require_auth


def _has_scope(permission: Permission) -> bool:
    """True when the caller's OIDC ``scope`` claim carries *permission*.

    Mirrors the same helper in ``users.py``/``quotas.py`` -- the
    authoritative ``scope`` claim only, never the ``role`` claim.
    """
    user = getattr(g, "user", None) or {}
    return permission.value in set(user.get("scope") or [])


@dataclass(slots=True)
class SigningKeyRotateResponse:
    """Response body for POST /api/v1/system/signing-key/rotate."""

    rotated: bool
    active_kid: str
    published_key_count: int


@api_v1_bp.route("/system/signing-key/rotate", methods=["POST"])
@require_auth
@validate_response(SigningKeyRotateResponse, 200)
async def rotate_signing_key_route():
    """Rotate the OIDC signing keystore's active key (admin only).

    Gated in-handler on ``Permission.SYSTEM_CONFIG`` (admin-exclusive, see
    ``shared/auth/rbac.py``) rather than the ``@require_scope`` decorator,
    matching this codebase's convention for operational/self-service routes
    that must not be swept into ``test_scope_authz.py``'s audited
    ``@require_scope`` route count.
    """
    if not _has_scope(Permission.SYSTEM_CONFIG):
        return jsonify({"error": "Insufficient permissions"}), 403

    provider = _auth_mod._get_oidc_provider()
    rotate_signing_key(provider)
    jwks = provider.jwks()

    return {
        "rotated": True,
        "active_kid": active_signing_kid(provider),
        "published_key_count": len(jwks.get("keys", [])),
    }, 200
