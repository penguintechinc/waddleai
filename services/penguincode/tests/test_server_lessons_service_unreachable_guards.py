"""Coverage completion for `penguincode_cli.server.services.lessons` (T-L2b).

`tests/test_server_lessons_service.py` already exercises every behavioral
branch of `LessonsServiceImpl`'s four RPCs, but it always pairs each `abort`
call with the real `grpc.aio.ServicerContext` contract -- `abort()` itself
always raises -- so the `raise AssertionError("unreachable")` guard lines
immediately following every `abort()` call never actually execute. Those
lines exist purely so `mypy --strict` can prove narrowing (e.g. `record` is
non-`None` past that point) -- they are not dead code to delete, since a
caller could in principle construct a non-conforming `ServicerContext`
double whose `abort()` returns normally, and this module must still fail
loudly (`AssertionError`), not silently fall through with bad data.

This file closes exactly that gap, plus two other gaps the existing suite
never reaches at all:

- `_require_flag_enabled`/`_require_approve_scope`/the four RPCs' `abort()`
  call sites, when paired with a *non-raising* context double, actually
  execute their trailing `raise AssertionError("unreachable")` guard.
- `ListPendingLessons`' `except ValueError` branch (`_store.list_pending`
  raising) -- no existing test ever makes that store call raise.
- `_build_scoped_memory_manager` -- every existing test injects
  `scoped_memory=` directly, so this module's real construction-failure
  fallback (a disabled/unreachable Ollama/pgvector at startup must never
  crash the server) is never exercised.

# regression: lessons-promotion coverage gate (penguincode #cov)
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import grpc
import pytest

import penguincode_cli.auth.middleware as auth_middleware
import penguincode_cli.server.services.lessons as lessons_module
from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import MemoryConfig, Settings
from penguincode_cli.proto import (
    ApproveLessonRequest,
    ListPendingLessonsRequest,
    PromoteLessonRequest,
)
from penguincode_cli.server.services.lessons import LESSONS_APPROVE_SCOPE, LessonsServiceImpl
from tests.test_server_lessons_service import (
    _FLAG_ENV,
    _ctx,
    _FakeScopedMemory,
    _FakeStore,
    _record,
)

_PENDING_ID = "pending-1"


class _NonRaisingContext:
    """`grpc.aio.ServicerContext` double whose `abort()` returns normally.

    A real `grpc.aio.ServicerContext.abort()` always raises
    `grpc.aio.AbortError` -- this double intentionally violates that so the
    module's trailing `raise AssertionError("unreachable")` guards (present
    for `mypy --strict` narrowing, see this file's module docstring) are
    directly exercised rather than being truly dead code.
    """

    def __init__(self) -> None:
        self.aborted_with: tuple[Any, str] | None = None

    async def abort(self, code: Any, details: str) -> None:
        self.aborted_with = (code, details)


@pytest.fixture(autouse=True)
def _no_leftover_scope() -> Iterator[None]:
    """Guarantee `current_scope_context()` is `None` before/after each test."""
    assert auth_middleware.current_scope_context() is None
    yield
    auth_middleware._current_scope.set(None)


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here wants `LESSONS_PROMOTION_FLAG` on by default."""
    monkeypatch.setenv(_FLAG_ENV, "true")


def _service(
    *, store: _FakeStore | None = None, scoped_memory: _FakeScopedMemory | None = None
) -> LessonsServiceImpl:
    return LessonsServiceImpl(
        Settings(),
        store=store or _FakeStore(),
        scoped_memory=scoped_memory or _FakeScopedMemory(),
    )


class TestRequireFlagEnabledUnreachableGuard:
    @pytest.mark.asyncio
    async def test_raises_assertion_when_abort_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Covers `_require_flag_enabled`'s trailing unreachable-guard line."""
        monkeypatch.setenv(_FLAG_ENV, "false")
        ctx = _ctx()
        context = _NonRaisingContext()
        with pytest.raises(AssertionError, match="unreachable"):
            await lessons_module._require_flag_enabled(ctx, context)
        assert context.aborted_with is not None
        assert context.aborted_with[0] == grpc.StatusCode.FAILED_PRECONDITION


class TestRequireApproveScopeUnreachableGuard:
    @pytest.mark.asyncio
    async def test_raises_assertion_when_abort_does_not_raise(self) -> None:
        """Covers `_require_approve_scope`'s trailing unreachable-guard line."""
        ctx = _ctx(scopes=())
        context = _NonRaisingContext()
        with pytest.raises(AssertionError, match="unreachable"):
            await lessons_module._require_approve_scope(ctx, context)
        assert context.aborted_with is not None
        assert context.aborted_with[0] == grpc.StatusCode.PERMISSION_DENIED


class TestPromoteLessonInvalidArgumentUnreachableGuard:
    @pytest.mark.asyncio
    async def test_raises_assertion_when_abort_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Covers `PromoteLesson`'s `except ValueError` trailing unreachable-guard line."""
        ctx = _ctx()
        token = auth_middleware._current_scope.set(ctx)
        try:
            from penguincode_cli.lessons.scrub import ScrubResult, Verdict

            clean_result = ScrubResult(
                generalized_text="clean", redactions=[], verdict=Verdict(clean=True)
            )

            async def _fake_scrub(ctx: ScopeContext, content: str, **kwargs: Any) -> ScrubResult:
                return clean_result

            monkeypatch.setattr(lessons_module, "generalize_and_scrub", _fake_scrub)
            store = _FakeStore()
            store.create_pending.side_effect = ValueError("not one of the caller's own teams")
            service = _service(store=store)
            context = _NonRaisingContext()

            with pytest.raises(AssertionError, match="unreachable"):
                await service.PromoteLesson(
                    PromoteLessonRequest(
                        api_version="v1", source_content="x", team_id="not-my-team"
                    ),
                    context,
                )
            assert context.aborted_with is not None
            assert context.aborted_with[0] == grpc.StatusCode.INVALID_ARGUMENT
        finally:
            auth_middleware._current_scope.reset(token)


class TestListPendingLessonsValueError:
    @pytest.mark.asyncio
    async def test_store_raising_value_error_aborts_invalid_argument(self) -> None:
        """No existing test ever makes `list_pending` raise -- covers the
        `except ValueError` branch's abort call (real, raising context)."""
        ctx = _ctx()
        token = auth_middleware._current_scope.set(ctx)
        try:
            store = _FakeStore()
            store.list_pending.side_effect = ValueError("bad status filter")
            service = _service(store=store)

            from tests.test_server_lessons_service import AbortCalledError, _FakeContext

            context = _FakeContext()
            with pytest.raises(AbortCalledError):
                await service.ListPendingLessons(
                    ListPendingLessonsRequest(api_version="v1", status="not-a-status"), context
                )
            assert context.aborted_with is not None
            assert context.aborted_with[0] == grpc.StatusCode.INVALID_ARGUMENT
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_raises_assertion_when_abort_does_not_raise(self) -> None:
        """Non-raising context variant of the same path -- covers the
        trailing unreachable-guard line inside the `except ValueError` branch."""
        ctx = _ctx()
        token = auth_middleware._current_scope.set(ctx)
        try:
            store = _FakeStore()
            store.list_pending.side_effect = ValueError("bad status filter")
            service = _service(store=store)
            context = _NonRaisingContext()

            with pytest.raises(AssertionError, match="unreachable"):
                await service.ListPendingLessons(
                    ListPendingLessonsRequest(api_version="v1", status="not-a-status"),
                    context,
                )
            assert context.aborted_with is not None
            assert context.aborted_with[0] == grpc.StatusCode.INVALID_ARGUMENT
        finally:
            auth_middleware._current_scope.reset(token)


class TestApproveLessonUnreachableGuards:
    @pytest.mark.asyncio
    async def test_not_found_raises_assertion_when_abort_does_not_raise(self) -> None:
        """Covers the `record is None` branch's trailing unreachable-guard line."""
        ctx = _ctx(scopes=(LESSONS_APPROVE_SCOPE,))
        token = auth_middleware._current_scope.set(ctx)
        try:
            store = _FakeStore(get_result=None)
            service = _service(store=store)
            context = _NonRaisingContext()

            with pytest.raises(AssertionError, match="unreachable"):
                await service.ApproveLesson(
                    ApproveLessonRequest(api_version="v1", pending_id="missing"),
                    context,
                )
            assert context.aborted_with is not None
            assert context.aborted_with[0] == grpc.StatusCode.NOT_FOUND
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_already_reviewed_raises_assertion_when_abort_does_not_raise(self) -> None:
        """Covers the `record.status != "pending"` branch's unreachable-guard line."""
        ctx = _ctx(scopes=(LESSONS_APPROVE_SCOPE,), user_id="approver-1")
        token = auth_middleware._current_scope.set(ctx)
        try:
            record = _record(proposer_user_id="proposer-1", status="approved")
            store = _FakeStore(get_result=record)
            service = _service(store=store)
            context = _NonRaisingContext()

            with pytest.raises(AssertionError, match="unreachable"):
                await service.ApproveLesson(
                    ApproveLessonRequest(api_version="v1", pending_id=_PENDING_ID),
                    context,
                )
            assert context.aborted_with is not None
            assert context.aborted_with[0] == grpc.StatusCode.FAILED_PRECONDITION
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_self_approval_raises_assertion_when_abort_does_not_raise(self) -> None:
        """Covers the separation-of-duties branch's trailing unreachable-guard line."""
        ctx = _ctx(scopes=(LESSONS_APPROVE_SCOPE,), user_id="same-user")
        token = auth_middleware._current_scope.set(ctx)
        try:
            record = _record(proposer_user_id="same-user", status="pending")
            store = _FakeStore(get_result=record)
            service = _service(store=store)
            context = _NonRaisingContext()

            with pytest.raises(AssertionError, match="unreachable"):
                await service.ApproveLesson(
                    ApproveLessonRequest(api_version="v1", pending_id=_PENDING_ID),
                    context,
                )
            assert context.aborted_with is not None
            assert context.aborted_with[0] == grpc.StatusCode.PERMISSION_DENIED
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_reverification_failure_raises_assertion_when_abort_does_not_raise(
        self,
    ) -> None:
        """Covers the F6 re-verification-failure branch's unreachable-guard line."""
        ctx = _ctx(scopes=(LESSONS_APPROVE_SCOPE,), user_id="approver-1")
        token = auth_middleware._current_scope.set(ctx)
        try:
            record = _record(
                proposer_user_id="proposer-1",
                generalized_text="The rollout at Acme Corp took three extra days.",
            )
            store = _FakeStore(get_result=record)

            class _GraphStoreKnowsAcme:
                def list_node_keys(self, ctx: ScopeContext, kind: str, node_types: Any) -> list[str]:
                    return ["Acme Corp"]

            service = LessonsServiceImpl(
                Settings(),
                store=store,
                scoped_memory=_FakeScopedMemory(),
                graph_store=_GraphStoreKnowsAcme(),
            )
            context = _NonRaisingContext()

            with pytest.raises(AssertionError, match="unreachable"):
                await service.ApproveLesson(
                    ApproveLessonRequest(api_version="v1", pending_id=_PENDING_ID),
                    context,
                )
            assert context.aborted_with is not None
            assert context.aborted_with[0] == grpc.StatusCode.FAILED_PRECONDITION
            store.set_status.assert_not_called()
        finally:
            auth_middleware._current_scope.reset(token)

    @pytest.mark.asyncio
    async def test_race_lost_raises_assertion_when_abort_does_not_raise(self) -> None:
        """Covers the F5 lost-the-race (`LookupError` on `set_status`) unreachable-guard line."""
        ctx = _ctx(scopes=(LESSONS_APPROVE_SCOPE,), user_id="approver-1")
        token = auth_middleware._current_scope.set(ctx)
        try:
            record = _record(proposer_user_id="proposer-1")
            store = _FakeStore(get_result=record)
            store.set_status.side_effect = LookupError("already reviewed")
            service = _service(store=store)
            context = _NonRaisingContext()

            with pytest.raises(AssertionError, match="unreachable"):
                await service.ApproveLesson(
                    ApproveLessonRequest(api_version="v1", pending_id=_PENDING_ID),
                    context,
                )
            assert context.aborted_with is not None
            assert context.aborted_with[0] == grpc.StatusCode.FAILED_PRECONDITION
        finally:
            auth_middleware._current_scope.reset(token)


class TestRejectLessonUnreachableGuard:
    @pytest.mark.asyncio
    async def test_not_found_raises_assertion_when_abort_does_not_raise(self) -> None:
        """Covers `RejectLesson`'s `except LookupError` trailing unreachable-guard line."""
        from penguincode_cli.proto import RejectLessonRequest

        ctx = _ctx(scopes=(LESSONS_APPROVE_SCOPE,))
        token = auth_middleware._current_scope.set(ctx)
        try:
            store = _FakeStore()
            store.set_status.side_effect = LookupError("no such pending lesson")
            service = _service(store=store)
            context = _NonRaisingContext()

            with pytest.raises(AssertionError, match="unreachable"):
                await service.RejectLesson(
                    RejectLessonRequest(api_version="v1", pending_id="missing"),
                    context,
                )
            assert context.aborted_with is not None
            assert context.aborted_with[0] == grpc.StatusCode.NOT_FOUND
        finally:
            auth_middleware._current_scope.reset(token)


class TestBuildScopedMemoryManager:
    """No existing test leaves `scoped_memory=` unset, so the real
    `_build_scoped_memory_manager` fallback (used by `LessonsServiceImpl.__init__`
    when no double is injected) is never exercised."""

    def test_disabled_memory_config_constructs_without_error(self) -> None:
        """Try-succeeds path (294-297, 301): `MemoryConfig(enabled=False)`
        short-circuits before any mem0/Ollama network access."""
        settings = Settings(memory=MemoryConfig(enabled=False))
        manager = lessons_module._build_scoped_memory_manager(settings)
        assert manager is not None

    def test_construction_failure_degrades_to_disabled_manager(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Except path (298-300): a construction-time failure (unreachable
        Ollama/pgvector) must degrade to a disabled MemoryManager, never crash
        the server at startup."""
        monkeypatch.setattr(
            lessons_module,
            "create_memory_manager",
            MagicMock(side_effect=RuntimeError("ollama unreachable")),
        )
        settings = Settings()
        manager = lessons_module._build_scoped_memory_manager(settings)
        assert manager is not None

    def test_lessons_service_impl_falls_back_when_scoped_memory_not_injected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: constructing `LessonsServiceImpl` without a `scoped_memory=`
        double exercises `_build_scoped_memory_manager` through `__init__` itself."""
        settings = Settings(memory=MemoryConfig(enabled=False))
        service = LessonsServiceImpl(settings, store=_FakeStore())
        assert service._scoped_memory is not None
