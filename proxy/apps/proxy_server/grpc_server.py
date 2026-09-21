"""WaddleAI gRPC Server.

Receives calls from the Go AILB and delegates to the Python agent/engine
layer (RoutingEngineRouteEvaluator, SecurityAgent, UsageTracker) and memory
subsystem (WaddleAIMemoryManager).

Usage:
    # Standalone (blocking)
    start_grpc_server(port=50051, server_components=components)

    # Alongside Quart/Flask (daemon thread)
    run_grpc_in_thread(port=50051, components=components)
"""

from __future__ import annotations

import functools
import hmac
import os
import threading
from collections.abc import Callable
from concurrent import futures
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import grpc
import structlog
from grpc_proto.waddleai.v1 import proxy_pb2 as waddleai_pb2
from grpc_proto.waddleai.v1 import proxy_pb2_grpc as waddleai_pb2_grpc

from shared.agents import SecurityAgent, UsageTracker
from shared.agents.usage_tracker import UsageReport as AgentUsageReport
from shared.routing.grpc_adapter import RoutingEngineRouteEvaluator
from shared.utils.memory_integration import WaddleAIMemoryManager

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# gRPC Authentication Interceptor (fail-closed)
# ---------------------------------------------------------------------------


class GrpcAuthInterceptor(grpc.ServerInterceptor):
    """Server-side interceptor requiring Bearer token in call metadata.

    Mandatory authentication for all gRPC calls. Every call must include
    Authorization metadata with value 'Bearer <token>' where <token>
    matches the configured secret (constant-time comparison via hmac).

    Fail-closed: if the configured token is unset/None/empty, all calls
    are rejected with UNAUTHENTICATED. This ensures that if the env var
    is not set (accidental misconfiguration), the gRPC surface remains
    protected rather than falling open.
    """

    def __init__(self, configured_token: str | None) -> None:
        """Initialize with configured secret token.

        Args:
            configured_token: Pre-shared Bearer token. If None or empty,
                all calls are rejected.

        """
        self.configured_token = configured_token

    def intercept_service(
        self, continuation: Callable, handler_call_details: grpc.HandlerCallDetails
    ) -> grpc.RpcMethodHandler:
        """Intercept all service calls and validate Bearer token."""
        # Fail-closed: if no token configured, reject all calls
        if not self.configured_token:
            return self._abort(grpc.StatusCode.UNAUTHENTICATED, "gRPC auth not configured")

        # Extract authorization metadata (gRPC metadata keys are lowercase)
        metadata = dict(handler_call_details.invocation_metadata)
        auth_header = metadata.get("authorization", "")

        # Validate format: "Bearer <token>"
        if not auth_header.startswith("Bearer "):
            return self._abort(
                grpc.StatusCode.UNAUTHENTICATED, "Missing or invalid authorization header"
            )

        # Extract token and validate with constant-time comparison
        provided_token = auth_header[7:]  # Strip "Bearer "
        if not hmac.compare_digest(provided_token, self.configured_token):
            return self._abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid token")

        # Token valid, proceed to handler
        return continuation(handler_call_details)

    @staticmethod
    def _abort(code: grpc.StatusCode, details: str) -> grpc.RpcMethodHandler:
        """Return a handler that immediately aborts with the given code and details."""

        def abort_handler(request, context: grpc.ServicerContext):
            context.abort(code, details)

        return grpc.unary_unary_rpc_method_handler(abort_handler)


# ---------------------------------------------------------------------------
# api_version routing (house gRPC-versioning contract)
# ---------------------------------------------------------------------------

#: Only wire-compatible request version this server understands today.
SUPPORTED_API_VERSIONS: frozenset[str] = frozenset({"v1"})


@dataclass(slots=True)
class ApiVersionRouter:
    """Method decorator enforcing a supported ``api_version`` on every RPC.

    Every request message on ``WaddleAIService`` carries ``api_version``
    (proto field 1). Wrapping a servicer method with this router makes the
    version check the first thing that runs: a known version passes through
    to the handler unmodified, while a missing or unknown version aborts
    UNIMPLEMENTED before any component (routing/security/memory/usage) is
    touched -- callers never silently fall through to a handler built for a
    different wire shape.
    """

    supported_versions: frozenset[str] = field(default_factory=lambda: SUPPORTED_API_VERSIONS)

    def __call__(self, handler: Callable[..., Any]) -> Callable[..., Any]:
        """Return *handler* wrapped with an api_version precondition check."""

        @functools.wraps(handler)
        def _wrapped(servicer: Any, request: Any, context: grpc.ServicerContext) -> Any:
            version = request.api_version
            if version not in self.supported_versions:
                context.abort(
                    grpc.StatusCode.UNIMPLEMENTED,
                    f"api_version {version} not supported",
                )
            return handler(servicer, request, context)

        return _wrapped


#: Shared instance applied to every unary WaddleAIServiceServicer RPC below.
require_api_version = ApiVersionRouter()


# ---------------------------------------------------------------------------
# Per-caller identity (tenant / user isolation)
# ---------------------------------------------------------------------------

#: Call-metadata key carrying the *per-caller* WaddleAI credential.
#:
#: Deliberately distinct from ``authorization``, which carries the single
#: shared ``PROXY_GRPC_AUTH_TOKEN`` and proves only that the caller is the
#: AILB -- it establishes no user and no tenant. The value of this key uses
#: the exact same grammar as the REST ``Authorization`` header (a raw
#: ``wa-``/``sk-`` API key, or ``Bearer <jwt>``) so that both surfaces verify
#: through one code path rather than two.
CALLER_CREDENTIAL_METADATA_KEY = "x-waddleai-credential"

#: Migration escape hatch restoring the pre-hardening, unscoped behaviour
#: (``user_id`` read from the request body, ``organization_id`` hardcoded to
#: 0) for out-of-repo callers that cannot yet forward a per-caller
#: credential. Defaults OFF; every call served through it logs at ERROR.
LEGACY_UNSCOPED_IDENTITY_ENV_VAR = "WADDLEAI_GRPC_ALLOW_UNSCOPED_IDENTITY"

_TRUTHY_ENV_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def _legacy_unscoped_identity_enabled() -> bool:
    """Report whether the legacy unscoped-identity escape hatch is switched on.

    Fail-secure by construction: an unset, empty, or unrecognised value yields
    ``False``, so a deployment that never sets the variable is always
    tenant-scoped.
    """
    return os.getenv(LEGACY_UNSCOPED_IDENTITY_ENV_VAR, "").strip().lower() in _TRUTHY_ENV_VALUES


@dataclass(slots=True, frozen=True)
class CallerIdentity:
    """Identity of a gRPC caller, derived from a *verified* credential.

    Only ever produced by :attr:`ServerComponents.identity_resolver`; it is
    never assembled from request-body fields, which are attacker-controlled.
    """

    user_id: int
    organization_id: int
    api_key_id: int | None = None
    username: str = ""


#: Verifies a raw credential string, returning the caller's identity and
#: raising on anything invalid, expired, or unknown.
IdentityResolver = Callable[[str], CallerIdentity]


# ---------------------------------------------------------------------------
# Server components container
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ServerComponents:
    """Holds references to the agent and memory subsystems.

    All subsystem fields are optional so the server can start in a degraded
    mode when a subsystem is unavailable. ``identity_resolver`` is what makes
    the memory/usage RPCs tenant-safe: without it (and without the legacy
    escape hatch) those RPCs refuse every call.
    """

    routing_agent: RoutingEngineRouteEvaluator | None = None
    security_agent: SecurityAgent | None = None
    usage_tracker: UsageTracker | None = None
    memory_manager: WaddleAIMemoryManager | None = None
    identity_resolver: IdentityResolver | None = None
    allow_unscoped_identity: bool = field(default_factory=_legacy_unscoped_identity_enabled)


# ---------------------------------------------------------------------------
# Servicer implementation
# ---------------------------------------------------------------------------


class WaddleAIServiceServicer(waddleai_pb2_grpc.WaddleAIServiceServicer):
    """Concrete implementation of the WaddleAIService gRPC service."""

    def __init__(self, components: ServerComponents) -> None:
        """Bind the servicer to its backing agent/memory components."""
        self._components = components

    # ---- caller identity --------------------------------------------------

    def _verified_identity(self, context: grpc.ServicerContext) -> CallerIdentity | None:
        """Verify the per-caller credential in call metadata, when one is sent.

        Returns ``None`` when the caller sent no credential at all; aborts the
        RPC when a credential is present but cannot be verified. The request
        body is never consulted -- identity comes from the credential only.
        """
        metadata = {str(key).lower(): value for key, value in (context.invocation_metadata() or ())}
        credential = str(metadata.get(CALLER_CREDENTIAL_METADATA_KEY) or "").strip()
        if not credential:
            return None

        resolver = self._components.identity_resolver
        if resolver is None:
            logger.error(
                "gRPC caller credential supplied but no identity resolver is wired",
                metadata_key=CALLER_CREDENTIAL_METADATA_KEY,
            )
            context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "Caller credential verification is unavailable",
            )
            return None  # pragma: no cover -- context.abort() raises

        try:
            return resolver(credential)
        except Exception as exc:
            logger.warning("gRPC caller credential rejected", error=str(exc))
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid caller credential")
            return None  # pragma: no cover -- context.abort() raises

    def _required_identity(self, context: grpc.ServicerContext) -> CallerIdentity | None:
        """Return the verified caller identity, or refuse the call outright.

        Returns ``None`` only when the legacy escape hatch is enabled, in which
        case the call proceeds unscoped and an ERROR is logged every time.
        """
        identity = self._verified_identity(context)
        if identity is not None:
            return identity

        if self._components.allow_unscoped_identity:
            logger.error(
                "gRPC call served WITHOUT tenant isolation -- legacy escape hatch active; "
                "unset the env var and forward a per-caller credential instead",
                env_var=LEGACY_UNSCOPED_IDENTITY_ENV_VAR,
                metadata_key=CALLER_CREDENTIAL_METADATA_KEY,
            )
            return None

        context.abort(
            grpc.StatusCode.PERMISSION_DENIED,
            f"Per-caller credential required in '{CALLER_CREDENTIAL_METADATA_KEY}' metadata",
        )
        return None  # pragma: no cover -- context.abort() raises

    def _memory_scope(self, request: Any, context: grpc.ServicerContext) -> tuple[int, int]:
        """Resolve the ``(user_id, organization_id)`` this call is allowed to touch.

        Both values come from the verified credential. The legacy escape hatch
        is the only remaining path that reads ``request.user_id`` and pools
        every tenant into organization 0.
        """
        identity = self._required_identity(context)
        if identity is None:
            return _safe_int(request.user_id, default=0), 0
        return identity.user_id, identity.organization_id

    # ---- EvaluateRoute ----------------------------------------------------

    @require_api_version
    def EvaluateRoute(  # noqa: N802 -- gRPC servicer method name mandated by generated proto stub
        self,
        request: waddleai_pb2.RouteRequest,
        context: grpc.ServicerContext,
    ) -> waddleai_pb2.RouteResponse:
        """Classify prompt complexity and recommend a model."""
        agent = self._components.routing_agent
        if agent is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details("RoutingAgent not configured")
            return waddleai_pb2.RouteResponse()

        try:
            import asyncio

            decision = asyncio.run(
                agent.evaluate(
                    prompt=request.prompt,
                    tool_type=request.tool_type,
                    region=request.region or "NA",
                )
            )

            return waddleai_pb2.RouteResponse(
                recommended_model=decision.model,
                complexity=decision.complexity,
                target_type=decision.target_type,
                confidence=decision.confidence,
                reasoning=decision.reasoning,
            )
        except Exception as exc:
            logger.error("EvaluateRoute failed", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"Routing evaluation error: {exc}")
            return waddleai_pb2.RouteResponse()

    # ---- EvaluateSecurity -------------------------------------------------

    @require_api_version
    def EvaluateSecurity(  # noqa: N802 -- gRPC servicer method name mandated by proto stub
        self,
        request: waddleai_pb2.SecurityRequest,
        context: grpc.ServicerContext,
    ) -> waddleai_pb2.SecurityResponse:
        """Evaluate a raw command for security threats."""
        # Advisory identity only (SecurityAgent uses user_id for audit logging,
        # not as a data boundary), so an absent credential is not fatal here --
        # but a body-supplied user_id is never honoured either way.
        identity = self._verified_identity(context)

        agent = self._components.security_agent
        if agent is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details("SecurityAgent not configured")
            return waddleai_pb2.SecurityResponse()

        try:
            import asyncio

            user_id: int | None = identity.user_id if identity is not None else None

            decision = asyncio.run(
                agent.evaluate(
                    raw_command=request.raw_command,
                    tool_type=request.tool_type,
                    user_id=user_id,
                )
            )

            return waddleai_pb2.SecurityResponse(
                safe=decision.safe,
                risk_score=decision.risk_score,
                threat_type=decision.threat_type or "",
                explanation=decision.explanation,
                blocked=decision.blocked,
            )
        except Exception as exc:
            logger.error("EvaluateSecurity failed", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"Security evaluation error: {exc}")
            return waddleai_pb2.SecurityResponse()

    # ---- StoreTurn --------------------------------------------------------

    @require_api_version
    def StoreTurn(  # noqa: N802 -- gRPC servicer method name mandated by generated proto stub
        self,
        request: waddleai_pb2.StoreTurnRequest,
        context: grpc.ServicerContext,
    ) -> waddleai_pb2.StoreTurnResponse:
        """Store a conversation turn in the memory subsystem."""
        # Identity is resolved before the availability check so an
        # unauthenticated caller learns nothing about server configuration,
        # and outside the try/except so context.abort() is not swallowed.
        user_id_int, organization_id = self._memory_scope(request, context)

        mgr = self._components.memory_manager
        if mgr is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details("MemoryManager not configured")
            return waddleai_pb2.StoreTurnResponse(success=False)

        try:
            import asyncio

            messages: list[dict[str, str]] = [
                {"role": "user", "content": request.user_message},
            ]
            metadata: dict[str, Any] = dict(request.metadata)
            metadata.setdefault("model", request.model)
            metadata.setdefault("provider", request.provider)

            success = asyncio.run(
                mgr.add_conversation_turn(
                    user_id=user_id_int,
                    organization_id=organization_id,
                    messages=messages,
                    response=request.assistant_response,
                    session_id=request.session_id or None,
                    metadata=metadata,
                )
            )

            return waddleai_pb2.StoreTurnResponse(success=success)
        except Exception as exc:
            logger.error("StoreTurn failed", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"Store turn error: {exc}")
            return waddleai_pb2.StoreTurnResponse(success=False)

    # ---- GetContext -------------------------------------------------------

    @require_api_version
    def GetContext(  # noqa: N802 -- gRPC servicer method name mandated by generated proto stub
        self,
        request: waddleai_pb2.GetContextRequest,
        context: grpc.ServicerContext,
    ) -> waddleai_pb2.GetContextResponse:
        """Retrieve conversation context for a session."""
        user_id_int, organization_id = self._memory_scope(request, context)

        mgr = self._components.memory_manager
        if mgr is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details("MemoryManager not configured")
            return waddleai_pb2.GetContextResponse()

        try:
            import asyncio

            limit = request.limit if request.limit > 0 else 5

            conv_context = asyncio.run(
                mgr.get_conversation_context(
                    user_id=user_id_int,
                    organization_id=organization_id,
                    current_messages=[],
                    session_id=request.session_id or None,
                    context_limit=limit,
                )
            )

            proto_memories = _memory_entries_to_proto(conv_context.relevant_memories)

            return waddleai_pb2.GetContextResponse(
                memories=proto_memories,
                summary=conv_context.conversation_summary or "",
            )
        except Exception as exc:
            logger.error("GetContext failed", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"Get context error: {exc}")
            return waddleai_pb2.GetContextResponse()

    # ---- SearchMemories ---------------------------------------------------

    @require_api_version
    def SearchMemories(  # noqa: N802 -- gRPC servicer method name mandated by generated proto stub
        self,
        request: waddleai_pb2.SearchMemoriesRequest,
        context: grpc.ServicerContext,
    ) -> waddleai_pb2.SearchMemoriesResponse:
        """Search memories by query text."""
        user_id_int, organization_id = self._memory_scope(request, context)

        mgr = self._components.memory_manager
        if mgr is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details("MemoryManager not configured")
            return waddleai_pb2.SearchMemoriesResponse()

        try:
            import asyncio

            limit = request.limit if request.limit > 0 else 10
            threshold = request.threshold if request.threshold > 0.0 else 0.7

            results = asyncio.run(
                mgr.memory_store.search_memories(
                    query=request.query,
                    user_id=user_id_int,
                    organization_id=organization_id,
                    limit=limit,
                    min_relevance=threshold,
                )
            )

            proto_memories = _memory_entries_to_proto(results)

            return waddleai_pb2.SearchMemoriesResponse(results=proto_memories)
        except Exception as exc:
            logger.error("SearchMemories failed", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"Search memories error: {exc}")
            return waddleai_pb2.SearchMemoriesResponse()

    # ---- ReportUsage ------------------------------------------------------

    @require_api_version
    def ReportUsage(  # noqa: N802 -- gRPC servicer method name mandated by generated proto stub
        self,
        request: waddleai_pb2.UsageReport,
        context: grpc.ServicerContext,
    ) -> waddleai_pb2.UsageAck:
        """Record token usage for a completed request."""
        identity = self._required_identity(context)
        if identity is None:
            # Legacy escape hatch only -- already logged at ERROR upstream.
            report_user_id = request.user_id
            report_api_key_id = request.api_key_id or None
        else:
            report_user_id = str(identity.user_id)
            report_api_key_id = (
                str(identity.api_key_id) if identity.api_key_id is not None else None
            )

        tracker = self._components.usage_tracker
        if tracker is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details("UsageTracker not configured")
            return waddleai_pb2.UsageAck(
                accepted=False,
                quota_exceeded=False,
                message="UsageTracker not configured",
            )

        try:
            import asyncio

            report = AgentUsageReport(
                user_id=report_user_id,
                model=request.model,
                input_tokens=request.input_tokens,
                output_tokens=request.output_tokens,
                total_tokens=request.total_tokens,
                api_key_id=report_api_key_id,
                provider=request.provider or None,
                latency_ms=float(request.latency_ms) if request.latency_ms else None,
                request_id=request.request_id or None,
            )

            ack = asyncio.run(tracker.record_usage(report))

            return waddleai_pb2.UsageAck(
                accepted=ack.accepted,
                quota_exceeded=ack.quota_exceeded,
                message=ack.message,
            )
        except Exception as exc:
            logger.error("ReportUsage failed", error=str(exc))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"Usage reporting error: {exc}")
            return waddleai_pb2.UsageAck(
                accepted=False,
                quota_exceeded=False,
                message=f"Internal error: {exc}",
            )


# ---------------------------------------------------------------------------
# Server lifecycle helpers
# ---------------------------------------------------------------------------


def start_grpc_server(
    port: int = 50051,
    server_components: ServerComponents | None = None,
    max_workers: int = 10,
    grpc_auth_token: str | None = None,
) -> grpc.Server:
    """Create, configure, and start the gRPC server.

    The server binds to an insecure port. TLS termination is expected to
    be handled by the Kubernetes ingress / service mesh.

    All calls require Bearer token authentication via the GrpcAuthInterceptor.
    If grpc_auth_token is unset/empty, all calls are rejected (fail-closed).

    Args:
        port: TCP port to listen on.
        server_components: Pre-built agent/memory components.  When
            ``None`` the server starts with all subsystems unavailable.
        max_workers: Thread-pool size for the gRPC executor.
        grpc_auth_token: Pre-shared Bearer token (from PROXY_GRPC_AUTH_TOKEN env).
            If None or empty, all gRPC calls are rejected.

    Returns:
        The running :class:`grpc.Server` instance.  The caller is
        responsible for calling ``server.wait_for_termination()`` or
        ``server.stop(grace)``.

    """
    components = server_components or ServerComponents()

    # Create auth interceptor (fail-closed if token not configured)
    auth_interceptor = GrpcAuthInterceptor(grpc_auth_token)

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        interceptors=[auth_interceptor],
    )

    servicer = WaddleAIServiceServicer(components)
    waddleai_pb2_grpc.add_WaddleAIServiceServicer_to_server(servicer, server)

    bound_port = server.add_insecure_port(f"[::]:{port}")
    server.start()

    if components.allow_unscoped_identity:
        logger.error(
            "gRPC server starting with the LEGACY UNSCOPED IDENTITY escape hatch enabled -- "
            "memory/usage RPCs will trust request-body user_id and pool all tenants into "
            "organization 0; this is a migration aid only",
            env_var=LEGACY_UNSCOPED_IDENTITY_ENV_VAR,
        )

    auth_status = "configured" if grpc_auth_token else "NOT CONFIGURED (all calls rejected)"
    logger.info(
        "gRPC server started",
        port=bound_port,
        max_workers=max_workers,
        auth=auth_status,
        routing_agent="ok" if components.routing_agent else "unavailable",
        security_agent="ok" if components.security_agent else "unavailable",
        usage_tracker="ok" if components.usage_tracker else "unavailable",
        memory_manager="ok" if components.memory_manager else "unavailable",
        identity_resolver="ok" if components.identity_resolver else "unavailable",
        legacy_unscoped_identity=components.allow_unscoped_identity,
    )

    return server


def run_grpc_in_thread(
    port: int = 50051,
    components: ServerComponents | None = None,
    max_workers: int = 10,
    grpc_auth_token: str | None = None,
) -> grpc.Server:
    """Start the gRPC server in a daemon thread.

    Useful when the main thread runs Quart / Flask.

    Args:
        port: TCP port to listen on.
        components: Pre-built agent/memory components.
        max_workers: Thread-pool size for the gRPC executor.
        grpc_auth_token: Pre-shared Bearer token (from PROXY_GRPC_AUTH_TOKEN env).

    Returns:
        The running :class:`grpc.Server` instance.

    """
    server = start_grpc_server(
        port=port,
        server_components=components,
        max_workers=max_workers,
        grpc_auth_token=grpc_auth_token,
    )

    def _wait() -> None:
        server.wait_for_termination()

    thread = threading.Thread(target=_wait, daemon=True, name="grpc-server")
    thread.start()
    logger.info("gRPC server running in daemon thread", port=port)

    return server


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_int(value: str, *, default: int = 0) -> int:
    """Parse *value* as an int, returning *default* on failure."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _memory_entries_to_proto(
    entries: list[Any],
) -> list[waddleai_pb2.MemoryEntry]:
    """Convert internal MemoryEntry objects to protobuf MemoryEntry messages."""
    proto_entries: list[waddleai_pb2.MemoryEntry] = []
    for entry in entries:
        metadata_dict: dict[str, str] = {}
        if hasattr(entry, "metadata") and isinstance(entry.metadata, dict):
            metadata_dict = {str(k): str(v) for k, v in entry.metadata.items()}

        created_at_str = ""
        if hasattr(entry, "created_at"):
            if isinstance(entry.created_at, datetime):
                created_at_str = entry.created_at.isoformat()
            elif isinstance(entry.created_at, str):
                created_at_str = entry.created_at

        proto_entries.append(
            waddleai_pb2.MemoryEntry(
                id=getattr(entry, "id", ""),
                content=getattr(entry, "content", ""),
                role=getattr(entry, "role", ""),
                similarity=getattr(entry, "relevance_score", 0.0),
                created_at=created_at_str,
                metadata=metadata_dict,
            )
        )
    return proto_entries
