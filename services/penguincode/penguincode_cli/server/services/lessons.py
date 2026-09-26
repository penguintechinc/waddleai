"""LessonsService gRPC servicer (T-L2b): the lessons-learned promotion review workflow.

Wires T-L1's safety pipeline (``lessons.scrub.generalize_and_scrub``/
``verify_scrubbed``) and T-L2a's storage (``lessons.store.PendingLessonStore``)
and wire contract (``proto/lessons/v1/lessons.proto``) together server-side,
exactly as ``server/services/knowledge.py`` (F2) wires the knowledge
platform: every handler derives its ``ScopeContext`` exclusively from the
caller's validated WaddleAI JWT (``auth.middleware.current_scope_context()``)
and aborts ``UNAUTHENTICATED`` before doing any work if none is present --
never a client-supplied tenant/org/team/user, per ``lessons.proto``'s scope-
model contract.

**Review is a privileged action.** ``PromoteLesson``/``ListPendingLessons``
require only a valid ScopeContext (any team member may propose or see their
tenant's review queue) -- but ``ApproveLesson``/``RejectLesson`` additionally
require :data:`LESSONS_APPROVE_SCOPE` in the caller's ``ctx.scopes``, per
security.md's OIDC-scopes-only authz rule (never a role name). Approval is
also separation-of-duties enforced: a caller can never approve a lesson it
itself proposed, even holding the approve scope -- the whole point of review
is an independent second set of eyes before something goes firm-wide.

**Approval is the only path to firm-wide (tenant) visibility.** On approve,
this handler writes the pending lesson's ``generalized_text`` into memory at
``visibility="tenant"`` via ``tools.memory.ScopedMemoryManager.add`` --
tenant-visibility memories are readable by every team in the tenant (see
that module's ``_is_visible``), which is exactly "shared firm-wide" in the
consulting analogy ``lessons.scrub``'s module docstring sets up. The write is
best-effort (mirrors ``ScopedMemoryManager.add`` returning ``None`` when
memory is disabled or the ``penguincode.rag`` flag is off elsewhere in this
codebase -- a degraded memory layer is an operational state, not a failure);
the ``pending_lessons`` row's status transition is this RPC's authoritative,
always-attempted effect.

**Flag-gated** on :data:`LESSONS_PROMOTION_FLAG`: ``PromoteLesson`` gets this
for free from ``generalize_and_scrub`` (a disabled flag degrades to a
``blocked=true`` response, never an abort -- preserving the proto's designed
"verifier rejected, nothing persisted" contract). ``ListPendingLessons``/
``ApproveLesson``/``RejectLesson`` call no scrub pipeline of their own, so
each checks the flag explicitly and aborts ``FAILED_PRECONDITION`` when off.

Every ``PendingLessonStore`` call is blocking psycopg I/O -- run via
``asyncio.to_thread`` (mirrors ``server/services/knowledge.py``'s
``IndexCode`` handler wrapping ``graphs.code.index_code`` the same way),
never called directly from this async servicer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

import grpc

from penguincode_cli.auth.middleware import current_scope_context
from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import MemoryConfig, Settings
from penguincode_cli.flags import is_enabled
from penguincode_cli.lessons.scrub import LESSONS_PROMOTION_FLAG, generalize_and_scrub
from penguincode_cli.lessons.scrub import Finding as ScrubFinding
from penguincode_cli.lessons.store import PendingLessonRecord, PendingLessonStore
from penguincode_cli.observability.otel import store_span
from penguincode_cli.proto import (
    ApproveLessonRequest,
    ApproveLessonResponse,
    LessonsServiceServicer,
    ListPendingLessonsRequest,
    ListPendingLessonsResponse,
    PendingLesson,
    PromoteLessonRequest,
    PromoteLessonResponse,
    RejectLessonRequest,
    RejectLessonResponse,
)
from penguincode_cli.proto import Finding as ProtoFinding
from penguincode_cli.tools.memory import (
    MemoryManager,
    ScopedMemoryManager,
    create_memory_manager,
    create_scoped_memory_manager,
)

logger = logging.getLogger(__name__)

#: The elevated scope `ApproveLesson`/`RejectLesson` require, per security.md's
#: OIDC-scopes-only authz rule ("never branch on role names"). Distinct from
#: any of the four knowledge-platform flags -- this gates the *review*
#: action, not the promotion pipeline itself (see `LESSONS_PROMOTION_FLAG`).
LESSONS_APPROVE_SCOPE = "lessons:approve"


@runtime_checkable
class _PendingLessonStoreLike(Protocol):
    """Structural match for the four `PendingLessonStore` methods this servicer calls.

    Typed as a narrow local Protocol (mirroring `server.services.knowledge`'s
    `_IndexerLike`/`_ScopedMemoryLike` style) so a test double needs only
    satisfy this exact shape -- not construct a real `PendingLessonStore`
    (and therefore a real DSN/connection).
    """

    def create_pending(
        self,
        ctx: ScopeContext,
        generalized_text: str,
        findings: Sequence[ScrubFinding],
        source_team_id: str | None,
    ) -> str: ...

    def list_pending(
        self, ctx: ScopeContext, *, status: str = "pending"
    ) -> list[PendingLessonRecord]: ...

    def get(self, ctx: ScopeContext, pending_id: str) -> PendingLessonRecord | None: ...

    def set_status(
        self,
        ctx: ScopeContext,
        pending_id: str,
        status: str,
        *,
        reviewer: str,
        reason: str | None = None,
    ) -> None: ...


@runtime_checkable
class _ScopedMemoryLike(Protocol):
    """Structural match for the one `ScopedMemoryManager` method this servicer calls."""

    async def add(
        self,
        ctx: ScopeContext,
        content: str,
        *,
        visibility: str = "user",
        team_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None: ...


async def _require_scope(context: grpc.aio.ServicerContext) -> ScopeContext:
    """Return the in-flight request's `ScopeContext`, or abort `UNAUTHENTICATED`.

    Identical contract to `server.services.knowledge`'s helper of the same
    name (duplicated here rather than imported -- this module stays self-
    contained, matching this codebase's existing convention, e.g.
    `lessons.scrub`'s duplicated `_extract_json_block`): the sole source of
    scope is `current_scope_context()`, never a client-supplied identity.
    """
    ctx = current_scope_context()
    if ctx is None:
        await context.abort(
            grpc.StatusCode.UNAUTHENTICATED,
            "no ScopeContext available -- missing or invalid WaddleAI JWT",
        )
    assert ctx is not None  # context.abort() always raises; unreachable otherwise
    return ctx


async def _require_flag_enabled(ctx: ScopeContext, context: grpc.aio.ServicerContext) -> None:
    """Abort `FAILED_PRECONDITION` unless `LESSONS_PROMOTION_FLAG` is on for `ctx`.

    `PromoteLesson` never calls this -- it gets the same gate for free inside
    `generalize_and_scrub` (a disabled flag there degrades to a
    `blocked=true` response, never an RPC abort, preserving the proto's
    designed "verifier rejected, nothing persisted" contract). Every other
    RPC in this servicer calls no scrub pipeline of its own, so each checks
    explicitly.
    """
    if not is_enabled(LESSONS_PROMOTION_FLAG, ctx):
        await context.abort(
            grpc.StatusCode.FAILED_PRECONDITION, f"{LESSONS_PROMOTION_FLAG} is disabled"
        )
        raise AssertionError("unreachable")  # abort() always raises


async def _require_approve_scope(ctx: ScopeContext, context: grpc.aio.ServicerContext) -> None:
    """Abort `PERMISSION_DENIED` unless `LESSONS_APPROVE_SCOPE` is in `ctx.scopes`.

    Scope-based, never a role name (security.md OIDC Claims & Scopes) --
    checks the caller's own validated JWT scopes, never anything
    client-supplied on the request.
    """
    if LESSONS_APPROVE_SCOPE not in ctx.scopes:
        await context.abort(
            grpc.StatusCode.PERMISSION_DENIED,
            f"requires the {LESSONS_APPROVE_SCOPE!r} scope",
        )
        raise AssertionError("unreachable")  # abort() always raises


def _to_proto_pending_lesson(record: PendingLessonRecord) -> PendingLesson:
    """Map a DB-facing `PendingLessonRecord` to its wire `PendingLesson` message."""
    return PendingLesson(
        id=record.id,
        generalized_text=record.generalized_text,
        status=record.status,
        findings=[
            ProtoFinding(kind=finding.kind, detail=finding.detail) for finding in record.findings
        ],
        proposer=record.proposer_user_id or "",
        source_team=record.source_team_id or "",
        created_at=record.created_at,
        reviewer=record.reviewed_by or "",
        reviewed_at=record.reviewed_at or "",
    )


def _build_scoped_memory_manager(settings: Settings) -> ScopedMemoryManager:
    """Construct a `ScopedMemoryManager`, degrading to disabled on construction failure.

    Duplicated from `server.services.knowledge`'s identical helper (this
    module stays self-contained, see this module's docstring) -- an
    unreachable Ollama/pgvector at server startup must never crash the
    process; `ScopedMemoryManager.add` already degrades gracefully (returns
    `None`) when the wrapped manager is disabled.
    """
    try:
        manager = create_memory_manager(
            settings.memory, settings.ollama.api_url, settings.models.orchestration
        )
    except Exception as exc:  # noqa: BLE001 -- mem0/Ollama outage at construction must not crash the server
        logger.warning("lessons: MemoryManager construction failed, memory disabled: %s", exc)
        manager = MemoryManager(MemoryConfig(enabled=False), settings.ollama.api_url)
    return create_scoped_memory_manager(manager)


class LessonsServiceImpl(LessonsServiceServicer):
    """Server-side implementation of all four `LessonsService` RPCs.

    `store`/`scoped_memory` are constructed from `settings` by default but
    keyword-only injectable for tests, mirroring `KnowledgeServiceImpl`'s own
    seams.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        store: _PendingLessonStoreLike | None = None,
        scoped_memory: _ScopedMemoryLike | None = None,
    ) -> None:
        self._settings = settings
        self._store: _PendingLessonStoreLike = (
            store if store is not None else PendingLessonStore(dsn=settings.graph.postgres.url)
        )
        self._scoped_memory = (
            scoped_memory if scoped_memory is not None else _build_scoped_memory_manager(settings)
        )

    async def PromoteLesson(
        self, request: PromoteLessonRequest, context: grpc.aio.ServicerContext
    ) -> PromoteLessonResponse:
        """Scrub+verify `source_content`; a clean result becomes a new pending lesson.

        Any team member may propose -- no elevated scope required, only a
        valid `ScopeContext`. `team_id`, when given, must be one of the
        caller's own teams; `PendingLessonStore.create_pending` enforces that
        (mirrors `stores.vector._validate_team_id`) and this handler maps its
        `ValueError` to `INVALID_ARGUMENT`.
        """
        ctx = await _require_scope(context)
        team_id = request.team_id or None
        source_metadata = dict(request.source_metadata)

        with store_span("lessons.PromoteLesson", has_team=bool(team_id)):
            result = await generalize_and_scrub(
                ctx, request.source_content, source_metadata=source_metadata
            )

            if not result.verdict.clean:
                return PromoteLessonResponse(
                    blocked=True,
                    pending_id="",
                    findings=[
                        ProtoFinding(kind=finding.kind.value, detail=finding.detail)
                        for finding in result.verdict.findings
                    ],
                )

            try:
                pending_id = await asyncio.to_thread(
                    self._store.create_pending,
                    ctx,
                    result.generalized_text,
                    result.verdict.findings,
                    team_id,
                )
            except ValueError as exc:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
                raise AssertionError("unreachable") from exc  # abort() always raises

        return PromoteLessonResponse(blocked=False, pending_id=pending_id, findings=[])

    async def ListPendingLessons(
        self, request: ListPendingLessonsRequest, context: grpc.aio.ServicerContext
    ) -> ListPendingLessonsResponse:
        """List the caller's tenant's review queue -- never team-scoped (a tenant-wide action)."""
        ctx = await _require_scope(context)
        await _require_flag_enabled(ctx, context)
        status = request.status or "pending"

        with store_span("lessons.ListPendingLessons", status=status):
            try:
                records = await asyncio.to_thread(self._store.list_pending, ctx, status=status)
            except ValueError as exc:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
                raise AssertionError("unreachable") from exc  # abort() always raises

        return ListPendingLessonsResponse(
            pending_lessons=[_to_proto_pending_lesson(record) for record in records]
        )

    async def ApproveLesson(
        self, request: ApproveLessonRequest, context: grpc.aio.ServicerContext
    ) -> ApproveLessonResponse:
        """Approve a pending lesson: writes it firm-wide (tenant visibility), then marks approved.

        Requires `LESSONS_APPROVE_SCOPE` and enforces separation of duties --
        see this module's docstring. The memory write is best-effort (see
        docstring); the `pending_lessons` status transition always runs.
        """
        ctx = await _require_scope(context)
        await _require_flag_enabled(ctx, context)
        await _require_approve_scope(ctx, context)

        with store_span("lessons.ApproveLesson"):
            record = await asyncio.to_thread(self._store.get, ctx, request.pending_id)
            if record is None:
                await context.abort(grpc.StatusCode.NOT_FOUND, "pending lesson not found")
                raise AssertionError("unreachable")  # abort() always raises
            if record.status != "pending":
                await context.abort(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    f"pending lesson {request.pending_id!r} is already {record.status!r}",
                )
                raise AssertionError("unreachable")  # abort() always raises
            if record.proposer_user_id is not None and record.proposer_user_id == ctx.user_id:
                await context.abort(
                    grpc.StatusCode.PERMISSION_DENIED,
                    "separation of duties: cannot approve a lesson you proposed yourself",
                )
                raise AssertionError("unreachable")  # abort() always raises

            write_result = await self._scoped_memory.add(
                ctx,
                record.generalized_text,
                visibility="tenant",
                metadata={"source": "lessons-promotion", "pending_lesson_id": record.id},
            )
            if write_result is None:
                logger.warning(
                    "lessons.ApproveLesson: firm-wide memory write skipped (memory disabled "
                    "or the penguincode.rag flag is off) for pending_id=%s",
                    record.id,
                )

            try:
                await asyncio.to_thread(
                    self._store.set_status,
                    ctx,
                    request.pending_id,
                    "approved",
                    reviewer=ctx.user_id,
                )
            except LookupError as exc:
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
                raise AssertionError("unreachable") from exc  # abort() always raises

        return ApproveLessonResponse(approved=True)

    async def RejectLesson(
        self, request: RejectLessonRequest, context: grpc.aio.ServicerContext
    ) -> RejectLessonResponse:
        """Reject a pending lesson with a human-readable reason. Requires `LESSONS_APPROVE_SCOPE`."""
        ctx = await _require_scope(context)
        await _require_flag_enabled(ctx, context)
        await _require_approve_scope(ctx, context)

        with store_span("lessons.RejectLesson"):
            try:
                await asyncio.to_thread(
                    self._store.set_status,
                    ctx,
                    request.pending_id,
                    "rejected",
                    reviewer=ctx.user_id,
                    reason=request.reason or None,
                )
            except LookupError as exc:
                await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
                raise AssertionError("unreachable") from exc  # abort() always raises

        return RejectLessonResponse(rejected=True)


__all__ = ["LESSONS_APPROVE_SCOPE", "LessonsServiceImpl"]
