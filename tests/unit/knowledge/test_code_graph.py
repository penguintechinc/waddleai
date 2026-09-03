"""Tests for shared.knowledge.code_graph: structural node + CONTAINS extraction (Task 9)."""

from __future__ import annotations

from unittest.mock import patch

from shared.knowledge.code_graph import GraphEdgeDraft, GraphFragment, GraphNodeDraft, extract_graph

_PY = """
def top():
    pass

class C:
    def m(self):
        pass
"""


def test_nodes_and_contains() -> None:
    """Module/Function/Class/Method nodes and their CONTAINS edges are extracted."""
    frag = extract_graph("pkg/mod.py", _PY)
    labels = {(n.label, n.qualified_name) for n in frag.nodes}
    assert ("Module", "pkg/mod.py") in labels
    assert ("Function", "top") in labels
    assert ("Class", "C") in labels
    assert ("Method", "C.m") in labels

    contains = {(e.src_qn, e.dst_qn) for e in frag.edges if e.edge_type == "CONTAINS"}
    assert ("pkg/mod.py", "top") in contains  # module contains top-level fn
    assert ("pkg/mod.py", "C") in contains  # module contains class
    assert ("C", "C.m") in contains  # class contains method


def test_only_contains_edges_emitted() -> None:
    """Task 9 emits CONTAINS only -- other edge types are Task 10's job."""
    frag = extract_graph("pkg/mod.py", _PY)
    assert {e.edge_type for e in frag.edges} == {"CONTAINS"}


def test_non_parseable_returns_module_only_no_crash() -> None:
    """Binary content degrades gracefully to a module-only fragment, never raises."""
    frag = extract_graph("data.bin", "\x00\x01binary")
    assert [n.label for n in frag.nodes] == ["Module"]
    assert frag.edges == []


def test_unmapped_extension_returns_module_only() -> None:
    """A path with no grammar mapping (and no explicit language) still returns Module."""
    frag = extract_graph("notes.txt", "just some text, not code")
    assert [n.label for n in frag.nodes] == ["Module"]
    assert frag.edges == []


def test_empty_source_returns_module_only() -> None:
    """An empty file parses to a module with no definitions -- no crash."""
    frag = extract_graph("pkg/empty.py", "")
    assert [n.label for n in frag.nodes] == ["Module"]
    assert frag.edges == []


def test_every_node_carries_path() -> None:
    """Every emitted node (including Module) carries the source file's path."""
    frag = extract_graph("pkg/mod.py", _PY)
    assert all(n.path == "pkg/mod.py" for n in frag.nodes)


def test_module_qualified_name_is_the_path() -> None:
    """The Module node's qualified_name is the repo-relative path, per spec."""
    frag = extract_graph("pkg/mod.py", _PY)
    module_nodes = [n for n in frag.nodes if n.label == "Module"]
    assert len(module_nodes) == 1
    assert module_nodes[0].qualified_name == "pkg/mod.py"
    assert module_nodes[0].name == "pkg/mod.py"


def test_javascript_method_via_explicit_language() -> None:
    """A non-Python grammar (JS) with body-nested methods nests correctly, generic reuse."""
    js_src = """class Widget {
  render(ctx) {
    return ctx;
  }
}

function standalone(x) {
  return x + 1;
}
"""
    frag = extract_graph("widget.js", js_src, language="javascript")
    labels = {(n.label, n.qualified_name) for n in frag.nodes}
    assert ("Class", "Widget") in labels
    assert ("Method", "Widget.render") in labels
    assert ("Function", "standalone") in labels
    contains = {(e.src_qn, e.dst_qn) for e in frag.edges if e.edge_type == "CONTAINS"}
    assert ("Widget", "Widget.render") in contains
    assert ("widget.js", "standalone") in contains


def test_go_receiver_method_degrades_gracefully() -> None:
    """Go's receiver-style methods aren't AST-nested under their type_declaration.

    Same limitation the chunker itself has (its header only names an
    enclosing class when the grammar nests the definition in the class
    body -- see ``_enclosing_class_name``); a receiver method still becomes
    a valid, non-crashing Method node, just without a resolvable class
    association -- not silently dropped, not mis-associated with the wrong
    type.
    """
    go_src = """package main

type Server struct{}

func (s *Server) Handle(req int) int {
\treturn req + 1
}

func NewServer() *Server {
\treturn &Server{}
}
"""
    frag = extract_graph("srv.go", go_src, language="go")
    labels = {(n.label, n.qualified_name) for n in frag.nodes}
    assert ("Class", "Server") in labels
    assert ("Function", "NewServer") in labels
    method_nodes = [n for n in frag.nodes if n.label == "Method"]
    assert len(method_nodes) == 1
    assert method_nodes[0].name == "Handle"


def test_graph_node_draft_is_frozen_slots() -> None:
    """GraphNodeDraft is an immutable value object -- attribute mutation raises."""
    node = GraphNodeDraft(label="Module", qualified_name="p.py", name="p.py", path="p.py")
    try:
        node.label = "Class"  # type: ignore[misc]
    except (AttributeError, TypeError):
        pass
    else:
        raise AssertionError("GraphNodeDraft must be frozen")


def test_graph_edge_draft_is_frozen_slots() -> None:
    """GraphEdgeDraft is an immutable value object -- attribute mutation raises."""
    edge = GraphEdgeDraft(edge_type="CONTAINS", src_qn="a", dst_qn="b", path="p.py")
    try:
        edge.edge_type = "CALLS"  # type: ignore[misc]
    except (AttributeError, TypeError):
        pass
    else:
        raise AssertionError("GraphEdgeDraft must be frozen")


def test_explicit_unmapped_language_returns_module_only() -> None:
    """An explicit language with no entry in _GRAMMARS still degrades gracefully."""
    frag = extract_graph("weird.cbl", "IDENTIFICATION DIVISION.", language="cobol")
    assert [n.label for n in frag.nodes] == ["Module"]
    assert frag.edges == []


def test_parser_failure_returns_module_only() -> None:
    """A tree-sitter parser exception (e.g. grammar load failure) never propagates."""
    with patch(
        "tree_sitter_language_pack.get_parser", side_effect=RuntimeError("grammar unavailable")
    ):
        frag = extract_graph("pkg/mod.py", _PY)
    assert [n.label for n in frag.nodes] == ["Module"]
    assert frag.edges == []


def test_graph_fragment_defaults_are_independent() -> None:
    """Default-constructed GraphFragments never share a mutable list (mutable-default bug)."""
    a = GraphFragment()
    b = GraphFragment()
    a.nodes.append(GraphNodeDraft("Module", "x", "x", "x"))
    assert b.nodes == []
