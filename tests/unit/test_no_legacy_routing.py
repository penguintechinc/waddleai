"""Retirement guard (spec §7.6, Task 14): the legacy routing systems stay dead.

Asserts the three retired legacy routing systems are actually gone --
un-importable and absent from non-test source -- so a future change can
never silently resurrect them alongside shared.routing.RoutingEngine.
"""

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("shared", "services", "proxy")

_RETIRED_MODULES = (
    "shared.agents.routing_agent",
    "shared.agents.routing_matrix",
    "shared.agents.mf_classifier",
)

_RETIRED_SYMBOLS = (
    "RoutingAgent",
    "RoutingMatrix",
    "RoutingMatrixEntry",
    "MatrixFactorizationClassifier",
)

# Patterns for actual CODE usage (imports, calls, dict/string literals) of
# the retired modules/classes/hardcoded dict/NL-instructions key --
# deliberately narrower than a bare substring match on the plan's Task 14
# Step 4 verification command, which would also flag this file's own
# docstrings and every "why we retired X" comment left behind as
# documentation (expected and desired -- see shared/routing/__init__.py,
# shared/agents/__init__.py, shared/routing/grpc_adapter.py,
# services/management/app/api/v1/routing_*.py).
_FORBIDDEN_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"^\s*(from|import)\s+shared\.agents\.routing_agent\b",
        r"^\s*(from|import)\s+shared\.agents\.routing_matrix\b",
        r"^\s*(from|import)\s+shared\.agents\.mf_classifier\b",
        r"^\s*from\s+shared\.agents\s+import\s+.*\b"
        r"(RoutingAgent|RoutingMatrix|RoutingMatrixEntry|MatrixFactorizationClassifier)\b",
        r"def _load_model_configs\b",
        r"MatrixFactorizationClassifier\(",
        r'redis_client\.(get|set)\("routing:instructions"',
    )
)


class TestRetiredModulesAreUnimportable:
    """The three retired routing_agent/routing_matrix/mf_classifier modules are gone."""

    @pytest.mark.parametrize("module_name", _RETIRED_MODULES)
    def test_module_is_unimportable(self, module_name: str) -> None:
        """Each retired module raises ImportError."""
        with pytest.raises(ImportError):
            __import__(module_name)

    def test_symbols_not_exported_from_shared_agents(self) -> None:
        """Confirm shared.agents no longer exports any retired symbol."""
        import shared.agents as agents_pkg

        for symbol in _RETIRED_SYMBOLS:
            assert not hasattr(agents_pkg, symbol), f"{symbol} should no longer be exported"
            assert symbol not in agents_pkg.__all__


def _scan_for_forbidden_code(base_dir: Path) -> list[str]:
    """Return 'path:lineno:content' for every forbidden-pattern match under base_dir."""
    hits: list[str] = []
    for py_file in base_dir.rglob("*.py"):
        if "/tests/" in str(py_file).replace("\\", "/"):
            continue
        text = py_file.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(pattern.search(line) for pattern in _FORBIDDEN_PATTERNS):
                hits.append(f"{py_file}:{lineno}:{line.strip()}")
    return hits


class TestNoLegacyReferencesInSource:
    """Non-test source never references the retired modules/dict/Redis key again."""

    def test_no_forbidden_code_patterns_outside_tests(self) -> None:
        """Every forbidden CODE pattern (not docstrings) returns zero non-test hits.

        Narrower than the plan's Task 14 Step 4 bare-substring verification
        command (which also matches this file's own docstrings and the
        retirement rationale left behind as comments -- see the module
        docstring above); this asserts the retired modules are never
        actually imported/called/re-seeded again, while still allowing
        "why we retired X" documentation to exist.
        """
        hits: list[str] = []
        for dir_name in _SCAN_DIRS:
            hits.extend(_scan_for_forbidden_code(_REPO_ROOT / dir_name))
        assert hits == [], "Legacy routing references found outside tests:\n" + "\n".join(hits)
