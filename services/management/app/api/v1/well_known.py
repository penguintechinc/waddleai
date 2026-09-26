"""WaddleAI Management API - OIDC discovery & JWKS publication (headless-auth H3).

Publishes this process's RS256 *public* signing keys (JWKS, RFC 7517) and an
OIDC discovery document (OpenID Connect Discovery 1.0) so any validator --
the proxy's future relying party (H4), a third-party service, or a human
debugging a token -- can verify a WaddleAI-issued JWT's signature without the
shared ``SIGNING_KEY_FILE`` mounted locally. Both routes are UNAUTHENTICATED
by design, the same exception ``/api/v1/auth/login`` already carries
(backend.md OpenAPI): a validator has to discover the keys before it can
verify anything, including a token that would authenticate it here.

Deliberately not wired into ``quart-schema``'s OpenAPI build (like
``/healthz``/``/readyz``/``/metrics``) -- these are standard well-known
metadata endpoints, not application resources.
"""

from __future__ import annotations

from quart import Blueprint, Response, jsonify

from . import auth as _auth_mod

# Module-qualified access (``_auth_mod._get_oidc_provider()``), never
# ``from .auth import _get_oidc_provider`` -- the latter binds the function
# object at import time, so a test's
# ``patch("services.management.app.api.v1.auth._get_oidc_provider", ...)``
# (see tests/unit/management/conftest.py) would silently miss this module.
# Looking the attribute up on every call keeps this in sync with that patch,
# and with the single process-wide ``lru_cache``d provider ``auth.py`` itself
# calls -- reusing that exact singleton is what makes the JWKS this blueprint
# publishes match the key that actually signs tokens in this process.

well_known_bp = Blueprint("well_known", __name__)

# Short cache: long enough to absorb a validator's poll interval, short
# enough that a rotation (see shared.auth.penguin_auth.rotate_signing_key)
# propagates to caches quickly rather than leaving a stale kid published for
# a long window.
_CACHE_MAX_AGE_SECONDS = 300


@well_known_bp.route("/.well-known/jwks.json", methods=["GET"])
async def jwks() -> tuple[Response, int]:
    """Return the JSON Web Key Set of this process's active PUBLIC signing keys.

    ``OIDCProvider.jwks()`` delegates to the keystore's ``get_jwks()``, which
    serialises ``private_key.public_key()`` for every active key -- private
    key material is never constructed for, or reachable from, this response.
    """
    provider = _auth_mod._get_oidc_provider()
    resp = jsonify(provider.jwks())
    resp.cache_control.max_age = _CACHE_MAX_AGE_SECONDS
    resp.cache_control.public = True
    return resp, 200


@well_known_bp.route("/.well-known/openid-configuration", methods=["GET"])
async def discovery() -> tuple[Response, int]:
    """Return the OIDC discovery document for this issuer.

    ``issuer``/``jwks_uri``/``*_endpoint`` are all derived from
    ``OIDC_ISSUER_URL`` (see ``shared.auth.penguin_auth.create_oidc_provider``),
    so they resolve to this service's real, externally-reachable base URL as
    long as that env var is set to it -- required for any external validator
    (H4) to be able to follow ``jwks_uri`` back to the route above.
    """
    provider = _auth_mod._get_oidc_provider()
    resp = jsonify(provider.discovery_document())
    resp.cache_control.max_age = _CACHE_MAX_AGE_SECONDS
    resp.cache_control.public = True
    return resp, 200
