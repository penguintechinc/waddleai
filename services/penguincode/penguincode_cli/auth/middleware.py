"""WaddleAI JWT validation + ScopeContext middleware for penguincode.

Standalone by design: validates a WaddleAI-issued RS256 JWT against a public
key or JWKS endpoint supplied entirely through env (``WADDLEAI_JWT_*``) --
never by importing ``shared.auth``/management internals (spec section 8,
plan Global Constraints: "penguincode must stay standalone").

Covers both surfaces penguincode exposes:

* gRPC (``WaddleAIAuthInterceptor``) -- extracts the bearer token from the
  ``authorization`` invocation-metadata entry, mirroring the shape of
  ``server.interceptors.JWTValidationInterceptor`` (which validates
  penguincode's own local client-server HS256 secret -- a different token
  for a different purpose; the two are not interchangeable, see the
  Integration Point note below).
* REST/API (``authenticate_request``) -- extracts the bearer token from an
  HTTP ``Authorization`` header for use in a Quart ``before_request`` hook.

SPIFFE-ready per security.md: ``default_spiffe_verifier`` extracts a caller's
mTLS X.509-SVID SPIFFE ID when present. It is an *additive* enrichment, never
a substitute for JWT validation -- every inter-service call still requires a
short-lived signed JWT regardless of transport (security.md Service-to-
-Service Auth), so this hook is not wired into ``WaddleAIAuthInterceptor``'s
gate; it is a standalone helper called from within a service's own RPC
handler, where the live ``grpc.aio.ServicerContext`` (and thus the mTLS peer
identity) is actually available -- interceptors only see
``HandlerCallDetails`` (method + metadata), not the peer context.

Integration Point (not yet wired -- see plan Task T2):
``penguincode_cli/server/main.py:70-83`` builds the gRPC ``interceptors``
list and conditionally appends the existing local ``JWTValidationInterceptor``.
Adding ``WaddleAIAuthInterceptor(WaddleAIJWTValidator())`` there requires
first deciding how it coexists with that local interceptor (both currently
read the same ``authorization`` metadata key for two different token kinds)
-- a decision left to whoever wires this in, not resolved here to avoid
breaking the existing local-auth path/tests. Likewise
``penguincode_cli/server/rest_app.py:19-49`` (``create_rest_app``) is where
``authenticate_request`` would plug into a new ``@app.before_request`` hook
alongside the existing ``jwt_secret``-based admin-endpoint auth.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import grpc
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from penguincode_cli.auth.scope import ScopeContext, ScopeValidationError, scope_from_claims

_DEFAULT_ISSUER = "https://waddleai.localhost.local"
_DEFAULT_AUDIENCE = "waddleai-api"
_DEFAULT_ALGORITHMS = ("RS256",)
#: JWK Set cache TTL / HTTP timeout defaults (headless-auth H4). "Sane" per
#: spec: long enough that a hot validation path almost never touches the
#: network (a cache hit never does -- see WaddleAIJWTValidator docstring),
#: short enough that a rotation propagates within a few minutes. Matches
#: shared/auth/jwks_verifier.py's proxy-side defaults so the two
#: independently-implemented (penguincode stays standalone -- see module
#: docstring) validators behave identically in production.
_DEFAULT_JWKS_CACHE_TTL_SECONDS = 300.0
_DEFAULT_JWKS_HTTP_TIMEOUT_SECONDS = 10.0


def _require_https_or_localhost(url: str, field_name: str) -> None:
    """Reject a non-HTTPS JWKS URL, except for localhost (local dev/tests).

    Standalone duplicate of ``penguin_aaa.hardening.validators.
    validate_https_url`` (penguincode imports neither ``penguin_aaa`` nor
    ``shared.auth`` -- see module docstring) enforcing the same rule: a
    validator fetching signing keys over plaintext HTTP to a non-local host
    is a MITM/spoofing risk, so it fails at config-construction time rather
    than silently degrading transport security.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    is_localhost = hostname in {"localhost", "127.0.0.1", "::1"} or hostname.endswith(".localhost")
    if not is_localhost and parsed.scheme != "https":
        raise ValueError(
            f"{field_name} must use HTTPS for non-localhost URLs, got scheme: {parsed.scheme!r}"
        )


class TokenValidationError(Exception):
    """Raised when a bearer token fails extraction, signature, claims, or scope validation.

    The single failure type surfaced by this module's public entrypoints
    (``WaddleAIJWTValidator``, ``authenticate_request``,
    ``WaddleAIAuthInterceptor``) so callers need only catch one exception.
    """


@dataclass(slots=True, frozen=True)
class JWTValidatorConfig:
    """Verification config for WaddleAI-issued JWTs, sourced entirely from env.

    Exactly one of ``public_key``/``jwks_url`` is expected in practice (a
    static key for today's self-issued tokens, JWKS once an external
    IdP/rotation is in play) -- ``jwks_url`` takes precedence if both are set.
    """

    public_key: str | None
    jwks_url: str | None
    issuer: str
    audience: str
    algorithms: tuple[str, ...]
    jwks_cache_ttl_seconds: float = _DEFAULT_JWKS_CACHE_TTL_SECONDS
    jwks_http_timeout_seconds: float = _DEFAULT_JWKS_HTTP_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        """Reject a plaintext-HTTP ``jwks_url`` against a non-local host."""
        if self.jwks_url:
            _require_https_or_localhost(self.jwks_url, "jwks_url")

    @classmethod
    def from_env(cls) -> JWTValidatorConfig:
        """Build config from ``WADDLEAI_JWT_*`` env vars (never CLI args)."""
        public_key = os.environ.get("WADDLEAI_JWT_PUBLIC_KEY")
        key_file = os.environ.get("WADDLEAI_JWT_PUBLIC_KEY_FILE")
        if not public_key and key_file:
            public_key = Path(key_file).read_text(encoding="utf-8")

        jwks_url = os.environ.get("WADDLEAI_JWT_JWKS_URL") or None
        issuer = os.environ.get("WADDLEAI_JWT_ISSUER", _DEFAULT_ISSUER)
        audience = os.environ.get("WADDLEAI_JWT_AUDIENCE", _DEFAULT_AUDIENCE)

        algorithms_raw = os.environ.get("WADDLEAI_JWT_ALGORITHMS", "")
        algorithms = (
            tuple(a.strip() for a in algorithms_raw.split(",") if a.strip())
            if algorithms_raw
            else _DEFAULT_ALGORITHMS
        )

        jwks_cache_ttl_seconds = float(
            os.environ.get(
                "WADDLEAI_JWT_JWKS_CACHE_TTL_SECONDS", str(_DEFAULT_JWKS_CACHE_TTL_SECONDS)
            )
        )
        jwks_http_timeout_seconds = float(
            os.environ.get(
                "WADDLEAI_JWT_JWKS_TIMEOUT_SECONDS", str(_DEFAULT_JWKS_HTTP_TIMEOUT_SECONDS)
            )
        )

        return cls(
            public_key=public_key or None,
            jwks_url=jwks_url,
            issuer=issuer,
            audience=audience,
            algorithms=algorithms,
            jwks_cache_ttl_seconds=jwks_cache_ttl_seconds,
            jwks_http_timeout_seconds=jwks_http_timeout_seconds,
        )


class WaddleAIJWTValidator:
    """Validates WaddleAI-issued RS256 JWTs and derives a ``ScopeContext``.

    Self-contained: verification key comes from ``JWTValidatorConfig``
    (public key or JWKS, both env-sourced) -- no import of
    ``penguin_aaa``/``shared.auth`` so penguincode stays standalone.

    JWKS mode (headless-auth H4) constructs a single ``jwt.PyJWKClient``
    for this validator's lifetime rather than per ``validate()`` call, so
    the JWK Set is fetched at most once per
    ``config.jwks_cache_ttl_seconds`` and reused across every request:

    * **Caching** -- a request for an already-cached ``kid`` never touches
      the network, so a JWKS outage entirely inside the cache window is
      invisible to callers.
    * **Rotation** -- an unrecognised ``kid`` triggers exactly one forced
      refetch before giving up (``PyJWKClient.get_signing_key``), so a key
      rotated in after the last fetch is picked up on the next token that
      uses it.
    * **Fail-closed** -- if the JWKS endpoint cannot be reached and no
      cached key resolves the token's ``kid``, ``PyJWKClient`` raises;
      ``validate()`` below turns that into ``TokenValidationError`` like
      every other failure, never a silently-accepted token.

    Static-key mode (``public_key``) is unchanged and remains the local-dev
    fallback: no network involved at all, used whenever ``jwks_url`` is
    unset (``jwks_url`` still takes precedence when both are configured).
    """

    def __init__(self, config: JWTValidatorConfig | None = None) -> None:
        """Bind this validator to *config* (defaults to ``JWTValidatorConfig.from_env()``)."""
        self._config = config or JWTValidatorConfig.from_env()
        self._jwks_client: jwt.PyJWKClient | None = None

    def _get_jwks_client(self) -> jwt.PyJWKClient:
        """Return this validator's persistent ``PyJWKClient``, building it on first use.

        Built lazily (not in ``__init__``) so constructing a validator never
        requires ``jwks_url`` to already be reachable; the JWK Set itself is
        still only ever fetched from ``validate()``/``_signing_key()``.
        """
        if self._jwks_client is None:
            if self._config.jwks_url is None:
                # Only ever called from _signing_key()'s `if self._config.jwks_url:`
                # branch; a bare `assert` here would both vanish under
                # optimised bytecode (-O) and trip bandit B101, so guard
                # explicitly instead.
                raise TokenValidationError("jwks_url is not configured")
            self._jwks_client = jwt.PyJWKClient(
                self._config.jwks_url,
                lifespan=self._config.jwks_cache_ttl_seconds,
                timeout=self._config.jwks_http_timeout_seconds,
            )
        return self._jwks_client

    def _signing_key(self, token: str) -> str | RSAPublicKey:
        """Resolve the key to verify *token* with: JWKS first, else the static public key.

        A ``PyJWK``'s ``.key`` is a ``cryptography`` key object, not PEM
        text -- ``str()``-wrapping it here (as a prior version of this
        method did) produces a Python repr, not a usable key, and
        ``jwt.decode()`` silently fails to parse it (regression: this was
        never caught because the only previous test faked ``PyJWKClient``
        with a ``.key`` that was already a PEM string). ``jwt.decode()``
        accepts either a PEM string (static-key mode) or a raw key object
        (JWKS mode) directly, so neither path needs converting.
        """
        if self._config.jwks_url:
            # PyJWK.key is typed Any upstream (jwt.algorithms.Algorithm.from_jwk
            # has no return annotation); cast documents the actual runtime
            # type this codebase relies on (RSA keys only -- see
            # ALLOWED_ALGORITHMS-equivalent RS256-only default above).
            return cast("RSAPublicKey", self._get_jwks_client().get_signing_key_from_jwt(token).key)
        if self._config.public_key:
            return self._config.public_key
        raise TokenValidationError(
            "no verification key configured "
            "(set WADDLEAI_JWT_PUBLIC_KEY[_FILE] or WADDLEAI_JWT_JWKS_URL)"
        )

    def validate(self, token: str) -> dict[str, Any]:
        """Decode and verify *token* (signature, ``exp``, ``iss``, ``aud``); return raw claims.

        Raises ``TokenValidationError`` on any failure -- expired, bad
        signature, wrong issuer/audience, no key configured, an unknown
        ``kid`` (even after JWKS's one forced refetch), or the JWKS
        endpoint being unreachable with nothing usable cached. There is no
        fail-open path: a key-resolution failure always rejects the token.
        """
        try:
            key = self._signing_key(token)
        except TokenValidationError:
            raise
        except jwt.PyJWTError as exc:
            # jwt.PyJWKClient.get_signing_key_from_jwt raises PyJWKClientError
            # (unknown kid after refetch) or PyJWKClientConnectionError
            # (endpoint unreachable) -- both are PyJWTError subclasses, not
            # InvalidTokenError, so they need their own catch here.
            raise TokenValidationError(f"unable to resolve a signing key: {exc}") from exc
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=list(self._config.algorithms),
                audience=self._config.audience,
                issuer=self._config.issuer,
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenValidationError("token expired") from exc
        except jwt.InvalidTokenError as exc:
            raise TokenValidationError(f"invalid token: {exc}") from exc
        return claims

    def scope_context(self, token: str) -> ScopeContext:
        """Validate *token* and build the request's ``ScopeContext``.

        Raises ``TokenValidationError`` for both token-level failures
        (see ``validate``) and claims-level failures (missing tenant/sub) --
        callers only need to handle one exception type.
        """
        claims = self.validate(token)
        try:
            return scope_from_claims(claims)
        except ScopeValidationError as exc:
            raise TokenValidationError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Token extraction (shared by both gRPC metadata and HTTP headers)
# ---------------------------------------------------------------------------


def extract_bearer_token(header_value: str | None) -> str | None:
    """Extract the raw JWT from an ``Authorization: Bearer <token>`` value."""
    if not header_value:
        return None
    scheme, _, token = header_value.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def extract_token_from_grpc_metadata(metadata: Sequence[tuple[str, str]] | None) -> str | None:
    """Find the bearer token in gRPC invocation metadata (``authorization`` key)."""
    if not metadata:
        return None
    for key, value in metadata:
        if key.lower() == "authorization":
            return extract_bearer_token(value)
    return None


def extract_token_from_headers(headers: Mapping[str, str]) -> str | None:
    """Find the bearer token in an HTTP header mapping (case-insensitive lookup)."""
    for key, value in headers.items():
        if key.lower() == "authorization":
            return extract_bearer_token(value)
    return None


def authenticate_request(
    headers: Mapping[str, str], validator: WaddleAIJWTValidator
) -> ScopeContext:
    """Validate an HTTP request's ``Authorization`` header into a ``ScopeContext``.

    Integration point for penguincode's Quart REST surface -- call from an
    ``@app.before_request`` hook once wired (see module docstring); not yet
    installed in ``server/rest_app.py``.
    """
    token = extract_token_from_headers(headers)
    if not token:
        raise TokenValidationError("missing bearer token")
    return validator.scope_context(token)


# ---------------------------------------------------------------------------
# gRPC interceptor + request-scoped ScopeContext propagation
# ---------------------------------------------------------------------------

_current_scope: ContextVar[ScopeContext | None] = ContextVar(
    "penguincode_scope_context", default=None
)


def current_scope_context() -> ScopeContext | None:
    """Return the ``ScopeContext`` bound to the in-flight request, if any.

    Set by ``WaddleAIAuthInterceptor`` before invoking the real RPC handler;
    read it from within a service implementation instead of re-validating.
    """
    return _current_scope.get()


class WaddleAIAuthInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
    # grpc ships no type stubs (no types-grpcio pin here), so ServerInterceptor
    # resolves to Any; identical to the pre-existing server/interceptors.py
    # subclasses (also unannotated for the same reason).
    """gRPC interceptor validating a WaddleAI JWT and exposing a ``ScopeContext``.

    Mirrors ``server.interceptors.JWTValidationInterceptor``'s shape
    (excluded-methods list, ``UNAUTHENTICATED`` abort) but validates
    WaddleAI-issued RS256 tokens and derives the request's tenant-bounded
    ``ScopeContext`` rather than penguincode's local client-server secret.
    Not yet added to ``server/main.py``'s interceptor chain -- see module
    docstring Integration Point.
    """

    def __init__(
        self,
        validator: WaddleAIJWTValidator,
        *,
        excluded_methods: Iterable[str] = (),
    ) -> None:
        """Bind this interceptor to *validator*; *excluded_methods* skip auth (e.g. health checks)."""
        self._validator = validator
        self._excluded_methods = set(excluded_methods)

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Any],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> Any:
        """Validate the request's bearer token, then delegate or reject.

        On success, stashes the derived ``ScopeContext`` in a contextvar
        (read via ``current_scope_context()``) before calling *continuation*.
        """
        method = handler_call_details.method

        if method in self._excluded_methods:
            return await continuation(handler_call_details)

        token = extract_token_from_grpc_metadata(handler_call_details.invocation_metadata)
        if not token:
            return self._reject("missing bearer token")

        try:
            ctx = self._validator.scope_context(token)
        except TokenValidationError as exc:
            return self._reject(str(exc))

        _current_scope.set(ctx)
        return await continuation(handler_call_details)

    def _reject(self, message: str) -> Any:
        """Return a handler that aborts the call with ``UNAUTHENTICATED``."""

        async def abort_handler(request: Any, context: Any) -> None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, message)

        return grpc.unary_unary_rpc_method_handler(abort_handler)


# ---------------------------------------------------------------------------
# SPIFFE readiness (security.md Service-to-Service Auth) -- additive, never
# a JWT bypass. Called from within a service's own RPC handler (the live
# ServicerContext is not available at intercept_service time).
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SpiffeIdentity:
    """A verified SPIFFE workload identity (mTLS X.509-SVID SAN URI)."""

    spiffe_id: str
    trust_domain: str


SpiffeVerifier = Callable[[Any], "SpiffeIdentity | None"]


def default_spiffe_verifier(context: Any) -> SpiffeIdentity | None:
    """Best-effort SPIFFE ID extraction from an mTLS peer certificate.

    Reads ``context.auth_context()`` (populated by gRPC's transport-security
    layer for mTLS calls) for a ``spiffe://`` URI SAN entry. Returns ``None``
    when the call isn't mTLS-authenticated or carries no SPIFFE URI -- the
    normal case until SPIRE is deployed in a given environment (see
    security.md: "every service is SPIFFE-ready ... regardless of whether
    SPIRE is deployed"). Chain/trust-domain validation is delegated to the
    SPIRE-issued transport credentials themselves, not re-implemented here.
    """
    auth_context = context.auth_context() or {}
    for entry in auth_context.get("x509_subject_alternative_name", []):
        value = entry.decode() if isinstance(entry, bytes) else entry
        if value.startswith("spiffe://"):
            trust_domain = value.removeprefix("spiffe://").split("/", 1)[0]
            return SpiffeIdentity(spiffe_id=value, trust_domain=trust_domain)
    return None
