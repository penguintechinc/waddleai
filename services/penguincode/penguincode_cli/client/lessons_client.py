"""Thin gRPC client for `LessonsService` (T-L2b): the CLI's only path to the server-side
lessons-learned promotion review workflow.

Mirrors `client/knowledge_client.py`'s shape exactly: one Python-native method per RPC,
identity attached exclusively via `WaddleAITokenProvider.get_auth_metadata()` (never a
client-supplied tenant/org/team/user, per `lessons.proto`'s scope-model contract), every
response decoded into a plain, slotted dataclass rather than the raw proto message, and every
gRPC failure translated into one of `LessonsClientError`'s subclasses before reaching a
caller -- a raw `grpc.aio.AioRpcError` never reaches the REPL (`core/repl.py`).

**`LessonsPermissionDeniedError` is distinct from `LessonsAuthError`.** `ApproveLesson`/
`RejectLesson` reject a caller lacking the elevated approve scope with `PERMISSION_DENIED`
even though it *has* a perfectly valid WaddleAI JWT -- that is a materially different,
user-actionable condition ("you don't hold the approval scope") from `UNAUTHENTICATED`
("your token itself is missing/invalid"), so the REPL can report each with a distinct,
useful message (see `core/repl.py`'s `/lesson` command handlers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import grpc
from google.protobuf import struct_pb2

from penguincode_cli.client.waddleai_auth import WaddleAIAuthError, WaddleAITokenProvider
from penguincode_cli.config.settings import ServerConfig
from penguincode_cli.proto import (
    ApproveLessonRequest,
    ApproveLessonResponse,
    LessonsServiceStub,
    ListPendingLessonsRequest,
    ListPendingLessonsResponse,
    PendingLesson,
    PromoteLessonRequest,
    PromoteLessonResponse,
    RejectLessonRequest,
    RejectLessonResponse,
)

#: `LessonsService`'s only versioning field today -- stamped on every request per
#: `lessons.proto`; bump when a breaking wire change is introduced.
_API_VERSION = "v1"


class LessonsClientError(Exception):
    """Base error for every `LessonsClient` failure.

    `core/repl.py` catches this -- never a raw `grpc.aio.AioRpcError` or an unguarded
    traceback reaches the user.
    """


class LessonsServerUnavailableError(LessonsClientError):
    """The penguincode server could not be reached (`UNAVAILABLE`/`DEADLINE_EXCEEDED`)."""


class LessonsAuthError(LessonsClientError):
    """The call was rejected as `UNAUTHENTICATED`, or no WaddleAI token could be acquired at
    all (see `WaddleAIAuthError`) -- the caller's *token* is missing/invalid, distinct from
    `LessonsPermissionDeniedError` (a valid token lacking a required scope).
    """


class LessonsPermissionDeniedError(LessonsClientError):
    """The call was rejected as `PERMISSION_DENIED` -- a valid WaddleAI JWT that lacks the
    elevated approve scope (`server.services.lessons.LESSONS_APPROVE_SCOPE`)
    `ApproveLesson`/`RejectLesson` require, or that fails the separation-of-duties check.
    """


# ==================== Client-side response shapes ====================
# Plain, slotted dataclasses -- never the raw proto message, so a caller never needs to
# import anything from `penguincode_cli.proto` to consume a `LessonsClient` response.


@dataclass(slots=True, frozen=True)
class LessonFinding:
    """One residual confidentiality issue -- decoded from the proto `Finding`."""

    kind: str
    detail: str


@dataclass(slots=True, frozen=True)
class PendingLessonItem:
    """One pending-review lesson -- decoded from the proto `PendingLesson`."""

    id: str
    generalized_text: str
    status: str
    proposer: str
    source_team: str
    created_at: str
    reviewer: str
    reviewed_at: str
    findings: list[LessonFinding] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class PromoteLessonResult:
    """Outcome of one `PromoteLesson` call -- decoded from the proto `PromoteLessonResponse`.

    `blocked=True` means the verifier rejected the content outright: `pending_id` is empty
    and `findings` explains why. `blocked=False` means a new pending row was created;
    `pending_id` names it and `findings` is always empty.
    """

    blocked: bool
    pending_id: str
    findings: list[LessonFinding] = field(default_factory=list)


def _struct(data: dict[str, Any] | None) -> struct_pb2.Struct:
    """Build a `google.protobuf.Struct` from a plain dict, defaulting to empty."""
    proto_struct = struct_pb2.Struct()
    if data:
        proto_struct.update(data)
    return proto_struct


def _adapt_findings(findings: Any) -> list[LessonFinding]:
    """Decode a repeated proto `Finding` field into plain `LessonFinding`s."""
    return [LessonFinding(kind=f.kind, detail=f.detail) for f in findings]


def _adapt_pending_lesson(item: PendingLesson) -> PendingLessonItem:
    """Decode one proto `PendingLesson` into a `PendingLessonItem`."""
    return PendingLessonItem(
        id=item.id,
        generalized_text=item.generalized_text,
        status=item.status,
        proposer=item.proposer,
        source_team=item.source_team,
        created_at=item.created_at,
        reviewer=item.reviewer,
        reviewed_at=item.reviewed_at,
        findings=_adapt_findings(item.findings),
    )


class LessonsClient:
    """Thin gRPC client for all four `LessonsService` RPCs.

    A single instance is meant to be shared for a CLI session's lifetime, mirroring
    `KnowledgeClient` -- the underlying `grpc.aio.Channel` is created lazily on first use and
    reused across calls.
    """

    def __init__(
        self,
        server_config: ServerConfig,
        token_provider: WaddleAITokenProvider | None = None,
        *,
        channel: grpc.aio.Channel | None = None,
    ) -> None:
        """Bind this client to *server_config* (host/port/tls, see `config.settings.ServerConfig`).

        *token_provider*/*channel* are test seams -- production code leaves both at their
        defaults (a real `WaddleAITokenProvider` and a lazily created `grpc.aio.Channel`).
        """
        self._server_config = server_config
        self._token_provider = token_provider or WaddleAITokenProvider()
        self._channel = channel
        self._stub: LessonsServiceStub | None = None

    def _ensure_stub(self) -> LessonsServiceStub:
        """Lazily create the channel/stub on first use; reused for every subsequent call."""
        if self._stub is None:
            address = f"{self._server_config.host}:{self._server_config.port}"
            if self._channel is None:
                self._channel = (
                    grpc.aio.secure_channel(address, grpc.ssl_channel_credentials())
                    if self._server_config.tls_enabled
                    else grpc.aio.insecure_channel(address)
                )
            # grpc's generated stub classes ship no type stubs -- same known limitation
            # `client/knowledge_client.py`'s own stub construction already carries.
            self._stub = LessonsServiceStub(self._channel)  # type: ignore[no-untyped-call]
        return self._stub

    async def close(self) -> None:
        """Close the underlying channel, if one was ever opened."""
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    async def _auth_metadata(self) -> list[tuple[str, str]]:
        """Acquire the WaddleAI bearer-token metadata for one call.

        Never logs the token itself (security.md Token & Secret Hygiene) -- only the fact
        that acquisition failed.
        """
        try:
            return await self._token_provider.get_auth_metadata()
        except WaddleAIAuthError as exc:
            raise LessonsAuthError(f"could not acquire a WaddleAI token: {exc}") from exc

    async def _call(self, rpc: Any, request: Any) -> Any:
        """Attach auth metadata, invoke *rpc*, and translate any failure into a
        `LessonsClientError` subclass -- the single choke point every public method routes
        through so no raw `grpc.aio.AioRpcError`/traceback ever reaches a caller.
        """
        metadata = await self._auth_metadata()
        try:
            return await rpc(request, metadata=metadata)
        except grpc.aio.AioRpcError as exc:
            code = exc.code()
            if code == grpc.StatusCode.PERMISSION_DENIED:
                raise LessonsPermissionDeniedError(
                    f"server denied the request: {exc.details()}"
                ) from exc
            if code == grpc.StatusCode.UNAUTHENTICATED:
                raise LessonsAuthError(f"server rejected the request: {exc.details()}") from exc
            if code in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                address = f"{self._server_config.host}:{self._server_config.port}"
                raise LessonsServerUnavailableError(
                    f"penguincode server unreachable at {address}: {exc.details()}"
                ) from exc
            raise LessonsClientError(f"server error ({code.name}): {exc.details()}") from exc

    async def promote(
        self,
        *,
        source_content: str,
        team_id: str = "",
        source_metadata: dict[str, Any] | None = None,
    ) -> PromoteLessonResult:
        """Propose `source_content` as a firm-wide lesson. Runs the server's scrub+verify
        pipeline; a clean result creates a new pending row (mirrors
        `server.services.lessons.LessonsServiceImpl.PromoteLesson`).
        """
        stub = self._ensure_stub()
        request = PromoteLessonRequest(
            api_version=_API_VERSION,
            source_content=source_content,
            team_id=team_id,
            source_metadata=_struct(source_metadata),
        )
        response: PromoteLessonResponse = await self._call(stub.PromoteLesson, request)
        return PromoteLessonResult(
            blocked=response.blocked,
            pending_id=response.pending_id,
            findings=_adapt_findings(response.findings),
        )

    async def list_pending(self, *, status: str = "") -> list[PendingLessonItem]:
        """List the caller's tenant's review queue. Empty `status` defaults to "pending"
        server-side (never team-scoped -- review is a tenant-wide administrative action).
        """
        stub = self._ensure_stub()
        request = ListPendingLessonsRequest(api_version=_API_VERSION, status=status)
        response: ListPendingLessonsResponse = await self._call(stub.ListPendingLessons, request)
        return [_adapt_pending_lesson(item) for item in response.pending_lessons]

    async def approve(self, pending_id: str) -> bool:
        """Approve a pending lesson: writes it firm-wide (tenant visibility) server-side and
        marks it approved. Raises `LessonsPermissionDeniedError` if the caller lacks the
        elevated approve scope or fails the separation-of-duties check (proposed it itself).
        """
        stub = self._ensure_stub()
        request = ApproveLessonRequest(api_version=_API_VERSION, pending_id=pending_id)
        response: ApproveLessonResponse = await self._call(stub.ApproveLesson, request)
        return bool(response.approved)

    async def reject(self, pending_id: str, *, reason: str = "") -> bool:
        """Reject a pending lesson with a human-readable `reason`. Same authz requirement as
        `approve`.
        """
        stub = self._ensure_stub()
        request = RejectLessonRequest(
            api_version=_API_VERSION, pending_id=pending_id, reason=reason
        )
        response: RejectLessonResponse = await self._call(stub.RejectLesson, request)
        return bool(response.rejected)


__all__ = [
    "LessonFinding",
    "LessonsAuthError",
    "LessonsClient",
    "LessonsClientError",
    "LessonsPermissionDeniedError",
    "LessonsServerUnavailableError",
    "PendingLessonItem",
    "PromoteLessonResult",
]
