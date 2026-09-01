"""Structural graph extraction from the same tree-sitter AST CodeRAG chunks (spec §4a).

Deterministic -- tree-sitter only, no LLM (the >=2B minimum-model rule is
N/A here; there is no model in this path). Emits ``Module``/``Class``/
``Method``/``Function`` nodes and ``CONTAINS`` nesting edges here; Task 10
extends this module with ``Field`` nodes and ``EXTENDS``/``CALLS``/
``REFERENCES`` edges (Python reference language). Reuses the chunker's
grammar node-type sets, definition walker, and parser
(``shared.knowledge.code_chunker``) so the graph is graph-shaped output of
the exact same AST the chunker already parses -- no separate tree-sitter
setup is duplicated here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shared.knowledge.code_chunker import (
    _GRAMMARS,
    _enclosing_class_name,
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

    return frag


__all__ = ["GraphEdgeDraft", "GraphFragment", "GraphNodeDraft", "extract_graph"]
