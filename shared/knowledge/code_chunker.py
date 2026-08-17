"""CodeRAG tree-sitter chunker (§9.1).

Function/class/module-boundary chunking via ``tree-sitter`` +
``tree-sitter-language-pack`` (both MIT). Each chunk is prefixed with a
``path > class > signature`` context header so a retrieved chunk is
self-describing even without its surrounding file. Unparseable or binary
files, and languages without a mapped grammar, fall back to fixed
line-window chunking with overlap.

Pure/CPU-only -- no I/O here. Callers (the CodeRAG worker) run this off the
event loop via ``asyncio.to_thread``/``ProcessPoolExecutor``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

_LANGUAGE_BY_EXTENSION: dict[str, str] = {
    "py": "python",
    "go": "go",
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "ts": "typescript",
    "tsx": "tsx",
    "rs": "rust",
    "java": "java",
    "rb": "ruby",
    "c": "c",
    "h": "c",
    "cpp": "cpp",
    "hpp": "cpp",
    "cc": "cpp",
    "cs": "c_sharp",
}


@dataclass(slots=True, frozen=True)
class _LangGrammar:
    """Node-type sets that identify definitions in one tree-sitter grammar."""

    class_types: frozenset[str]
    function_types: frozenset[str]
    method_types: frozenset[str]


_PY_GRAMMAR = _LangGrammar(
    class_types=frozenset({"class_definition"}),
    function_types=frozenset({"function_definition"}),
    method_types=frozenset(),  # python methods are function_definition nested in a class
)
_GO_GRAMMAR = _LangGrammar(
    class_types=frozenset({"type_declaration"}),
    function_types=frozenset({"function_declaration"}),
    method_types=frozenset({"method_declaration"}),
)
_JS_LIKE_GRAMMAR = _LangGrammar(
    class_types=frozenset({"class_declaration"}),
    function_types=frozenset({"function_declaration"}),
    method_types=frozenset({"method_definition"}),
)

_GRAMMARS: dict[str, _LangGrammar] = {
    "python": _PY_GRAMMAR,
    "go": _GO_GRAMMAR,
    "javascript": _JS_LIKE_GRAMMAR,
    "typescript": _JS_LIKE_GRAMMAR,
    "tsx": _JS_LIKE_GRAMMAR,
    "java": _JS_LIKE_GRAMMAR,
}


@dataclass(slots=True, frozen=True)
class CodeChunkDraft:
    """One chunk ready to embed + upsert into ``code_chunks`` (migration 012)."""

    path: str
    symbol: str | None
    kind: str  # function | method | class | module | window
    start_line: int
    end_line: int
    content: str
    """Header-prefixed body: ``"{header}\\n{body}"``."""
    content_hash: str


def _content_hash(header: str, body: str) -> str:
    return hashlib.sha256(f"{header}\n{body}".encode()).hexdigest()


def _resolve_language(path: str, language: str | None) -> str | None:
    if language:
        return language
    if "." not in path:
        return None
    ext = path.rsplit(".", 1)[-1].lower()
    return _LANGUAGE_BY_EXTENSION.get(ext)


def _looks_binary(source: str) -> bool:
    """Cheap binary-content heuristic: a NUL byte never appears in real source."""
    return "\x00" in source


def _get_name(node: object) -> str | None:
    """Resolve a definition node's identifier, with a type_spec fallback (Go)."""
    name_node = node.child_by_field_name("name")  # type: ignore[attr-defined]
    if name_node is not None:
        return name_node.text.decode("utf-8", errors="replace")
    # Go's type_declaration (our "class" stand-in) carries the name on a
    # nested type_spec child rather than on itself.
    for child in node.children:  # type: ignore[attr-defined]
        if child.type == "type_spec":
            inner = child.child_by_field_name("name")
            if inner is not None:
                return inner.text.decode("utf-8", errors="replace")
    return None


def _signature_text(node: object, source_bytes: bytes) -> str:
    """The declaration line(s) before the body: e.g. 'def foo(x, y):'."""
    body = node.child_by_field_name("body")  # type: ignore[attr-defined]
    end = body.start_byte if body is not None else node.end_byte  # type: ignore[attr-defined]
    raw = source_bytes[node.start_byte : end].decode("utf-8", errors="replace")  # type: ignore[attr-defined]
    collapsed = " ".join(raw.split())
    return collapsed.rstrip(":{").strip()


def _walk_definitions(node: object, grammar: _LangGrammar, class_name: str | None = None):
    """Yield (kind, name, node) for each definition, document order, DFS."""
    node_type = node.type  # type: ignore[attr-defined]
    kind: str | None = None
    if node_type in grammar.class_types:
        kind = "class"
    elif node_type in grammar.method_types:
        kind = "method"
    elif node_type in grammar.function_types:
        kind = "method" if class_name else "function"

    if kind:
        name = _get_name(node)
        yield (kind, name, node)
        if kind == "class":
            # Recurse to find methods, but not into function/method bodies
            # (no nested-closure chunks).
            for child in node.children:  # type: ignore[attr-defined]
                yield from _walk_definitions(child, grammar, class_name=name)
        return

    for child in node.children:  # type: ignore[attr-defined]
        yield from _walk_definitions(child, grammar, class_name=class_name)


def _leftover_module_groups(root: object, grammar: _LangGrammar) -> list[list[object]]:
    """Group root's direct children that are NOT definitions into contiguous runs."""
    definition_types = grammar.class_types | grammar.function_types | grammar.method_types
    groups: list[list[object]] = []
    current: list[object] = []
    for child in root.children:  # type: ignore[attr-defined]
        if child.type in definition_types:  # type: ignore[attr-defined]
            if current:
                groups.append(current)
                current = []
        else:
            current.append(child)
    if current:
        groups.append(current)
    return groups


def _chunk_with_tree_sitter(path: str, source: str, language: str) -> list[CodeChunkDraft] | None:
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError:  # pragma: no cover - dependency is required in prod
        return None

    try:
        parser = get_parser(language)  # type: ignore[arg-type]
    except Exception:
        return None

    source_bytes = source.encode("utf-8")
    try:
        tree = parser.parse(source_bytes)
    except Exception:
        return None

    grammar = _GRAMMARS.get(language)
    if grammar is None:
        return None

    chunks: list[CodeChunkDraft] = []

    for kind, name, def_node in _walk_definitions(tree.root_node, grammar):
        start_line = def_node.start_point[0] + 1
        end_line = def_node.end_point[0] + 1
        body_bytes = source_bytes[def_node.start_byte : def_node.end_byte]
        body = body_bytes.decode("utf-8", errors="replace")
        signature = _signature_text(def_node, source_bytes)

        if kind == "class":
            header = f"{path} > {name or 'anonymous'}"
        elif kind == "method":
            # class_name is threaded through _walk_definitions but not
            # returned directly; recover it from the header convention by
            # re-deriving from the enclosing class via the parent chain.
            enclosing = _enclosing_class_name(def_node, grammar)
            header = f"{path} > {enclosing} > {signature}" if enclosing else f"{path} > {signature}"
        else:
            header = f"{path} > {signature}"

        chunks.append(
            CodeChunkDraft(
                path=path,
                symbol=name,
                kind=kind,
                start_line=start_line,
                end_line=end_line,
                content=f"{header}\n{body}",
                content_hash=_content_hash(header, body),
            )
        )

    for group in _leftover_module_groups(tree.root_node, grammar):
        first, last = group[0], group[-1]
        body = source_bytes[first.start_byte : last.end_byte].decode("utf-8", errors="replace")  # type: ignore[attr-defined]
        if not body.strip():
            continue
        header = f"{path} > module"
        start_line = first.start_point[0] + 1  # type: ignore[attr-defined]
        end_line = last.end_point[0] + 1  # type: ignore[attr-defined]
        chunks.append(
            CodeChunkDraft(
                path=path,
                symbol=None,
                kind="module",
                start_line=start_line,
                end_line=end_line,
                content=f"{header}\n{body}",
                content_hash=_content_hash(header, body),
            )
        )

    chunks.sort(key=lambda c: (c.start_line, c.end_line))
    return chunks


def _enclosing_class_name(node: object, grammar: _LangGrammar) -> str | None:
    """Walk up via `.parent` to find the nearest enclosing class-type node's name."""
    current = getattr(node, "parent", None)
    while current is not None:
        if current.type in grammar.class_types:
            return _get_name(current)
        current = getattr(current, "parent", None)
    return None


def _chunk_line_windows(
    path: str, source: str, window_lines: int = 60, overlap_lines: int = 10
) -> list[CodeChunkDraft]:
    """Fixed line-window fallback for unparseable/binary files or unmapped languages."""
    lines = source.splitlines()
    if not lines:
        return []

    step = max(1, window_lines - overlap_lines)
    chunks: list[CodeChunkDraft] = []
    start = 0
    while start < len(lines):
        end = min(start + window_lines, len(lines))
        body = "\n".join(lines[start:end])
        header = f"{path} (lines {start + 1}-{end})"
        chunks.append(
            CodeChunkDraft(
                path=path,
                symbol=None,
                kind="window",
                start_line=start + 1,
                end_line=end,
                content=f"{header}\n{body}",
                content_hash=_content_hash(header, body),
            )
        )
        if end == len(lines):
            break
        start += step
    return chunks


def chunk_code(path: str, source: str, language: str | None = None) -> list[CodeChunkDraft]:
    """Chunk ``source`` at function/class/module boundaries.

    Args:
        path: Repo-relative file path, used in every chunk's context header.
        source: File content as text.
        language: Explicit tree-sitter grammar name; inferred from ``path``'s
            extension when omitted.

    Returns:
        Deterministic, document-ordered list of chunks. Falls back to
        line-window chunking (``kind="window"``) for binary content, unmapped
        extensions, or any tree-sitter parse failure.
    """
    resolved_language = _resolve_language(path, language)
    if resolved_language is None or _looks_binary(source):
        return _chunk_line_windows(path, source)

    chunks = _chunk_with_tree_sitter(path, source, resolved_language)
    if chunks is None:
        return _chunk_line_windows(path, source)
    return chunks


__all__ = ["CodeChunkDraft", "chunk_code"]
