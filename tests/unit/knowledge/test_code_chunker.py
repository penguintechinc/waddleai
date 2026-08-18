"""Tests for shared.knowledge.code_chunker: tree-sitter chunking (§9.1)."""

from __future__ import annotations

from shared.knowledge.code_chunker import CodeChunkDraft, chunk_code

_PYTHON_FIXTURE = '''"""Module docstring."""

import os

CONSTANT = 42


class Widget:
    """A widget."""

    def render(self, ctx):
        return ctx

    def close(self):
        pass


class Gadget:
    def spin(self):
        return "spinning"


def standalone(x):
    return x + 1
'''

_GO_FIXTURE = """package main

type Server struct{}

func (s *Server) Handle(req int) int {
\treturn req + 1
}

func NewServer() *Server {
\treturn &Server{}
}
"""

_JS_FIXTURE = """class Widget {
  render(ctx) {
    return ctx;
  }
}

function standalone(x) {
  return x + 1;
}
"""


class TestPythonChunking:
    """(a) Python fixture: one chunk per func/method/class-body, correct kind + lines + header."""

    def test_yields_one_chunk_per_function_method_and_class(self) -> None:
        """Two classes with methods + one free function -> 5 definition chunks + module leftover."""
        chunks = chunk_code("widgets.py", _PYTHON_FIXTURE)

        kinds = [c.kind for c in chunks]
        assert kinds.count("class") == 2
        assert kinds.count("method") == 3  # Widget.render, Widget.close, Gadget.spin
        assert kinds.count("function") == 1  # standalone
        assert kinds.count("module") == 1  # docstring + import + constant, leftover

    def test_symbol_and_kind_correct_for_each_definition(self) -> None:
        """Each chunk's symbol matches its identifier and kind matches its role."""
        chunks = chunk_code("widgets.py", _PYTHON_FIXTURE)
        by_symbol = {c.symbol: c for c in chunks if c.symbol}

        assert by_symbol["Widget"].kind == "class"
        assert by_symbol["Gadget"].kind == "class"
        assert by_symbol["render"].kind == "method"
        assert by_symbol["close"].kind == "method"
        assert by_symbol["spin"].kind == "method"
        assert by_symbol["standalone"].kind == "function"

    def test_start_and_end_lines_are_correct(self) -> None:
        """standalone() spans exactly its def..return lines (1-indexed)."""
        chunks = chunk_code("widgets.py", _PYTHON_FIXTURE)
        standalone = next(c for c in chunks if c.symbol == "standalone")

        lines = _PYTHON_FIXTURE.splitlines()
        def_line = next(
            i for i, line in enumerate(lines, start=1) if line.startswith("def standalone")
        )
        assert standalone.start_line == def_line
        assert standalone.end_line == def_line + 1

    def test_header_is_path_class_signature(self) -> None:
        """Method chunks carry a 'path > class > signature' header; functions 'path > signature'."""
        chunks = chunk_code("widgets.py", _PYTHON_FIXTURE)
        render = next(c for c in chunks if c.symbol == "render")
        standalone = next(c for c in chunks if c.symbol == "standalone")

        assert render.content.startswith("widgets.py > Widget > def render(self, ctx)")
        assert standalone.content.startswith("widgets.py > def standalone(x)")

    def test_content_hash_is_stable_and_unique_per_chunk(self) -> None:
        """(d) Each chunk carries a content_hash, distinct chunks hash differently."""
        chunks = chunk_code("widgets.py", _PYTHON_FIXTURE)
        hashes = [c.content_hash for c in chunks]

        assert len(hashes) == len(set(hashes))
        assert all(len(h) == 64 for h in hashes)  # sha256 hex digest


class TestGoAndJavaScriptChunking:
    """(b) Go/JS fixtures chunk at their own grammar's function/type boundaries."""

    def test_go_chunks_functions_methods_and_type_declaration(self) -> None:
        """Go: method_declaration/function_declaration/type_declaration map correctly."""
        chunks = chunk_code("server.go", _GO_FIXTURE)
        kinds_by_symbol = {c.symbol: c.kind for c in chunks if c.symbol}

        assert kinds_by_symbol["Server"] == "class"
        assert kinds_by_symbol["Handle"] == "method"
        assert kinds_by_symbol["NewServer"] == "function"

    def test_javascript_chunks_class_method_and_function(self) -> None:
        """JS: class_declaration/method_definition/function_declaration map correctly."""
        chunks = chunk_code("widget.js", _JS_FIXTURE)
        kinds_by_symbol = {c.symbol: c.kind for c in chunks if c.symbol}

        assert kinds_by_symbol["Widget"] == "class"
        assert kinds_by_symbol["render"] == "method"
        assert kinds_by_symbol["standalone"] == "function"

    def test_javascript_method_header_names_enclosing_class(self) -> None:
        """JS method chunk header follows path > class > signature too."""
        chunks = chunk_code("widget.js", _JS_FIXTURE)
        render = next(c for c in chunks if c.symbol == "render")

        assert render.content.startswith("widget.js > Widget > render(ctx)")


class TestFallbackLineWindow:
    """(c) Unparseable/binary files and unmapped extensions fall back to line windows."""

    def test_unmapped_extension_falls_back_to_window(self) -> None:
        """A .txt file (no grammar mapping) chunks via line windows, kind='window'."""
        source = "\n".join(f"line {i}" for i in range(1, 21))
        chunks = chunk_code("notes.txt", source)

        assert all(c.kind == "window" for c in chunks)
        assert all(c.symbol is None for c in chunks)

    def test_binary_content_falls_back_to_window(self) -> None:
        """Content containing a NUL byte is treated as binary and never sent to tree-sitter."""
        source = "some\x00binary\x00content" * 5
        chunks = chunk_code("blob.py", source)

        assert all(c.kind == "window" for c in chunks)

    def test_window_fallback_has_overlap_and_covers_all_lines(self) -> None:
        """Line-window chunks overlap and collectively cover the whole file."""
        source = "\n".join(f"line {i}" for i in range(1, 201))
        chunks = chunk_code("big.txt", source)

        assert len(chunks) > 1
        assert chunks[0].start_line == 1
        assert chunks[-1].end_line == 200
        # Overlap: the next chunk's start is before the previous chunk's end.
        for prev, nxt in zip(chunks, chunks[1:], strict=False):
            assert nxt.start_line <= prev.end_line

    def test_empty_file_yields_no_chunks(self) -> None:
        """An empty file produces zero chunks, not an error."""
        assert chunk_code("empty.txt", "") == []


class TestDeterminism:
    """(e) Chunking is deterministic across runs."""

    def test_identical_input_yields_identical_output(self) -> None:
        """Two chunk_code() calls on the same input produce byte-identical results."""
        first = chunk_code("widgets.py", _PYTHON_FIXTURE)
        second = chunk_code("widgets.py", _PYTHON_FIXTURE)

        assert first == second

    def test_result_is_a_list_of_code_chunk_drafts(self) -> None:
        """chunk_code() returns CodeChunkDraft instances with all required fields."""
        chunks = chunk_code("widgets.py", _PYTHON_FIXTURE)
        assert chunks
        assert all(isinstance(c, CodeChunkDraft) for c in chunks)
