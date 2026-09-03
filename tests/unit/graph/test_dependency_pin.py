"""Regression tests for the exact-pinned `neo4j` async driver dependency.

Guards Task 1 of the graph platform Phase 1 plan: the `neo4j` package must
stay exact-pinned in requirements.in (never >=/~=) and must import as the
v5.x async driver that Tasks 4/5/14 build on.
"""

from __future__ import annotations

import pathlib
import re


def test_neo4j_pinned_exact_in_requirements_in() -> None:
    """requirements.in must pin neo4j with == to a 5.x version, not a range."""
    text = pathlib.Path("requirements.in").read_text()
    assert re.search(r"^neo4j==5\.\d+\.\d+\b", text, re.M), (
        "neo4j must be exact-pinned (==), not >=/~="
    )


def test_neo4j_importable_v5() -> None:
    """The neo4j package must import and expose the async driver entrypoint."""
    import neo4j

    assert neo4j.__version__.startswith("5."), neo4j.__version__
    assert hasattr(neo4j, "AsyncGraphDatabase")
