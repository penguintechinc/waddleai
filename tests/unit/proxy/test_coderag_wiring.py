"""Regression guard: proxy/apps/proxy_server/main.py must not wire the CodeRAG no-op.

Same grep-based technique as tests/unit/management/test_scope_authz.py's
test_no_require_role_outside_tests -- catches the sources={} no-op (or a
NotWiredKnowledgeService default) being reintroduced.
"""

from __future__ import annotations

from pathlib import Path

from shared.knowledge.coderag_backend import (
    CodeKnowledgeSourceAdapter,
    build_code_knowledge_sources,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MAIN_PY = REPO_ROOT / "proxy" / "apps" / "proxy_server" / "main.py"


def test_main_py_does_not_wire_the_sources_noop() -> None:
    """main.py must call build_code_knowledge_sources, not construct sources={} directly."""
    text = MAIN_PY.read_text()
    assert "sources={}" not in text
    assert "build_code_knowledge_sources" in text


def test_main_py_passes_a_real_mcp_service_factory() -> None:
    """MCPMount must be constructed with a service_factory, not the all-stub default."""
    text = MAIN_PY.read_text()
    assert "service_factory=" in text
    assert "CodeRagKnowledgeService" in text


def test_build_code_knowledge_sources_is_independently_testable() -> None:
    """The factory function itself returns a working 'code' source without booting the app."""
    sources = build_code_knowledge_sources(db=object())
    assert isinstance(sources["code"], CodeKnowledgeSourceAdapter)
