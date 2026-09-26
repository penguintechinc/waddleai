"""Tests for `penguincode_cli.server.services.lessons` -- `LessonsServiceImpl` (T-L2b).

TDD: written before `penguincode_cli/server/services/lessons.py` existed;
must fail with an ImportError/ModuleNotFoundError until implemented. Mirrors
`tests/test_server_knowledge_service.py`'s style exactly (same `_FakeContext`/
`AbortCalledError`/`scope_ctx` fixtures, same Protocol-based fake-injection
seams) -- see that file for the pattern this one follows.

Proves, per RPC:

- No `ScopeContext` present -> `UNAUTHENTICATED`, before any store/scrub call.
- `PromoteLesson`: a non-clean scrub verdict blocks (no pending row created);
  a clean verdict creates one. One end-to-end pair (real `generalize_and_scrub`
  + `verify_scrubbed`, only Ollama faked) proves client-confidential content
  is genuinely caught, not just a mocked-function unit test.
- `ListPendingLessons`: flag-gated; status defaults to "pending"; every
  `PendingLessonRecord` field maps onto the wire `PendingLesson` correctly.
- `ApproveLesson`: flag-gated; requires `LESSONS_APPROVE_SCOPE`
  (`PERMISSION_DENIED` without it); separation-of-duties blocks a proposer
  approving their own lesson; a valid approval writes a tenant-visibility
  memory and transitions the row to "approved".
- `RejectLesson`: same authz gate; transitions to "rejected" with a reason.

A live-Postgres class at the bottom proves the property no mock can: an
`ApproveLesson`-written tenant-visibility lesson is then readable by a
*different team, same tenant* caller -- "firm-wide", not just "not
team-scoped" in name only.

# regression: lessons-promotion (T-L2b -- LessonsService server handlers)
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import grpc
import psycopg
import pytest

import penguincode_cli.auth.middleware as auth_middleware
import penguincode_cli.server.services.lessons as lessons_module
from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import MemoryConfig, Settings
from penguincode_cli.db.migrate import run_migrations
from penguincode_cli.lessons.scrub import Finding, IssueKind, ScrubResult, Verdict
from penguincode_cli.lessons.store import FindingRecord, PendingLessonRecord, PendingLessonStore
from penguincode_cli.proto import (
    ApproveLessonRequest,
    ListPendingLessonsRequest,
    PromoteLessonRequest,
    RejectLessonRequest,
)
from penguincode_cli.server.services.lessons import LESSONS_APPROVE_SCOPE, LessonsServiceImpl
from penguincode_cli.tools.memory import MemoryManager, create_scoped_memory_manager

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set -- live-Postgres LessonsService tests are CI-pending",
)

_FLAG_ENV = "PENGUINCODE_FLAG_LESSONS_PROMOTION"
_RAG_FLAG_ENV = "PENGUINCODE_FLAG_RAG"


def _ctx(tenant_id: str = "tenant-a", **overrides: Any) -> ScopeContext:
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "org_id": "org-a",
        "team_ids": ("team-a",),
        "user_id": "user-a",
        "scopes": (),
    }
    defaults.update(overrides)
    return ScopeContext(**defaults)


class AbortCalledError(Exception):
    """Raised by `_FakeContext.abort` -- mirrors real `grpc.aio` abort semantics."""


class _FakeContext:
    """Minimal `grpc.aio.ServicerContext` double: records the abort call and raises."""

    def __init__(self) -> None:
        self.aborted_with: tuple[Any, str] | None = None

    async def abort(self, code: Any, details: str) -> None:
        self.aborted_with = (code, details)
        raise AbortCalledError(details)


@pytest.fixture
def scope_ctx() -> Iterator[ScopeContext]:
    """Install a real `ScopeContext` into the auth contextvar for the test's duration."""
    ctx = _ctx()
    token = auth_middleware._current_scope.set(ctx)
    yield ctx
    auth_middleware._current_scope.reset(token)


@pytest.fixture(autouse=True)
def _no_leftover_scope() -> Iterator[None]:
    """Guarantee `current_scope_context()` is `None` by default in every test."""
    assert auth_middleware.current_scope_context() is None
    yield
    auth_middleware._current_scope.set(None)


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test except the dedicated flag-off tests wants the flag ON by default."""
    monkeypatch.setenv(_FLAG_ENV, "true")


class _FakeStore:
    """Records `create_pending`/`list_pending`/`get`/`set_status` calls via plain `MagicMock`s
    (these are invoked through `asyncio.to_thread`, so a sync `MagicMock` -- not `AsyncMock` --
    is the right double).
    """

    def __init__(
        self,
        *,
        create_pending_result: str = "pending-1",
        get_result: PendingLessonRecord | None = None,
        list_result: list[PendingLessonRecord] | None = None,
    ) -> None:
        self.create_pending = MagicMock(return_value=create_pending_result)
        self.list_pending = MagicMock(return_value=list_result or [])
        self.get = MagicMock(return_value=get_result)
        self.set_status = MagicMock(return_value=None)


class _FakeScopedMemory:
    """Records `add` calls; result is set per-test via the `AsyncMock`."""

    def __init__(self) -> None:
        self.add = AsyncMock(
            return_value={"results": [{"id": "m1", "memory": "text", "event": "ADD"}]}
        )


def _service(
    *, store: _FakeStore | None = None, scoped_memory: _FakeScopedMemory | None = None
) -> LessonsServiceImpl:
    return LessonsServiceImpl(
        Settings(),
        store=store or _FakeStore(),
        scoped_memory=scoped_memory or _FakeScopedMemory(),
    )


def _record(
    *,
    id: str = "pending-1",
    tenant_id: str = "tenant-a",
    org_id: str | None = "org-a",
    source_team_id: str | None = "team-a",
    proposer_user_id: str | None = "proposer-1",
    generalized_text: str = "a generalized, client-agnostic lesson",
    status: str = "pending",
    findings: list[FindingRecord] | None = None,
    reviewed_by: str | None = None,
    reviewed_at: str | None = None,
    created_at: str = "2026-09-25T00:00:00+00:00",
) -> PendingLessonRecord:
    return PendingLessonRecord(
        id=id,
        tenant_id=tenant_id,
        org_id=org_id,
        source_team_id=source_team_id,
        proposer_user_id=proposer_user_id,
        generalized_text=generalized_text,
        status=status,
        findings=findings or [],
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# UNAUTHENTICATED: every RPC aborts before touching the store/scrub pipeline.
# ---------------------------------------------------------------------------


class TestRequireScope:
    @pytest.mark.asyncio
    async def test_promote_without_scope_aborts_unauthenticated(self) -> None:
        service = _service()
        context = _FakeContext()
        with pytest.raises(AbortCalledError):
            await service.PromoteLesson(
                PromoteLessonRequest(api_version="v1", source_content="x"), context
            )
        assert context.aborted_with is not None
        assert context.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_list_without_scope_aborts_unauthenticated(self) -> None:
        service = _service()
        context = _FakeContext()
        with pytest.raises(AbortCalledError):
            await service.ListPendingLessons(ListPendingLessonsRequest(api_version="v1"), context)
        assert context.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_approve_without_scope_aborts_unauthenticated(self) -> None:
        service = _service()
        context = _FakeContext()
        with pytest.raises(AbortCalledError):
            await service.ApproveLesson(
                ApproveLessonRequest(api_version="v1", pending_id="p1"), context
            )
        assert context.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_reject_without_scope_aborts_unauthenticated(self) -> None:
        service = _service()
        context = _FakeContext()
        with pytest.raises(AbortCalledError):
            await service.RejectLesson(
                RejectLessonRequest(api_version="v1", pending_id="p1"), context
            )
        assert context.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED  # type: ignore[index]


# ---------------------------------------------------------------------------
# PromoteLesson -> lessons.scrub.generalize_and_scrub -> PendingLessonStore.create_pending
# ---------------------------------------------------------------------------


class TestPromoteLesson:
    @pytest.mark.asyncio
    async def test_clean_result_creates_pending_and_returns_id(
        self, scope_ctx: ScopeContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clean_result = ScrubResult(
            generalized_text="a fully generalized lesson",
            redactions=[],
            verdict=Verdict(clean=True),
        )

        async def _fake_scrub(ctx: ScopeContext, content: str, **kwargs: Any) -> ScrubResult:
            assert ctx is scope_ctx
            assert content == "raw client lesson"
            return clean_result

        monkeypatch.setattr(lessons_module, "generalize_and_scrub", _fake_scrub)
        store = _FakeStore(create_pending_result="new-pending-id")
        service = _service(store=store)

        response = await service.PromoteLesson(
            PromoteLessonRequest(
                api_version="v1", source_content="raw client lesson", team_id="team-a"
            ),
            _FakeContext(),
        )

        assert response.blocked is False
        assert response.pending_id == "new-pending-id"
        assert list(response.findings) == []
        store.create_pending.assert_called_once_with(
            scope_ctx, "a fully generalized lesson", [], "team-a"
        )

    @pytest.mark.asyncio
    async def test_blocked_result_returns_findings_and_creates_nothing(
        self, scope_ctx: ScopeContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blocked_result = ScrubResult(
            generalized_text="still has residue",
            redactions=[],
            verdict=Verdict(
                clean=False,
                findings=[
                    Finding(kind=IssueKind.CLIENT_IDENTIFIER, detail="residual identifier detected")
                ],
            ),
        )

        async def _fake_scrub(ctx: ScopeContext, content: str, **kwargs: Any) -> ScrubResult:
            return blocked_result

        monkeypatch.setattr(lessons_module, "generalize_and_scrub", _fake_scrub)
        store = _FakeStore()
        service = _service(store=store)

        response = await service.PromoteLesson(
            PromoteLessonRequest(api_version="v1", source_content="names AcmeCorp explicitly"),
            _FakeContext(),
        )

        assert response.blocked is True
        assert response.pending_id == ""
        assert len(response.findings) == 1
        assert response.findings[0].kind == "client_identifier"
        store.create_pending.assert_not_called()

    @pytest.mark.asyncio
    async def test_team_id_not_callers_own_team_maps_to_invalid_argument(
        self, scope_ctx: ScopeContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clean_result = ScrubResult(
            generalized_text="clean", redactions=[], verdict=Verdict(clean=True)
        )

        async def _fake_scrub(ctx: ScopeContext, content: str, **kwargs: Any) -> ScrubResult:
            return clean_result

        monkeypatch.setattr(lessons_module, "generalize_and_scrub", _fake_scrub)
        store = _FakeStore()
        store.create_pending.side_effect = ValueError(
            "source_team_id is not one of the caller's own teams"
        )
        service = _service(store=store)
        context = _FakeContext()

        with pytest.raises(AbortCalledError):
            await service.PromoteLesson(
                PromoteLessonRequest(api_version="v1", source_content="x", team_id="not-my-team"),
                context,
            )
        assert context.aborted_with[0] == grpc.StatusCode.INVALID_ARGUMENT  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_empty_team_id_passed_as_none(
        self, scope_ctx: ScopeContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clean_result = ScrubResult(
            generalized_text="clean", redactions=[], verdict=Verdict(clean=True)
        )

        async def _fake_scrub(ctx: ScopeContext, content: str, **kwargs: Any) -> ScrubResult:
            return clean_result

        monkeypatch.setattr(lessons_module, "generalize_and_scrub", _fake_scrub)
        store = _FakeStore()
        service = _service(store=store)

        await service.PromoteLesson(
            PromoteLessonRequest(api_version="v1", source_content="x"), _FakeContext()
        )

        store.create_pending.assert_called_once_with(scope_ctx, "clean", [], None)


class TestPromoteLessonEndToEnd:
    """No mocked `generalize_and_scrub` -- the real scrub + verify pipeline runs, with only
    Ollama faked (mirrors `tests/test_lessons_scrub.py`'s own mocking boundary). Proves the
    RPC handler genuinely blocks client-confidential content, not just a mocked-function
    unit test.
    """

    @pytest.mark.asyncio
    async def test_confidential_content_is_blocked_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx(tenant_id="tenant-acme-001", org_id=None, team_ids=(), user_id=str(uuid.uuid4()))
        token = auth_middleware._current_scope.set(ctx)
        try:
            # The "LLM" fails to strip the tenant's own identifier -- verify_scrubbed's
            # independent client-identifier check is the backstop that must catch it.
            # `lessons_module.generalize_and_scrub` is untouched (still the real function) --
            # only its internal `OllamaClient` construction is faked.
            _fake_ollama_response(monkeypatch, "the client tenant-acme-001 had an outage")
            store = _FakeStore()
            service = _service(store=store)

            response = await service.PromoteLesson(
                PromoteLessonRequest(
                    api_version="v1", source_content="tenant-acme-001 had a prod outage last week"
                ),
                _FakeContext(),
            )

            assert response.blocked is True
            assert any(f.kind == "client_identifier" for f in response.findings)
            store.create_pending.assert_not_called()
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_clean_generalized_content_creates_pending_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx(tenant_id="tenant-acme-001", org_id=None, team_ids=(), user_id=str(uuid.uuid4()))
        token = auth_middleware._current_scope.set(ctx)
        try:
            _fake_ollama_response(
                monkeypatch,
                "A client experienced a production outage due to a misconfigured "
                "load balancer health check.",
            )
            store = _FakeStore(create_pending_result="pid-end-to-end")
            service = _service(store=store)

            response = await service.PromoteLesson(
                PromoteLessonRequest(
                    api_version="v1", source_content="tenant-acme-001 had a prod outage last week"
                ),
                _FakeContext(),
            )

            assert response.blocked is False
            assert response.pending_id == "pid-end-to-end"
            store.create_pending.assert_called_once()
        finally:
            auth_middleware._current_scope.reset(token)


class _FakeOllamaMessage:
    """Stand-in for `ollama.types.Message` -- only `.content` is read by `_call_llm`."""

    def __init__(self, content: str) -> None:
        self.content = content


class _FakeOllamaChunk:
    """Stand-in for `ollama.types.ChatResponse` -- only `.message` is read by `_call_llm`."""

    def __init__(self, content: str) -> None:
        self.message = _FakeOllamaMessage(content)


class _FakeOllamaChatClient:
    """Stand-in for `OllamaClient` as an async context manager -- `.chat()` streams one chunk
    carrying a fixed `{"generalized_text": ...}` JSON payload, mirroring
    `tests/test_lessons_scrub.py`'s own `_mock_ollama_client` mocking boundary.
    """

    def __init__(self, response_text: str) -> None:
        self._response_text = response_text

    async def __aenter__(self) -> _FakeOllamaChatClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def chat(self, **_kwargs: Any) -> Any:
        import json

        yield _FakeOllamaChunk(json.dumps({"generalized_text": self._response_text}))


def _fake_ollama_response(monkeypatch: pytest.MonkeyPatch, llm_output_text: str) -> None:
    """Fake only `lessons.scrub`'s `OllamaClient` construction for the duration of one test --
    `generalize_and_scrub` itself is never mocked, so its real generalization-parsing,
    deterministic-redaction, and `verify_scrubbed` logic all run genuinely end-to-end.
    """
    monkeypatch.setattr(
        "penguincode_cli.lessons.scrub.OllamaClient",
        lambda **_kw: _FakeOllamaChatClient(llm_output_text),
    )


# ---------------------------------------------------------------------------
# ListPendingLessons -> PendingLessonStore.list_pending
# ---------------------------------------------------------------------------


class TestListPendingLessons:
    @pytest.mark.asyncio
    async def test_flag_off_aborts_failed_precondition(
        self, scope_ctx: ScopeContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG_ENV, "false")
        service = _service()
        context = _FakeContext()
        with pytest.raises(AbortCalledError):
            await service.ListPendingLessons(ListPendingLessonsRequest(api_version="v1"), context)
        assert context.aborted_with[0] == grpc.StatusCode.FAILED_PRECONDITION  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_default_status_is_pending(self, scope_ctx: ScopeContext) -> None:
        store = _FakeStore()
        service = _service(store=store)

        await service.ListPendingLessons(
            ListPendingLessonsRequest(api_version="v1"), _FakeContext()
        )

        store.list_pending.assert_called_once_with(scope_ctx, status="pending")

    @pytest.mark.asyncio
    async def test_explicit_status_forwarded(self, scope_ctx: ScopeContext) -> None:
        store = _FakeStore()
        service = _service(store=store)

        await service.ListPendingLessons(
            ListPendingLessonsRequest(api_version="v1", status="approved"), _FakeContext()
        )

        store.list_pending.assert_called_once_with(scope_ctx, status="approved")

    @pytest.mark.asyncio
    async def test_maps_every_field(self, scope_ctx: ScopeContext) -> None:
        record = _record(
            findings=[FindingRecord(kind="email", detail="residual email address detected")],
            reviewed_by="reviewer-1",
            reviewed_at="2026-09-26T00:00:00+00:00",
            status="approved",
        )
        store = _FakeStore(list_result=[record])
        service = _service(store=store)

        response = await service.ListPendingLessons(
            ListPendingLessonsRequest(api_version="v1", status="approved"), _FakeContext()
        )

        assert len(response.pending_lessons) == 1
        pl = response.pending_lessons[0]
        assert pl.id == "pending-1"
        assert pl.generalized_text == "a generalized, client-agnostic lesson"
        assert pl.status == "approved"
        assert pl.findings[0].kind == "email"
        assert pl.proposer == "proposer-1"
        assert pl.source_team == "team-a"
        assert pl.reviewer == "reviewer-1"
        assert pl.reviewed_at == "2026-09-26T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_maps_none_fields_to_empty_strings(self, scope_ctx: ScopeContext) -> None:
        record = _record(
            source_team_id=None, proposer_user_id=None, reviewed_by=None, reviewed_at=None
        )
        store = _FakeStore(list_result=[record])
        service = _service(store=store)

        response = await service.ListPendingLessons(
            ListPendingLessonsRequest(api_version="v1"), _FakeContext()
        )

        pl = response.pending_lessons[0]
        assert pl.proposer == ""
        assert pl.source_team == ""
        assert pl.reviewer == ""
        assert pl.reviewed_at == ""


# ---------------------------------------------------------------------------
# ApproveLesson -> authz gate -> ScopedMemoryManager.add -> PendingLessonStore.set_status
# ---------------------------------------------------------------------------


class TestApproveLesson:
    @pytest.mark.asyncio
    async def test_without_approve_scope_returns_permission_denied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx(scopes=())
        token = auth_middleware._current_scope.set(ctx)
        try:
            store = _FakeStore()
            service = _service(store=store)
            context = _FakeContext()

            with pytest.raises(AbortCalledError):
                await service.ApproveLesson(
                    ApproveLessonRequest(api_version="v1", pending_id="pending-1"), context
                )
            assert context.aborted_with[0] == grpc.StatusCode.PERMISSION_DENIED  # type: ignore[index]
            store.get.assert_not_called()
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_flag_off_aborts_failed_precondition_even_with_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG_ENV, "false")
        ctx = _ctx(scopes=(LESSONS_APPROVE_SCOPE,))
        token = auth_middleware._current_scope.set(ctx)
        try:
            service = _service()
            context = _FakeContext()
            with pytest.raises(AbortCalledError):
                await service.ApproveLesson(
                    ApproveLessonRequest(api_version="v1", pending_id="pending-1"), context
                )
            assert context.aborted_with[0] == grpc.StatusCode.FAILED_PRECONDITION  # type: ignore[index]
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_unknown_pending_id_returns_not_found(self) -> None:
        ctx = _ctx(scopes=(LESSONS_APPROVE_SCOPE,))
        token = auth_middleware._current_scope.set(ctx)
        try:
            store = _FakeStore(get_result=None)
            service = _service(store=store)
            context = _FakeContext()
            with pytest.raises(AbortCalledError):
                await service.ApproveLesson(
                    ApproveLessonRequest(api_version="v1", pending_id="missing"), context
                )
            assert context.aborted_with[0] == grpc.StatusCode.NOT_FOUND  # type: ignore[index]
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_already_reviewed_returns_failed_precondition(self) -> None:
        ctx = _ctx(scopes=(LESSONS_APPROVE_SCOPE,), user_id="approver-1")
        token = auth_middleware._current_scope.set(ctx)
        try:
            record = _record(proposer_user_id="proposer-1", status="approved")
            store = _FakeStore(get_result=record)
            scoped_memory = _FakeScopedMemory()
            service = _service(store=store, scoped_memory=scoped_memory)
            context = _FakeContext()
            with pytest.raises(AbortCalledError):
                await service.ApproveLesson(
                    ApproveLessonRequest(api_version="v1", pending_id="pending-1"), context
                )
            assert context.aborted_with[0] == grpc.StatusCode.FAILED_PRECONDITION  # type: ignore[index]
            scoped_memory.add.assert_not_awaited()
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_self_approval_blocked_by_separation_of_duties(self) -> None:
        ctx = _ctx(scopes=(LESSONS_APPROVE_SCOPE,), user_id="same-user")
        token = auth_middleware._current_scope.set(ctx)
        try:
            record = _record(proposer_user_id="same-user", status="pending")
            store = _FakeStore(get_result=record)
            scoped_memory = _FakeScopedMemory()
            service = _service(store=store, scoped_memory=scoped_memory)
            context = _FakeContext()

            with pytest.raises(AbortCalledError):
                await service.ApproveLesson(
                    ApproveLessonRequest(api_version="v1", pending_id="pending-1"), context
                )
            assert context.aborted_with[0] == grpc.StatusCode.PERMISSION_DENIED  # type: ignore[index]
            scoped_memory.add.assert_not_awaited()
            store.set_status.assert_not_called()
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_approve_writes_tenant_visibility_memory_and_sets_status(self) -> None:
        ctx = _ctx(scopes=(LESSONS_APPROVE_SCOPE,), user_id="approver-1")
        token = auth_middleware._current_scope.set(ctx)
        try:
            record = _record(
                id="pending-9", proposer_user_id="proposer-1", generalized_text="the lesson text"
            )
            store = _FakeStore(get_result=record)
            scoped_memory = _FakeScopedMemory()
            service = _service(store=store, scoped_memory=scoped_memory)

            response = await service.ApproveLesson(
                ApproveLessonRequest(api_version="v1", pending_id="pending-9"), _FakeContext()
            )

            assert response.approved is True
            scoped_memory.add.assert_awaited_once_with(
                ctx,
                "the lesson text",
                visibility="tenant",
                metadata={"source": "lessons-promotion", "pending_lesson_id": "pending-9"},
            )
            store.set_status.assert_called_once_with(
                ctx, "pending-9", "approved", reviewer="approver-1"
            )
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_memory_write_disabled_still_approves(self) -> None:
        """`ScopedMemoryManager.add` returning `None` (memory disabled / RAG flag off) is a
        degraded operational state, not a failure -- the review decision still records.
        """
        ctx = _ctx(scopes=(LESSONS_APPROVE_SCOPE,), user_id="approver-1")
        token = auth_middleware._current_scope.set(ctx)
        try:
            record = _record(proposer_user_id="proposer-1")
            store = _FakeStore(get_result=record)
            scoped_memory = _FakeScopedMemory()
            scoped_memory.add.return_value = None
            service = _service(store=store, scoped_memory=scoped_memory)

            response = await service.ApproveLesson(
                ApproveLessonRequest(api_version="v1", pending_id="pending-1"), _FakeContext()
            )

            assert response.approved is True
            store.set_status.assert_called_once()
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_race_lost_to_another_reviewer_maps_to_failed_precondition(self) -> None:
        ctx = _ctx(scopes=(LESSONS_APPROVE_SCOPE,), user_id="approver-1")
        token = auth_middleware._current_scope.set(ctx)
        try:
            record = _record(proposer_user_id="proposer-1")
            store = _FakeStore(get_result=record)
            store.set_status.side_effect = LookupError("already reviewed")
            service = _service(store=store)
            context = _FakeContext()

            with pytest.raises(AbortCalledError):
                await service.ApproveLesson(
                    ApproveLessonRequest(api_version="v1", pending_id="pending-1"), context
                )
            assert context.aborted_with[0] == grpc.StatusCode.FAILED_PRECONDITION  # type: ignore[index]
        finally:
            auth_middleware._current_scope.reset(token)


# ---------------------------------------------------------------------------
# RejectLesson -> authz gate -> PendingLessonStore.set_status
# ---------------------------------------------------------------------------


class TestRejectLesson:
    @pytest.mark.asyncio
    async def test_without_approve_scope_returns_permission_denied(self) -> None:
        ctx = _ctx(scopes=())
        token = auth_middleware._current_scope.set(ctx)
        try:
            store = _FakeStore()
            service = _service(store=store)
            context = _FakeContext()
            with pytest.raises(AbortCalledError):
                await service.RejectLesson(
                    RejectLessonRequest(api_version="v1", pending_id="pending-1"), context
                )
            assert context.aborted_with[0] == grpc.StatusCode.PERMISSION_DENIED  # type: ignore[index]
            store.set_status.assert_not_called()
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_flag_off_aborts_failed_precondition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG_ENV, "false")
        ctx = _ctx(scopes=(LESSONS_APPROVE_SCOPE,))
        token = auth_middleware._current_scope.set(ctx)
        try:
            service = _service()
            context = _FakeContext()
            with pytest.raises(AbortCalledError):
                await service.RejectLesson(
                    RejectLessonRequest(api_version="v1", pending_id="pending-1"), context
                )
            assert context.aborted_with[0] == grpc.StatusCode.FAILED_PRECONDITION  # type: ignore[index]
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_reject_calls_set_status_with_reason(self) -> None:
        ctx = _ctx(scopes=(LESSONS_APPROVE_SCOPE,), user_id="approver-1")
        token = auth_middleware._current_scope.set(ctx)
        try:
            store = _FakeStore()
            service = _service(store=store)

            response = await service.RejectLesson(
                RejectLessonRequest(
                    api_version="v1", pending_id="pending-1", reason="still names the client"
                ),
                _FakeContext(),
            )

            assert response.rejected is True
            store.set_status.assert_called_once_with(
                ctx, "pending-1", "rejected", reviewer="approver-1", reason="still names the client"
            )
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_empty_reason_passed_as_none(self) -> None:
        ctx = _ctx(scopes=(LESSONS_APPROVE_SCOPE,), user_id="approver-1")
        token = auth_middleware._current_scope.set(ctx)
        try:
            store = _FakeStore()
            service = _service(store=store)

            await service.RejectLesson(
                RejectLessonRequest(api_version="v1", pending_id="pending-1"), _FakeContext()
            )

            store.set_status.assert_called_once_with(
                ctx, "pending-1", "rejected", reviewer="approver-1", reason=None
            )
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_unknown_id_maps_lookup_error_to_not_found(self) -> None:
        ctx = _ctx(scopes=(LESSONS_APPROVE_SCOPE,))
        token = auth_middleware._current_scope.set(ctx)
        try:
            store = _FakeStore()
            store.set_status.side_effect = LookupError("no such pending lesson")
            service = _service(store=store)
            context = _FakeContext()
            with pytest.raises(AbortCalledError):
                await service.RejectLesson(
                    RejectLessonRequest(api_version="v1", pending_id="missing"), context
                )
            assert context.aborted_with[0] == grpc.StatusCode.NOT_FOUND  # type: ignore[index]
        finally:
            auth_middleware._current_scope.reset(token)


# ---------------------------------------------------------------------------
# Live-Postgres: proves the property no mock can -- an approved lesson is
# readable by a DIFFERENT team, SAME tenant caller (firm-wide, not just
# "not team-scoped" in name). Real PendingLessonStore (live pgvector); the
# memory write uses a real ScopedMemoryManager wrapping a fake mem0 backend
# (no real Ollama/pgvector network calls -- mirrors "mock Ollama for scrub"
# applied to the memory boundary; ScopedMemoryManager's own scope-stamping/
# read-filtering logic runs for real).
# ---------------------------------------------------------------------------


class _FakeMem0Backend:
    """Minimal stand-in for mem0's `Memory` class -- in-process, no network.

    What this test proves is `ApproveLesson` + `ScopedMemoryManager`'s real
    scope-stamping/read-filtering logic, not mem0's own embedding/vector-
    search behavior (out of scope here, and already mem0's own concern).
    """

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    def add(
        self, *, messages: list[dict[str, str]], user_id: str, metadata: dict[str, Any], infer: bool
    ) -> dict[str, Any]:
        row = {
            "id": str(uuid.uuid4()),
            "memory": messages[0]["content"],
            "metadata": dict(metadata),
            "_mem0_user_id": user_id,
        }
        self._rows.append(row)
        return {"results": [{"id": row["id"], "memory": row["memory"], "event": "ADD"}]}

    def search(self, *, query: str, filters: dict[str, Any], top_k: int) -> dict[str, Any]:
        matches = [r for r in self._rows if r["_mem0_user_id"] == filters.get("user_id")]
        return {
            "results": [
                {"id": r["id"], "memory": r["memory"], "metadata": r["metadata"], "score": 1.0}
                for r in matches[:top_k]
            ]
        }


@pytest.fixture(scope="module")
def lessons_live_dsn() -> Iterator[str]:
    assert TEST_DATABASE_URL is not None  # narrows type; skipif already guards this
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS penguincode CASCADE")
    run_migrations(dsn=TEST_DATABASE_URL)
    yield TEST_DATABASE_URL


@requires_postgres
class TestApproveLessonLiveFirmWideVisibility:
    @pytest.mark.asyncio
    async def test_approved_lesson_is_readable_by_different_team_same_tenant_caller(
        self, lessons_live_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_RAG_FLAG_ENV, "true")

        tenant_id = str(uuid.uuid4())
        team_a = str(uuid.uuid4())
        team_c = str(uuid.uuid4())
        proposer_id = str(uuid.uuid4())
        approver_id = str(uuid.uuid4())

        store = PendingLessonStore(dsn=lessons_live_dsn)
        proposer_ctx = _ctx(
            tenant_id=tenant_id, org_id=None, team_ids=(team_a,), user_id=proposer_id, scopes=()
        )
        approver_ctx = _ctx(
            tenant_id=tenant_id,
            org_id=None,
            team_ids=(str(uuid.uuid4()),),
            user_id=approver_id,
            scopes=(LESSONS_APPROVE_SCOPE,),
        )
        # A different team in the SAME tenant -- never the proposer's or the approver's team.
        reader_ctx = _ctx(
            tenant_id=tenant_id,
            org_id=None,
            team_ids=(team_c,),
            user_id=str(uuid.uuid4()),
            scopes=(),
        )

        lesson_text = "Always run a canary before a full load-balancer config rollout."
        pending_id = store.create_pending(proposer_ctx, lesson_text, [], team_a)

        manager = MemoryManager(MemoryConfig(enabled=False), "http://ollama.invalid")
        manager.memory = _FakeMem0Backend()  # bypass real mem0 backend (see class docstring)
        scoped_memory = create_scoped_memory_manager(manager)

        service = LessonsServiceImpl(Settings(), store=store, scoped_memory=scoped_memory)

        token = auth_middleware._current_scope.set(approver_ctx)
        try:
            response = await service.ApproveLesson(
                ApproveLessonRequest(api_version="v1", pending_id=pending_id), _FakeContext()
            )
        finally:
            auth_middleware._current_scope.reset(token)

        assert response.approved is True
        record = store.get(approver_ctx, pending_id)
        assert record is not None
        assert record.status == "approved"
        assert record.reviewed_by == approver_id

        # Firm-wide: a caller on a THIRD team, same tenant, can read it back.
        results = await scoped_memory.search(reader_ctx, lesson_text)
        assert any(r["memory"] == lesson_text for r in results)
