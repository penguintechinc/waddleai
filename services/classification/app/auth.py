"""JWT auth for the classification gRPC surface (penguin-aaa JWKS verification).

Every ``ClassificationService`` RPC needs a valid token carrying the
``classification:analyze`` scope, or an mTLS peer whose SPIFFE ID is allow-listed.
Verification reuses ``shared.auth.jwks_verifier`` (penguin-aaa claims, PyJWT); this
module adds no crypto. Tokens are never logged; only reason codes are.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import grpc
import jwt
from jwt import PyJWKClient, PyJWKClientConnectionError
from penguin_aaa.authn.types import Claims
from prometheus_client import Counter

from services.classification.app.obs import get_logger
from shared.auth.jwks_verifier import (
    JWKSVerificationError,
    JWKSVerifier,
    JWKSVerifierConfig,
    SigningKeyResolver,
)

REQUIRED_SCOPE = "classification:analyze"
HEALTH_METHOD_PREFIX = "/grpc.health.v1.Health/"
MAX_STALE_KEY_SECONDS = 3_600.0  # last-known JWKS keys outlive an outage at most 1h (token TTL cap)
OUTAGE_WARN_INTERVAL_SECONDS = 60.0
MAX_KNOWN_KEYS = 16

log = get_logger(__name__)
_AUTH_FAILURES = Counter(
    "classification_auth_failures_total", "Rejected classification RPCs by reason", ["reason"]
)


class TokenVerifier(Protocol):
    """Anything that turns a raw bearer token into validated penguin-aaa claims."""

    def verify_token(self, raw_token: str) -> Claims:
        """Return claims or raise ``JWKSVerificationError``."""
        ...


class LastKnownKeyResolver:
    """Signing-key resolver that falls back to last-known keys when the JWKS is unreachable.

    A fetch failure never rejects a token whose ``kid`` was resolved earlier (bounded
    staleness); the signature is still verified downstream, so it can never accept a
    forged token. An unknown ``kid`` with an unreachable JWKS still fails closed.
    """

    def __init__(
        self,
        inner: SigningKeyResolver,
        max_stale_seconds: float = MAX_STALE_KEY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Wrap ``inner`` (typically ``jwt.PyJWKClient``) with a bounded last-known cache."""
        self._inner = inner
        self._max_stale = max_stale_seconds
        self._clock = clock
        self._known: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = threading.Lock()
        self._last_warn = float("-inf")

    def get_signing_key_from_jwt(self, token: str) -> Any:
        """Resolve via the JWKS; on connection failure serve a fresh-enough cached key."""
        try:
            key = self._inner.get_signing_key_from_jwt(token)
        except PyJWKClientConnectionError:
            cached = self._lookup(token)
            if cached is None:
                raise
            return cached
        self._remember(token, key)
        return key

    @staticmethod
    def _kid(token: str) -> str | None:
        """Extract the unverified ``kid`` header (only used as a cache index)."""
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            return None
        return kid if isinstance(kid, str) else None

    def _remember(self, token: str, key: Any) -> None:
        """Record the resolved key under its kid, evicting the oldest beyond the bound."""
        kid = self._kid(token)
        if kid is None:
            return
        with self._lock:
            self._known[kid] = (key, self._clock())
            self._known.move_to_end(kid)
            while len(self._known) > MAX_KNOWN_KEYS:
                self._known.popitem(last=False)

    def _lookup(self, token: str) -> Any | None:
        """Return a cached key for the token's kid if it is not too stale."""
        kid = self._kid(token)
        with self._lock:
            entry = self._known.get(kid) if kid is not None else None
        if entry is None or self._clock() - entry[1] > self._max_stale:
            return None
        now = self._clock()
        if now - self._last_warn >= OUTAGE_WARN_INTERVAL_SECONDS:  # throttle: one per interval
            self._last_warn = now
            log.warning("jwks_unreachable_serving_last_known_key")
        return entry[0]


def build_verifier(
    issuer: str, audience: str, jwks_url: str, resolver: SigningKeyResolver | None = None
) -> JWKSVerifier:
    """Build the penguin-aaa JWKS verifier wrapped with a last-known-key resolver."""
    config = JWKSVerifierConfig(issuer=issuer, audience=audience, jwks_url=jwks_url)
    inner = resolver or PyJWKClient(
        jwks_url, lifespan=config.cache_ttl_seconds, timeout=config.http_timeout_seconds
    )
    return JWKSVerifier(config, LastKnownKeyResolver(inner))


@dataclass(slots=True, frozen=True)
class AuthDecision:
    """Outcome of authenticating one RPC; ``code`` None means allowed."""

    code: grpc.StatusCode | None
    reason: str


_ALLOW = AuthDecision(None, "ok")


def _bearer(metadata: Any) -> str | None:
    """Pull the bearer token from invocation metadata, or None if absent/malformed."""
    for key, value in metadata or ():
        if key.lower() == "authorization" and isinstance(value, str):
            scheme, _, token = value.partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
    return None


class JwtAuthInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]  # grpc is untyped
    """Server interceptor enforcing JWT (or allow-listed SPIFFE mTLS) auth on every RPC."""

    def __init__(
        self,
        verifier: TokenVerifier,
        spiffe_allowed_ids: frozenset[str] = frozenset(),
        required_scope: str = REQUIRED_SCOPE,
    ) -> None:
        """Bind the token verifier, SPIFFE allow-list (empty = disabled) and required scope."""
        self._verifier = verifier
        self._spiffe = spiffe_allowed_ids
        self._scope = required_scope

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        """Wrap the resolved unary handler so auth runs before the servicer."""
        handler = await continuation(handler_call_details)
        if handler is None or handler_call_details.method.startswith(HEALTH_METHOD_PREFIX):
            return handler
        behavior = handler.unary_unary
        if behavior is None:  # only unary RPCs exist in the v1 contract; fail closed otherwise

            async def deny(request: Any, context: Any) -> Any:
                await context.abort(grpc.StatusCode.UNIMPLEMENTED, "unsupported rpc type")

            return grpc.unary_unary_rpc_method_handler(deny)

        async def guarded(request: Any, context: Any) -> Any:
            decision = await self.authenticate(context)
            if decision.code is not None:
                _AUTH_FAILURES.labels(reason=decision.reason).inc()
                log.warning("classification_auth_rejected", reason=decision.reason)
                await context.abort(decision.code, decision.reason)
            return await behavior(request, context)

        return handler._replace(unary_unary=guarded)

    async def authenticate(self, context: Any) -> AuthDecision:
        """Authorize via allow-listed SPIFFE mTLS peer, else a bearer JWT with the scope."""
        if self._spiffe_peer_allowed(context):
            return _ALLOW
        token = _bearer(context.invocation_metadata())
        if token is None:
            return AuthDecision(grpc.StatusCode.UNAUTHENTICATED, "missing_token")
        try:
            claims = await asyncio.to_thread(self._verifier.verify_token, token)
        except JWKSVerificationError as exc:
            expired = "expired" in str(exc).lower()
            return AuthDecision(
                grpc.StatusCode.UNAUTHENTICATED, "token_expired" if expired else "invalid_token"
            )
        if self._scope not in claims.scope:
            return AuthDecision(grpc.StatusCode.PERMISSION_DENIED, "insufficient_scope")
        return _ALLOW

    def _spiffe_peer_allowed(self, context: Any) -> bool:
        """True when a TLS-verified peer presents an allow-listed SPIFFE URI SAN."""
        if not self._spiffe:
            return False
        auth_ctx = context.auth_context() or {}
        if b"ssl" not in auth_ctx.get("transport_security_type", []):
            return False
        sans = auth_ctx.get("x509_subject_alternative_name", [])
        for raw in sans:
            san = raw.decode("utf-8", errors="replace")
            if san.startswith("spiffe://") and san in self._spiffe:  # SPIFFE URI SANs only
                return True
        return False
