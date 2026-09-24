"""Regression tests for the 2026-09-14 proxy gRPC security audit.

Finding 1 (HIGH) -- ``StoreTurn``/``GetContext``/``SearchMemories``/
``ReportUsage`` derived ``user_id`` from the client-supplied protobuf body and
hardcoded ``organization_id=0``, so any holder of the shared
``PROXY_GRPC_AUTH_TOKEN`` could read, write or poison any user's memory and
every tenant pooled into organization 0.

Finding 2 (MEDIUM) -- ``EvaluateSecurity``/``ReportUsage`` were wired through
``getattr(component, "<attr>", None)`` probes for attributes nothing ever
assigns, so both RPCs returned UNAVAILABLE unconditionally.

regression: audit-2026-09-14
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import grpc
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PROXY_SERVER_DIR = str(REPO_ROOT / "proxy" / "apps" / "proxy_server")
# grpc_server.py does `from grpc_proto.waddleai.v1 import ...` (bare import),
# which only resolves once this directory is itself on sys.path.
if PROXY_SERVER_DIR not in sys.path:
    sys.path.insert(0, PROXY_SERVER_DIR)

from proxy.apps.proxy_server.grpc_server import (  # noqa: E402
    CALLER_CREDENTIAL_METADATA_KEY,
    LEGACY_UNSCOPED_IDENTITY_ENV_VAR,
    CallerIdentity,
    ServerComponents,
    WaddleAIServiceServicer,
    _legacy_unscoped_identity_enabled,
    waddleai_pb2,
)
from shared.agents.security_agent import SecurityAgent, SecurityDecision  # noqa: E402
from shared.agents.usage_tracker import UsageAck, UsageTracker  # noqa: E402
from shared.utils.memory_integration import (  # noqa: E402
    ConversationContext,
    MemoryEntry,
    WaddleAIMemoryManager,
)

pytestmark = pytest.mark.security


# ---------------------------------------------------------------------------
# Hand-written fakes (self-contained: tests/unit/proxy has no __init__.py, so
# importing them from the sibling test module would rely on pytest's rootdir
# sys.path injection and break under mypy)
# ---------------------------------------------------------------------------


class FakeServicerContext:
    """Stand-in for grpc.ServicerContext carrying configurable call metadata."""

    def __init__(self, metadata: list[tuple[str, str]] | None = None) -> None:
        """Start with no status set and the given call metadata."""
        self.code: grpc.StatusCode | None = None
        self.details: str | None = None
        self._metadata: list[tuple[str, str]] = metadata or []

    def invocation_metadata(self) -> list[tuple[str, str]]:
        """Return the call metadata, as grpc.ServicerContext does."""
        return self._metadata

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


class AbortedError(Exception):
    """Raised by FakeServicerContext.abort, mirroring grpc abort() semantics."""

    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        """Record the abort code/details for assertions."""
        super().__init__(details)
        self.code = code
        self.details = details


@dataclass(slots=True)
class FakeSecurityAgent:
    """Stand-in for SecurityAgent; returns a canned SecurityDecision."""

    result: SecurityDecision | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def evaluate(
        self, raw_command: str, tool_type: str, user_id: int | None = None
    ) -> SecurityDecision:
        """Record the call and return the canned decision."""
        self.calls.append({"raw_command": raw_command, "tool_type": tool_type, "user_id": user_id})
        assert self.result is not None
        return self.result


@dataclass(slots=True)
class FakeUsageTracker:
    """Stand-in for UsageTracker; returns a canned UsageAck."""

    result: UsageAck | None = None
    calls: list[Any] = field(default_factory=list)

    async def record_usage(self, report: Any) -> UsageAck:
        """Record the submitted report and return the canned ack."""
        self.calls.append(report)
        assert self.result is not None
        return self.result


@dataclass(slots=True)
class FakeMemoryStore:
    """Stand-in for WaddleAIMemoryManager.memory_store, used by SearchMemories."""

    results: list[MemoryEntry] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def search_memories(
        self,
        query: str,
        user_id: int,
        organization_id: int,
        limit: int,
        min_relevance: float,
    ) -> list[MemoryEntry]:
        """Record the call and return the canned results."""
        self.calls.append(
            {
                "query": query,
                "user_id": user_id,
                "organization_id": organization_id,
                "limit": limit,
                "min_relevance": min_relevance,
            }
        )
        return self.results


@dataclass(slots=True)
class FakeMemoryManager:
    """Stand-in for WaddleAIMemoryManager; canned StoreTurn/GetContext responses."""

    store_turn_result: bool = True
    store_turn_calls: list[dict[str, Any]] = field(default_factory=list)
    context_result: ConversationContext | None = None
    context_calls: list[dict[str, Any]] = field(default_factory=list)
    memory_store: FakeMemoryStore = field(default_factory=FakeMemoryStore)

    async def add_conversation_turn(self, **kwargs: Any) -> bool:
        """Record the call and return the canned success flag."""
        self.store_turn_calls.append(kwargs)
        return self.store_turn_result

    async def get_conversation_context(self, **kwargs: Any) -> ConversationContext:
        """Record the call and return the canned context."""
        self.context_calls.append(kwargs)
        assert self.context_result is not None
        return self.context_result


#: Identity that a valid credential resolves to. Every request body below
#: carries a *different* user_id, so any test that still trusts the body fails.
ALICE = CallerIdentity(user_id=11, organization_id=101, api_key_id=9001)

#: A second tenant, used to prove one caller cannot reach the other's org.
BOB = CallerIdentity(user_id=22, organization_id=202, api_key_id=9002)

_CREDENTIALS: dict[str, CallerIdentity] = {"wa-alice": ALICE, "wa-bob": BOB}

#: The body-supplied identity an attacker would try to impersonate.
SPOOFED_USER_ID = "999999"


def _resolver(credential: str) -> CallerIdentity:
    """Resolve a known test credential, raising on anything else."""
    try:
        return _CREDENTIALS[credential]
    except KeyError as exc:
        raise ValueError("unknown credential") from exc


def _ctx(credential: str | None) -> FakeServicerContext:
    """Build a servicer context presenting *credential*, or none at all."""
    if credential is None:
        return FakeServicerContext()
    return FakeServicerContext(metadata=[(CALLER_CREDENTIAL_METADATA_KEY, credential)])


def _memory_components(mgr: FakeMemoryManager, **kwargs: Any) -> ServerComponents:
    """ServerComponents with *mgr* wired plus the test identity resolver."""
    kwargs.setdefault("identity_resolver", _resolver)
    return ServerComponents(memory_manager=cast(WaddleAIMemoryManager, mgr), **kwargs)


def _empty_context() -> ConversationContext:
    """A ConversationContext with no memories, for GetContext happy paths."""
    return ConversationContext(
        user_id=0,
        organization_id=0,
        session_id=None,
        recent_messages=[],
        relevant_memories=[],
        conversation_summary="",
    )


class TestClientSuppliedIdentityIsNotHonoured:
    """A body-supplied user_id must never reach the memory subsystem.

    regression: audit-2026-09-14
    """

    def test_store_turn_ignores_body_user_id(self) -> None:
        """StoreTurn scopes the write to the credential's user/org, not the body's."""
        mgr = FakeMemoryManager(store_turn_result=True)
        servicer = WaddleAIServiceServicer(_memory_components(mgr))

        servicer.StoreTurn(
            waddleai_pb2.StoreTurnRequest(
                api_version="v1", user_id=SPOOFED_USER_ID, user_message="hi"
            ),
            _ctx("wa-alice"),
        )

        call = mgr.store_turn_calls[0]
        assert call["user_id"] == ALICE.user_id
        assert call["organization_id"] == ALICE.organization_id
        assert call["user_id"] != int(SPOOFED_USER_ID)

    def test_get_context_ignores_body_user_id(self) -> None:
        """GetContext reads only the credential holder's context."""
        mgr = FakeMemoryManager(context_result=_empty_context())
        servicer = WaddleAIServiceServicer(_memory_components(mgr))

        servicer.GetContext(
            waddleai_pb2.GetContextRequest(api_version="v1", user_id=SPOOFED_USER_ID),
            _ctx("wa-alice"),
        )

        call = mgr.context_calls[0]
        assert call["user_id"] == ALICE.user_id
        assert call["organization_id"] == ALICE.organization_id

    def test_search_memories_ignores_body_user_id(self) -> None:
        """SearchMemories searches only the credential holder's memories."""
        mgr = FakeMemoryManager()
        servicer = WaddleAIServiceServicer(_memory_components(mgr))

        servicer.SearchMemories(
            waddleai_pb2.SearchMemoriesRequest(
                api_version="v1", query="q", user_id=SPOOFED_USER_ID
            ),
            _ctx("wa-alice"),
        )

        call = mgr.memory_store.calls[0]
        assert call["user_id"] == ALICE.user_id
        assert call["organization_id"] == ALICE.organization_id

    def test_report_usage_ignores_body_user_id_and_api_key_id(self) -> None:
        """ReportUsage bills the credential holder, not whoever the body names."""
        tracker = FakeUsageTracker(
            result=UsageAck(accepted=True, quota_exceeded=False, message="ok")
        )
        servicer = WaddleAIServiceServicer(
            ServerComponents(usage_tracker=cast(UsageTracker, tracker), identity_resolver=_resolver)
        )

        servicer.ReportUsage(
            waddleai_pb2.UsageReport(
                api_version="v1", user_id=SPOOFED_USER_ID, api_key_id="key-of-someone-else"
            ),
            _ctx("wa-alice"),
        )

        report = tracker.calls[0]
        assert report.user_id == str(ALICE.user_id)
        assert report.api_key_id == str(ALICE.api_key_id)

    def test_evaluate_security_ignores_body_user_id(self) -> None:
        """EvaluateSecurity audits under the credential's user, not the body's."""
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
        servicer = WaddleAIServiceServicer(
            ServerComponents(security_agent=cast(SecurityAgent, agent), identity_resolver=_resolver)
        )

        servicer.EvaluateSecurity(
            waddleai_pb2.SecurityRequest(
                api_version="v1", raw_command="rm -rf /", user_id=SPOOFED_USER_ID
            ),
            _ctx("wa-alice"),
        )

        assert agent.calls[0]["user_id"] == ALICE.user_id


class TestCrossOrgAccessIsRefused:
    """Each credential is confined to its own organization.

    regression: audit-2026-09-14
    """

    def test_two_credentials_never_share_an_organization(self) -> None:
        """Alice's and Bob's writes land in different orgs, never a shared org 0."""
        mgr = FakeMemoryManager(store_turn_result=True)
        servicer = WaddleAIServiceServicer(_memory_components(mgr))

        for credential in ("wa-alice", "wa-bob"):
            servicer.StoreTurn(
                waddleai_pb2.StoreTurnRequest(
                    api_version="v1", user_id=SPOOFED_USER_ID, user_message="hi"
                ),
                _ctx(credential),
            )

        orgs = [call["organization_id"] for call in mgr.store_turn_calls]
        assert orgs == [ALICE.organization_id, BOB.organization_id]
        assert 0 not in orgs

    def test_bob_cannot_read_alices_memories_by_naming_her_user_id(self) -> None:
        """Bob presenting his own credential is scoped to Bob, whatever the body says."""
        mgr = FakeMemoryManager()
        servicer = WaddleAIServiceServicer(_memory_components(mgr))

        servicer.SearchMemories(
            waddleai_pb2.SearchMemoriesRequest(
                api_version="v1", query="alice secrets", user_id=str(ALICE.user_id)
            ),
            _ctx("wa-bob"),
        )

        call = mgr.memory_store.calls[0]
        assert call["user_id"] == BOB.user_id
        assert call["organization_id"] == BOB.organization_id

    @pytest.mark.parametrize(
        ("rpc", "request_message"),
        [
            ("StoreTurn", waddleai_pb2.StoreTurnRequest(api_version="v1", user_message="hi")),
            ("GetContext", waddleai_pb2.GetContextRequest(api_version="v1")),
            ("SearchMemories", waddleai_pb2.SearchMemoriesRequest(api_version="v1", query="q")),
            ("ReportUsage", waddleai_pb2.UsageReport(api_version="v1")),
        ],
    )
    def test_no_credential_is_refused_permission_denied(
        self, rpc: str, request_message: Any
    ) -> None:
        """Without a per-caller credential the scoped RPCs fail closed."""
        mgr = FakeMemoryManager(store_turn_result=True, context_result=_empty_context())
        tracker = FakeUsageTracker(
            result=UsageAck(accepted=True, quota_exceeded=False, message="ok")
        )
        components = _memory_components(mgr, usage_tracker=cast(UsageTracker, tracker))
        servicer = WaddleAIServiceServicer(components)
        ctx = _ctx(None)

        with pytest.raises(Exception):  # noqa: B017 -- FakeServicerContext.abort raises
            getattr(servicer, rpc)(request_message, ctx)

        assert ctx.code == grpc.StatusCode.PERMISSION_DENIED
        assert mgr.store_turn_calls == []
        assert mgr.context_calls == []
        assert mgr.memory_store.calls == []
        assert tracker.calls == []

    def test_invalid_credential_is_refused_unauthenticated(self) -> None:
        """A credential the resolver rejects aborts UNAUTHENTICATED, never falls through."""
        mgr = FakeMemoryManager(store_turn_result=True)
        servicer = WaddleAIServiceServicer(_memory_components(mgr))
        ctx = _ctx("wa-forged")

        with pytest.raises(Exception):  # noqa: B017 -- FakeServicerContext.abort raises
            servicer.StoreTurn(
                waddleai_pb2.StoreTurnRequest(api_version="v1", user_message="hi"), ctx
            )

        assert ctx.code == grpc.StatusCode.UNAUTHENTICATED
        assert mgr.store_turn_calls == []

    def test_credential_without_a_resolver_is_refused(self) -> None:
        """A credential the server cannot verify is refused, not silently ignored."""
        mgr = FakeMemoryManager(store_turn_result=True)
        servicer = WaddleAIServiceServicer(
            ServerComponents(memory_manager=cast(WaddleAIMemoryManager, mgr))
        )
        ctx = _ctx("wa-alice")

        with pytest.raises(Exception):  # noqa: B017 -- FakeServicerContext.abort raises
            servicer.StoreTurn(
                waddleai_pb2.StoreTurnRequest(api_version="v1", user_message="hi"), ctx
            )

        assert ctx.code == grpc.StatusCode.UNAUTHENTICATED
        assert mgr.store_turn_calls == []


class TestLegacyEscapeHatchDefaultsSecure:
    """The migration escape hatch exists, defaults OFF, and is loud when used.

    regression: audit-2026-09-14
    """

    def test_env_var_unset_means_secure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unset escape-hatch variable resolves to the secure behaviour."""
        monkeypatch.delenv(LEGACY_UNSCOPED_IDENTITY_ENV_VAR, raising=False)
        assert _legacy_unscoped_identity_enabled() is False
        assert ServerComponents().allow_unscoped_identity is False

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe", "  "])
    def test_non_truthy_values_stay_secure(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Anything but an explicit truthy opt-in keeps tenant isolation on."""
        monkeypatch.setenv(LEGACY_UNSCOPED_IDENTITY_ENV_VAR, value)
        assert _legacy_unscoped_identity_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_values_enable_the_hatch(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit opt-in is honoured, so the migration path genuinely exists."""
        monkeypatch.setenv(LEGACY_UNSCOPED_IDENTITY_ENV_VAR, value)
        assert _legacy_unscoped_identity_enabled() is True

    def test_hatch_restores_legacy_unscoped_behaviour(self) -> None:
        """With the hatch on, the pre-fix body-derived, org-0 behaviour returns."""
        mgr = FakeMemoryManager(store_turn_result=True)
        servicer = WaddleAIServiceServicer(_memory_components(mgr, allow_unscoped_identity=True))

        servicer.StoreTurn(
            waddleai_pb2.StoreTurnRequest(api_version="v1", user_id="7", user_message="hi"),
            _ctx(None),
        )

        call = mgr.store_turn_calls[0]
        assert call["user_id"] == 7
        assert call["organization_id"] == 0

    def test_hatch_does_not_override_a_presented_credential(self) -> None:
        """Even with the hatch on, a real credential still wins over the body."""
        mgr = FakeMemoryManager(store_turn_result=True)
        servicer = WaddleAIServiceServicer(_memory_components(mgr, allow_unscoped_identity=True))

        servicer.StoreTurn(
            waddleai_pb2.StoreTurnRequest(
                api_version="v1", user_id=SPOOFED_USER_ID, user_message="hi"
            ),
            _ctx("wa-alice"),
        )

        call = mgr.store_turn_calls[0]
        assert call["user_id"] == ALICE.user_id
        assert call["organization_id"] == ALICE.organization_id


class TestDeadRpcComponentsAreReachable:
    """EvaluateSecurity and ReportUsage must reach a real component.

    regression: audit-2026-09-14
    """

    def test_evaluate_security_reaches_the_agent(self) -> None:
        """A wired SecurityAgent is actually invoked -- not short-circuited to UNAVAILABLE."""
        agent = FakeSecurityAgent(
            result=SecurityDecision(
                safe=False,
                risk_score=0.9,
                threat_type="prompt_injection",
                explanation="blocked",
                blocked=True,
                matched_patterns=[],
            )
        )
        ctx = _ctx("wa-alice")
        servicer = WaddleAIServiceServicer(
            ServerComponents(security_agent=cast(SecurityAgent, agent), identity_resolver=_resolver)
        )

        response = servicer.EvaluateSecurity(
            waddleai_pb2.SecurityRequest(api_version="v1", raw_command="rm -rf /"), ctx
        )

        assert ctx.code != grpc.StatusCode.UNAVAILABLE
        assert len(agent.calls) == 1
        assert response.blocked is True

    def test_report_usage_reaches_the_tracker(self) -> None:
        """A wired UsageTracker is actually invoked -- not short-circuited to UNAVAILABLE."""
        tracker = FakeUsageTracker(
            result=UsageAck(accepted=True, quota_exceeded=False, message="recorded")
        )
        ctx = _ctx("wa-alice")
        servicer = WaddleAIServiceServicer(
            ServerComponents(usage_tracker=cast(UsageTracker, tracker), identity_resolver=_resolver)
        )

        response = servicer.ReportUsage(
            waddleai_pb2.UsageReport(api_version="v1", total_tokens=10), ctx
        )

        assert ctx.code != grpc.StatusCode.UNAVAILABLE
        assert len(tracker.calls) == 1
        assert response.accepted is True

    def test_startup_wiring_uses_no_unset_attribute_probes(self) -> None:
        """main.py must not reintroduce getattr(component, ..., None) component wiring.

        The literal source check is the point: three prior instances of this
        bug (#212, #217, routing_agent) all passed every behavioural test while
        silently yielding None in production.
        """
        source = (REPO_ROOT / "proxy" / "apps" / "proxy_server" / "main.py").read_text()
        assert 'getattr(self.security_scanner, "security_agent"' not in source
        assert 'getattr(self.token_manager, "usage_tracker"' not in source
        assert "security_agent=self._build_security_agent(" in source
        assert "usage_tracker=UsageTracker(" in source
