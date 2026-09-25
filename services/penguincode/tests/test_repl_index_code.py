"""Tests for `REPLSession.handle_index_code` (F3: thin gRPC client).

`/index-code` used to drive `graphs.code.index_code` (T11) locally; it now drives the
server's `IndexCode` RPC via `self.knowledge_client` -- `graphs.code.index_code` runs
entirely server-side now. `REPLSession.__new__(REPLSession)` (see
`tests/test_docs_rag.py`'s `TestREPLLanguageDetection`) exercises the handler without
running the full async `__aenter__` startup sequence; `self.knowledge_client` is stubbed
directly with a mock exposing `index_code` as an `AsyncMock`.

# regression: penguincode-knowledge-platform (F3 -- thin gRPC client + CLI conversion)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from penguincode_cli.client.knowledge_client import KnowledgeClientError
from penguincode_cli.config.settings import Settings
from penguincode_cli.core.repl import REPLSession


def _session(tmp_path: Path, *, knowledge_client) -> REPLSession:  # type: ignore[no-untyped-def]
    session = REPLSession.__new__(REPLSession)
    session.knowledge_client = knowledge_client
    session.project_dir = tmp_path
    session.settings = Settings()
    return session


def _fake_client(**overrides):  # type: ignore[no-untyped-def]
    client = AsyncMock()
    client.index_code = AsyncMock(**overrides)
    return client


class TestIndexCodeCommand:
    async def test_not_connected_is_a_clear_noop(self, tmp_path: Path) -> None:
        """No `KnowledgeClient` (server not connected) degrades gracefully -- no crash."""
        session = _session(tmp_path, knowledge_client=None)

        with patch("penguincode_cli.core.repl.print_info") as mock_info:
            await session.handle_index_code(str(tmp_path))

        assert mock_info.called
        assert "penguincode server" in mock_info.call_args[0][0]

    async def test_missing_path_reports_error(self, tmp_path: Path) -> None:
        session = _session(tmp_path, knowledge_client=_fake_client())
        missing = tmp_path / "does-not-exist"

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session.handle_index_code(str(missing))

        assert mock_error.called
        assert "not found" in mock_error.call_args[0][0].lower()

    async def test_file_path_rejected_as_not_a_directory(self, tmp_path: Path) -> None:
        session = _session(tmp_path, knowledge_client=_fake_client())
        a_file = tmp_path / "file.py"
        a_file.write_text("x = 1\n")

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session.handle_index_code(str(a_file))

        assert mock_error.called
        assert "not a directory" in mock_error.call_args[0][0].lower()

    async def test_calls_index_code_rpc_and_reports_node_edge_counts(self, tmp_path: Path) -> None:
        client = _fake_client(return_value=(1, 1))
        session = _session(tmp_path, knowledge_client=client)

        with patch("penguincode_cli.core.repl.print_success") as mock_success:
            await session.handle_index_code(str(tmp_path))

        client.index_code.assert_awaited_once()
        _, kwargs = client.index_code.call_args
        assert Path(kwargs["root_path"]) == tmp_path.resolve()
        mock_success.assert_called_once()
        assert "1 node" in mock_success.call_args[0][0]
        assert "1 edge" in mock_success.call_args[0][0]

    async def test_default_path_is_project_dir_when_no_arg_given(self, tmp_path: Path) -> None:
        client = _fake_client(return_value=(0, 0))
        session = _session(tmp_path, knowledge_client=client)

        await session.handle_index_code("")

        client.index_code.assert_awaited_once()
        _, kwargs = client.index_code.call_args
        assert Path(kwargs["root_path"]) == tmp_path.resolve()

    async def test_flag_off_reports_disabled_not_error(self, tmp_path: Path) -> None:
        """`IndexCode` RPC returns `None` when `penguincode.code-graph` is off."""
        client = _fake_client(return_value=None)
        session = _session(tmp_path, knowledge_client=client)

        with patch("penguincode_cli.core.repl.print_info") as mock_info:
            await session.handle_index_code(str(tmp_path))

        assert mock_info.called
        assert "disabled" in mock_info.call_args[0][0].lower()

    async def test_server_failure_is_reported_not_raised(self, tmp_path: Path) -> None:
        """A `KnowledgeClientError` (server unreachable/auth failure) surfaces as an error
        message, never crashes the REPL."""
        client = _fake_client(side_effect=KnowledgeClientError("penguincode server unreachable"))
        session = _session(tmp_path, knowledge_client=client)

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session.handle_index_code(str(tmp_path))

        assert mock_error.called
        assert "penguincode server unreachable" in mock_error.call_args[0][0]


class TestIndexCodeCommandDispatch:
    async def test_slash_index_code_routes_to_handler(self, tmp_path: Path) -> None:
        session = _session(tmp_path, knowledge_client=None)

        with patch.object(session, "handle_index_code") as mock_handler:
            mock_handler.return_value = None
            keep_going = await session.handle_command("/index-code some/path")

        assert keep_going is True
        mock_handler.assert_called_once_with("some/path")
