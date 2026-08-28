"""Guard against the legacy WebSockets MCP server re-appearing (Q#5).

The legacy WS-MCP (`management/apps/mcp_server/`, `examples/mcp_client_example.py`,
`tests/unit/test_mcp_interface.py`) has no consumers and was deleted with no
compat window; this worktree's repo layout (`services/management/`, no
top-level `management/` or `examples/`) already has no such paths, so the
"deletion" step is a no-op here -- this guard makes that permanent.

Deliberately narrow on `shared/utils/mcp_interface.py`: the proxy-memory
branch (merging ahead of this one) adds a *new*, legitimate
`shared/utils/mcp_interface.py` with an `MCPServer` class for
`scratchpad_put/get/list` -- unrelated to the legacy WebSockets transport.
Grepping for `MCPServer(` generically would false-positive against that
file once merged, so this guard matches transport-specific legacy symbols
(`websockets.serve(`, `MCP_PORT`, `handle_client(`) instead of the class
name alone.
"""

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# Transport-specific legacy WS-MCP symbols only -- see module docstring on
# why a bare "MCPServer(" grep would collide with the legitimate
# scratchpad MCPServer landing via the proxy-memory branch.
LEGACY_SYMBOLS = ("websockets.serve(", "MCP_PORT", "handle_client(", "create_mcp_server(")

SEARCH_DIRS = ("shared", "services/management", "examples", "proxy")


def test_legacy_management_apps_mcp_server_path_does_not_exist():
    """Legacy management apps mcp server path does not exist."""
    assert not (REPO_ROOT / "management" / "apps" / "mcp_server").exists()


def test_legacy_management_apps_mcp_server_main_unimportable():
    """Legacy management apps mcp server main unimportable."""
    with pytest.raises(ImportError):
        importlib.import_module("management.apps.mcp_server.main")


def test_legacy_examples_mcp_client_example_does_not_exist():
    """Legacy examples mcp client example does not exist."""
    assert not (REPO_ROOT / "examples" / "mcp_client_example.py").exists()


def test_legacy_test_mcp_interface_does_not_exist():
    """Legacy test mcp interface does not exist."""
    assert not (REPO_ROOT / "tests" / "unit" / "test_mcp_interface.py").exists()


def test_no_legacy_websockets_mcp_symbols_anywhere():
    """No legacy websockets mcp symbols anywhere.

    Pure-Python file scan (not a `grep` subprocess) -- no dependency on an
    external binary being on PATH, and no subprocess/shell surface to
    reason about at all.
    """
    hits: list[str] = []
    for search_dir in SEARCH_DIRS:
        base = REPO_ROOT / search_dir
        if not base.exists():
            continue
        for py_file in base.rglob("*.py"):
            text = py_file.read_text(encoding="utf-8", errors="ignore")
            for needle in LEGACY_SYMBOLS:
                if needle in text:
                    hits.append(f"{py_file.relative_to(REPO_ROOT)}: {needle!r}")
    assert hits == [], "legacy WS-MCP symbol(s) found:\n" + "\n".join(hits)
