"""Tests for shared.knowledge.code_graph: Python EXTENDS/CALLS/REFERENCES/Field (Task 10)."""

from __future__ import annotations

from shared.knowledge.code_graph import extract_graph

_PY = """
class Base:
    pass

class Derived(Base):
    kind = "d"
    def run(self):
        return helper()

def helper():
    return 1
"""


def _edges(frag, edge_type: str) -> set[tuple[str, str]]:
    return {(e.src_qn, e.dst_qn) for e in frag.edges if e.edge_type == edge_type}


def test_extends_edge() -> None:
    """A class's identifier base classes become EXTENDS edges to their bare name."""
    frag = extract_graph("m.py", _PY)
    assert ("Derived", "Base") in _edges(frag, "EXTENDS")


def test_field_node_and_contains() -> None:
    """A class-body top-level assignment target becomes a Field node + CONTAINS edge."""
    frag = extract_graph("m.py", _PY)
    assert ("Field", "Derived.kind") in {(n.label, n.qualified_name) for n in frag.nodes}
    assert ("Derived", "Derived.kind") in _edges(frag, "CONTAINS")


def test_calls_edge_intrafile() -> None:
    """An unqualified call resolved to a known top-level function becomes a CALLS edge."""
    frag = extract_graph("m.py", _PY)
    assert ("Derived.run", "helper") in _edges(frag, "CALLS")


def test_unresolved_external_call_skipped() -> None:
    """A call to a name with no matching node in this file is skipped, not guessed."""
    frag = extract_graph("m.py", "def f():\n    external_thing()\n")
    assert _edges(frag, "CALLS") == set()


def test_references_edge_for_constructor_call() -> None:
    """A call to a known class name (construction) becomes REFERENCES, not CALLS."""
    src = "class Base:\n    pass\n\ndef make():\n    return Base()\n"
    frag = extract_graph("m.py", src)
    assert ("make", "Base") in _edges(frag, "REFERENCES")
    assert _edges(frag, "CALLS") == set()


def test_method_call_via_self_resolves_to_class_method() -> None:
    """``self.other()`` resolves to ``Class.other`` when that method exists in-file."""
    src = (
        "class Widget:\n"
        "    def run(self):\n"
        "        return self.other()\n"
        "    def other(self):\n"
        "        return 1\n"
    )
    frag = extract_graph("m.py", src)
    assert ("Widget.run", "Widget.other") in _edges(frag, "CALLS")


def test_dotted_call_from_non_method_is_unresolved() -> None:
    """A dotted call (``os.path.join()``) from a plain function has no enclosing class.

    ``enclosing_class`` is None there, so the attribute-call resolution
    path is deliberately not taken -- no CALLS/REFERENCES edge, not a
    guess.
    """
    frag = extract_graph("m.py", "def f():\n    os.path.join()\n")
    assert _edges(frag, "CALLS") == set()
    assert _edges(frag, "REFERENCES") == set()


def test_no_new_edge_types_on_source_with_no_such_constructs() -> None:
    """A file with no bases/fields/calls emits only CONTAINS -- no Task 9 regression."""
    src = "def top():\n    pass\n\nclass C:\n    def m(self):\n        pass\n"
    frag = extract_graph("mod.py", src)
    assert {e.edge_type for e in frag.edges} == {"CONTAINS"}


def test_other_language_unaffected_by_python_edge_pass() -> None:
    """A non-Python source keeps Task 9's node/CONTAINS-only behavior -- no regression."""
    js_src = "class Widget {\n  render(ctx) {\n    return ctx;\n  }\n}\n"
    frag = extract_graph("widget.js", js_src, language="javascript")
    assert {e.edge_type for e in frag.edges} == {"CONTAINS"}
    assert all(n.label != "Field" for n in frag.nodes)
