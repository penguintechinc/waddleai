"""gRPC interceptors for authentication and request processing.

**Chat RS256 gate (tenancy-gap fix).** `ChatService` RPCs used to run
entirely under the legacy HS256 path below (`JWTValidationInterceptor`/
`PassthroughInterceptor`), with real multi-tenant scoping faked via a
synthesized `_legacy` pseudo-tenant keyed on the HS256 token's `sub` (see
`server/services/chat.py`'s module docstring) -- unlike `KnowledgeService`/
`LessonsService`, which `server/main.py`'s `MethodPrefixRoutingInterceptor`
already routes to `auth.middleware.WaddleAIAuthInterceptor` (RS256,
JWKS/public-key-validated, derives a real tenant-bounded `ScopeContext`).

Rather than teaching `server/main.py`'s nested `MethodPrefixRoutingInterceptor`
a third prefix (which would require every caller of `JWTValidationInterceptor`/
`PassthroughInterceptor` to also thread a shared `WaddleAIAuthInterceptor`
instance through unrelated call sites), the gate is applied *inside* both
classes below via `_maybe_route_chat_through_rs256`: any call whose method
starts with `_CHAT_SERVICE_METHOD_PREFIX` is diverted to a `WaddleAIAuthInterceptor`
instead of this module's own HS256/passthrough logic, unless the
`penguincode.disable-chat-rs256-gate` opt-out kill switch is ON (see
`flags.client.DISABLE_CHAT_RS256_GATE_FLAG`) -- the emergency rollback to the
pre-fix legacy behavior, logged at WARN exactly once per process. Every
construction site (`server/main.py`'s `legacy_interceptor`, today's sole
caller) gets this for free with no change to its own call site.
"""

import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import grpc
import jwt
from opentelemetry import trace

from penguincode_cli.auth.middleware import WaddleAIAuthInterceptor, WaddleAIJWTValidator
from penguincode_cli.flags.client import DISABLE_CHAT_RS256_GATE_FLAG, SYSTEM_SCOPE, is_enabled
from penguincode_cli.observability import otel

logger = logging.getLogger(__name__)

#: Method path prefix for every `ChatService` RPC (see
#: `proto/penguincode.proto`'s `service ChatService` -- the top-level
#: `package penguincode;`, mirroring `/penguincode.AuthService/...` and
#: `/penguincode.HealthService/...` below). Used by both
#: `JWTValidationInterceptor` and `PassthroughInterceptor` to divert Chat
#: calls to the RS256 gate -- see module docstring.
_CHAT_SERVICE_METHOD_PREFIX = "/penguincode.ChatService/"

#: Warn-once latch for the kill-switch fallback (see
#: `_maybe_route_chat_through_rs256`) -- a live deployment with the switch ON
#: would otherwise log this on every single `ChatService` call.
_chat_rs256_gate_disabled_warned = False
_chat_rs256_gate_disabled_warn_lock = threading.Lock()


def _warn_chat_rs256_gate_disabled_once() -> None:
    """Log, exactly once per process, that the Chat RS256 gate is disabled."""
    global _chat_rs256_gate_disabled_warned
    with _chat_rs256_gate_disabled_warn_lock:
        if _chat_rs256_gate_disabled_warned:
            return
        _chat_rs256_gate_disabled_warned = True
    logger.warning(
        "%s is ON -- ChatService RPCs are falling back to legacy HS256 auth with a "
        "synthesized pseudo-tenant scope instead of the RS256 WaddleAI ScopeContext "
        "gate; this is an emergency rollback path only, not the steady-state default",
        DISABLE_CHAT_RS256_GATE_FLAG,
    )


def reset_chat_rs256_warning_for_testing() -> None:
    """Clear the warn-once latch -- test isolation only, never called in production."""
    global _chat_rs256_gate_disabled_warned
    with _chat_rs256_gate_disabled_warn_lock:
        _chat_rs256_gate_disabled_warned = False


class _ChatWaddleAIInterceptorHolder:
    """Lazily resolves the `WaddleAIAuthInterceptor` used to gate `ChatService` calls.

    *override*, when given (test seam, or a future caller wiring a shared
    instance), is always preferred -- this mirrors every other
    constructor-injection test seam in this codebase (e.g.
    `ChatServiceImpl.__init__`'s `session_store`). Without an override, one
    instance is built lazily, from env, the first time any `ChatService`
    call needs it, and reused for the lifetime of the holder -- the JWKS
    cache (if `WADDLEAI_JWT_JWKS_URL` is configured) is therefore warmed at
    most once per process per holder, not once per call.
    """

    def __init__(self, override: grpc.aio.ServerInterceptor | None = None) -> None:
        """Bind this holder to *override*, or defer to lazy env-driven construction."""
        self._override = override
        self._built: grpc.aio.ServerInterceptor | None = None

    def get(self) -> grpc.aio.ServerInterceptor:
        """Return the bound `WaddleAIAuthInterceptor`, building the default on first use."""
        if self._override is not None:
            return self._override
        if self._built is None:
            self._built = WaddleAIAuthInterceptor(WaddleAIJWTValidator())
        return self._built


async def _maybe_route_chat_through_rs256(
    chat_gate: _ChatWaddleAIInterceptorHolder,
    method: str,
    continuation: Callable[[grpc.HandlerCallDetails], Any],
    handler_call_details: grpc.HandlerCallDetails,
) -> tuple[bool, Any]:
    """Divert a `ChatService` call to the RS256 gate, unless the kill switch is ON.

    Returns `(True, result)` when *method* was a `ChatService` call that this
    function fully handled (the caller must return `result` immediately,
    never falling through to its own HS256/passthrough logic) and
    `(False, None)` for every other method, or when the kill switch is ON
    (logged at WARN exactly once -- see `_warn_chat_rs256_gate_disabled_once`)
    and the caller should proceed with its own legacy behavior instead.
    """
    if not method.startswith(_CHAT_SERVICE_METHOD_PREFIX):
        return False, None
    if is_enabled(DISABLE_CHAT_RS256_GATE_FLAG, SYSTEM_SCOPE):
        _warn_chat_rs256_gate_disabled_once()
        return False, None
    result = await chat_gate.get().intercept_service(continuation, handler_call_details)
    return True, result


class JWTValidationInterceptor(grpc.aio.ServerInterceptor):
    """Interceptor that validates JWT tokens on incoming requests.

    Extracts token from 'authorization' metadata and validates it.
    Skips validation for excluded methods (e.g., Authenticate, Health).
    """

    def __init__(
        self,
        jwt_secret: str,
        excluded_methods: list[str] | None = None,
        *,
        waddleai_interceptor: grpc.aio.ServerInterceptor | None = None,
    ):
        self.jwt_secret = jwt_secret
        self.excluded_methods = set(excluded_methods or [])
        self._chat_gate = _ChatWaddleAIInterceptorHolder(waddleai_interceptor)

    async def intercept_service(
        self,
        continuation: Callable,
        handler_call_details: grpc.HandlerCallDetails,
    ):
        """Intercept and validate requests."""
        method = handler_call_details.method

        # Chat RS256 gate (tenancy-gap fix) -- see module docstring. Checked
        # ahead of `excluded_methods`/HS256 validation: a ChatService call
        # must never fall through to this interceptor's own HS256 logic
        # while the gate is active.
        handled, result = await _maybe_route_chat_through_rs256(
            self._chat_gate, method, continuation, handler_call_details
        )
        if handled:
            return result

        # Skip validation for excluded methods
        if method in self.excluded_methods:
            return await continuation(handler_call_details)

        # Extract authorization header
        metadata = dict(handler_call_details.invocation_metadata or [])
        auth_header = metadata.get("authorization", "")

        if not auth_header:
            return self._unauthenticated_handler("Missing authorization header")

        # Extract token from "Bearer <token>"
        if not auth_header.startswith("Bearer "):
            return self._unauthenticated_handler("Invalid authorization format")

        token = auth_header[7:]  # Remove "Bearer " prefix

        # Validate token
        try:
            claims = jwt.decode(
                token,
                self.jwt_secret,
                algorithms=["HS256"],
            )

            if claims.get("type") != "access":
                return self._unauthenticated_handler("Invalid token type")

            # Token is valid, continue with request
            logger.debug(f"Authenticated request from {claims.get('sub')} to {method}")
            return await continuation(handler_call_details)

        except jwt.ExpiredSignatureError:
            return self._unauthenticated_handler("Token expired")
        except jwt.InvalidTokenError as e:
            return self._unauthenticated_handler(f"Invalid token: {e}")

    def _unauthenticated_handler(self, message: str):
        """Return a handler that rejects the request."""

        async def abort_handler(request, context):
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                message,
            )

        return grpc.unary_unary_rpc_method_handler(abort_handler)


class PassthroughInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
    # grpc ships no type stubs (no types-grpcio pin here), so ServerInterceptor
    # resolves to Any -- identical to every other subclass in this file.
    """No-op interceptor: every call goes straight through to its real handler.

    Used as `MethodPrefixRoutingInterceptor`'s "unmatched" branch (see below)
    when penguincode's local HS256 auth is disabled
    (``settings.auth.enabled=False``) but `KnowledgeService`'s RS256 gate must
    still be installed unconditionally -- see ``server/main.py``'s module
    docstring ("Interceptor reconciliation").

    `ChatService` calls are the one exception to "no auth check at all" --
    they still go through the Chat RS256 gate (module docstring above)
    unless the kill switch is ON, exactly like `JWTValidationInterceptor`.
    Local standalone mode having no local HS256 secret configured does not
    exempt it from the tenancy fix: a `ChatService` caller in that mode
    still needs a real WaddleAI RS256 JWT by default.
    """

    def __init__(self, *, waddleai_interceptor: grpc.aio.ServerInterceptor | None = None) -> None:
        """Bind this passthrough's Chat RS256 gate to *waddleai_interceptor* (test seam)."""
        self._chat_gate = _ChatWaddleAIInterceptorHolder(waddleai_interceptor)

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Any],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> Any:
        """Delegate unconditionally to *continuation* -- except a gated `ChatService` call."""
        handled, result = await _maybe_route_chat_through_rs256(
            self._chat_gate, handler_call_details.method, continuation, handler_call_details
        )
        if handled:
            return result
        return await continuation(handler_call_details)


class MethodPrefixRoutingInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
    # grpc ships no type stubs (no types-grpcio pin here), so ServerInterceptor
    # resolves to Any -- identical to every other subclass in this file.
    """Routes each call to one of two interceptors, chosen by its method path's prefix.

    Reconciles two interceptors that would otherwise both claim the same
    ``authorization`` invocation-metadata key for two different token kinds --
    penguincode's local HS256 client-server secret (`JWTValidationInterceptor`)
    vs. a WaddleAI-issued RS256 JWT (`auth.middleware.WaddleAIAuthInterceptor`)
    -- see ``server/main.py``'s "Interceptor reconciliation" module docstring
    note for the full rationale.

    A call whose method starts with *prefix* is gated by *matched*; every
    other call is gated by *unmatched*. Exactly one interceptor ever sees a
    given call, so neither needs its own ``excluded_methods`` to enumerate
    the other's methods -- new RPCs on either side of the split need no
    change here as long as their method path keeps the same prefix
    convention.
    """

    def __init__(
        self,
        prefix: str,
        *,
        matched: grpc.aio.ServerInterceptor,
        unmatched: grpc.aio.ServerInterceptor,
    ) -> None:
        """Bind this router to *prefix*, dispatching to *matched*/*unmatched* accordingly."""
        self._prefix = prefix
        self._matched = matched
        self._unmatched = unmatched

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Any],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> Any:
        """Dispatch to whichever interceptor owns *handler_call_details*'s method."""
        target = (
            self._matched
            if handler_call_details.method.startswith(self._prefix)
            else self._unmatched
        )
        return await target.intercept_service(continuation, handler_call_details)


class LoggingInterceptor(grpc.aio.ServerInterceptor):
    """Interceptor that logs all requests."""

    async def intercept_service(
        self,
        continuation: Callable,
        handler_call_details: grpc.HandlerCallDetails,
    ):
        """Log request details."""
        method = handler_call_details.method
        logger.info(f"Request: {method}")

        try:
            result = await continuation(handler_call_details)
            logger.info(f"Completed: {method}")
            return result
        except Exception as e:
            logger.error(f"Error in {method}: {e}")
            raise


def _status_label(context: Any, *, raised: bool) -> str:
    """The ``grpc.StatusCode`` name set on *context*, or a safe fallback.

    A successful unary/streaming call rarely calls ``set_code``/``abort``
    explicitly, so ``context.code()`` is usually ``None`` -- that case maps
    to ``"OK"`` when nothing raised, ``"UNKNOWN"`` when something did (e.g.
    an exception that is not a gRPC-aware abort).
    """
    code = context.code()
    if code is not None:
        # grpc ships no type stubs, so `code` is `Any` here -- `str()` keeps
        # this function's declared `-> str` honest rather than returning Any.
        return str(code.name)
    return "UNKNOWN" if raised else "OK"


def _finish_rpc_span(
    span: trace.Span,
    context: Any,
    service: str,
    method: str,
    start: float,
    exc: BaseException | None,
) -> None:
    """Set the final status attribute on *span* and emit the RPC metrics.

    Shared tail for both the unary and streaming wrappers below -- kept as
    one function so the status-label logic and the metric emission can
    never drift between the two call shapes. Does *not* call
    ``span.record_exception``/``set_status`` itself on failure --
    ``otel.rpc_server_span``'s underlying ``start_as_current_span`` context
    manager already does both automatically when an exception propagates
    out of its ``with`` block (its default ``record_exception``/
    ``set_status_on_exception`` behavior), so doing it here too would
    double-record the same exception.
    """
    label = _status_label(context, raised=exc is not None)
    span.set_attribute("rpc.grpc.status_code", label)
    duration_ms = (time.perf_counter() - start) * 1000
    otel.record_rpc_server_call(service, method, label, duration_ms)


class TracingInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
    # grpc ships no type stubs (no types-grpcio pin here), so ServerInterceptor
    # resolves to Any -- identical to every other subclass in this file.
    """Opens a SERVER span + records duration/count metrics for every RPC.

    Installed outermost (ahead of the auth/routing interceptors in
    ``server/main.py``) so every call -- authenticated, rejected, or
    successful -- gets exactly one span and one metric point. Extracts
    incoming W3C ``traceparent``/``baggage`` metadata (see
    ``observability.otel.rpc_server_span``) so a caller's span, if any,
    becomes this span's parent, completing the cross-service trace chain
    required by the org's OTel observability rule (O1, gRPC server
    hardening). Span/metric labels (``rpc.service``, ``rpc.method``,
    ``rpc.grpc.status_code``) are all bounded, closed-set values taken from
    the server's own registered method table and gRPC's ``StatusCode``
    enum -- never request content, tenant, or user identifiers.

    Supports all four RPC shapes (unary-unary, unary-stream, stream-unary,
    stream-stream); streaming responses are timed end-to-end, from the
    first item requested to the generator's exhaustion or failure.
    """

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Any],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> Any:
        """Wrap the real handler's behavior function with a span + metrics."""
        handler = await continuation(handler_call_details)
        if handler is None:
            return None

        method_path = (handler_call_details.method or "").lstrip("/")
        service, _, method_name = method_path.rpartition("/")
        carrier: Mapping[str, str] = dict(handler_call_details.invocation_metadata or [])

        if handler.unary_unary is not None:
            return handler._replace(
                unary_unary=self._wrap_unary(handler.unary_unary, service, method_name, carrier)
            )
        if handler.stream_unary is not None:
            return handler._replace(
                stream_unary=self._wrap_unary(handler.stream_unary, service, method_name, carrier)
            )
        if handler.unary_stream is not None:
            return handler._replace(
                unary_stream=self._wrap_stream(handler.unary_stream, service, method_name, carrier)
            )
        if handler.stream_stream is not None:
            return handler._replace(
                stream_stream=self._wrap_stream(
                    handler.stream_stream, service, method_name, carrier
                )
            )
        return handler

    @staticmethod
    def _wrap_unary(
        behavior: Callable[..., Any], service: str, method: str, carrier: Mapping[str, str]
    ) -> Callable[..., Any]:
        """Wrap a unary-response behavior (unary_unary or stream_unary)."""

        async def _instrumented(request_or_iterator: Any, context: Any) -> Any:
            start = time.perf_counter()
            with otel.rpc_server_span(service, method, carrier) as span:
                try:
                    response = await behavior(request_or_iterator, context)
                except Exception as exc:
                    _finish_rpc_span(span, context, service, method, start, exc)
                    raise
                _finish_rpc_span(span, context, service, method, start, None)
                return response

        return _instrumented

    @staticmethod
    def _wrap_stream(
        behavior: Callable[..., AsyncIterator[Any]],
        service: str,
        method: str,
        carrier: Mapping[str, str],
    ) -> Callable[..., AsyncIterator[Any]]:
        """Wrap a streaming-response behavior (unary_stream or stream_stream)."""

        async def _instrumented(request_or_iterator: Any, context: Any) -> AsyncIterator[Any]:
            start = time.perf_counter()
            with otel.rpc_server_span(service, method, carrier) as span:
                try:
                    async for item in behavior(request_or_iterator, context):
                        yield item
                except Exception as exc:
                    _finish_rpc_span(span, context, service, method, start, exc)
                    raise
                else:
                    _finish_rpc_span(span, context, service, method, start, None)

        return _instrumented
