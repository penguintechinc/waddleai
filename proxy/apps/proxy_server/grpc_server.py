"""
WaddleAI gRPC Server — receives calls from the Go AILB and delegates
to the Python agent layer (RoutingAgent, SecurityAgent, UsageTracker)
and memory subsystem (WaddleAIMemoryManager).

Usage:
    # Standalone (blocking)
    start_grpc_server(port=50051, server_components=components)

    # Alongside Quart/Flask (daemon thread)
    run_grpc_in_thread(port=50051, components=components)
"""

from __future__ import annotations

import threading
from concurrent import futures
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import grpc
import structlog
from grpc_proto.marchproxy import waddleai_pb2, waddleai_pb2_grpc

from shared.agents import RoutingAgent, SecurityAgent, UsageTracker
from shared.agents.usage_tracker import UsageReport as AgentUsageReport
from shared.utils.memory_integration import WaddleAIMemoryManager

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Server components container
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ServerComponents:
    """Holds references to the agent and memory subsystems.

    All fields are optional so the server can start in a degraded mode
    when a subsystem is unavailable.
    """

    routing_agent: Optional[RoutingAgent] = None
    security_agent: Optional[SecurityAgent] = None
    usage_tracker: Optional[UsageTracker] = None
    memory_manager: Optional[WaddleAIMemoryManager] = None


# ---------------------------------------------------------------------------
# Servicer implementation
# ---------------------------------------------------------------------------


class WaddleAIServiceServicer(waddleai_pb2_grpc.WaddleAIServiceServicer):
    """Concrete implementation of the WaddleAIService gRPC service."""

    def __init__(self, components: ServerComponents) -> None:
        self._components = components

    # ---- EvaluateRoute ----------------------------------------------------

    def EvaluateRoute(
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

    def EvaluateSecurity(
        self,
        request: waddleai_pb2.SecurityRequest,
        context: grpc.ServicerContext,
    ) -> waddleai_pb2.SecurityResponse:
        """Evaluate a raw command for security threats."""
        agent = self._components.security_agent
        if agent is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details("SecurityAgent not configured")
            return waddleai_pb2.SecurityResponse()

        try:
            import asyncio

            user_id: Optional[int] = None
            if request.user_id:
                try:
                    user_id = int(request.user_id)
                except ValueError:
                    user_id = None

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

    def StoreTurn(
        self,
        request: waddleai_pb2.StoreTurnRequest,
        context: grpc.ServicerContext,
    ) -> waddleai_pb2.StoreTurnResponse:
        """Store a conversation turn in the memory subsystem."""
        mgr = self._components.memory_manager
        if mgr is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details("MemoryManager not configured")
            return waddleai_pb2.StoreTurnResponse(success=False)

        try:
            import asyncio

            user_id_int = _safe_int(request.user_id, default=0)
            messages: List[Dict[str, str]] = [
                {"role": "user", "content": request.user_message},
            ]
            metadata: Dict[str, Any] = dict(request.metadata)
            metadata.setdefault("model", request.model)
            metadata.setdefault("provider", request.provider)

            success = asyncio.run(
                mgr.add_conversation_turn(
                    user_id=user_id_int,
                    organization_id=0,
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

    def GetContext(
        self,
        request: waddleai_pb2.GetContextRequest,
        context: grpc.ServicerContext,
    ) -> waddleai_pb2.GetContextResponse:
        """Retrieve conversation context for a session."""
        mgr = self._components.memory_manager
        if mgr is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details("MemoryManager not configured")
            return waddleai_pb2.GetContextResponse()

        try:
            import asyncio

            user_id_int = _safe_int(request.user_id, default=0)
            limit = request.limit if request.limit > 0 else 5

            conv_context = asyncio.run(
                mgr.get_conversation_context(
                    user_id=user_id_int,
                    organization_id=0,
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

    def SearchMemories(
        self,
        request: waddleai_pb2.SearchMemoriesRequest,
        context: grpc.ServicerContext,
    ) -> waddleai_pb2.SearchMemoriesResponse:
        """Search memories by query text."""
        mgr = self._components.memory_manager
        if mgr is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details("MemoryManager not configured")
            return waddleai_pb2.SearchMemoriesResponse()

        try:
            import asyncio

            user_id_int = _safe_int(request.user_id, default=0)
            limit = request.limit if request.limit > 0 else 10
            threshold = request.threshold if request.threshold > 0.0 else 0.7

            results = asyncio.run(
                mgr.memory_store.search_memories(
                    query=request.query,
                    user_id=user_id_int,
                    organization_id=0,
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

    def ReportUsage(
        self,
        request: waddleai_pb2.UsageReport,
        context: grpc.ServicerContext,
    ) -> waddleai_pb2.UsageAck:
        """Record token usage for a completed request."""
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
                user_id=request.user_id,
                model=request.model,
                input_tokens=request.input_tokens,
                output_tokens=request.output_tokens,
                total_tokens=request.total_tokens,
                api_key_id=request.api_key_id or None,
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
    server_components: Optional[ServerComponents] = None,
    max_workers: int = 10,
) -> grpc.Server:
    """Create, configure, and start the gRPC server.

    The server binds to an insecure port. TLS termination is expected to
    be handled by the Kubernetes ingress / service mesh.

    Args:
        port: TCP port to listen on.
        server_components: Pre-built agent/memory components.  When
            ``None`` the server starts with all subsystems unavailable.
        max_workers: Thread-pool size for the gRPC executor.

    Returns:
        The running :class:`grpc.Server` instance.  The caller is
        responsible for calling ``server.wait_for_termination()`` or
        ``server.stop(grace)``.
    """
    components = server_components or ServerComponents()
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
    )

    servicer = WaddleAIServiceServicer(components)
    waddleai_pb2_grpc.add_WaddleAIServiceServicer_to_server(servicer, server)

    bound_port = server.add_insecure_port(f"[::]:{port}")
    server.start()

    logger.info(
        "gRPC server started",
        port=bound_port,
        max_workers=max_workers,
        routing_agent="ok" if components.routing_agent else "unavailable",
        security_agent="ok" if components.security_agent else "unavailable",
        usage_tracker="ok" if components.usage_tracker else "unavailable",
        memory_manager="ok" if components.memory_manager else "unavailable",
    )

    return server


def run_grpc_in_thread(
    port: int = 50051,
    components: Optional[ServerComponents] = None,
    max_workers: int = 10,
) -> grpc.Server:
    """Start the gRPC server in a daemon thread.

    Useful when the main thread runs Quart / Flask.

    Args:
        port: TCP port to listen on.
        components: Pre-built agent/memory components.
        max_workers: Thread-pool size for the gRPC executor.

    Returns:
        The running :class:`grpc.Server` instance.
    """
    server = start_grpc_server(
        port=port,
        server_components=components,
        max_workers=max_workers,
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
    entries: List[Any],
) -> List[waddleai_pb2.MemoryEntry]:
    """Convert internal MemoryEntry objects to protobuf MemoryEntry messages."""
    proto_entries: List[waddleai_pb2.MemoryEntry] = []
    for entry in entries:
        metadata_dict: Dict[str, str] = {}
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
