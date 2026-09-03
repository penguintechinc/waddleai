"""Structural graph extraction from the same tree-sitter AST CodeRAG chunks (spec §4a).

Deterministic -- tree-sitter only, no LLM (the >=2B minimum-model rule is
N/A here; there is no model in this path). Emits ``Module``/``Class``/
``Method``/``Function`` nodes and ``CONTAINS`` nesting edges for every
grammar the chunker supports; for Python -- the Phase 1 reference language
for edge extraction (decomposition decision, spec §4a) -- additionally
emits ``Field`` nodes and ``EXTENDS``/``CALLS``/``REFERENCES`` edges.
Cross-file CALLS resolution is out of scope: an unqualified call to a name
with no matching node in this file is skipped, never guessed. Reuses the
chunker's grammar node-type sets, definition walker, and parser
(``shared.knowledge.code_chunker``) so the graph is graph-shaped output of
the exact same AST the chunker already parses -- no separate tree-sitter
setup is duplicated here, and every qualified name here matches the
Class/Method/Function names Task 9's walk already produced (no divergent
re-derivation of the nesting/enclosing rules).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import cast

from shared.knowledge.code_chunker import (
    _GRAMMARS,
    _enclosing_class_name,
    _LangGrammar,
    _looks_binary,
    _resolve_language,
    _walk_definitions,
)


@dataclass(slots=True, frozen=True)
class GraphNodeDraft:
    """One structural node destined for the graph.

    Keyed later by ``TenantScope.node_key()`` in the worker (Task 11) --
    ``qualified_name`` here is the language-level name only, with no tenant
    prefix.
    """

    label: str
    qualified_name: str
    name: str | None
    path: str


@dataclass(slots=True, frozen=True)
class GraphEdgeDraft:
    """One structural edge between two qualified names within this file's scope."""

    edge_type: str
    src_qn: str
    dst_qn: str
    path: str


@dataclass(slots=True)
class GraphFragment:
    """All nodes + edges extracted from one file, for incremental emission.

    Mutable (unlike the ``*Draft`` value objects) -- extraction appends to
    it in place as the AST is walked, and Task 10 extends the same instance
    with additional edge/node passes.
    """

    nodes: list[GraphNodeDraft] = field(default_factory=list)
    edges: list[GraphEdgeDraft] = field(default_factory=list)


def extract_graph(path: str, source: str, language: str | None = None) -> GraphFragment:
    """Extract Module/Class/Method/Function nodes and CONTAINS edges from ``source``.

    Always yields exactly one ``Module`` node keyed by ``path``. Falls back
    to a module-only fragment -- never raises -- when the language is
    unmapped, the content looks binary, or the grammar fails to parse.

    Args:
        path: Repo-relative file path; also the Module node's qualified name.
        source: File content as text.
        language: Explicit tree-sitter grammar name; inferred from ``path``'s
            extension when omitted.

    Returns:
        A ``GraphFragment`` in document order.

    """
    frag = GraphFragment()
    frag.nodes.append(GraphNodeDraft(label="Module", qualified_name=path, name=path, path=path))

    resolved_language = _resolve_language(path, language)
    if resolved_language is None or _looks_binary(source):
        return frag

    grammar = _GRAMMARS.get(resolved_language)
    if grammar is None:
        return frag

    try:
        from tree_sitter_language_pack import get_parser

        parser = get_parser(resolved_language)
        tree = parser.parse(source.encode("utf-8"))
    except Exception:
        return frag

    for kind, name, def_node in _walk_definitions(tree.root_node, grammar):
        resolved_name = name or "anonymous"
        if kind == "class":
            qn = resolved_name
            frag.nodes.append(GraphNodeDraft("Class", qn, resolved_name, path))
            frag.edges.append(GraphEdgeDraft("CONTAINS", path, qn, path))
        elif kind == "method":
            enclosing = _enclosing_class_name(def_node, grammar) or "anonymous"
            qn = f"{enclosing}.{resolved_name}"
            frag.nodes.append(GraphNodeDraft("Method", qn, resolved_name, path))
            frag.edges.append(GraphEdgeDraft("CONTAINS", enclosing, qn, path))
        else:  # kind == "function"
            qn = resolved_name
            frag.nodes.append(GraphNodeDraft("Function", qn, resolved_name, path))
            frag.edges.append(GraphEdgeDraft("CONTAINS", path, qn, path))

    if resolved_language == "python":
        _python_edges(tree.root_node, grammar, path, frag)

    return frag


def _python_edges(root: object, grammar: _LangGrammar, path: str, frag: GraphFragment) -> None:
    """Field/EXTENDS/CALLS/REFERENCES for Python -- the Phase 1 reference language.

    A second pass over the same tree Task 9 already walked, re-running
    ``_walk_definitions`` so every qualified name here is derived exactly
    the way Task 9 derives it (bare class name; ``Enclosing.method`` for a
    method) rather than independently re-threading nesting depth, which
    would drift from Task 9's own naming for nested classes.
    """
    known_functions = {n.qualified_name for n in frag.nodes if n.label in ("Function", "Method")}
    known_classes = {n.qualified_name for n in frag.nodes if n.label == "Class"}

    for kind, name, def_node in _walk_definitions(root, grammar):
        resolved_name = name or "anonymous"
        if kind == "class":
            _extract_class_edges(def_node, resolved_name, path, frag)
        else:
            enclosing = _enclosing_class_name(def_node, grammar) if kind == "method" else None
            def_qn = f"{enclosing}.{resolved_name}" if enclosing else resolved_name
            _extract_call_edges(
                def_node, def_qn, enclosing, known_functions, known_classes, path, frag
            )


def _extract_class_edges(class_node: object, class_qn: str, path: str, frag: GraphFragment) -> None:
    """EXTENDS edges to identifier base classes + Field nodes/CONTAINS for one class."""
    supers = class_node.child_by_field_name("superclasses")  # type: ignore[attr-defined]
    if supers is not None:
        for base in supers.children:
            if base.type == "identifier":
                base_name = base.text.decode("utf-8", errors="replace")
                frag.edges.append(GraphEdgeDraft("EXTENDS", class_qn, base_name, path))

    body = class_node.child_by_field_name("body")  # type: ignore[attr-defined]
    for stmt in body.children if body is not None else []:
        target = _assignment_target(stmt)
        if target is not None and target.type == "identifier":  # type: ignore[attr-defined]
            field_name = target.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
            field_qn = f"{class_qn}.{field_name}"
            frag.nodes.append(GraphNodeDraft("Field", field_qn, field_name, path))
            frag.edges.append(GraphEdgeDraft("CONTAINS", class_qn, field_qn, path))


def _assignment_target(stmt: object) -> object | None:
    """Return a class-body ``assignment`` statement's ``left`` (target) node, or None."""
    if stmt.type != "assignment":  # type: ignore[attr-defined]
        return None
    return cast("object | None", stmt.child_by_field_name("left"))  # type: ignore[attr-defined]


def _extract_call_edges(
    def_node: object,
    def_qn: str,
    enclosing_class: str | None,
    known_functions: set[str],
    known_classes: set[str],
    path: str,
    frag: GraphFragment,
) -> None:
    """CALLS/REFERENCES from every ``call`` node anywhere in this def's body.

    An unqualified call (``helper()``) resolves against known top-level
    functions/methods in this file. An attribute call (``self.other()``)
    resolves against ``{enclosing_class}.{attr}`` when this def is itself a
    method -- a dotted call from a non-method (no enclosing class) is left
    unresolved. A callee matching a known class name is a REFERENCES edge
    (construction), not CALLS. Anything else -- external/imported, or
    simply unresolved -- is skipped, never guessed. ``function``/
    ``attribute`` are mandatory fields on ``call``/``attribute`` nodes in
    this grammar, so no None-field guard is needed here.
    """
    for call in _iter_calls(def_node):
        fn = call.child_by_field_name("function")  # type: ignore[attr-defined]
        callee: str | None = None
        if fn.type == "identifier":
            callee = fn.text.decode("utf-8", errors="replace")
        elif fn.type == "attribute" and enclosing_class is not None:
            attr = fn.child_by_field_name("attribute")
            callee = f"{enclosing_class}.{attr.text.decode('utf-8', errors='replace')}"

        if callee is None:
            continue
        if callee in known_functions:
            frag.edges.append(GraphEdgeDraft("CALLS", def_qn, callee, path))
        elif callee in known_classes:
            frag.edges.append(GraphEdgeDraft("REFERENCES", def_qn, callee, path))
        # else: unresolved (external/imported) call -- skipped, not guessed.


def _iter_calls(node: object) -> Iterator[object]:
    """Yield every ``call`` node in ``node``'s subtree, any depth (nested closures included)."""
    for child in node.children:  # type: ignore[attr-defined]
        if child.type == "call":
            yield child
        yield from _iter_calls(child)


__all__ = ["GraphEdgeDraft", "GraphFragment", "GraphNodeDraft", "extract_graph"]
