"""Guards against test modules that silently vanish from collection.

tests/unit/test_memory_integration.py and tests/unit/test_token_manager.py
used to wrap a *first-party* import in try/except ImportError ->
pytest.skip(allow_module_level=True). When the production module was
refactored and the import genuinely broke, pytest treated it exactly like an
intentionally-missing optional dependency: the whole file vanished from
collection, reported as one ordinary "skipped" line, and the suite stayed
green for months while zero tests in either file ever ran.

This module statically walks every file under tests/unit/, finds every
module-level skip mechanism (pytest.importorskip, pytest.skip(...,
allow_module_level=True), pytestmark = pytest.mark.skip(...), and try/except
ImportError blocks around an import), and for anything guarding a
shared./services./proxy./apps. import, re-executes that import for real. A
first-party import failing is a bug in the test or the production code --
never a legitimate reason to skip. Third-party optional dependencies (heavy
ML/codegen libs deliberately left out of the lean CI test image) are
allowlisted explicitly, by package name, with a reason; an unlisted
third-party importorskip target fails the guard too, so a new optional
dependency has to be allowlisted on purpose, not silently.

Only each file's top-level statements are inspected. A pytest.importorskip()
call inside a fixture or test function skips just that one test -- already
visible as an ordinary per-test skip in the run summary -- not the whole
module's collection, so it isn't the failure mode this guard exists for.
"""

from __future__ import annotations

import ast
import importlib
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_UNIT_DIR = REPO_ROOT / "tests" / "unit"

FIRST_PARTY_PREFIXES = ("shared", "services", "proxy", "apps")

# Third-party packages that may legitimately be absent from this venv -- heavy
# ML/codegen dependencies deliberately left out of the lean CI test image.
# Every entry needs a reason; see the module docstring for why unlisted
# third-party targets fail the guard instead of being silently ignored.
ALLOWLISTED_OPTIONAL_DEPS: dict[str, str] = {
    "sentence_transformers": "embedding model lib -- not installed in the lean CI test image",
    "torch": "pulled in transitively by sentence_transformers/heavy ML deps -- same reason",
    "chromadb": "optional vectorstore backend, not part of the default test image",
    "grpc_tools": "protobuf/gRPC codegen tool, dev-only, not part of the runtime test image",
}


@dataclass(slots=True)
class ImportCheck:
    """One import that a module-level skip guard would silently swallow if it failed.

    `names` lists the symbols pulled from `module` (empty for a bare `import
    module` or an importorskip target with no attribute-level check to make).
    """

    file: Path
    lineno: int
    module: str
    names: tuple[str, ...] = ()


@dataclass(slots=True)
class OptionalDepCheck:
    """A pytest.importorskip(...) target that isn't a first-party module.

    Must appear in ALLOWLISTED_OPTIONAL_DEPS with a reason, or the guard
    treats it as an unreviewed skip and fails.
    """

    file: Path
    lineno: int
    package: str


@dataclass(slots=True)
class UnconditionalSkipMark:
    """A module-level `pytestmark = pytest.mark.skip(...)` (not skipif).

    Every legitimate environment-conditional skip in this repo already uses
    skipif with a documented condition; an unconditional mark left in a
    committed file is either dead code or a masked failure.
    """

    file: Path
    lineno: int


def _is_pytest_attr(node: ast.expr | None, attr: str) -> bool:
    """True if `node` is the plain attribute access `pytest.<attr>`."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == "pytest"
    )


def _is_pytest_mark_attr(node: ast.expr | None, attr: str) -> bool:
    """True if `node` is the two-level attribute access `pytest.mark.<attr>`."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "pytest"
    )


def _first_party_module(name: str | None) -> bool:
    """True if a dotted module path belongs to this repo's first-party code."""
    if not name:
        return False
    return name.split(".", 1)[0] in FIRST_PARTY_PREFIXES


def _handler_catches_import_error(handler: ast.ExceptHandler) -> bool:
    """True if an except clause catches ImportError -- bare, named, or tupled."""
    if handler.type is None:
        return True
    candidates = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(n, ast.Name) and n.id == "ImportError" for n in candidates)


def _handler_calls_module_level_skip(handler: ast.ExceptHandler) -> bool:
    """True if an except-block calls pytest.skip(..., allow_module_level=True)."""
    for node in ast.walk(handler):
        if isinstance(node, ast.Call) and _is_pytest_attr(node.func, "skip"):
            for kw in node.keywords:
                if (
                    kw.arg == "allow_module_level"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                ):
                    return True
    return False


def _scan_module(
    path: Path, source: str
) -> tuple[list[ImportCheck], list[OptionalDepCheck], list[UnconditionalSkipMark]]:
    """Find every module-level skip mechanism in one test file's top-level statements."""
    tree = ast.parse(source, filename=str(path))
    import_checks: list[ImportCheck] = []
    optional_dep_checks: list[OptionalDepCheck] = []
    unconditional_marks: list[UnconditionalSkipMark] = []

    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            if _is_pytest_attr(call.func, "importorskip") and call.args:
                arg = call.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    target = arg.value
                    if _first_party_module(target):
                        import_checks.append(
                            ImportCheck(file=path, lineno=node.lineno, module=target)
                        )
                    else:
                        optional_dep_checks.append(
                            OptionalDepCheck(file=path, lineno=node.lineno, package=target)
                        )

        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
                value = node.value
                func = value.func if isinstance(value, ast.Call) else value
                if _is_pytest_mark_attr(func, "skip"):
                    unconditional_marks.append(UnconditionalSkipMark(file=path, lineno=node.lineno))

        elif isinstance(node, ast.Try):
            guarded = any(
                _handler_catches_import_error(h) and _handler_calls_module_level_skip(h)
                for h in node.handlers
            )
            if not guarded:
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and _first_party_module(stmt.module):
                    names = tuple(a.name for a in stmt.names)
                    import_checks.append(
                        ImportCheck(
                            file=path, lineno=stmt.lineno, module=stmt.module or "", names=names
                        )
                    )
                elif isinstance(stmt, ast.Import):
                    for alias in stmt.names:
                        if _first_party_module(alias.name):
                            import_checks.append(
                                ImportCheck(file=path, lineno=stmt.lineno, module=alias.name)
                            )

    return import_checks, optional_dep_checks, unconditional_marks


def _check_import(check: ImportCheck) -> None:
    """Actually perform the import a skip guard would otherwise swallow.

    Raises whatever exception the real import raises -- ModuleNotFoundError,
    ImportError, or AttributeError for a name that doesn't exist on an
    otherwise-importable module.
    """
    module = importlib.import_module(check.module)
    for name in check.names:
        if name != "*" and not hasattr(module, name):
            raise AttributeError(f"module {check.module!r} has no attribute {name!r}")


def _iter_test_files() -> list[Path]:
    """Every test module under tests/unit/, excluding bytecode caches."""
    return sorted(p for p in TESTS_UNIT_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_first_party_import_is_silently_skipped() -> None:
    """Every first-party import guarded by a module-level skip must actually succeed.

    See the module docstring for the historical bug this catches. Third-party
    importorskip targets must be explicitly allowlisted by name.
    """
    failures: list[str] = []
    total_checked = 0

    for path in _iter_test_files():
        source = path.read_text()
        import_checks, optional_dep_checks, _ = _scan_module(path, source)
        rel = path.relative_to(REPO_ROOT)

        for check in import_checks:
            total_checked += 1
            try:
                _check_import(check)
            except Exception as exc:  # noqa: BLE001 -- any failure here is the finding
                failures.append(
                    f"{rel}:{check.lineno}: first-party import {check.module!r} is guarded "
                    f"by a module-level skip but fails to import for real "
                    f"({type(exc).__name__}: {exc}) -- this is a bug the skip is hiding, "
                    f"not a legitimate reason to skip."
                )

        for dep in optional_dep_checks:
            total_checked += 1
            if dep.package not in ALLOWLISTED_OPTIONAL_DEPS:
                failures.append(
                    f"{rel}:{dep.lineno}: pytest.importorskip({dep.package!r}) targets a "
                    f"package not in ALLOWLISTED_OPTIONAL_DEPS -- add it there with a reason, "
                    f"or fix the underlying skip if it's actually first-party."
                )

    # A zero-item scan is a failure, not a pass: it means either every skip
    # guard vanished from the repo (great, update this comment and the
    # assertion) or the scanner itself broke. Known guards as of this
    # writing: shared.utils.llm_connectors's try/except (still present,
    # still importing cleanly), two sentence_transformers importorskip
    # calls, and one grpc_tools importorskip call.
    assert total_checked > 0, (
        "the scanner found zero module-level skip guards in tests/unit/ -- "
        "verify _scan_module still recognizes the patterns it's supposed to"
    )
    assert not failures, "\n" + "\n".join(failures)


def test_no_unconditional_module_level_skip_mark() -> None:
    """No test file may unconditionally skip itself via `pytestmark = pytest.mark.skip(...)`.

    Legitimate environment-conditional skips in this repo use `skipif` with a
    documented condition (e.g. tests/unit/management/test_migration_009b.py's
    alembic-chain check) -- an *unconditional* skip left in a committed file
    is either dead code that should be deleted or a masked failure.
    """
    failures = [
        f"{mark.file.relative_to(REPO_ROOT)}:{mark.lineno}: unconditional "
        f"`pytestmark = pytest.mark.skip(...)` -- use skipif with a documented "
        f"condition, or delete the dead file."
        for path in _iter_test_files()
        for mark in _scan_module(path, path.read_text())[2]
    ]
    assert not failures, "\n" + "\n".join(failures)


def test_scanner_flags_a_broken_first_party_try_except_guard(tmp_path: Path) -> None:
    """Prove _scan_module + _check_import flag a first-party import that fails.

    A synthetic file guards a nonexistent shared.* module behind the exact
    try/except ImportError -> pytest.skip(allow_module_level=True) pattern
    that hid test_memory_integration.py's and test_token_manager.py's
    breakage for months. If this test ever stops failing on its own (i.e. the
    assertions below stop holding), the guard logic itself is broken.
    """
    bad_file = tmp_path / "test_synthetic_broken_skip.py"
    bad_file.write_text(
        "import pytest\n"
        "try:\n"
        "    from shared.this_module_does_not_exist import Nope\n"
        "except ImportError as e:\n"
        "    pytest.skip(f'skipping: {e}', allow_module_level=True)\n"
    )
    import_checks, _, _ = _scan_module(bad_file, bad_file.read_text())
    assert len(import_checks) == 1
    assert import_checks[0].module == "shared.this_module_does_not_exist"
    with pytest.raises(ModuleNotFoundError):
        _check_import(import_checks[0])


def test_scanner_flags_an_unallowlisted_third_party_importorskip(tmp_path: Path) -> None:
    """A pytest.importorskip target that isn't first-party must be allowlisted by name."""
    f = tmp_path / "test_synthetic_unlisted_dep.py"
    f.write_text('import pytest\npytest.importorskip("some_totally_unlisted_package")\n')
    _, optional_dep_checks, _ = _scan_module(f, f.read_text())
    assert len(optional_dep_checks) == 1
    assert optional_dep_checks[0].package not in ALLOWLISTED_OPTIONAL_DEPS


def test_scanner_allows_an_allowlisted_third_party_importorskip(tmp_path: Path) -> None:
    """A pytest.importorskip target that IS allowlisted is recognized as such."""
    f = tmp_path / "test_synthetic_allowlisted_dep.py"
    f.write_text('import pytest\npytest.importorskip("sentence_transformers")\n')
    _, optional_dep_checks, _ = _scan_module(f, f.read_text())
    assert len(optional_dep_checks) == 1
    assert optional_dep_checks[0].package in ALLOWLISTED_OPTIONAL_DEPS


def test_scanner_flags_unconditional_skip_but_not_skipif(tmp_path: Path) -> None:
    """A bare `pytestmark = pytest.mark.skip(...)` is flagged; `skipif` is not."""
    unconditional = tmp_path / "test_synthetic_unconditional_skip.py"
    unconditional.write_text("import pytest\npytestmark = pytest.mark.skip(reason='dead file')\n")
    _, _, marks = _scan_module(unconditional, unconditional.read_text())
    assert len(marks) == 1

    conditional = tmp_path / "test_synthetic_conditional_skip.py"
    conditional.write_text(
        "import pytest\npytestmark = pytest.mark.skipif(True, reason='documented condition')\n"
    )
    _, _, conditional_marks = _scan_module(conditional, conditional.read_text())
    assert conditional_marks == []
