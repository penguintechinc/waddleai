"""Unit tests for the WaddleAI gRPC server (proxy sidecar).

Covers ``GrpcAuthInterceptor`` (fail-closed bearer-token auth), every
``WaddleAIServiceServicer`` RPC method against hand-written fakes for the
routing/security/usage/memory subsystems, the ``_safe_int`` /
``_memory_entries_to_proto`` helpers, and ``start_grpc_server`` /
``run_grpc_in_thread`` lifecycle (real ``grpc.server`` bound to an ephemeral
loopback port, stopped in the same test -- no external network).
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import grpc
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PROXY_SERVER_DIR = str(REPO_ROOT / "proxy" / "apps" / "proxy_server")
# grpc_server.py does `from grpc_proto.waddleai.v1 import ...` (bare import,
# no `apps.proxy_server.` prefix) -- only resolves once this directory is on
# sys.path itself, mirroring proxy/apps/proxy_server/main.py's own setup.
if PROXY_SERVER_DIR not in sys.path:
    sys.path.insert(0, PROXY_SERVER_DIR)

from proxy.apps.proxy_server.grpc_server import (  # noqa: E402
    SUPPORTED_API_VERSIONS,
    ApiVersionRouter,
    GrpcAuthInterceptor,
    ServerComponents,
    WaddleAIServiceServicer,
    _memory_entries_to_proto,
    _safe_int,
    require_api_version,
    run_grpc_in_thread,
    start_grpc_server,
    waddleai_pb2,
)
from shared.agents.security_agent import SecurityDecision  # noqa: E402
from shared.agents.usage_tracker import UsageAck  # noqa: E402
from shared.routing.grpc_adapter import RouteEvaluation  # noqa: E402
from shared.utils.memory_integration import ConversationContext, MemoryEntry  # noqa: E402

# ---------------------------------------------------------------------------
# Hand-written fakes (no spec-less MagicMock)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MockHandlerCallDetails:
    """Stand-in for grpc.HandlerCallDetails carrying invocation metadata."""

    invocation_metadata: list[tuple[str, str]]
    method: str


class AbortedError(Exception):
    """Raised by FakeServicerContext.abort, mirroring real grpc abort() semantics."""

    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        """Record the abort code/details for assertions."""
        super().__init__(details)
        self.code = code
        self.details = details


class FakeServicerContext:
    """Hand-written stand-in for grpc.ServicerContext (spec limited to used methods)."""

    def __init__(self) -> None:
        """Start with no status set."""
        self.code: grpc.StatusCode | None = None
        self.details: str | None = None

    def set_code(self, code: grpc.StatusCode) -> None:
        """Record the status code the servicer set."""
        self.code = code

    def set_details(self, details: str) -> None:
        """Record the status details the servicer set."""
        self.details = details

    def abort(self, code: grpc.StatusCode, details: str) -> None:
        """Raise, matching real grpc.ServicerContext.abort()'s control-flow break."""
        self.code = code
        self.details = details
        raise AbortedError(code, details)


@dataclass(slots=True)
class FakeRoutingAgent:
    """Stand-in for RoutingEngineRouteEvaluator; returns a canned result or raises."""

    result: RouteEvaluation | None = None
    exc: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def evaluate(self, prompt: str, tool_type: str, region: str = "NA") -> RouteEvaluation:
        """Record the call and return the canned result, or raise the canned exception."""
        self.calls.append({"prompt": prompt, "tool_type": tool_type, "region": region})
        if self.exc is not None:
            raise self.exc
        assert self.result is not None
        return self.result


@dataclass(slots=True)
class FakeSecurityAgent:
    """Stand-in for SecurityAgent; returns a canned SecurityDecision or raises."""

    result: SecurityDecision | None = None
    exc: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def evaluate(
        self, raw_command: str, tool_type: str, user_id: int | None = None
    ) -> SecurityDecision:
        """Record the call and return the canned decision, or raise the canned exception."""
        self.calls.append({"raw_command": raw_command, "tool_type": tool_type, "user_id": user_id})
        if self.exc is not None:
            raise self.exc
        assert self.result is not None
        return self.result


@dataclass(slots=True)
class FakeUsageTracker:
    """Stand-in for UsageTracker; returns a canned UsageAck or raises."""

    result: UsageAck | None = None
    exc: Exception | None = None
    calls: list[Any] = field(default_factory=list)

    async def record_usage(self, report: Any) -> UsageAck:
        """Record the submitted report and return the canned ack, or raise."""
        self.calls.append(report)
        if self.exc is not None:
            raise self.exc
        assert self.result is not None
        return self.result


@dataclass(slots=True)
class FakeMemoryStore:
    """Stand-in for WaddleAIMemoryManager.memory_store, used by SearchMemories."""

    results: list[MemoryEntry] = field(default_factory=list)
    exc: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def search_memories(
        self,
        query: str,
        user_id: int,
        organization_id: int,
        limit: int,
        min_relevance: float,
    ) -> list[MemoryEntry]:
        """Record the call and return the canned results, or raise."""
        self.calls.append(
            {
                "query": query,
                "user_id": user_id,
                "organization_id": organization_id,
                "limit": limit,
                "min_relevance": min_relevance,
            }
        )
        if self.exc is not None:
            raise self.exc
        return self.results


@dataclass(slots=True)
class FakeMemoryManager:
    """Stand-in for WaddleAIMemoryManager; canned responses for StoreTurn/GetContext."""

    store_turn_result: bool = True
    store_turn_exc: Exception | None = None
    store_turn_calls: list[dict[str, Any]] = field(default_factory=list)

    context_result: ConversationContext | None = None
    context_exc: Exception | None = None
    context_calls: list[dict[str, Any]] = field(default_factory=list)

    memory_store: FakeMemoryStore = field(default_factory=FakeMemoryStore)

    async def add_conversation_turn(self, **kwargs: Any) -> bool:
        """Record the call and return the canned success flag, or raise."""
        self.store_turn_calls.append(kwargs)
        if self.store_turn_exc is not None:
            raise self.store_turn_exc
        return self.store_turn_result

    async def get_conversation_context(self, **kwargs: Any) -> ConversationContext:
        """Record the call and return the canned context, or raise."""
        self.context_calls.append(kwargs)
        if self.context_exc is not None:
            raise self.context_exc
        assert self.context_result is not None
        return self.context_result


class _BareMemoryEntry:
    """Object with none of MemoryEntry's optional attributes -- exercises hasattr fallbacks."""


# ---------------------------------------------------------------------------
# GrpcAuthInterceptor
# ---------------------------------------------------------------------------


class TestGrpcAuthInterceptor:
    """Bearer-token auth interceptor: fail-closed, constant-time comparison."""

    def test_missing_metadata_aborts_unauthenticated(self) -> None:
        """No authorization key at all -> abort handler with UNAUTHENTICATED."""
        interceptor = GrpcAuthInterceptor("secret-token")
        details = MockHandlerCallDetails(
            invocation_metadata=[("content-type", "application/grpc")], method="m"
        )

        handler = interceptor.intercept_service(lambda d: "unreachable", details)
        ctx = FakeServicerContext()
        with pytest.raises(AbortedError):
            handler.unary_unary(None, ctx)
        assert ctx.code == grpc.StatusCode.UNAUTHENTICATED
        assert ctx.details == "Missing or invalid authorization header"

    def test_non_bearer_scheme_aborts_unauthenticated(self) -> None:
        """Non-'Bearer' auth scheme (e.g. ApiKey) -> UNAUTHENTICATED."""
        interceptor = GrpcAuthInterceptor("secret-token")
        details = MockHandlerCallDetails(
            invocation_metadata=[("authorization", "ApiKey some-key")], method="m"
        )

        handler = interceptor.intercept_service(lambda d: "unreachable", details)
        ctx = FakeServicerContext()
        with pytest.raises(AbortedError):
            handler.unary_unary(None, ctx)
        assert ctx.code == grpc.StatusCode.UNAUTHENTICATED

    def test_wrong_token_aborts_unauthenticated(self) -> None:
        """Correct scheme, wrong token -> UNAUTHENTICATED with 'Invalid token'."""
        interceptor = GrpcAuthInterceptor("correct-token")
        details = MockHandlerCallDetails(
            invocation_metadata=[("authorization", "Bearer wrong-token")], method="m"
        )

        handler = interceptor.intercept_service(lambda d: "unreachable", details)
        ctx = FakeServicerContext()
        with pytest.raises(AbortedError):
            handler.unary_unary(None, ctx)
        assert ctx.details == "Invalid token"

    def test_correct_token_proceeds_to_continuation(self) -> None:
        """Correct Bearer token -> continuation is invoked and its result returned."""
        interceptor = GrpcAuthInterceptor("correct-token")
        details = MockHandlerCallDetails(
            invocation_metadata=[("authorization", "Bearer correct-token")], method="m"
        )
        seen: list[MockHandlerCallDetails] = []

        def continuation(d: MockHandlerCallDetails) -> str:
            seen.append(d)
            return "real-handler"

        result = interceptor.intercept_service(continuation, details)
        assert result == "real-handler"
        assert seen == [details]

    def test_fail_closed_when_no_token_configured(self) -> None:
        """No configured token (None) -> every call rejected, even with a header."""
        interceptor = GrpcAuthInterceptor(None)
        details = MockHandlerCallDetails(
            invocation_metadata=[("authorization", "Bearer any-token")], method="m"
        )

        handler = interceptor.intercept_service(lambda d: "unreachable", details)
        ctx = FakeServicerContext()
        with pytest.raises(AbortedError):
            handler.unary_unary(None, ctx)
        assert ctx.details == "gRPC auth not configured"

    def test_fail_closed_when_token_configured_empty(self) -> None:
        """Empty-string configured token -> same fail-closed rejection as None."""
        interceptor = GrpcAuthInterceptor("")
        details = MockHandlerCallDetails(
            invocation_metadata=[("authorization", "Bearer any-token")], method="m"
        )

        handler = interceptor.intercept_service(lambda d: "unreachable", details)
        ctx = FakeServicerContext()
        with pytest.raises(AbortedError):
            handler.unary_unary(None, ctx)
        assert ctx.code == grpc.StatusCode.UNAUTHENTICATED

    def test_metadata_lookup_is_case_sensitive(self) -> None:
        """Uppercase 'Authorization' key is not matched by the lowercase .get() lookup."""
        interceptor = GrpcAuthInterceptor("secret-token")
        details = MockHandlerCallDetails(
            invocation_metadata=[("Authorization", "Bearer secret-token")], method="m"
        )

        handler = interceptor.intercept_service(lambda d: "unreachable", details)
        ctx = FakeServicerContext()
        with pytest.raises(AbortedError):
            handler.unary_unary(None, ctx)
        assert ctx.details == "Missing or invalid authorization header"


# ---------------------------------------------------------------------------
# EvaluateRoute
# ---------------------------------------------------------------------------


class TestEvaluateRoute:
    """WaddleAIServiceServicer.EvaluateRoute -- prompt routing recommendation."""

    def test_unavailable_when_routing_agent_not_configured(self) -> None:
        """No routing_agent configured -> UNAVAILABLE and an empty response."""
        servicer = WaddleAIServiceServicer(ServerComponents())
        ctx = FakeServicerContext()

        response = servicer.EvaluateRoute(
            waddleai_pb2.RouteRequest(api_version="v1", prompt="hi"), ctx
        )

        assert ctx.code == grpc.StatusCode.UNAVAILABLE
        assert response.recommended_model == ""

    def test_returns_recommendation_and_defaults_region(self) -> None:
        """Success path returns the agent's decision; empty region defaults to 'NA'."""
        agent = FakeRoutingAgent(
            result=RouteEvaluation(
                model="gpt-4",
                complexity="high",
                target_type="chat",
                confidence=0.9,
                reasoning="complex prompt",
            )
        )
        servicer = WaddleAIServiceServicer(ServerComponents(routing_agent=agent))
        ctx = FakeServicerContext()
        request = waddleai_pb2.RouteRequest(
            api_version="v1", prompt="explain quantum computing", tool_type="general"
        )

        response = servicer.EvaluateRoute(request, ctx)

        assert response.recommended_model == "gpt-4"
        assert response.complexity == "high"
        assert response.confidence == pytest.approx(0.9)
        assert agent.calls == [{"prompt": request.prompt, "tool_type": "general", "region": "NA"}]

    def test_internal_error_sets_status_and_empty_response(self) -> None:
        """Agent exception -> INTERNAL status and an empty RouteResponse."""
        agent = FakeRoutingAgent(exc=RuntimeError("engine down"))
        servicer = WaddleAIServiceServicer(ServerComponents(routing_agent=agent))
        ctx = FakeServicerContext()

        response = servicer.EvaluateRoute(
            waddleai_pb2.RouteRequest(api_version="v1", prompt="x"), ctx
        )

        assert ctx.code == grpc.StatusCode.INTERNAL
        assert "engine down" in ctx.details
        assert response.recommended_model == ""


# ---------------------------------------------------------------------------
# EvaluateSecurity
# ---------------------------------------------------------------------------


class TestEvaluateSecurity:
    """WaddleAIServiceServicer.EvaluateSecurity -- command/prompt threat evaluation."""

    def test_unavailable_when_security_agent_not_configured(self) -> None:
        """No security_agent configured -> UNAVAILABLE and an empty response."""
        servicer = WaddleAIServiceServicer(ServerComponents())
        ctx = FakeServicerContext()

        response = servicer.EvaluateSecurity(
            waddleai_pb2.SecurityRequest(api_version="v1", raw_command="ls"), ctx
        )

        assert ctx.code == grpc.StatusCode.UNAVAILABLE
        assert response.safe is False

    def test_blocks_flagged_content(self) -> None:
        """Unsafe decision -> response mirrors safe=False/blocked=True and threat_type."""
        agent = FakeSecurityAgent(
            result=SecurityDecision(
                safe=False,
                risk_score=0.95,
                threat_type="prompt_injection",
                explanation="matched known jailbreak pattern",
                blocked=True,
                matched_patterns=["ignore previous instructions"],
            )
        )
        servicer = WaddleAIServiceServicer(ServerComponents(security_agent=agent))
        ctx = FakeServicerContext()
        request = waddleai_pb2.SecurityRequest(
            api_version="v1", raw_command="rm -rf /", tool_type="bash"
        )

        response = servicer.EvaluateSecurity(request, ctx)

        assert response.safe is False
        assert response.blocked is True
        assert response.threat_type == "prompt_injection"
        assert ctx.code is None

    def test_none_threat_type_becomes_empty_string(self) -> None:
        """SecurityDecision.threat_type=None serialises to the proto's empty string."""
        agent = FakeSecurityAgent(
            result=SecurityDecision(
                safe=True,
                risk_score=0.0,
                threat_type=None,
                explanation="clean",
                blocked=False,
                matched_patterns=[],
            )
        )
        servicer = WaddleAIServiceServicer(ServerComponents(security_agent=agent))
        ctx = FakeServicerContext()

        response = servicer.EvaluateSecurity(
            waddleai_pb2.SecurityRequest(api_version="v1", raw_command="echo hi"), ctx
        )

        assert response.threat_type == ""

    def test_valid_numeric_user_id_is_parsed(self) -> None:
        """Numeric string user_id is converted to int before being passed to the agent."""
        agent = FakeSecurityAgent(
            result=SecurityDecision(
                safe=True,
                risk_score=0.0,
                threat_type=None,
                explanation="ok",
                blocked=False,
                matched_patterns=[],
            )
        )
        servicer = WaddleAIServiceServicer(ServerComponents(security_agent=agent))
        ctx = FakeServicerContext()

        servicer.EvaluateSecurity(
            waddleai_pb2.SecurityRequest(api_version="v1", raw_command="cmd", user_id="42"), ctx
        )

        assert agent.calls[0]["user_id"] == 42

    def test_non_numeric_user_id_falls_back_to_none(self) -> None:
        """Non-numeric user_id string -> ValueError caught, user_id passed as None."""
        agent = FakeSecurityAgent(
            result=SecurityDecision(
                safe=True,
                risk_score=0.0,
                threat_type=None,
                explanation="ok",
                blocked=False,
                matched_patterns=[],
            )
        )
        servicer = WaddleAIServiceServicer(ServerComponents(security_agent=agent))
        ctx = FakeServicerContext()

        servicer.EvaluateSecurity(
            waddleai_pb2.SecurityRequest(api_version="v1", raw_command="cmd", user_id="not-an-int"),
            ctx,
        )

        assert agent.calls[0]["user_id"] is None

    def test_internal_error_sets_status_and_empty_response(self) -> None:
        """Agent exception -> INTERNAL status and an empty SecurityResponse."""
        agent = FakeSecurityAgent(exc=ValueError("scanner unavailable"))
        servicer = WaddleAIServiceServicer(ServerComponents(security_agent=agent))
        ctx = FakeServicerContext()

        response = servicer.EvaluateSecurity(
            waddleai_pb2.SecurityRequest(api_version="v1", raw_command="x"), ctx
        )

        assert ctx.code == grpc.StatusCode.INTERNAL
        assert "scanner unavailable" in ctx.details
        assert response.safe is False


# ---------------------------------------------------------------------------
# StoreTurn
# ---------------------------------------------------------------------------


class TestStoreTurn:
    """WaddleAIServiceServicer.StoreTurn -- persists a conversation turn."""

    def test_unavailable_when_memory_manager_not_configured(self) -> None:
        """No memory_manager configured -> UNAVAILABLE and success=False."""
        servicer = WaddleAIServiceServicer(ServerComponents())
        ctx = FakeServicerContext()

        response = servicer.StoreTurn(
            waddleai_pb2.StoreTurnRequest(api_version="v1", user_message="hi"), ctx
        )

        assert ctx.code == grpc.StatusCode.UNAVAILABLE
        assert response.success is False

    def test_persists_and_returns_ack(self) -> None:
        """Success path stores the turn and echoes success=True; defaults model/provider."""
        mgr = FakeMemoryManager(store_turn_result=True)
        servicer = WaddleAIServiceServicer(ServerComponents(memory_manager=mgr))
        ctx = FakeServicerContext()
        request = waddleai_pb2.StoreTurnRequest(
            api_version="v1",
            session_id="sess-1",
            user_id="7",
            user_message="hello",
            assistant_response="hi there",
            model="gpt-4",
            provider="openai",
        )

        response = servicer.StoreTurn(request, ctx)

        assert response.success is True
        call = mgr.store_turn_calls[0]
        assert call["user_id"] == 7
        assert call["organization_id"] == 0
        assert call["session_id"] == "sess-1"
        assert call["metadata"]["model"] == "gpt-4"
        assert call["metadata"]["provider"] == "openai"

    def test_empty_session_id_becomes_none(self) -> None:
        """Empty session_id string is normalised to None before delegating."""
        mgr = FakeMemoryManager(store_turn_result=True)
        servicer = WaddleAIServiceServicer(ServerComponents(memory_manager=mgr))
        ctx = FakeServicerContext()

        servicer.StoreTurn(
            waddleai_pb2.StoreTurnRequest(api_version="v1", session_id="", user_message="hi"), ctx
        )

        assert mgr.store_turn_calls[0]["session_id"] is None

    def test_metadata_setdefault_preserves_explicit_model_key(self) -> None:
        """Explicit metadata['model'] is not overwritten by request.model (setdefault semantics)."""
        mgr = FakeMemoryManager(store_turn_result=True)
        servicer = WaddleAIServiceServicer(ServerComponents(memory_manager=mgr))
        ctx = FakeServicerContext()
        request = waddleai_pb2.StoreTurnRequest(api_version="v1", user_message="hi", model="gpt-4")
        request.metadata["model"] = "already-set"

        servicer.StoreTurn(request, ctx)

        assert mgr.store_turn_calls[0]["metadata"]["model"] == "already-set"

    def test_manager_returns_false_is_forwarded(self) -> None:
        """Manager returning success=False (not an exception) is forwarded as-is."""
        mgr = FakeMemoryManager(store_turn_result=False)
        servicer = WaddleAIServiceServicer(ServerComponents(memory_manager=mgr))
        ctx = FakeServicerContext()

        response = servicer.StoreTurn(
            waddleai_pb2.StoreTurnRequest(api_version="v1", user_message="hi"), ctx
        )

        assert response.success is False
        assert ctx.code is None

    def test_internal_error_sets_status_and_success_false(self) -> None:
        """Manager exception -> INTERNAL status and success=False."""
        mgr = FakeMemoryManager(store_turn_exc=RuntimeError("db unavailable"))
        servicer = WaddleAIServiceServicer(ServerComponents(memory_manager=mgr))
        ctx = FakeServicerContext()

        response = servicer.StoreTurn(
            waddleai_pb2.StoreTurnRequest(api_version="v1", user_message="hi"), ctx
        )

        assert ctx.code == grpc.StatusCode.INTERNAL
        assert "db unavailable" in ctx.details
        assert response.success is False


# ---------------------------------------------------------------------------
# GetContext
# ---------------------------------------------------------------------------


class TestGetContext:
    """WaddleAIServiceServicer.GetContext -- retrieves conversation context."""

    def test_unavailable_when_memory_manager_not_configured(self) -> None:
        """No memory_manager configured -> UNAVAILABLE and an empty response."""
        servicer = WaddleAIServiceServicer(ServerComponents())
        ctx = FakeServicerContext()

        response = servicer.GetContext(
            waddleai_pb2.GetContextRequest(api_version="v1", session_id="s"), ctx
        )

        assert ctx.code == grpc.StatusCode.UNAVAILABLE
        assert list(response.memories) == []

    def test_empty_session_returns_empty_memory_list(self) -> None:
        """No relevant memories -> GetContextResponse with an empty memories list."""
        mgr = FakeMemoryManager(
            context_result=ConversationContext(
                user_id=0,
                organization_id=0,
                session_id="sess-1",
                recent_messages=[],
                relevant_memories=[],
                conversation_summary=None,
            )
        )
        servicer = WaddleAIServiceServicer(ServerComponents(memory_manager=mgr))
        ctx = FakeServicerContext()

        response = servicer.GetContext(
            waddleai_pb2.GetContextRequest(api_version="v1", session_id="sess-1"), ctx
        )

        assert list(response.memories) == []
        assert response.summary == ""

    def test_zero_limit_defaults_to_five(self) -> None:
        """limit<=0 (proto default) is normalised to the default context_limit of 5."""
        mgr = FakeMemoryManager(
            context_result=ConversationContext(
                user_id=0,
                organization_id=0,
                session_id=None,
                recent_messages=[],
                relevant_memories=[],
            )
        )
        servicer = WaddleAIServiceServicer(ServerComponents(memory_manager=mgr))
        ctx = FakeServicerContext()

        servicer.GetContext(waddleai_pb2.GetContextRequest(api_version="v1", limit=0), ctx)

        assert mgr.context_calls[0]["context_limit"] == 5

    def test_positive_limit_is_forwarded(self) -> None:
        """A positive limit is forwarded unchanged as context_limit."""
        mgr = FakeMemoryManager(
            context_result=ConversationContext(
                user_id=0,
                organization_id=0,
                session_id=None,
                recent_messages=[],
                relevant_memories=[],
            )
        )
        servicer = WaddleAIServiceServicer(ServerComponents(memory_manager=mgr))
        ctx = FakeServicerContext()

        servicer.GetContext(waddleai_pb2.GetContextRequest(api_version="v1", limit=3), ctx)

        assert mgr.context_calls[0]["context_limit"] == 3

    def test_internal_error_sets_status_and_empty_response(self) -> None:
        """Manager exception -> INTERNAL status and an empty GetContextResponse."""
        mgr = FakeMemoryManager(context_exc=RuntimeError("vector store down"))
        servicer = WaddleAIServiceServicer(ServerComponents(memory_manager=mgr))
        ctx = FakeServicerContext()

        response = servicer.GetContext(waddleai_pb2.GetContextRequest(api_version="v1"), ctx)

        assert ctx.code == grpc.StatusCode.INTERNAL
        assert "vector store down" in ctx.details
        assert list(response.memories) == []


# ---------------------------------------------------------------------------
# SearchMemories
# ---------------------------------------------------------------------------


class TestSearchMemories:
    """WaddleAIServiceServicer.SearchMemories -- query-text memory search."""

    def test_unavailable_when_memory_manager_not_configured(self) -> None:
        """No memory_manager configured -> UNAVAILABLE and an empty response."""
        servicer = WaddleAIServiceServicer(ServerComponents())
        ctx = FakeServicerContext()

        response = servicer.SearchMemories(
            waddleai_pb2.SearchMemoriesRequest(api_version="v1", query="q"), ctx
        )

        assert ctx.code == grpc.StatusCode.UNAVAILABLE
        assert list(response.results) == []

    def test_returns_ranked_results(self) -> None:
        """Search results from the store are converted to proto MemoryEntry objects."""
        entry = MemoryEntry(
            id="mem-1",
            user_id=1,
            organization_id=0,
            session_id="sess-1",
            content="remember this",
            metadata={"topic": "billing"},
            embedding=None,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            relevance_score=0.88,
        )
        mgr = FakeMemoryManager(memory_store=FakeMemoryStore(results=[entry]))
        servicer = WaddleAIServiceServicer(ServerComponents(memory_manager=mgr))
        ctx = FakeServicerContext()

        response = servicer.SearchMemories(
            waddleai_pb2.SearchMemoriesRequest(
                api_version="v1", query="billing question", user_id="1"
            ),
            ctx,
        )

        results = list(response.results)
        assert len(results) == 1
        assert results[0].id == "mem-1"
        assert results[0].similarity == pytest.approx(0.88)
        assert dict(results[0].metadata) == {"topic": "billing"}

    def test_default_limit_and_threshold_applied(self) -> None:
        """limit<=0 and threshold<=0.0 fall back to defaults (10, 0.7)."""
        store = FakeMemoryStore(results=[])
        mgr = FakeMemoryManager(memory_store=store)
        servicer = WaddleAIServiceServicer(ServerComponents(memory_manager=mgr))
        ctx = FakeServicerContext()

        servicer.SearchMemories(
            waddleai_pb2.SearchMemoriesRequest(api_version="v1", query="q"), ctx
        )

        assert store.calls[0]["limit"] == 10
        assert store.calls[0]["min_relevance"] == pytest.approx(0.7)

    def test_custom_limit_and_threshold_forwarded(self) -> None:
        """Positive limit/threshold values are forwarded unchanged."""
        store = FakeMemoryStore(results=[])
        mgr = FakeMemoryManager(memory_store=store)
        servicer = WaddleAIServiceServicer(ServerComponents(memory_manager=mgr))
        ctx = FakeServicerContext()

        servicer.SearchMemories(
            waddleai_pb2.SearchMemoriesRequest(
                api_version="v1", query="q", limit=25, threshold=0.4
            ),
            ctx,
        )

        assert store.calls[0]["limit"] == 25
        assert store.calls[0]["min_relevance"] == pytest.approx(0.4)

    def test_internal_error_sets_status_and_empty_response(self) -> None:
        """Store exception -> INTERNAL status and an empty SearchMemoriesResponse."""
        store = FakeMemoryStore(exc=RuntimeError("index corrupt"))
        mgr = FakeMemoryManager(memory_store=store)
        servicer = WaddleAIServiceServicer(ServerComponents(memory_manager=mgr))
        ctx = FakeServicerContext()

        response = servicer.SearchMemories(
            waddleai_pb2.SearchMemoriesRequest(api_version="v1", query="q"), ctx
        )

        assert ctx.code == grpc.StatusCode.INTERNAL
        assert "index corrupt" in ctx.details
        assert list(response.results) == []


# ---------------------------------------------------------------------------
# ReportUsage
# ---------------------------------------------------------------------------


class TestReportUsage:
    """WaddleAIServiceServicer.ReportUsage -- records per-request token usage."""

    def test_unavailable_when_usage_tracker_not_configured(self) -> None:
        """No usage_tracker configured -> UNAVAILABLE and a rejected UsageAck."""
        servicer = WaddleAIServiceServicer(ServerComponents())
        ctx = FakeServicerContext()

        response = servicer.ReportUsage(
            waddleai_pb2.UsageReport(api_version="v1", user_id="u1"), ctx
        )

        assert ctx.code == grpc.StatusCode.UNAVAILABLE
        assert response.accepted is False
        assert response.message == "UsageTracker not configured"

    def test_records_token_counts(self) -> None:
        """Success path forwards token counts and returns the tracker's ack."""
        tracker = FakeUsageTracker(
            result=UsageAck(accepted=True, quota_exceeded=False, message="recorded")
        )
        servicer = WaddleAIServiceServicer(ServerComponents(usage_tracker=tracker))
        ctx = FakeServicerContext()
        request = waddleai_pb2.UsageReport(
            api_version="v1",
            user_id="u1",
            model="gpt-4",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            api_key_id="key-1",
            provider="openai",
            latency_ms=250,
            request_id="req-1",
        )

        response = servicer.ReportUsage(request, ctx)

        assert response.accepted is True
        assert response.message == "recorded"
        report = tracker.calls[0]
        assert report.input_tokens == 100
        assert report.total_tokens == 150
        assert report.api_key_id == "key-1"
        assert report.latency_ms == pytest.approx(250.0)

    def test_zero_latency_becomes_none(self) -> None:
        """latency_ms=0 (proto default) is normalised to None, not 0.0."""
        tracker = FakeUsageTracker(
            result=UsageAck(accepted=True, quota_exceeded=False, message="ok")
        )
        servicer = WaddleAIServiceServicer(ServerComponents(usage_tracker=tracker))
        ctx = FakeServicerContext()

        servicer.ReportUsage(
            waddleai_pb2.UsageReport(api_version="v1", user_id="u1", latency_ms=0), ctx
        )

        assert tracker.calls[0].latency_ms is None

    def test_empty_optional_fields_become_none(self) -> None:
        """Empty api_key_id/provider/request_id strings are normalised to None."""
        tracker = FakeUsageTracker(
            result=UsageAck(accepted=True, quota_exceeded=False, message="ok")
        )
        servicer = WaddleAIServiceServicer(ServerComponents(usage_tracker=tracker))
        ctx = FakeServicerContext()

        servicer.ReportUsage(waddleai_pb2.UsageReport(api_version="v1", user_id="u1"), ctx)

        report = tracker.calls[0]
        assert report.api_key_id is None
        assert report.provider is None
        assert report.request_id is None

    def test_quota_exceeded_is_forwarded(self) -> None:
        """A quota-exceeded ack from the tracker is forwarded to the caller unchanged."""
        tracker = FakeUsageTracker(
            result=UsageAck(accepted=False, quota_exceeded=True, message="quota exceeded")
        )
        servicer = WaddleAIServiceServicer(ServerComponents(usage_tracker=tracker))
        ctx = FakeServicerContext()

        response = servicer.ReportUsage(
            waddleai_pb2.UsageReport(api_version="v1", user_id="u1"), ctx
        )

        assert response.accepted is False
        assert response.quota_exceeded is True

    def test_internal_error_sets_status_and_rejected_ack(self) -> None:
        """Tracker exception -> INTERNAL status and an ack embedding the error message."""
        tracker = FakeUsageTracker(exc=RuntimeError("quota service down"))
        servicer = WaddleAIServiceServicer(ServerComponents(usage_tracker=tracker))
        ctx = FakeServicerContext()

        response = servicer.ReportUsage(
            waddleai_pb2.UsageReport(api_version="v1", user_id="u1"), ctx
        )

        assert ctx.code == grpc.StatusCode.INTERNAL
        assert response.accepted is False
        assert "quota service down" in response.message


# ---------------------------------------------------------------------------
# _safe_int
# ---------------------------------------------------------------------------


class TestSafeInt:
    """_safe_int -- best-effort int parsing with a fallback default."""

    def test_parses_valid_int_string(self) -> None:
        """A numeric string parses to its int value."""
        assert _safe_int("42") == 42

    def test_invalid_string_returns_default(self) -> None:
        """A non-numeric string returns the given default (ValueError branch)."""
        assert _safe_int("not-a-number", default=7) == 7

    def test_none_value_returns_default(self) -> None:
        """None input returns the given default (TypeError branch)."""
        assert _safe_int(None, default=3) == 3  # type: ignore[arg-type]

    def test_default_parameter_defaults_to_zero(self) -> None:
        """Omitting default falls back to 0."""
        assert _safe_int("bad") == 0


# ---------------------------------------------------------------------------
# _memory_entries_to_proto
# ---------------------------------------------------------------------------


class TestMemoryEntriesToProto:
    """_memory_entries_to_proto -- converts internal MemoryEntry objects to protobuf."""

    def test_full_entry_with_dict_metadata_and_datetime(self) -> None:
        """A well-formed entry with dict metadata and a datetime created_at converts fully."""
        entry = MemoryEntry(
            id="mem-1",
            user_id=1,
            organization_id=0,
            session_id="s",
            content="hello",
            metadata={"k": "v"},
            embedding=None,
            created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            relevance_score=0.5,
        )

        proto_entries = _memory_entries_to_proto([entry])

        assert len(proto_entries) == 1
        result = proto_entries[0]
        assert result.id == "mem-1"
        assert result.content == "hello"
        assert result.similarity == pytest.approx(0.5)
        assert result.created_at == entry.created_at.isoformat()
        assert dict(result.metadata) == {"k": "v"}

    def test_entry_with_string_created_at(self) -> None:
        """created_at already a string is passed through unchanged."""
        entry = MemoryEntry(
            id="mem-2",
            user_id=1,
            organization_id=0,
            session_id=None,
            content="text",
            metadata={},
            embedding=None,
            created_at="2026-01-01T00:00:00",
        )

        proto_entries = _memory_entries_to_proto([entry])

        assert proto_entries[0].created_at == "2026-01-01T00:00:00"

    def test_entry_with_created_at_of_unsupported_type(self) -> None:
        """created_at present but neither datetime nor str -> created_at_str stays empty."""
        entry = MemoryEntry(
            id="mem-4",
            user_id=1,
            organization_id=0,
            session_id=None,
            content="text",
            metadata={},
            embedding=None,
            created_at=12345,  # type: ignore[arg-type]
        )

        proto_entries = _memory_entries_to_proto([entry])

        assert proto_entries[0].created_at == ""

    def test_entry_with_non_dict_metadata_defaults_empty(self) -> None:
        """Metadata present but not a dict falls back to an empty proto map."""
        entry = MemoryEntry(
            id="mem-3",
            user_id=1,
            organization_id=0,
            session_id=None,
            content="text",
            metadata="not-a-dict",  # type: ignore[arg-type]
            embedding=None,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

        proto_entries = _memory_entries_to_proto([entry])

        assert dict(proto_entries[0].metadata) == {}

    def test_bare_object_missing_all_optional_attributes(self) -> None:
        """An object lacking metadata/created_at entirely still converts via getattr defaults."""
        entry = _BareMemoryEntry()

        proto_entries = _memory_entries_to_proto([entry])

        assert len(proto_entries) == 1
        result = proto_entries[0]
        assert result.id == ""
        assert result.content == ""
        assert result.created_at == ""
        assert dict(result.metadata) == {}

    def test_empty_list_returns_empty_list(self) -> None:
        """No entries -> no proto entries."""
        assert _memory_entries_to_proto([]) == []


# ---------------------------------------------------------------------------
# Server lifecycle (real grpc.server on an ephemeral loopback port)
# ---------------------------------------------------------------------------


class TestServerLifecycle:
    """start_grpc_server / run_grpc_in_thread -- real grpc.server bind/start/stop."""

    def test_start_grpc_server_binds_ephemeral_port_and_stops(self) -> None:
        """port=0 lets the OS assign a free port; the returned server is running and stoppable."""
        server = start_grpc_server(
            port=0,
            server_components=ServerComponents(),
            max_workers=2,
            grpc_auth_token="test-token",  # noqa: S106 -- fixed test value, not a real secret
        )
        try:
            assert isinstance(server, grpc.Server)
        finally:
            server.stop(grace=None)

    def test_start_grpc_server_defaults_components_when_none(self) -> None:
        """server_components=None starts the server in fully degraded mode without raising."""
        server = start_grpc_server(
            port=0,
            server_components=None,
            grpc_auth_token="tok",  # noqa: S106 -- test value
        )
        try:
            assert isinstance(server, grpc.Server)
        finally:
            server.stop(grace=None)

    def test_run_grpc_in_thread_starts_daemon_thread(self) -> None:
        """run_grpc_in_thread starts the server and a named daemon thread waiting on it."""
        threads_before = {t.name for t in threading.enumerate()}

        server = run_grpc_in_thread(
            port=0,
            components=ServerComponents(),
            max_workers=2,
            grpc_auth_token="tok",  # noqa: S106 -- test value
        )
        try:
            # Give the daemon thread a moment to register itself.
            for _ in range(50):
                if "grpc-server" in {t.name for t in threading.enumerate()} - threads_before:
                    break
                time.sleep(0.01)
            names_after = {t.name for t in threading.enumerate()}
            assert "grpc-server" in names_after
            grpc_thread = next(t for t in threading.enumerate() if t.name == "grpc-server")
            assert grpc_thread.daemon is True
        finally:
            server.stop(grace=None)


# ---------------------------------------------------------------------------
# api_version routing (house gRPC-versioning contract)
# ---------------------------------------------------------------------------

_VERSIONED_METHODS: list[tuple[str, type]] = [
    ("EvaluateRoute", waddleai_pb2.RouteRequest),
    ("EvaluateSecurity", waddleai_pb2.SecurityRequest),
    ("StoreTurn", waddleai_pb2.StoreTurnRequest),
    ("GetContext", waddleai_pb2.GetContextRequest),
    ("SearchMemories", waddleai_pb2.SearchMemoriesRequest),
    ("ReportUsage", waddleai_pb2.UsageReport),
]


class TestApiVersionRouting:
    """Every RPC rejects a missing/unknown api_version.

    Verifies this happens before any configured component
    (routing/security/memory/usage) is touched.
    """

    @pytest.mark.parametrize(("method_name", "request_cls"), _VERSIONED_METHODS)
    def test_missing_api_version_aborts_unimplemented(
        self, method_name: str, request_cls: type
    ) -> None:
        """Default (unset) api_version -> UNIMPLEMENTED naming the empty value."""
        servicer = WaddleAIServiceServicer(ServerComponents())
        ctx = FakeServicerContext()
        method = getattr(servicer, method_name)

        with pytest.raises(AbortedError) as exc_info:
            method(request_cls(), ctx)

        assert exc_info.value.code == grpc.StatusCode.UNIMPLEMENTED
        assert exc_info.value.details == "api_version  not supported"
        assert ctx.code == grpc.StatusCode.UNIMPLEMENTED

    @pytest.mark.parametrize(("method_name", "request_cls"), _VERSIONED_METHODS)
    def test_unknown_api_version_aborts_unimplemented(
        self, method_name: str, request_cls: type
    ) -> None:
        """An unrecognised api_version (e.g. 'v2') -> UNIMPLEMENTED naming the value."""
        servicer = WaddleAIServiceServicer(ServerComponents())
        ctx = FakeServicerContext()
        method = getattr(servicer, method_name)

        with pytest.raises(AbortedError) as exc_info:
            method(request_cls(api_version="v2"), ctx)

        assert exc_info.value.code == grpc.StatusCode.UNIMPLEMENTED
        assert exc_info.value.details == "api_version v2 not supported"

    @pytest.mark.parametrize(("method_name", "request_cls"), _VERSIONED_METHODS)
    def test_supported_version_reaches_component_unavailable_path(
        self, method_name: str, request_cls: type
    ) -> None:
        """A supported version reaches the normal UNAVAILABLE handling.

        With no configured component, the version check never masks the
        real not-configured behaviour.
        """
        servicer = WaddleAIServiceServicer(ServerComponents())
        ctx = FakeServicerContext()
        method = getattr(servicer, method_name)

        method(request_cls(api_version="v1"), ctx)

        assert ctx.code == grpc.StatusCode.UNAVAILABLE

    def test_default_supported_versions_is_v1_only(self) -> None:
        """The module-level SUPPORTED_API_VERSIONS constant is exactly {'v1'}."""
        assert SUPPORTED_API_VERSIONS == frozenset({"v1"})

    def test_require_api_version_singleton_uses_default_versions(self) -> None:
        """The shared `require_api_version` instance is built from the default set."""
        assert require_api_version.supported_versions == SUPPORTED_API_VERSIONS

    def test_router_call_wraps_arbitrary_handler_and_preserves_metadata(self) -> None:
        """ApiVersionRouter is a generic decorator.

        `__name__`/`__doc__` survive wrapping, and a supported version calls
        through to the wrapped handler unchanged.
        """
        router = ApiVersionRouter(supported_versions=frozenset({"v9"}))

        def handler(self: Any, request: Any, context: Any) -> str:
            """Original docstring."""
            return "handled"

        wrapped = router(handler)

        assert wrapped.__name__ == "handler"
        assert wrapped.__doc__ == "Original docstring."
        assert wrapped(
            None, waddleai_pb2.RouteRequest(api_version="v9"), FakeServicerContext()
        ) == ("handled")

    def test_router_call_rejects_version_outside_custom_set(self) -> None:
        """A router built with a non-default supported_versions set still aborts.

        UNIMPLEMENTED is raised for any version not in that custom set.
        """
        router = ApiVersionRouter(supported_versions=frozenset({"v9"}))
        ctx = FakeServicerContext()

        def handler(self: Any, request: Any, context: Any) -> str:
            return "unreachable"

        wrapped = router(handler)

        with pytest.raises(AbortedError) as exc_info:
            wrapped(None, waddleai_pb2.RouteRequest(api_version="v1"), ctx)

        assert exc_info.value.details == "api_version v1 not supported"
