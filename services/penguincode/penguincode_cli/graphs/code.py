"""Code graph extractor (T11): tree-sitter parse of a source tree -> `GraphStore` (`kind="code"`).

**Tree-sitter was not already a penguincode dependency** (verified 2026-09-25 by
direct inspection of `penguincode_cli/` and `pyproject.toml`/`requirements.in` --
no `tree_sitter` import, chunker, or grammar anywhere in the tree, despite the
platform plan's Tech Stack line saying "tree-sitter (present)"). This module adds
`tree-sitter==0.26.0` + `tree-sitter-python==0.25.0` (both MIT-licensed, verified
via PyPI classifiers), exact-pinned and hash-locked alongside every other
dependency here.

**Node/edge design** (`node_type` in `{file, class, function, symbol}`, `rel_type`
in `{imports, calls, defines, references}`, per the platform plan's Shared
Contracts):

- Only `file`/`class`/`function` nodes are emitted explicitly -- their
  `(node_type, key)` is definitively known from the parse. `symbol` endpoints
  (import targets, unresolved call callees, external base classes) are referenced
  only from edges and left to `GraphStore.upsert_edges`' auto-create path (T10) --
  this avoids the extractor needing cross-file dedup logic for a symbol like `os`
  that dozens of files might import in the same indexing batch.
- `imports`: the importing scope (file, or the enclosing function for a lazy
  `import` inside a function body) -> `symbol`. Plain `import a.b.c [as x]` ->
  `dst_key="a.b.c"`; `from a.b import c [as d]` -> `dst_key="a.b.c"` for each
  imported name (the *original* name, not the alias); a bare `from x import *`
  records one edge to the module itself (no names to enumerate).
- `defines`: file -> top-level class/function; class -> nested method/class;
  function -> nested function. Qualified keys are dotted (`path/to/file.py::Foo.bar`).
- `calls`: enclosing function/class/file -> `function` (same-file resolution:
  a bare-identifier call resolves to a `function` node **iff** a same-named
  top-level function is defined in this file) or -> `symbol` otherwise (attribute
  calls like `os.path.join(...)`, `self.method(...)`, or a name not defined in
  this file -- cross-file and attribute-target resolution are not attempted in
  this first cut; the callee's full source text becomes `dst_key`).
- `references`: class base classes only in this first cut (`class Foo(Base)` ->
  `Foo references Base`), resolved to `class` if `Base` is also defined
  somewhere in the same file, else `symbol`. Type annotations/decorators are a
  natural next extension, not implemented here.
- Call/reference detection walks statement bodies only -- it does not descend
  into decorator expressions or parameter default-value expressions (a
  documented v1 limitation; the common case of calls inside a function body is
  covered).

**Visibility default is `"team"`**: a codebase indexed here typically belongs to
one team's project, not the whole tenant, so scoping its graph to the caller's
team is the sensible default. A tenant-wide/shared codebase can pass
`visibility="tenant"` explicitly (which needs no `team_id`); callers relying on
the `"team"` default must supply `team_id` themselves -- enforced by
`GraphStore._scope_columns`, not by this module.

**Extensibility (languages):** add a file extension to `_LANGUAGE_EXTRACTORS`
mapped to a `(rel_path, source_bytes) -> ExtractionResult` function. Only Python
is implemented today; a new language needs its own extractor because tree-sitter
node/field names are grammar-specific (there is no generic AST shape to share).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import tree_sitter_python
from tree_sitter import Language, Node, Parser

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import GraphConfig
from penguincode_cli.flags import CODE_GRAPH_FLAG, is_enabled
from penguincode_cli.observability.otel import timed_store_operation
from penguincode_cli.stores.graph import GraphEdge, GraphNode, GraphStore, create_graph_store

logger = logging.getLogger(__name__)

#: Directories never descended into, regardless of language -- VCS metadata,
#: virtualenvs, and dependency/build trees would otherwise dominate a codebase
#: index with content the caller doesn't own. Any other dot-directory
#: (`.something/`) is also skipped -- in practice these are always tooling
#: state, never source the caller wants indexed.
DEFAULT_IGNORE_DIRS: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        ".tox",
        "site-packages",
    }
)


@dataclass(slots=True, frozen=True)
class ExtractionResult:
    """One extraction batch: every `GraphNode`/`GraphEdge` the code graph should write."""

    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)


def _text(node: Node) -> str:
    raw = node.text
    return raw.decode("utf-8", errors="replace") if raw is not None else ""


def _name_of(def_node: Node) -> str | None:
    """The `name` field of a `class_definition`/`function_definition`, or `None`."""
    name_node = def_node.child_by_field_name("name")
    return _text(name_node) if name_node is not None else None


def _import_target_text(name_node: Node) -> str:
    """The original (never the alias) dotted name of one `import`'s `name` field.

    `name_node` is either a `dotted_name` (plain `import a.b`) or an
    `aliased_import` (`import a.b as x` / `from m import a.b as x`) -- the alias
    itself is never used as the edge's `dst_key` so two files aliasing the same
    import differently still point at the same graph node.
    """
    if name_node.type == "aliased_import":
        original = name_node.child_by_field_name("name")
        if original is not None:
            return _text(original)
    return _text(name_node)


class _PythonFileExtractor:
    """Extracts one Python file's nodes/edges via two passes over its parse tree.

    Pass 1 (`_prepass`) records every class/function's qualified key so that
    pass 2 (`_walk`) can resolve calls and base-class references made *before*
    their target is defined later in the same file (forward references are
    common at module scope in Python).
    """

    def __init__(self, rel_path: str) -> None:
        self._rel_path = rel_path
        self._nodes: list[GraphNode] = []
        self._edges: list[GraphEdge] = []
        #: unqualified top-level function name -> full key; resolves bare
        #: identifier calls (`helper(x)`), not attribute calls (`self.helper()`).
        self._top_level_functions: dict[str, str] = {}
        #: unqualified class name (any nesting depth) -> full key; resolves
        #: base-class references.
        self._classes_by_name: dict[str, str] = {}

    def extract(self, root: Node) -> ExtractionResult:
        file_key = self._rel_path
        self._nodes.append(GraphNode(node_type="file", key=file_key, props={"language": "python"}))
        self._prepass(root, qualifier=None, is_top_level=True)
        self._walk(root, qualifier=None, container_type="file", container_key=file_key)
        return ExtractionResult(nodes=self._nodes, edges=self._edges)

    def _prepass(self, node: Node, *, qualifier: str | None, is_top_level: bool) -> None:
        for child in node.children:
            if child.type == "class_definition":
                name = _name_of(child)
                if name is None:
                    continue
                qualified = f"{qualifier}.{name}" if qualifier else name
                self._classes_by_name.setdefault(name, f"{self._rel_path}::{qualified}")
                body = child.child_by_field_name("body")
                if body is not None:
                    self._prepass(body, qualifier=qualified, is_top_level=False)
            elif child.type == "function_definition":
                name = _name_of(child)
                if name is None:
                    continue
                qualified = f"{qualifier}.{name}" if qualifier else name
                if is_top_level:
                    self._top_level_functions.setdefault(name, f"{self._rel_path}::{qualified}")
                body = child.child_by_field_name("body")
                if body is not None:
                    self._prepass(body, qualifier=qualified, is_top_level=False)
            else:
                self._prepass(child, qualifier=qualifier, is_top_level=is_top_level)

    def _walk(
        self, node: Node, *, qualifier: str | None, container_type: str, container_key: str
    ) -> None:
        if node.type == "import_statement":
            for name_node in node.children_by_field_name("name"):
                self._add_import_edge(container_type, container_key, _import_target_text(name_node))
            return

        if node.type == "import_from_statement":
            self._handle_import_from(node, container_type, container_key)
            return

        if node.type == "call":
            self._handle_call(node, container_type, container_key)
            for child in node.children:
                self._walk(
                    child,
                    qualifier=qualifier,
                    container_type=container_type,
                    container_key=container_key,
                )
            return

        if node.type in ("class_definition", "function_definition"):
            name = _name_of(node)
            if name is not None:
                qualified = f"{qualifier}.{name}" if qualifier else name
                key = f"{self._rel_path}::{qualified}"
                node_type = "class" if node.type == "class_definition" else "function"
                self._nodes.append(GraphNode(node_type=node_type, key=key, props={"name": name}))
                self._edges.append(
                    GraphEdge(
                        src_type=container_type,
                        src_key=container_key,
                        dst_type=node_type,
                        dst_key=key,
                        rel_type="defines",
                    )
                )
                if node.type == "class_definition":
                    self._handle_base_classes(node, class_key=key)
                body = node.child_by_field_name("body")
                if body is not None:
                    self._walk(
                        body, qualifier=qualified, container_type=node_type, container_key=key
                    )
                return
            # Unnamed/malformed definition (should not occur in valid Python) --
            # fall through to the generic walk below rather than dropping it.

        for child in node.children:
            self._walk(
                child,
                qualifier=qualifier,
                container_type=container_type,
                container_key=container_key,
            )

    def _add_import_edge(self, container_type: str, container_key: str, dst_key: str) -> None:
        self._edges.append(
            GraphEdge(
                src_type=container_type,
                src_key=container_key,
                dst_type="symbol",
                dst_key=dst_key,
                rel_type="imports",
            )
        )

    def _handle_import_from(self, node: Node, container_type: str, container_key: str) -> None:
        module_node = node.child_by_field_name("module_name")
        if module_node is None:
            return
        module = _text(module_node)
        name_nodes = list(node.children_by_field_name("name"))
        if not name_nodes:
            # `from x import *` -- no explicit names to enumerate.
            self._add_import_edge(container_type, container_key, module)
            return
        for name_node in name_nodes:
            imported = _import_target_text(name_node)
            self._add_import_edge(container_type, container_key, f"{module}.{imported}")

    def _handle_call(self, node: Node, container_type: str, container_key: str) -> None:
        func = node.child_by_field_name("function")
        if func is None:
            return
        if func.type == "identifier":
            name = _text(func)
            target_key = self._top_level_functions.get(name)
            if target_key is not None:
                self._edges.append(
                    GraphEdge(
                        src_type=container_type,
                        src_key=container_key,
                        dst_type="function",
                        dst_key=target_key,
                        rel_type="calls",
                    )
                )
                return
            dst_key = name
        else:
            # Attribute call (`os.path.join(...)`, `self.method(...)`) or any
            # other callee shape -- unresolved in this first cut.
            dst_key = _text(func)
        self._edges.append(
            GraphEdge(
                src_type=container_type,
                src_key=container_key,
                dst_type="symbol",
                dst_key=dst_key,
                rel_type="calls",
            )
        )

    def _handle_base_classes(self, class_node: Node, *, class_key: str) -> None:
        superclasses = class_node.child_by_field_name("superclasses")
        if superclasses is None:
            return
        for child in superclasses.children:
            if child.type in ("identifier", "attribute"):
                base_name = _text(child)
            else:
                continue  # '(', ')', ',', keyword_argument (e.g. metaclass=...)
            target_key = self._classes_by_name.get(base_name)
            if target_key is not None and target_key != class_key:
                self._edges.append(
                    GraphEdge(
                        src_type="class",
                        src_key=class_key,
                        dst_type="class",
                        dst_key=target_key,
                        rel_type="references",
                    )
                )
            else:
                self._edges.append(
                    GraphEdge(
                        src_type="class",
                        src_key=class_key,
                        dst_type="symbol",
                        dst_key=base_name,
                        rel_type="references",
                    )
                )


_python_parser_singleton: Parser | None = None


def _python_parser() -> Parser:
    global _python_parser_singleton
    if _python_parser_singleton is None:
        _python_parser_singleton = Parser(Language(tree_sitter_python.language()))
    return _python_parser_singleton


def parse_python_file(rel_path: str, source: bytes) -> ExtractionResult:
    """Parse one Python file's source into its `GraphNode`/`GraphEdge` extraction result."""
    tree = _python_parser().parse(source)
    return _PythonFileExtractor(rel_path).extract(tree.root_node)


#: Extension -> per-file extractor. Python only today -- see the module
#: docstring's Extensibility note for how to add a language.
_LANGUAGE_EXTRACTORS: Final[dict[str, Callable[[str, bytes], ExtractionResult]]] = {
    ".py": parse_python_file,
}


def extract_tree(
    root_path: str | Path, *, ignore_dirs: frozenset[str] = DEFAULT_IGNORE_DIRS
) -> ExtractionResult:
    """Walk `root_path`, parsing every file with a registered extractor into one
    merged `ExtractionResult`.

    Files are keyed by their POSIX-style path relative to `root_path`, so
    results are stable regardless of the caller's absolute path. A file that
    fails to read or parse is logged and skipped -- one malformed file must
    never abort indexing the rest of the tree. Duplicate edges (e.g. the same
    function called twice) are deduplicated here; `GraphStore.upsert_edges`
    would also collapse them via its own uniqueness constraint, but skipping
    the redundant round trip is cheap and free to do at this layer.
    """
    resolved_root = Path(root_path).resolve()
    all_nodes: list[GraphNode] = []
    all_edges: list[GraphEdge] = []
    seen_edges: set[tuple[str, str, str, str, str]] = set()

    for dirpath, dirnames, filenames in resolved_root.walk():
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs and not d.startswith(".")]
        for filename in filenames:
            extractor = _LANGUAGE_EXTRACTORS.get(Path(filename).suffix)
            if extractor is None:
                continue
            file_path = dirpath / filename
            rel_path = file_path.relative_to(resolved_root).as_posix()
            try:
                source = file_path.read_bytes()
            except OSError as exc:
                logger.warning(
                    "penguincode code-graph: skipping unreadable file %s: %s", rel_path, exc
                )
                continue
            try:
                result = extractor(rel_path, source)
            except Exception as exc:  # noqa: BLE001 -- one bad file must not abort the whole tree
                logger.warning("penguincode code-graph: failed to parse %s: %s", rel_path, exc)
                continue
            all_nodes.extend(result.nodes)
            for edge in result.edges:
                dedup_key = (
                    edge.src_type,
                    edge.src_key,
                    edge.dst_type,
                    edge.dst_key,
                    edge.rel_type,
                )
                if dedup_key in seen_edges:
                    continue
                seen_edges.add(dedup_key)
                all_edges.append(edge)

    return ExtractionResult(nodes=all_nodes, edges=all_edges)


def index_code(
    ctx: ScopeContext,
    root_path: str | Path,
    *,
    visibility: str = "team",
    team_id: str | None = None,
    graph_store: GraphStore | None = None,
    config: GraphConfig | None = None,
) -> ExtractionResult | None:
    """Entry point the code-indexing flow calls to (re)build the code graph for one tree.

    Gated on `penguincode.code-graph` (checked first, before any directory walk
    or parsing) -- OFF means this is a complete no-op: no I/O, no `GraphStore`
    writes. Returns `None` when skipped so a caller can distinguish "flag off"
    from "indexed zero files" (an empty `ExtractionResult`).

    `visibility` defaults to `"team"` -- see the module docstring's Visibility
    section; callers relying on the default must supply `team_id`, or
    `GraphStore` raises `ValueError` (never silently falls back to another
    scope). Pass an explicit `graph_store` in tests to avoid constructing a
    real `PostgresGraphStore`; production callers normally pass `config`
    instead and let this function build the store from it.
    """
    if not is_enabled(CODE_GRAPH_FLAG, ctx):
        logger.info("penguincode code-graph: flag off, skipping index of %s", root_path)
        return None

    store = graph_store if graph_store is not None else create_graph_store(config or GraphConfig())
    result = extract_tree(root_path)

    with timed_store_operation(
        "extraction",
        "graph.code.index",
        node_count=len(result.nodes),
        edge_count=len(result.edges),
    ):
        if result.nodes:
            store.upsert_nodes(ctx, "code", result.nodes, visibility=visibility, team_id=team_id)
        if result.edges:
            store.upsert_edges(ctx, "code", result.edges, visibility=visibility, team_id=team_id)

    return result


__all__ = [
    "DEFAULT_IGNORE_DIRS",
    "ExtractionResult",
    "extract_tree",
    "index_code",
    "parse_python_file",
]
