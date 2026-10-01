"""Tests for `REPLSession`'s `/docs status|clear|cleanup` subcommands (C1).

These three subcommands used to print "not available from the CLI" (F3 left them degraded
-- `KnowledgeService` had no matching RPC). C1 adds `IndexStatus`/`ClearIndex`/`CleanupIndex`
and rewires `_docs_status`/`_docs_clear`/`_docs_cleanup` to call
`self.knowledge_client.index_status()`/`.clear_index()`/`.cleanup_index()` -- this file proves
the degraded message is gone and each subcommand calls the right client method.

`REPLSession.__new__(REPLSession)` (see `tests/test_repl_index_code.py`'s identical pattern)
exercises the handlers without running the full async `__aenter__` startup sequence;
`self.knowledge_client`/`self.docs_fetcher`/`self.project_context` are stubbed directly.

# regression: docs-index-mgmt (C1 -- un-degrade /docs status|clear|cleanup)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from penguincode_cli.client.knowledge_client import (
    IndexStatus,
    KnowledgeClientError,
    LanguageIndexStatus,
    LibraryIndexStatus,
    LibraryRef,
)
from penguincode_cli.config.settings import Settings
from penguincode_cli.core.repl import REPLSession
from penguincode_cli.docs_rag.models import Language, Library, ProjectContext


def _session(*, knowledge_client, project_context=None, docs_fetcher=None) -> REPLSession:  # type: ignore[no-untyped-def]
    session = REPLSession.__new__(REPLSession)
    session.knowledge_client = knowledge_client
    session.project_context = project_context
    session.docs_fetcher = docs_fetcher
    session.settings = Settings()
    return session


def _fake_client() -> AsyncMock:
    client = AsyncMock()
    client.index_status = AsyncMock()
    client.clear_index = AsyncMock()
    client.cleanup_index = AsyncMock()
    return client


class TestDocsStatusCommand:
    async def test_not_available_message_is_gone(self) -> None:
        """The old F3 degraded message must never appear again."""
        client = AsyncMock()
        client.index_status = AsyncMock(
            return_value=IndexStatus(libraries={}, languages={}, total_chunks=0)
        )
        session = _session(knowledge_client=client)

        with patch("penguincode_cli.core.repl.print_info") as mock_info:
            await session._docs_status()

        for call in mock_info.call_args_list:
            assert "no status rpc" not in call.args[0].lower()
            assert "not available" not in call.args[0].lower()

    async def test_calls_index_status_and_reports_nothing_indexed(self) -> None:
        client = AsyncMock()
        client.index_status = AsyncMock(
            return_value=IndexStatus(libraries={}, languages={}, total_chunks=0)
        )
        session = _session(knowledge_client=client)

        with patch("penguincode_cli.core.repl.print_info") as mock_info:
            await session._docs_status()

        client.index_status.assert_awaited_once()
        assert any("nothing indexed" in c.args[0].lower() for c in mock_info.call_args_list)

    async def test_renders_library_and_language_status(self) -> None:
        client = AsyncMock()
        client.index_status = AsyncMock(
            return_value=IndexStatus(
                libraries={
                    "fastapi": LibraryIndexStatus(
                        chunk_count=7,
                        indexed_at="2026-09-25T00:00:00",
                        expires_at="2026-10-02T00:00:00",
                        is_expired=False,
                        language="python",
                    )
                },
                languages={
                    "rust": LanguageIndexStatus(
                        chunk_count=3,
                        indexed_at="2026-09-25T00:00:00",
                        expires_at="2026-10-02T00:00:00",
                        is_expired=True,
                    )
                },
                total_chunks=10,
            )
        )
        session = _session(knowledge_client=client)

        with patch("penguincode_cli.core.repl.console") as mock_console:
            await session._docs_status()

        printed = " ".join(str(c.args[0]) for c in mock_console.print.call_args_list if c.args)
        assert "fastapi" in printed
        assert "rust" in printed
        assert "10" in printed

    async def test_client_error_is_reported_not_raised(self) -> None:
        client = AsyncMock()
        client.index_status = AsyncMock(
            side_effect=KnowledgeClientError("penguincode server unreachable")
        )
        session = _session(knowledge_client=client)

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session._docs_status()

        assert mock_error.called
        assert "penguincode server unreachable" in mock_error.call_args[0][0]

    async def test_not_connected_skips_index_status_section(self) -> None:
        session = _session(knowledge_client=None)

        # Must not raise even with no knowledge_client, docs_fetcher, or project_context.
        await session._docs_status()


class TestDocsClearCommand:
    async def test_not_available_message_is_gone(self) -> None:
        client = _fake_client()
        client.clear_index.return_value = 5
        session = _session(knowledge_client=client)

        with (
            patch("penguincode_cli.core.repl.print_success"),
            patch("penguincode_cli.core.repl.print_info") as mock_info,
        ):
            await session._docs_clear("fastapi")

        for call in mock_info.call_args_list:
            assert "not available from the cli" not in call.args[0].lower()

    async def test_no_library_name_reports_usage(self) -> None:
        session = _session(knowledge_client=_fake_client())

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session._docs_clear("")

        assert mock_error.called
        assert "usage" in mock_error.call_args[0][0].lower()

    async def test_not_connected_reports_error(self) -> None:
        session = _session(knowledge_client=None)

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session._docs_clear("fastapi")

        assert mock_error.called
        assert "not connected" in mock_error.call_args[0][0].lower()

    async def test_calls_clear_index_with_library_name(self) -> None:
        client = _fake_client()
        client.clear_index.return_value = 5
        session = _session(knowledge_client=client)

        with patch("penguincode_cli.core.repl.print_success") as mock_success:
            await session._docs_clear("fastapi")

        client.clear_index.assert_awaited_once_with(library_name="fastapi")
        assert "5" in mock_success.call_args[0][0]

    async def test_nothing_indexed_reports_info_not_error(self) -> None:
        client = _fake_client()
        client.clear_index.return_value = 0
        session = _session(knowledge_client=client)

        with patch("penguincode_cli.core.repl.print_info") as mock_info:
            await session._docs_clear("unknown-lib")

        assert any("nothing indexed" in c.args[0].lower() for c in mock_info.call_args_list)

    async def test_client_error_is_reported_not_raised(self) -> None:
        client = _fake_client()
        client.clear_index.side_effect = KnowledgeClientError("boom")
        session = _session(knowledge_client=client)

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session._docs_clear("fastapi")

        assert mock_error.called
        assert "boom" in mock_error.call_args[0][0]


def _project_context() -> ProjectContext:
    return ProjectContext(
        languages=[Language.PYTHON],
        libraries=[Library(name="fastapi", language=Language.PYTHON, version="1.0")],
    )


class TestDocsCleanupCommand:
    async def test_not_available_message_is_gone(self) -> None:
        client = _fake_client()
        client.cleanup_index.return_value = {}
        docs_fetcher = MagicMock()
        docs_fetcher.cleanup_unused_libraries.return_value = {}
        session = _session(
            knowledge_client=client, project_context=_project_context(), docs_fetcher=docs_fetcher
        )

        with patch("penguincode_cli.core.repl.print_info") as mock_info:
            await session._docs_cleanup()

        for call in mock_info.call_args_list:
            assert "not available yet" not in call.args[0].lower()

    async def test_requires_project_context(self) -> None:
        session = _session(knowledge_client=_fake_client())

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session._docs_cleanup()

        assert mock_error.called
        assert "detect" in mock_error.call_args[0][0].lower()

    async def test_calls_cleanup_index_with_current_project_state(self) -> None:
        client = _fake_client()
        client.cleanup_index.return_value = {"old-lib": 4}
        docs_fetcher = MagicMock()
        docs_fetcher.cleanup_unused_libraries.return_value = {}
        session = _session(
            knowledge_client=client, project_context=_project_context(), docs_fetcher=docs_fetcher
        )

        await session._docs_cleanup()

        client.cleanup_index.assert_awaited_once()
        _, kwargs = client.cleanup_index.call_args
        assert kwargs["current_libraries"] == [
            LibraryRef(name="fastapi", language="python", version="1.0")
        ]
        assert kwargs["current_languages"] == ["python"]

    async def test_reports_nothing_to_clean_up_when_both_empty(self) -> None:
        client = _fake_client()
        client.cleanup_index.return_value = {}
        docs_fetcher = MagicMock()
        docs_fetcher.cleanup_unused_libraries.return_value = {}
        session = _session(
            knowledge_client=client, project_context=_project_context(), docs_fetcher=docs_fetcher
        )

        with patch("penguincode_cli.core.repl.print_info") as mock_info:
            await session._docs_cleanup()

        assert any("nothing to clean up" in c.args[0].lower() for c in mock_info.call_args_list)

    async def test_no_knowledge_client_still_cleans_up_cache_only(self) -> None:
        docs_fetcher = MagicMock()
        docs_fetcher.cleanup_unused_libraries.return_value = {"fastapi": 2}
        session = _session(
            knowledge_client=None, project_context=_project_context(), docs_fetcher=docs_fetcher
        )

        # Must not raise even with no knowledge_client.
        await session._docs_cleanup()

    async def test_client_error_is_reported_not_raised(self) -> None:
        client = _fake_client()
        client.cleanup_index.side_effect = KnowledgeClientError("boom")
        docs_fetcher = MagicMock()
        docs_fetcher.cleanup_unused_libraries.return_value = {}
        session = _session(
            knowledge_client=client, project_context=_project_context(), docs_fetcher=docs_fetcher
        )

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session._docs_cleanup()

        assert mock_error.called
        assert "boom" in mock_error.call_args[0][0]


class TestDocsCommandDispatch:
    @pytest.mark.parametrize(
        ("subcmd", "handler_name", "expected_args"),
        [
            ("status", "_docs_status", ()),
            ("clear fastapi", "_docs_clear", ("fastapi",)),
            ("cleanup", "_docs_cleanup", ()),
        ],
    )
    async def test_subcommand_routes_to_handler(
        self, subcmd: str, handler_name: str, expected_args: tuple[str, ...]
    ) -> None:
        session = _session(knowledge_client=None)
        session.settings.docs_rag.enabled = True

        with patch.object(session, handler_name, new=AsyncMock()) as mock_handler:
            await session.handle_docs_command(subcmd)

        mock_handler.assert_awaited_once_with(*expected_args)
