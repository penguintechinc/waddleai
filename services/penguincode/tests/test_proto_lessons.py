"""LessonsService gRPC contract (T-L2a): import + message-construction smoke test.

T-L2a is contract + storage only -- no server handlers, no client (T-L2b).
This test proves the generated stub/servicer/messages are importable from
``penguincode_cli.proto`` and that every request message's ``api_version``
field is field number 1 (the scope-model invariant knowledge.proto's own
contract test enforces identically -- see ``test_proto_knowledge.py``). It
does not exercise any RPC over a real channel -- that belongs to T-L2b.

# regression: lessons-promotion (T-L2a -- lessons proto contract + storage)
"""

from __future__ import annotations

import pytest

from penguincode_cli.proto import (
    ApproveLessonRequest,
    ApproveLessonResponse,
    Finding,
    ListPendingLessonsRequest,
    ListPendingLessonsResponse,
    PendingLesson,
    PromoteLessonRequest,
    PromoteLessonResponse,
    RejectLessonRequest,
    RejectLessonResponse,
    add_LessonsServiceServicer_to_server,
)
from penguincode_cli.proto.lessons.v1 import lessons_pb2_grpc

#: Every LessonsService request message, per the scope-model contract:
#: `api_version` is always field number 1 -- mirrors knowledge.proto's
#: identical invariant.
_REQUEST_MESSAGE_TYPES = (
    PromoteLessonRequest,
    ListPendingLessonsRequest,
    ApproveLessonRequest,
    RejectLessonRequest,
)

#: Field names that would leak client-supplied identity into the scope
#: model -- ScopeContext is derived server-side from the validated JWT only
#: (see lessons.proto's module docstring); `team_id` (on
#: `PromoteLessonRequest`) is the sole, documented exception (naming one of
#: the caller's own teams).
_FORBIDDEN_SCOPE_FIELDS = frozenset({"tenant_id", "org_id", "user_id", "owner_user_id"})


@pytest.mark.parametrize("message_type", _REQUEST_MESSAGE_TYPES)
def test_request_api_version_is_field_one(message_type: type) -> None:
    """Every request message's `api_version` is field number 1."""
    field = message_type.DESCRIPTOR.fields_by_name["api_version"]
    assert field.number == 1
    assert field.type == field.TYPE_STRING


@pytest.mark.parametrize("message_type", _REQUEST_MESSAGE_TYPES)
def test_request_never_carries_forbidden_scope_fields(message_type: type) -> None:
    """No request message carries a client-supplied tenant/org/user identity."""
    field_names = {f.name for f in message_type.DESCRIPTOR.fields}
    leaked = field_names & _FORBIDDEN_SCOPE_FIELDS
    assert not leaked, f"{message_type.__name__} leaks scope field(s): {leaked}"


def test_lessons_service_servicer_has_all_four_rpcs() -> None:
    """`LessonsServiceServicer` wires exactly the four RPCs T-L2a defines.

    A stub's RPC attributes are only set on `channel.unary_unary(...)` calls
    inside `__init__` (a real `grpc.Channel` is T-L2b's concern, not
    T-L2a's) -- verified here via the servicer instead, which declares each
    RPC as a plain method, introspectable with no channel at all.
    """
    expected = {"PromoteLesson", "ListPendingLessons", "ApproveLesson", "RejectLesson"}
    methods = {
        name for name in vars(lessons_pb2_grpc.LessonsServiceServicer) if not name.startswith("_")
    }
    assert methods == expected
    assert lessons_pb2_grpc.LessonsServiceStub.__init__.__code__.co_argcount == 2  # (self, channel)


def test_add_lessons_service_servicer_to_server_is_registered() -> None:
    """`add_LessonsServiceServicer_to_server` is exported and callable."""
    assert callable(add_LessonsServiceServicer_to_server)


def test_promote_lesson_request_shape() -> None:
    """`PromoteLessonRequest` carries source content + the caller's own team_id."""
    request = PromoteLessonRequest(
        api_version="v1",
        source_content="Always confirm the client's staging environment before...",
        team_id="team-1",
    )
    assert request.source_content.startswith("Always confirm")
    assert request.team_id == "team-1"


def test_promote_lesson_response_accepted_shape() -> None:
    """An accepted promotion: `blocked=False` + a `pending_id`, no findings."""
    response = PromoteLessonResponse(blocked=False, pending_id="pending-1")
    assert response.blocked is False
    assert response.pending_id == "pending-1"
    assert list(response.findings) == []


def test_promote_lesson_response_blocked_shape() -> None:
    """A blocked promotion: `blocked=True` + findings, no `pending_id`."""
    response = PromoteLessonResponse(
        blocked=True,
        findings=[Finding(kind="email", detail="residual email address detected")],
    )
    assert response.blocked is True
    assert response.pending_id == ""
    assert len(response.findings) == 1
    assert response.findings[0].kind == "email"


def test_list_pending_lessons_roundtrip_shape() -> None:
    """`ListPendingLessonsResponse` carries zero or more `PendingLesson` rows."""
    request = ListPendingLessonsRequest(api_version="v1", status="pending")
    lesson = PendingLesson(
        id="pending-1",
        generalized_text="A generalized, client-agnostic lesson.",
        status="pending",
        proposer="user-1",
        source_team="team-1",
        created_at="2026-09-25T00:00:00Z",
    )
    response = ListPendingLessonsResponse(pending_lessons=[lesson])

    assert request.status == "pending"
    assert len(response.pending_lessons) == 1
    assert response.pending_lessons[0].id == "pending-1"
    assert response.pending_lessons[0].reviewer == ""


def test_approve_and_reject_lesson_shapes() -> None:
    """`ApproveLesson`/`RejectLesson` request+response mirror the store's set_status contract."""
    approve_request = ApproveLessonRequest(api_version="v1", pending_id="pending-1")
    approve_response = ApproveLessonResponse(approved=True)
    assert approve_request.pending_id == "pending-1"
    assert approve_response.approved is True

    reject_request = RejectLessonRequest(
        api_version="v1", pending_id="pending-1", reason="still names the client"
    )
    reject_response = RejectLessonResponse(rejected=True)
    assert reject_request.reason == "still names the client"
    assert reject_response.rejected is True
