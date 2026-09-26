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
codebase -- a degraded memory layer is an operational state, not a failure).

**Approve's ordering (security review F5/F6).** Three things happen in this
exact order, never any other: (1) :func:`verify_scrubbed` re-runs on the
pending row's ``generalized_text`` with the server-authoritative identifier
set (see :func:`_known_tenant_identifier_names`) -- a failing re-verification
aborts ``FAILED_PRECONDITION`` with no status change and no write, leaving
the row `pending` for a human to reject or re-review; (2) the row's status
flips PENDING -> APPROVED via the single atomic
``UPDATE ... WHERE status = 'pending'`` (:meth:`PendingLessonStore.set_status`);
(3) the tenant-visibility memory write happens ONLY if that update actually
affected the row (no ``LookupError``) -- never before, and never
unconditionally. Reversing (2) and (3) is exactly the race two concurrent
approvers could hit: both would see the row `pending`, both would write, and
only the loser would then fail on the status flip -- after having already
written. Doing the atomic flip first and gating the write on its result
makes "write is safe to run" and "row this write is for is now `approved`"
the same fact, checked once.

**identifiers used by the F6 re-verification, and by `PromoteLesson`'s own
verification, come from three sources -- see
:func:`_known_tenant_identifier_names`:** ``ctx``'s ids (always present),
``source_metadata``'s names (proposer-supplied, may be omitted -- only used
by `PromoteLesson`, a pending row has no `source_metadata` of its own to
re-check at approval time), and this module's server-authoritative
supplement: every person/org/client/project entity name already recorded
anywhere in the tenant's graph store (queried tenant-wide, not just the
caller's own teams -- see ``stores.graph.GraphStore.list_node_keys``'s own
docstring for why), plus an operator-configured
``settings.lessons.known_identifiers`` list. Neither of those last two is
something a proposer -- or a prompt-injected LLM steered into omitting a
name -- can suppress.

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
from penguincode_cli.lessons.scrub import (
    LESSONS_PROMOTION_FLAG,
    generalize_and_scrub,
    verify_scrubbed,
)
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
from penguincode_cli.stores.graph import VALID_GRAPH_KINDS, create_graph_store
from penguincode_cli.tools.memory import (
    MemoryManager,
    ScopedMemoryManager,
    create_memory_manager,
    create_scoped_memory_manager,
)

logger = logging.getLogger(__name__)

#: Node types the T11-T13 graph extractors' LLM-driven categorization uses
#: for identifying entities (the extraction prompts' own vocabulary example
#: includes "person"/"organization" -- see `graphs.knowledge`/`graphs.memory`)
#: plus lessons-specific synonyms a human-curated node or a different
#: extraction pass might use. Matched case-insensitively against
#: `graph_nodes.node_type` by `stores.graph.GraphStore.list_node_keys` --
#: see `_known_tenant_identifier_names`.
_IDENTIFYING_NODE_TYPES: tuple[str, ...] = (
    "person",
    "organization",
    "org",
    "company",
    "client",
    "customer",
    "project",
    "engagement",
)

#: `stores.graph.VALID_GRAPH_KINDS`, sorted into a fixed, deterministic
#: order -- `_known_tenant_identifier_names` iterates this rather than the
#: frozenset directly, since frozenset iteration order over `str` members is
#: not stable across processes (CPython's per-process string hash
#: randomization), which would make test assertions on call order flaky.
_GRAPH_KINDS_CHECKED: tuple[str, ...] = tuple(sorted(VALID_GRAPH_KINDS))

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


@runtime_checkable
class _GraphStoreLike(Protocol):
    """Structural match for the one `GraphStore` method this servicer calls.

    See `stores.graph.GraphStore.list_node_keys`'s own docstring for why this
    is the one deliberately tenant-wide (not team/user-scoped) read in that
    module -- this servicer's `_known_tenant_identifier_names` is its sole
    caller.
    """

    def list_node_keys(
        self, ctx: ScopeContext, kind: str, node_types: Sequence[str]
    ) -> list[str]: ...


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


def _known_tenant_identifier_names(
    ctx: ScopeContext, graph_store: _GraphStoreLike, settings: Settings
) -> list[str]:
    """Server-authoritative identifier NAME terms a proposer cannot omit or suppress (F2+F3).

    Union of (a) `settings.lessons.known_identifiers` -- an operator-
    configured per-tenant list for names that never made it into the graph
    at all -- and (b) every person/org/client/project entity name already
    recorded anywhere in `ctx`'s tenant, across all three graph kinds
    (`code`/`knowledge`/`memory`), queried **tenant-wide** via
    `GraphStore.list_node_keys` (not scoped to the caller's own teams -- see
    that method's docstring for why this check must see every team's
    engagement, not just the caller's own).

    This is `verify_scrubbed`'s server-side counterpart to `source_metadata`:
    unlike that dict (which the proposer supplies and can omit, or a
    prompt-injected LLM can be steered into omitting), neither source here is
    under the proposer's control. A graph lookup failure for one `kind`
    degrades to "no extra terms from that kind" (logged, never raised) -- a
    graph-store outage must never block the review pipeline; the
    deterministic + ctx-id + metadata-name checks in `verify_scrubbed` still
    run regardless.
    """
    names: list[str] = list(settings.lessons.known_identifiers)
    for kind in _GRAPH_KINDS_CHECKED:
        try:
            names.extend(graph_store.list_node_keys(ctx, kind, _IDENTIFYING_NODE_TYPES))
        except Exception as exc:  # noqa: BLE001 -- a graph outage must not block lesson review
            logger.warning(
                "lessons: known-identifier graph lookup failed for kind=%s: %s", kind, exc
            )
    return names


class LessonsServiceImpl(LessonsServiceServicer):
    """Server-side implementation of all four `LessonsService` RPCs.

    `store`/`scoped_memory`/`graph_store` are constructed from `settings` by
    default but keyword-only injectable for tests, mirroring
    `KnowledgeServiceImpl`'s own seams.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        store: _PendingLessonStoreLike | None = None,
        scoped_memory: _ScopedMemoryLike | None = None,
        graph_store: _GraphStoreLike | None = None,
    ) -> None:
        self._settings = settings
        self._store: _PendingLessonStoreLike = (
            store if store is not None else PendingLessonStore(dsn=settings.graph.postgres.url)
        )
        self._scoped_memory = (
            scoped_memory if scoped_memory is not None else _build_scoped_memory_manager(settings)
        )
        self._graph_store: _GraphStoreLike = (
            graph_store if graph_store is not None else create_graph_store(settings.graph)
        )

    def _known_identifiers(self, ctx: ScopeContext) -> list[str]:
        """Thin instance wrapper around `_known_tenant_identifier_names` -- see that
        function's docstring for the identifier sources and degradation contract."""
        return _known_tenant_identifier_names(ctx, self._graph_store, self._settings)

    async def PromoteLesson(
        self, request: PromoteLessonRequest, context: grpc.aio.ServicerContext
    ) -> PromoteLessonResponse:
        """Scrub+verify `source_content`; a clean result becomes a new pending lesson.

        Any team member may propose -- no elevated scope required, only a
        valid `ScopeContext`. `team_id`, when given, must be one of the
        caller's own teams; `PendingLessonStore.create_pending` enforces that
        (mirrors `stores.vector._validate_team_id`) and this handler maps its
        `ValueError` to `INVALID_ARGUMENT`.

        `extra_identifier_terms` (F2+F3, security review) is gathered from
        `_known_tenant_identifier_names` -- server-authoritative names the
        proposer cannot omit from `source_metadata` -- and forwarded into
        `generalize_and_scrub`'s own `verify_scrubbed` call.
        """
        ctx = await _require_scope(context)
        team_id = request.team_id or None
        source_metadata = dict(request.source_metadata)

        with store_span("lessons.PromoteLesson", has_team=bool(team_id)):
            extra_identifier_terms = await asyncio.to_thread(self._known_identifiers, ctx)
            result = await generalize_and_scrub(
                ctx,
                request.source_content,
                source_metadata=source_metadata,
                extra_identifier_terms=extra_identifier_terms,
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
        """Approve a pending lesson: verify, flip status, THEN write firm-wide.

        Requires `LESSONS_APPROVE_SCOPE` and enforces separation of duties --
        see this module's docstring. Ordering is deliberate and load-bearing
        (F5/F6, security review -- see this module's docstring for the full
        rationale): a failing confidentiality re-verification aborts before
        any write or status change; the atomic PENDING -> APPROVED status
        flip runs before, and gates, the tenant-visibility memory write --
        never the reverse, which is exactly the race two concurrent
        approvers could otherwise hit.
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

            # F6: re-verify with the server-authoritative identifier set --
            # advisory-backstop defense in depth right before this content
            # would go firm-wide. No status change, no write, on failure.
            extra_identifier_terms = await asyncio.to_thread(self._known_identifiers, ctx)
            reverify = verify_scrubbed(
                ctx, record.generalized_text, extra_identifier_terms=extra_identifier_terms
            )
            if not reverify.clean:
                logger.warning(
                    "lessons.ApproveLesson: confidentiality re-verification failed for "
                    "pending_id=%s (%d finding(s)) -- aborting, no status change, no write",
                    record.id,
                    len(reverify.findings),
                )
                await context.abort(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    "confidentiality re-verification failed -- reject this lesson and have "
                    "it re-proposed",
                )
                raise AssertionError("unreachable")  # abort() always raises

            # F5: flip status FIRST, atomically (store.set_status's own
            # `UPDATE ... WHERE status = 'pending'`) -- the memory write
            # below only ever runs if this update actually affected the row.
            try:
                await asyncio.to_thread(
                    self._store.set_status,
                    ctx,
                    request.pending_id,
                    "approved",
                    reviewer=ctx.user_id,
                )
            except LookupError as exc:
                # Lost the race to a concurrent approver (or the row was
                # otherwise no longer `pending`) -- no write happens.
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
                raise AssertionError("unreachable") from exc  # abort() always raises

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
