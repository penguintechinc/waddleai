"""Tests for `REPLSession.handle_index_code` (T-wire: `/index-code` command).

Drives `graphs.code.index_code` (T11) -- penguincode's first code-repo
indexer entrypoint. Uses `REPLSession.__new__(REPLSession)` (see
`tests/test_docs_rag.py`'s `TestREPLLanguageDetection`) to exercise the
handler without running the full async `__aenter__` startup sequence.

# regression: penguincode-knowledge-platform (T-wire -- code-graph ingestion entrypoint)
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import Settings
from penguincode_cli.core.repl import REPLSession
from penguincode_cli.stores.graph import GraphEdge, GraphNode


def _ctx(**overrides: Any) -> ScopeContext:
    defaults: dict[str, Any] = {
        "tenant_id": str(uuid.uuid4()),
        "org_id": None,
        "team_ids": (),
        "user_id": str(uuid.uuid4()),
        "scopes": (),
    }
    defaults.update(overrides)
    return ScopeContext(**defaults)


def _session(tmp_path: Path, *, scope_ctx: ScopeContext | None) -> REPLSession:
    session = REPLSession.__new__(REPLSession)
    session.scope_ctx = scope_ctx
    session.project_dir = tmp_path
    session.settings = Settings()
    return session


class TestIndexCodeCommand:
    async def test_no_scope_ctx_is_a_clear_noop(self, tmp_path: Path) -> None:
        """No ScopeContext (CLI has no auth flow yet) degrades gracefully -- no crash."""
        session = _session(tmp_path, scope_ctx=None)

        with patch("penguincode_cli.core.repl.print_info") as mock_info:
            await session.handle_index_code(str(tmp_path))

        assert mock_info.called
        assert "ScopeContext" in mock_info.call_args[0][0]

    async def test_missing_path_reports_error(self, tmp_path: Path) -> None:
        session = _session(tmp_path, scope_ctx=_ctx())
        missing = tmp_path / "does-not-exist"

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session.handle_index_code(str(missing))

        assert mock_error.called
        assert "not found" in mock_error.call_args[0][0].lower()

    async def test_file_path_rejected_as_not_a_directory(self, tmp_path: Path) -> None:
        session = _session(tmp_path, scope_ctx=_ctx())
        a_file = tmp_path / "file.py"
        a_file.write_text("x = 1\n")

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session.handle_index_code(str(a_file))

        assert mock_error.called
        assert "not a directory" in mock_error.call_args[0][0].lower()

    async def test_calls_index_code_and_reports_node_edge_counts(self, tmp_path: Path) -> None:
        from penguincode_cli.graphs.code import ExtractionResult

        session = _session(tmp_path, scope_ctx=_ctx())
        calls: list[dict[str, Any]] = []

        def _fake_index_code(ctx, root_path, *, config=None, **_kw):  # type: ignore[no-untyped-def]
            calls.append({"ctx": ctx, "root_path": root_path, "config": config})
            return ExtractionResult(
                nodes=[GraphNode(node_type="file", key="a.py", props={})],
                edges=[
                    GraphEdge(
                        src_type="file",
                        src_key="a.py",
                        dst_type="function",
                        dst_key="a.py::foo",
                        rel_type="defines",
                    )
                ],
            )

        with (
            patch("penguincode_cli.graphs.code.index_code", _fake_index_code),
            patch("penguincode_cli.core.repl.print_success") as mock_success,
        ):
            await session.handle_index_code(str(tmp_path))

        assert len(calls) == 1
        assert calls[0]["ctx"] is session.scope_ctx
        assert Path(calls[0]["root_path"]) == tmp_path.resolve()
        mock_success.assert_called_once()
        assert "1 node" in mock_success.call_args[0][0]
        assert "1 edge" in mock_success.call_args[0][0]

    async def test_default_path_is_project_dir_when_no_arg_given(self, tmp_path: Path) -> None:
        from penguincode_cli.graphs.code import ExtractionResult

        session = _session(tmp_path, scope_ctx=_ctx())
        calls: list[Any] = []

        def _fake_index_code(ctx, root_path, *, config=None, **_kw):  # type: ignore[no-untyped-def]
            calls.append(root_path)
            return ExtractionResult(nodes=[], edges=[])

        with patch("penguincode_cli.graphs.code.index_code", _fake_index_code):
            await session.handle_index_code("")

        assert len(calls) == 1
        assert Path(calls[0]) == tmp_path.resolve()

    async def test_flag_off_reports_disabled_not_error(self, tmp_path: Path) -> None:
        """`index_code` returns `None` when `penguincode.code-graph` is off."""
        session = _session(tmp_path, scope_ctx=_ctx())

        with (
            patch("penguincode_cli.graphs.code.index_code", return_value=None),
            patch("penguincode_cli.core.repl.print_info") as mock_info,
        ):
            await session.handle_index_code(str(tmp_path))

        assert mock_info.called
        assert "disabled" in mock_info.call_args[0][0].lower()

    async def test_index_code_failure_is_reported_not_raised(self, tmp_path: Path) -> None:
        """A GraphStore/parse failure surfaces as an error message, never crashes the REPL."""
        session = _session(tmp_path, scope_ctx=_ctx())

        def _boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("graph store outage")

        with (
            patch("penguincode_cli.graphs.code.index_code", _boom),
            patch("penguincode_cli.core.repl.print_error") as mock_error,
        ):
            await session.handle_index_code(str(tmp_path))

        assert mock_error.called
        assert "graph store outage" in mock_error.call_args[0][0]


class TestIndexCodeCommandDispatch:
    async def test_slash_index_code_routes_to_handler(self, tmp_path: Path) -> None:
        session = _session(tmp_path, scope_ctx=None)

        with patch.object(session, "handle_index_code") as mock_handler:
            mock_handler.return_value = None
            keep_going = await session.handle_command("/index-code some/path")

        assert keep_going is True
        mock_handler.assert_called_once_with("some/path")
