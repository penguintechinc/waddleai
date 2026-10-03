"""Unit tests for `scripts/lint_gate.py` -- the ratchet lint/type gate itself.

The gate script sits outside `penguincode_cli` (not part of the installed
package, no `__init__.py` in `scripts/`), so it is loaded here by file path
via `importlib` rather than a normal package import -- each test gets its
own fresh module object (never registered in `sys.modules`) so monkeypatched
module-level globals (`RUFF_BASELINE_FILE`, `_run`, etc.) never leak between
tests.

Covers the three failure shapes Verification Integrity calls out for any
gate: a tool crash, a zero-files-examined "clean" false-positive, and the
real ratchet behavior (new finding vs. already-baselined finding) -- plus
`--write-baseline` actually writing what the next bare run then accepts.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_LINT_GATE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "lint_gate.py"


def _load_lint_gate() -> types.ModuleType:
    """Load a fresh, independent copy of `scripts/lint_gate.py`."""
    spec = importlib.util.spec_from_file_location("lint_gate_under_test", _LINT_GATE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lint_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """A fresh `lint_gate` module pointed at isolated, empty tmp baseline files."""
    module = _load_lint_gate()
    monkeypatch.setattr(module, "RUFF_BASELINE_FILE", tmp_path / ".ruff-baseline.txt")
    monkeypatch.setattr(module, "MYPY_BASELINE_FILE", tmp_path / ".mypy-baseline.txt")
    return module


def _fake_run(responses: dict[str, tuple[int, str]]):
    """Build a `_run` replacement keyed by the gated tool's first two argv tokens."""

    def _run(cmd: list[str]) -> tuple[int, str]:
        key = " ".join(cmd[:3])
        for prefix, result in responses.items():
            if key.startswith(prefix):
                return result
        raise AssertionError(f"unexpected command in test: {cmd}")

    return _run


_CLEAN_RUFF_SHOW_FILES = (0, "a.py\nb.py\n")
_CLEAN_RUFF_CHECK = (0, "")
_CLEAN_RUFF_FORMAT = (0, "2 files already formatted\n")
_CLEAN_MYPY = (0, "Success: no issues found in 5 source files\n")


class TestToolCrash:
    """A non-{0,1} exit is a crash, never a silent clean pass."""

    def test_ruff_show_files_crash_raises(self, lint_gate: types.ModuleType) -> None:
        lint_gate._run = _fake_run({"ruff check --show-files": (2, "usage error")})
        with pytest.raises(lint_gate.GateToolCrashedError, match="exited 2"):
            lint_gate._ruff_show_files()

    def test_mypy_crash_raises(self, lint_gate: types.ModuleType) -> None:
        lint_gate._run = _fake_run({"mypy --strict": (2, "Duplicate module named")})
        with pytest.raises(lint_gate.GateToolCrashedError, match="exited 2"):
            lint_gate._mypy_findings()

    def test_mypy_missing_summary_line_raises(self, lint_gate: types.ModuleType) -> None:
        """Exit 0/1 but no Found/Success line -- still not a trustworthy result."""
        lint_gate._run = _fake_run({"mypy --strict": (1, "some unrelated stderr noise\n")})
        with pytest.raises(lint_gate.GateToolCrashedError, match="no Found/Success"):
            lint_gate._mypy_findings()

    def test_mypy_summary_with_no_digits_raises(self, lint_gate: types.ModuleType) -> None:
        """A Found/Success line present but with no parseable file count is still a crash."""
        lint_gate._run = _fake_run(
            {"mypy --strict": (0, "Success: no issues found in source files\n")}
        )
        with pytest.raises(lint_gate.GateToolCrashedError, match="no file count"):
            lint_gate._mypy_findings()

    def test_ruff_check_findings_crash_raises(self, lint_gate: types.ModuleType) -> None:
        lint_gate._run = _fake_run({"ruff check --output-format=concise": (2, "usage error")})
        with pytest.raises(lint_gate.GateToolCrashedError, match="exited 2"):
            lint_gate._ruff_check_findings()

    def test_ruff_format_findings_crash_raises(self, lint_gate: types.ModuleType) -> None:
        lint_gate._run = _fake_run({"ruff format --check": (2, "usage error")})
        with pytest.raises(lint_gate.GateToolCrashedError, match="exited 2"):
            lint_gate._ruff_format_findings()


class TestZeroFilesExaminedIsAFailure:
    """Zero examined must read as FAIL, never as a clean pass."""

    def test_zero_ruff_files_fails_main(self, lint_gate: types.ModuleType) -> None:
        lint_gate._run = _fake_run(
            {
                "ruff check --show-files": (0, ""),
                "ruff check --output-format=concise": _CLEAN_RUFF_CHECK,
                "ruff format --check": _CLEAN_RUFF_FORMAT,
                "mypy --strict": _CLEAN_MYPY,
            }
        )
        assert lint_gate.main() == 1

    def test_zero_mypy_files_fails_main(self, lint_gate: types.ModuleType) -> None:
        lint_gate._run = _fake_run(
            {
                "ruff check --show-files": _CLEAN_RUFF_SHOW_FILES,
                "ruff check --output-format=concise": _CLEAN_RUFF_CHECK,
                "ruff format --check": _CLEAN_RUFF_FORMAT,
                "mypy --strict": (0, "Success: no issues found in 0 source files\n"),
            }
        )
        assert lint_gate.main() == 1


class TestBaselineRatchet:
    """The actual gate behavior: new findings fail, already-known ones don't."""

    def _patch_clean(self, lint_gate: types.ModuleType) -> None:
        lint_gate._run = _fake_run(
            {
                "ruff check --show-files": _CLEAN_RUFF_SHOW_FILES,
                "ruff check --output-format=concise": _CLEAN_RUFF_CHECK,
                "ruff format --check": _CLEAN_RUFF_FORMAT,
                "mypy --strict": _CLEAN_MYPY,
            }
        )

    def test_missing_baseline_raises(self, lint_gate: types.ModuleType) -> None:
        self._patch_clean(lint_gate)
        with pytest.raises(lint_gate.GateToolCrashedError, match="missing"):
            lint_gate.main()

    def test_matches_baseline_exactly_passes(self, lint_gate: types.ModuleType) -> None:
        lint_gate._run = _fake_run(
            {
                "ruff check --show-files": _CLEAN_RUFF_SHOW_FILES,
                "ruff check --output-format=concise": (
                    0,
                    "a.py:1:1: F401 unused import\n",
                ),
                "ruff format --check": _CLEAN_RUFF_FORMAT,
                "mypy --strict": _CLEAN_MYPY,
            }
        )
        lint_gate.RUFF_BASELINE_FILE.write_text("check|a.py:1:1: F401 unused import\n")
        lint_gate.MYPY_BASELINE_FILE.write_text("")

        assert lint_gate.main() == 0

    def test_new_finding_not_in_baseline_fails(self, lint_gate: types.ModuleType) -> None:
        lint_gate._run = _fake_run(
            {
                "ruff check --show-files": _CLEAN_RUFF_SHOW_FILES,
                "ruff check --output-format=concise": (
                    0,
                    "a.py:1:1: F401 unused import\nb.py:2:2: E501 line too long\n",
                ),
                "ruff format --check": _CLEAN_RUFF_FORMAT,
                "mypy --strict": _CLEAN_MYPY,
            }
        )
        # Baseline only knows about the F401 finding -- E501 is new.
        lint_gate.RUFF_BASELINE_FILE.write_text("check|a.py:1:1: F401 unused import\n")
        lint_gate.MYPY_BASELINE_FILE.write_text("")

        assert lint_gate.main() == 1

    def test_new_mypy_error_fails(self, lint_gate: types.ModuleType) -> None:
        lint_gate._run = _fake_run(
            {
                "ruff check --show-files": _CLEAN_RUFF_SHOW_FILES,
                "ruff check --output-format=concise": _CLEAN_RUFF_CHECK,
                "ruff format --check": _CLEAN_RUFF_FORMAT,
                "mypy --strict": (
                    0,
                    "a.py:1: error: new problem [assignment]\n"
                    "Found 1 error in 1 file (checked 5 source files)\n",
                ),
            }
        )
        lint_gate.RUFF_BASELINE_FILE.write_text("")
        lint_gate.MYPY_BASELINE_FILE.write_text("")

        assert lint_gate.main() == 1

    def test_fixable_summary_line_is_not_a_finding(self, lint_gate: types.ModuleType) -> None:
        """ruff's trailing '[*] N fixable with --fix' line must never count as a finding."""
        lint_gate._run = _fake_run(
            {
                "ruff check --show-files": _CLEAN_RUFF_SHOW_FILES,
                "ruff check --output-format=concise": (
                    0,
                    "a.py:1:1: F401 unused import\n[*] 1 fixable with the `--fix` option.\n",
                ),
                "ruff format --check": _CLEAN_RUFF_FORMAT,
                "mypy --strict": _CLEAN_MYPY,
            }
        )
        lint_gate.RUFF_BASELINE_FILE.write_text("check|a.py:1:1: F401 unused import\n")
        lint_gate.MYPY_BASELINE_FILE.write_text("")

        assert lint_gate.main() == 0


class TestWriteBaseline:
    """`--write-baseline` must capture exactly what a subsequent bare run accepts."""

    def test_write_then_rerun_passes(
        self, lint_gate: types.ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lint_gate._run = _fake_run(
            {
                "ruff check --show-files": _CLEAN_RUFF_SHOW_FILES,
                "ruff check --output-format=concise": (
                    0,
                    "a.py:1:1: F401 unused import\n",
                ),
                "ruff format --check": (
                    0,
                    "a.py:1:1: unformatted: File would be reformatted\n",
                ),
                "mypy --strict": (
                    0,
                    "a.py:1: error: some pre-existing debt [assignment]\n"
                    "Found 1 error in 1 file (checked 5 source files)\n",
                ),
            }
        )
        monkeypatch.setattr(sys, "argv", ["lint_gate.py", "--write-baseline"])

        assert lint_gate.main() == 0
        assert lint_gate.RUFF_BASELINE_FILE.read_text().splitlines() == [
            "check|a.py:1:1: F401 unused import",
            "format|a.py:1:1: unformatted: File would be reformatted",
        ]
        assert lint_gate.MYPY_BASELINE_FILE.read_text().splitlines() == [
            "a.py:1: error: some pre-existing debt [assignment]",
        ]

        monkeypatch.setattr(sys, "argv", ["lint_gate.py"])
        assert lint_gate.main() == 0
