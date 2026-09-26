"""Tests for `penguincode_cli.client.lessons_client` -- the thin gRPC `LessonsService`
client (T-L2b).

TDD: written before `penguincode_cli/client/lessons_client.py` existed; must fail with an
ImportError/ModuleNotFoundError until implemented. Mirrors `tests/test_knowledge_client.py`'s
style exactly -- `LessonsServiceStub` is monkeypatched to a fake with `AsyncMock` RPC methods,
so no real gRPC channel/server is ever involved.

Proves, per method:

- The right RPC is invoked with `api_version` set and the WaddleAI auth metadata attached.
- The proto response is adapted into the plain dataclass/bool shape callers expect.
- `grpc.aio.AioRpcError` translates correctly -- crucially, `PERMISSION_DENIED` maps to the
  DISTINCT `LessonsPermissionDeniedError` (not `LessonsAuthError`, unlike `KnowledgeClient`'s
  single-bucket mapping) -- see the client module's docstring for why that distinction
  matters here specifically (a valid token lacking the approve scope).

# regression: lessons-promotion (T-L2b -- thin gRPC client)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import grpc
import pytest
from grpc.aio import Metadata

from penguincode_cli.client.lessons_client import (
    LessonsAuthError,
    LessonsClient,
    LessonsClientError,
    LessonsPermissionDeniedError,
    LessonsServerUnavailableError,
)
from penguincode_cli.client.waddleai_auth import WaddleAIAuthError
from penguincode_cli.config.settings import ServerConfig
from penguincode_cli.proto import (
    ApproveLessonResponse,
    Finding,
    ListPendingLessonsResponse,
    PendingLesson,
    PromoteLessonResponse,
    RejectLessonResponse,
)

_AUTH_METADATA = [("authorization", "Bearer test-jwt")]


class _FakeStub:
    """Stand-in for `LessonsServiceStub` -- one `AsyncMock` per RPC."""

    def __init__(self) -> None:
        self.PromoteLesson = AsyncMock()
        self.ListPendingLessons = AsyncMock()
        self.ApproveLesson = AsyncMock()
        self.RejectLesson = AsyncMock()


def _rpc_error(code: grpc.StatusCode, details: str = "boom") -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(code, Metadata(), Metadata(), details=details)


def _client(
    monkeypatch: pytest.MonkeyPatch, stub: _FakeStub, *, token_ok: bool = True
) -> LessonsClient:
    monkeypatch.setattr(
        "penguincode_cli.client.lessons_client.LessonsServiceStub", lambda channel: stub
    )
    token_provider = AsyncMock()
    if token_ok:
        token_provider.get_auth_metadata = AsyncMock(return_value=_AUTH_METADATA)
    else:
        token_provider.get_auth_metadata = AsyncMock(
            side_effect=WaddleAIAuthError("no credentials configured")
        )
    server_config = ServerConfig(host="pc-server.internal", port=50051)
    # `channel=object()` short-circuits real `grpc.aio.insecure_channel()` creation --
    # `_ensure_stub` only checks `is None`, and `LessonsServiceStub` itself is patched above.
    return LessonsClient(server_config, token_provider=token_provider, channel=object())


class TestPromote:
    async def test_clean_sends_request_and_adapts_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.PromoteLesson.return_value = PromoteLessonResponse(
            blocked=False, pending_id="p1", findings=[]
        )
        client = _client(monkeypatch, stub)

        result = await client.promote(
            source_content="a client lesson", team_id="team-a", source_metadata={"org_name": "Acme"}
        )

        assert result.blocked is False
        assert result.pending_id == "p1"
        assert result.findings == []
        request, kwargs = stub.PromoteLesson.call_args.args[0], stub.PromoteLesson.call_args.kwargs
        assert request.api_version == "v1"
        assert request.source_content == "a client lesson"
        assert request.team_id == "team-a"
        assert dict(request.source_metadata) == {"org_name": "Acme"}
        assert kwargs["metadata"] == _AUTH_METADATA

    async def test_blocked_adapts_findings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.PromoteLesson.return_value = PromoteLessonResponse(
            blocked=True,
            pending_id="",
            findings=[Finding(kind="client_identifier", detail="residual identifier detected")],
        )
        client = _client(monkeypatch, stub)

        result = await client.promote(source_content="names the client explicitly")

        assert result.blocked is True
        assert result.pending_id == ""
        assert len(result.findings) == 1
        assert result.findings[0].kind == "client_identifier"
        assert result.findings[0].detail == "residual identifier detected"

    async def test_default_team_id_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.PromoteLesson.return_value = PromoteLessonResponse(blocked=False, pending_id="p1")
        client = _client(monkeypatch, stub)

        await client.promote(source_content="x")

        request = stub.PromoteLesson.call_args.args[0]
        assert request.team_id == ""


class TestListPending:
    async def test_adapts_every_pending_lesson_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.ListPendingLessons.return_value = ListPendingLessonsResponse(
            pending_lessons=[
                PendingLesson(
                    id="p1",
                    generalized_text="a lesson",
                    status="pending",
                    findings=[Finding(kind="email", detail="residual email address detected")],
                    proposer="user-1",
                    source_team="team-a",
                    created_at="2026-09-25T00:00:00Z",
                    reviewer="",
                    reviewed_at="",
                )
            ]
        )
        client = _client(monkeypatch, stub)

        results = await client.list_pending(status="pending")

        assert len(results) == 1
        item = results[0]
        assert item.id == "p1"
        assert item.generalized_text == "a lesson"
        assert item.status == "pending"
        assert item.findings[0].kind == "email"
        assert item.proposer == "user-1"
        assert item.source_team == "team-a"
        assert item.reviewer == ""
        request, kwargs = (
            stub.ListPendingLessons.call_args.args[0],
            stub.ListPendingLessons.call_args.kwargs,
        )
        assert request.api_version == "v1"
        assert request.status == "pending"
        assert kwargs["metadata"] == _AUTH_METADATA

    async def test_default_status_is_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.ListPendingLessons.return_value = ListPendingLessonsResponse(pending_lessons=[])
        client = _client(monkeypatch, stub)

        results = await client.list_pending()

        assert results == []
        request = stub.ListPendingLessons.call_args.args[0]
        assert request.status == ""


class TestApprove:
    async def test_approved_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.ApproveLesson.return_value = ApproveLessonResponse(approved=True)
        client = _client(monkeypatch, stub)

        result = await client.approve("p1")

        assert result is True
        request, kwargs = stub.ApproveLesson.call_args.args[0], stub.ApproveLesson.call_args.kwargs
        assert request.api_version == "v1"
        assert request.pending_id == "p1"
        assert kwargs["metadata"] == _AUTH_METADATA

    async def test_permission_denied_raises_distinct_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.ApproveLesson.side_effect = _rpc_error(
            grpc.StatusCode.PERMISSION_DENIED, "requires the 'lessons:approve' scope"
        )
        client = _client(monkeypatch, stub)

        with pytest.raises(LessonsPermissionDeniedError, match="lessons:approve"):
            await client.approve("p1")


class TestReject:
    async def test_rejected_true_with_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.RejectLesson.return_value = RejectLessonResponse(rejected=True)
        client = _client(monkeypatch, stub)

        result = await client.reject("p1", reason="still identifying")

        assert result is True
        request = stub.RejectLesson.call_args.args[0]
        assert request.pending_id == "p1"
        assert request.reason == "still identifying"

    async def test_default_reason_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.RejectLesson.return_value = RejectLessonResponse(rejected=True)
        client = _client(monkeypatch, stub)

        await client.reject("p1")

        request = stub.RejectLesson.call_args.args[0]
        assert request.reason == ""


class TestErrorTranslation:
    async def test_unavailable_raises_server_unavailable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.ListPendingLessons.side_effect = _rpc_error(
            grpc.StatusCode.UNAVAILABLE, "connection refused"
        )
        client = _client(monkeypatch, stub)

        with pytest.raises(LessonsServerUnavailableError, match="unreachable"):
            await client.list_pending()

    async def test_deadline_exceeded_raises_server_unavailable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.ListPendingLessons.side_effect = _rpc_error(grpc.StatusCode.DEADLINE_EXCEEDED)
        client = _client(monkeypatch, stub)

        with pytest.raises(LessonsServerUnavailableError):
            await client.list_pending()

    async def test_unauthenticated_raises_auth_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.ListPendingLessons.side_effect = _rpc_error(
            grpc.StatusCode.UNAUTHENTICATED, "bad token"
        )
        client = _client(monkeypatch, stub)

        with pytest.raises(LessonsAuthError, match="bad token"):
            await client.list_pending()

    async def test_permission_denied_raises_permission_denied_not_auth_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one deliberate divergence from `KnowledgeClient`'s error mapping -- see the
        client module's docstring."""
        stub = _FakeStub()
        stub.ListPendingLessons.side_effect = _rpc_error(grpc.StatusCode.PERMISSION_DENIED)
        client = _client(monkeypatch, stub)

        with pytest.raises(LessonsPermissionDeniedError):
            await client.list_pending()
        with pytest.raises(LessonsClientError):
            # Still a `LessonsClientError` subclass -- callers catching the base still work.
            await client.list_pending()

    async def test_other_status_raises_generic_client_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.ListPendingLessons.side_effect = _rpc_error(grpc.StatusCode.INTERNAL, "server bug")
        client = _client(monkeypatch, stub)

        with pytest.raises(LessonsClientError, match="INTERNAL"):
            await client.list_pending()

    async def test_token_acquisition_failure_raises_auth_error_without_calling_rpc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        client = _client(monkeypatch, stub, token_ok=False)

        with pytest.raises(LessonsAuthError, match="could not acquire a WaddleAI token"):
            await client.list_pending()

        stub.ListPendingLessons.assert_not_called()


class TestClose:
    async def test_close_closes_channel_and_resets_stub(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        monkeypatch.setattr(
            "penguincode_cli.client.lessons_client.LessonsServiceStub", lambda channel: stub
        )
        channel = AsyncMock()
        client = LessonsClient(
            ServerConfig(host="h", port=1), token_provider=AsyncMock(), channel=channel
        )
        client._ensure_stub()

        await client.close()

        channel.close.assert_awaited_once()
        assert client._channel is None
        assert client._stub is None

    async def test_close_without_ever_opening_is_a_noop(self) -> None:
        client = LessonsClient(ServerConfig(host="h", port=1), token_provider=AsyncMock())
        await client.close()  # must not raise
