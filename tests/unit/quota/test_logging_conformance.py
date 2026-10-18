"""Logging conformance (testing.md): no hand-rolled print/basicConfig in quota service code."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FILES = sorted(
    [*ROOT.glob("shared/quota/*.py"),
     ROOT / "shared/utils/conversion_rate_store.py",
     ROOT / "shared/utils/default_conversion_rates.py",
     ROOT / "proxy/apps/proxy_server/quota_hooks.py",
     ROOT / "proxy/apps/proxy_server/quota_sweeper.py",
     ROOT / "proxy/apps/proxy_server/pipeline/quota_stage.py",
     ROOT / "services/management/app/services/quota_pools.py",
     ROOT / "services/management/app/services/quota_invalidation.py",
     ROOT / "services/management/app/api/v1/quota_pools.py",
     ROOT / "services/management/app/api/v1/team_quota.py"]
)  # fmt: skip


def _hand_rolled(tree: ast.AST) -> list[str]:
    """print(...) calls and logging.basicConfig(...) calls found in a module."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "print":
                found.append(f"print@{node.lineno}")
            if isinstance(fn, ast.Attribute) and fn.attr == "basicConfig":
                found.append(f"basicConfig@{node.lineno}")
    return found


def test_scanner_examined_the_files_and_found_no_hand_rolled_logging():
    """A scanner on the wrong root reports clean, so the examined count is itself asserted."""
    assert len(FILES) >= 15, f"scanned only {len(FILES)} files"
    assert all(f.exists() for f in FILES), [str(f) for f in FILES if not f.exists()]
    offenders = {
        str(f.relative_to(ROOT)): hits
        for f in FILES
        if (hits := _hand_rolled(ast.parse(f.read_text())))
    }
    assert offenders == {}, offenders


def test_modules_that_log_use_the_module_logger():
    """Every quota module that logs does so through logging.getLogger(__name__)."""
    users = [f for f in FILES if "logger." in f.read_text()]
    assert len(users) >= 8, f"only {len(users)} quota modules log"
    assert all("logging.getLogger(__name__)" in f.read_text() for f in users)
