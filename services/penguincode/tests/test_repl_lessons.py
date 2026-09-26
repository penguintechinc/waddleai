"""Tests for `REPLSession`'s `/lesson` commands (T-L2b): the CLI-facing lessons-learned
promotion review workflow, driven entirely through `self.lessons_client` (a `LessonsClient`).

Mirrors `tests/test_repl_index_code.py`'s style exactly: `REPLSession.__new__(REPLSession)`
exercises each handler without the full async `__aenter__` startup sequence;
`self.lessons_client` is stubbed directly with a mock exposing each RPC method as an
`AsyncMock`.

# regression: lessons-promotion (T-L2b -- REPL `/lesson` commands)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from penguincode_cli.client.lessons_client import (
    LessonFinding,
    LessonsClientError,
    LessonsPermissionDeniedError,
    PendingLessonItem,
    PromoteLessonResult,
)
from penguincode_cli.config.settings import Settings
from penguincode_cli.core.repl import REPLSession


def _session(tmp_path: Path, *, lessons_client: AsyncMock | None) -> REPLSession:
    session = REPLSession.__new__(REPLSession)
    session.lessons_client = lessons_client
    session.project_dir = tmp_path
    session.settings = Settings()
    return session


def _fake_client(**overrides: dict[str, Any]) -> AsyncMock:
    client = AsyncMock()
    client.promote = AsyncMock(**(overrides.pop("promote", None) or {"return_value": None}))
    client.list_pending = AsyncMock(**(overrides.pop("list_pending", None) or {"return_value": []}))
    client.approve = AsyncMock(**(overrides.pop("approve", None) or {"return_value": True}))
    client.reject = AsyncMock(**(overrides.pop("reject", None) or {"return_value": True}))
    return client


class TestNotConnected:
    async def test_not_connected_is_a_clear_noop(self, tmp_path: Path) -> None:
        session = _session(tmp_path, lessons_client=None)

        with patch("penguincode_cli.core.repl.print_info") as mock_info:
            await session.handle_lesson_command("pending")

        assert mock_info.called
        assert "penguincode server" in mock_info.call_args[0][0]


class TestLessonPromote:
    async def test_missing_text_reports_usage_error(self, tmp_path: Path) -> None:
        session = _session(tmp_path, lessons_client=_fake_client())

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session.handle_lesson_command("promote")

        assert mock_error.called
        assert "Usage" in mock_error.call_args[0][0]

    async def test_clean_result_reports_pending_id(self, tmp_path: Path) -> None:
        client = _fake_client(
            promote={
                "return_value": PromoteLessonResult(blocked=False, pending_id="p1", findings=[])
            }
        )
        session = _session(tmp_path, lessons_client=client)

        with patch("penguincode_cli.core.repl.print_success") as mock_success:
            await session.handle_lesson_command("promote always run a canary first")

        client.promote.assert_awaited_once_with(source_content="always run a canary first")
        assert mock_success.called
        assert "p1" in mock_success.call_args[0][0]

    async def test_blocked_result_reports_findings(self, tmp_path: Path) -> None:
        client = _fake_client(
            promote={
                "return_value": PromoteLessonResult(
                    blocked=True,
                    pending_id="",
                    findings=[
                        LessonFinding(
                            kind="client_identifier", detail="residual identifier detected"
                        )
                    ],
                )
            }
        )
        session = _session(tmp_path, lessons_client=client)

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session.handle_lesson_command("promote names AcmeCorp explicitly")

        assert mock_error.called
        assert "blocked" in mock_error.call_args[0][0].lower()

    async def test_server_failure_is_reported_not_raised(self, tmp_path: Path) -> None:
        client = _fake_client(promote={"side_effect": LessonsClientError("server unreachable")})
        session = _session(tmp_path, lessons_client=client)

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session.handle_lesson_command("promote a lesson")

        assert mock_error.called
        assert "server unreachable" in mock_error.call_args[0][0]


class TestLessonPending:
    async def test_empty_queue_reports_info(self, tmp_path: Path) -> None:
        session = _session(tmp_path, lessons_client=_fake_client())

        with patch("penguincode_cli.core.repl.print_info") as mock_info:
            await session.handle_lesson_command("pending")

        assert mock_info.called
        assert "no pending lessons" in mock_info.call_args[0][0].lower()

    async def test_status_forwarded_to_client(self, tmp_path: Path) -> None:
        client = _fake_client()
        session = _session(tmp_path, lessons_client=client)

        await session.handle_lesson_command("pending approved")

        client.list_pending.assert_awaited_once_with(status="approved")

    async def test_nonempty_queue_prints_table(self, tmp_path: Path) -> None:
        client = _fake_client(
            list_pending={
                "return_value": [
                    PendingLessonItem(
                        id="p1",
                        generalized_text="a lesson",
                        status="pending",
                        proposer="user-1",
                        source_team="team-a",
                        created_at="2026-09-25T00:00:00Z",
                        reviewer="",
                        reviewed_at="",
                        findings=[],
                    )
                ]
            }
        )
        session = _session(tmp_path, lessons_client=client)

        with patch("penguincode_cli.core.repl.console.print") as mock_print:
            await session.handle_lesson_command("pending")

        assert mock_print.called


class TestLessonApprove:
    async def test_missing_id_reports_usage_error(self, tmp_path: Path) -> None:
        session = _session(tmp_path, lessons_client=_fake_client())

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session.handle_lesson_command("approve")

        assert mock_error.called
        assert "Usage" in mock_error.call_args[0][0]

    async def test_approved_reports_success(self, tmp_path: Path) -> None:
        client = _fake_client(approve={"return_value": True})
        session = _session(tmp_path, lessons_client=client)

        with patch("penguincode_cli.core.repl.print_success") as mock_success:
            await session.handle_lesson_command("approve p1")

        client.approve.assert_awaited_once_with("p1")
        assert mock_success.called
        assert "approved" in mock_success.call_args[0][0].lower()

    async def test_permission_denied_reports_distinct_message(self, tmp_path: Path) -> None:
        client = _fake_client(
            approve={"side_effect": LessonsPermissionDeniedError("requires lessons:approve")}
        )
        session = _session(tmp_path, lessons_client=client)

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session.handle_lesson_command("approve p1")

        assert mock_error.called
        assert "permission" in mock_error.call_args[0][0].lower()


class TestLessonReject:
    async def test_missing_id_reports_usage_error(self, tmp_path: Path) -> None:
        session = _session(tmp_path, lessons_client=_fake_client())

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session.handle_lesson_command("reject")

        assert mock_error.called
        assert "Usage" in mock_error.call_args[0][0]

    async def test_reject_with_reason_forwards_reason(self, tmp_path: Path) -> None:
        client = _fake_client(reject={"return_value": True})
        session = _session(tmp_path, lessons_client=client)

        with patch("penguincode_cli.core.repl.print_success") as mock_success:
            await session.handle_lesson_command("reject p1 still names the client")

        client.reject.assert_awaited_once_with("p1", reason="still names the client")
        assert mock_success.called

    async def test_permission_denied_reports_distinct_message(self, tmp_path: Path) -> None:
        client = _fake_client(
            reject={"side_effect": LessonsPermissionDeniedError("requires lessons:approve")}
        )
        session = _session(tmp_path, lessons_client=client)

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session.handle_lesson_command("reject p1")

        assert mock_error.called
        assert "permission" in mock_error.call_args[0][0].lower()


class TestLessonCommandDispatch:
    async def test_slash_lesson_routes_to_handler(self, tmp_path: Path) -> None:
        session = _session(tmp_path, lessons_client=None)

        with patch.object(session, "handle_lesson_command") as mock_handler:
            mock_handler.return_value = None
            keep_going = await session.handle_command("/lesson pending")

        assert keep_going is True
        mock_handler.assert_called_once_with("pending")

    async def test_unknown_subcommand_reports_error(self, tmp_path: Path) -> None:
        session = _session(tmp_path, lessons_client=_fake_client())

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session.handle_lesson_command("bogus")

        assert mock_error.called
        assert "Unknown" in mock_error.call_args[0][0]
