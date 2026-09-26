"""JWKS-based RS256 verification for WaddleAI-issued tokens (headless-auth H4).

Every token validator that used to check a Bearer token against this
*same process's* keystore (``shared.auth.penguin_auth.verify_token`` /
``LocalOIDCRelyingParty``, both requiring a shared ``SIGNING_KEY_FILE`` to
stay in sync across replicas) instead fetches the issuer's *public* keys
from its published JWKS (``/.well-known/jwks.json``, see
``services/management/app/api/v1/well_known.py``, H3) and selects the
verification key by the token's ``kid`` header. A validator never needs the
issuer's private key or a shared file mount.

``JWKSVerifier`` wraps a single, process-lifetime ``jwt.PyJWKClient``:

* **Caching** -- the JWK Set is fetched at most once per
  ``cache_ttl_seconds`` (``PyJWKClient``'s own TTL cache); a lookup for an
  already-cached ``kid`` never touches the network, so a JWKS outage that
  falls entirely within the cache window is invisible to callers.
* **Rotation** -- an unrecognised ``kid`` triggers exactly one forced
  refetch before giving up (``PyJWKClient.get_signing_key``), so a key
  rotated in *after* the last fetch is picked up on the next token that
  uses it, without waiting out the full TTL.
* **Fail-closed** -- if the JWKS endpoint cannot be reached and no cached
  key resolves the token's ``kid`` (either because nothing has ever been
  fetched successfully, or the forced refetch on an unknown ``kid`` itself
  fails), verification raises :class:`JWKSVerificationError` rather than
  accepting the token. There is no fail-open path anywhere in this module.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import jwt
from jwt import PyJWKClient, PyJWKClientConnectionError
from penguin_aaa.authn.oidc_rp import _normalise_list_fields
from penguin_aaa.authn.types import ALLOWED_RP_ALGORITHMS, MAX_TOKEN_SIZE, Claims
from penguin_aaa.hardening.validators import validate_algorithm, validate_https_url

logger = logging.getLogger(__name__)

#: Default JWKS document cache TTL. "Sane" per headless-auth H4's spec: long
#: enough that a validator on the hot path almost never touches the network,
#: short enough that a rotation (see shared.auth.penguin_auth.rotate_signing_key)
#: propagates within a few minutes. Matches the cache-control max-age the
#: management service's ``/.well-known/jwks.json`` route publishes.
DEFAULT_CACHE_TTL_SECONDS = 300.0
DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0


class JWKSVerificationError(Exception):
    """A JWKS-backed token verification failed, for any reason.

    The single exception type this module's public API raises -- signature
    failure, expiry, issuer/audience mismatch, unknown ``kid``, or the JWKS
    endpoint being unreachable all surface identically as "this token does
    not verify" so callers (``shared.auth.penguin_auth.verify_token_via_jwks``)
    need only catch one type to fail closed.
    """


class SigningKeyResolver(Protocol):
    """The subset of ``jwt.PyJWKClient``'s interface this module depends on.

    Lets tests and in-process (no-network) callers (see
    ``proxy/apps/proxy_server/main.py``'s contract-test wiring) supply a
    substitute without needing a real HTTP JWKS endpoint.
    """

    def get_signing_key_from_jwt(self, token: str) -> Any:
        """Return an object exposing ``.key`` -- the verification key for *token*."""
        ...


@dataclass(slots=True)
class JWKSVerifierConfig:
    """Configuration for a :class:`JWKSVerifier`."""

    issuer: str
    audience: str
    jwks_url: str
    algorithms: list[str] = field(default_factory=lambda: ["RS256"])
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS
    http_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS
    clock_skew: timedelta = field(default_factory=lambda: timedelta(seconds=30))

    def __post_init__(self) -> None:
        """Reject a non-HTTPS URL (localhost excepted), bad algorithm, or bad TTL/timeout."""
        validate_https_url(self.issuer, "issuer")
        validate_https_url(self.jwks_url, "jwks_url")
        if not self.algorithms:
            raise ValueError("algorithms must contain at least one entry")
        for alg in self.algorithms:
            validate_algorithm(alg, ALLOWED_RP_ALGORITHMS)
        if self.cache_ttl_seconds <= 0:
            raise ValueError("cache_ttl_seconds must be greater than 0")
        if self.http_timeout_seconds <= 0:
            raise ValueError("http_timeout_seconds must be greater than 0")


class JWKSVerifier:
    """Verifies RS256 JWTs by ``kid`` against a JWKS endpoint.

    Construct once per process (or per test) and reuse -- the wrapped
    ``PyJWKClient`` (or injected ``jwks_client``) is what carries the JWK
    Set cache described in this module's docstring across calls.
    """

    def __init__(
        self,
        config: JWKSVerifierConfig,
        jwks_client: SigningKeyResolver | None = None,
    ) -> None:
        """Bind this verifier to *config*, or to an injected *jwks_client*.

        *jwks_client* is for tests and no-network callers only; production
        code should leave it ``None`` so a real ``PyJWKClient`` is built
        against ``config.jwks_url``.
        """
        self._config = config
        self._jwks_client: SigningKeyResolver = jwks_client or PyJWKClient(
            config.jwks_url,
            lifespan=config.cache_ttl_seconds,
            timeout=config.http_timeout_seconds,
        )

    def verify_token(self, raw_token: str) -> Claims:
        """Validate *raw_token*'s signature (via JWKS), issuer, audience, and expiry.

        Returns the parsed :class:`~penguin_aaa.authn.types.Claims`.

        Raises:
            JWKSVerificationError: On any failure -- oversized token, unknown
                ``kid``, unreachable/empty JWKS with nothing usable cached,
                bad signature, expired token, issuer/audience mismatch, or a
                claims shape ``Claims`` rejects (e.g. empty ``tenant``).
        """
        if len(raw_token) > MAX_TOKEN_SIZE:
            raise JWKSVerificationError(
                f"Token exceeds maximum allowed size of {MAX_TOKEN_SIZE} bytes"
            )

        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(raw_token)
        except PyJWKClientConnectionError as exc:
            # The JWKS endpoint itself could not be reached (and, since we
            # got here at all, no cached key already resolved this kid --
            # see this module's docstring: a cache hit never touches the
            # network). Fail closed rather than accepting the token
            # unverified.
            logger.warning(
                "JWKS endpoint unreachable and key not cached", extra={"error": str(exc)}
            )
            raise JWKSVerificationError(f"JWKS endpoint unreachable: {exc}") from exc
        except jwt.PyJWTError as exc:
            # Malformed token header, or a kid genuinely absent from the
            # JWKS even after one forced refetch (jwt.PyJWKClient.
            # get_signing_key already retries once before giving up).
            logger.warning("JWKS key resolution failed", extra={"error": str(exc)})
            raise JWKSVerificationError(f"Unable to resolve a signing key: {exc}") from exc
        except Exception as exc:
            # Any other unexpected failure resolving the key -- fail closed.
            logger.warning("JWKS key resolution failed unexpectedly", extra={"error": str(exc)})
            raise JWKSVerificationError(f"JWKS endpoint unreachable: {exc}") from exc

        skew_seconds = int(self._config.clock_skew.total_seconds())
        try:
            payload: dict[str, Any] = jwt.decode(
                raw_token,
                signing_key.key,
                algorithms=self._config.algorithms,
                audience=self._config.audience,
                issuer=self._config.issuer,
                leeway=skew_seconds,
                # regression: headless-auth-secrev (I1) -- without an
                # explicit `require`, PyJWT only validates a claim *if
                # present*; a token missing `exp` (or `iss`/`aud`/`sub`)
                # would otherwise decode successfully instead of being
                # rejected as malformed/incomplete.
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise JWKSVerificationError(f"Token verification failed: {exc}") from exc

        # jwt.decode returns dict[str, Any]; normalise before pydantic validation
        # (same helper penguin_aaa's own OIDCRelyingParty/StaticKeyVerifier use).
        _normalise_list_fields(payload, ("aud", "scope", "roles", "teams"))

        for field_name in ("iat", "exp"):
            val = payload.get(field_name)
            if isinstance(val, (int, float)):
                payload[field_name] = datetime.fromtimestamp(val, tz=UTC)

        try:
            return Claims.model_validate(payload)
        except Exception as exc:
            raise JWKSVerificationError(f"Claims validation failed: {exc}") from exc


def create_jwks_verifier() -> JWKSVerifier:
    """Build a :class:`JWKSVerifier` from environment configuration.

    ``OIDC_JWKS_URL`` overrides the JWKS endpoint directly; unset, it is
    derived from ``OIDC_ISSUER_URL`` + ``/.well-known/jwks.json`` -- the
    exact route management publishes (H3). ``OIDC_ISSUER_URL``/
    ``OIDC_CLIENT_ID`` are the same env vars every other WaddleAI OIDC
    factory in this module family reads (see
    ``shared.auth.penguin_auth.create_oidc_provider``/``create_oidc_rp``),
    so a deployment only sets them once.
    """
    issuer = os.getenv("OIDC_ISSUER_URL", "https://waddleai.localhost.local")
    jwks_url = os.getenv("OIDC_JWKS_URL") or issuer.rstrip("/") + "/.well-known/jwks.json"
    config = JWKSVerifierConfig(
        issuer=issuer,
        audience=os.getenv("OIDC_CLIENT_ID", "waddleai-api"),
        jwks_url=jwks_url,
        cache_ttl_seconds=float(
            os.getenv("OIDC_JWKS_CACHE_TTL_SECONDS", str(DEFAULT_CACHE_TTL_SECONDS))
        ),
        http_timeout_seconds=float(
            os.getenv("OIDC_JWKS_HTTP_TIMEOUT_SECONDS", str(DEFAULT_HTTP_TIMEOUT_SECONDS))
        ),
    )
    return JWKSVerifier(config)
