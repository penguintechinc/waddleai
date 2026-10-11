#!/usr/bin/env python3
"""Ratchet lint/type gate for penguincode: fail on any lint/type finding not already known.

Mirrors the root ``scripts/mypy-gate.sh`` ratchet pattern (and this
package's own ``scripts/coverage_gate.py`` two-tier approach) for the same
reason: ``services/penguincode/Makefile``'s ``lint`` target ran flake8/
black/isort/mypy with every single tool call piped through ``|| true`` --
a gate that can never fail is not a gate (critical-rules.md Verification
Integrity), and it masked real debt (measured here: 87 ``ruff check``
findings, 61 ``ruff format --check`` files, 613 ``mypy --strict`` errors in
``penguincode_cli`` -- see ``.ruff-baseline.txt``/``.mypy-baseline.txt``).

This script does NOT fix that debt and does NOT re-mask the gate -- it
freezes the debt as a committed baseline and fails hard on anything new:

- ``ruff check --output-format=concise`` over the whole package tree
  (matches the pre-existing ``make lint`` scope) -- findings stored with a
  ``check|`` prefix in ``.ruff-baseline.txt``.
- ``ruff format --check --output-format=concise`` over the same tree --
  findings stored with a ``format|`` prefix in the same file (one file, two
  check kinds, so ``COMMON.md``'s two-baseline contract still holds: one
  baseline per *tool*, not per *subcommand*).
- ``mypy --strict penguincode_cli`` -- scoped to the actual installed
  package (``pyproject.toml``'s ``packages = ["penguincode_cli"]``, the same
  scope ``coverage_gate.py`` uses) rather than the whole directory, because
  this repo also carries a vendored copy of the monorepo's ``shared/py_libs``
  that makes mypy crash outright on a duplicate module name when pointed at
  ``.`` -- that crash is itself caught below (a "Found"/"Success" summary
  line failing to appear is treated as a hard failure, never a silent
  zero-findings pass). (Historical note: three stray root-level non-package
  scripts -- ``app.py``, ``client.py``, ``server/app.py`` -- used to live
  here too and contributed to this same crash; they were unreferenced dead
  code with real bandit findings (Flask ``debug=True``, no-timeout HTTP) and
  were deleted outright rather than carried forward, see RELEASE_NOTES.md.)

No ``subprocess`` call result is trusted by exit code alone: each tool's own
summary output (ruff's ``--show-files`` list, mypy's ``Found ... source
files`` / ``Success: ... source files`` line) is parsed to assert a
non-zero files-examined denominator before any diff is trusted -- a
scanner silently pointed at the wrong path, or a crashed tool run, reports
clean otherwise (critical-rules.md Verification Integrity).

Usage::

    python3 scripts/lint_gate.py                # gate: exit 1 on new findings
    python3 scripts/lint_gate.py --write-baseline  # regenerate both baselines
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPT_DIR.parent

RUFF_BASELINE_FILE = PACKAGE_ROOT / ".ruff-baseline.txt"
MYPY_BASELINE_FILE = PACKAGE_ROOT / ".mypy-baseline.txt"

#: Scope for `ruff check` / `ruff format --check` -- the whole package tree,
#: matching the Makefile's pre-existing (if previously masked) `lint` scope.
RUFF_TARGETS: tuple[str, ...] = (".",)

#: Scope for `mypy --strict` -- the one real installed package only. See the
#: module docstring for why `.` crashes mypy outright in this repo.
MYPY_TARGETS: tuple[str, ...] = ("penguincode_cli",)

_CHECK_PREFIX = "check|"
_FORMAT_PREFIX = "format|"


class GateToolCrashedError(RuntimeError):
    """Raised when a gated tool did not produce a trustworthy result.

    Covers both an outright crash (non-{0,1} exit with no parseable summary)
    and the zero-files-examined case -- both read as a false "clean" pass
    unless explicitly rejected here.
    """


def _run(cmd: list[str]) -> tuple[int, str]:
    """Run `cmd` from `PACKAGE_ROOT`, returning (exit_code, combined_stdout+stderr).

    Never raises on the tool's own non-zero exit -- ruff/mypy exit 1 when
    they find violations, which is the expected, common case handled by the
    caller's own parsing, not a subprocess failure.
    """
    proc = subprocess.run(  # noqa: S603 -- fixed, trusted argv, no shell
        cmd,
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _ruff_show_files() -> list[str]:
    """Return the list of files `ruff check` would examine under `RUFF_TARGETS`.

    Used only as the files-examined denominator -- `--show-files` prints the
    exact scan population independent of whether any violation was found,
    which `ruff check`'s own diagnostic output does not (a fully clean run
    prints nothing at all).
    """
    code, out = _run(["ruff", "check", "--show-files", *RUFF_TARGETS])
    if code not in (0, 1):
        raise GateToolCrashedError(f"ruff check --show-files exited {code}:\n{out}")
    files = [line for line in out.splitlines() if line.strip()]
    return files


def _ruff_check_findings() -> list[str]:
    """Current `ruff check` findings, one per line, `check|`-prefixed and sorted."""
    code, out = _run(["ruff", "check", "--output-format=concise", *RUFF_TARGETS])
    if code not in (0, 1):
        raise GateToolCrashedError(f"ruff check exited {code}:\n{out}")
    findings = [
        f"{_CHECK_PREFIX}{line}"
        for line in out.splitlines()
        if line
        and not line.startswith(("warning:", "Found ", "No fixes"))
        and "fixable with" not in line
    ]
    return sorted(findings)


def _ruff_format_findings() -> list[str]:
    """Current `ruff format --check` findings, one per file, `format|`-prefixed and sorted."""
    code, out = _run(["ruff", "format", "--check", "--output-format=concise", *RUFF_TARGETS])
    if code not in (0, 1):
        raise GateToolCrashedError(f"ruff format --check exited {code}:\n{out}")
    findings = [f"{_FORMAT_PREFIX}{line}" for line in out.splitlines() if "unformatted:" in line]
    return sorted(findings)


def _mypy_findings() -> tuple[list[str], int]:
    """Current `mypy --strict` error lines (sorted) and the files-examined count.

    Both summary shapes mypy prints carry the file count and must be
    accepted: ``Found N errors in M files (checked K source files)`` when
    errors exist, ``Success: no issues found in K source files`` when clean
    -- matching only the first would make a fully clean run read as zero
    files examined and fail the denominator guard below for the wrong
    reason.
    """
    code, out = _run(["mypy", "--strict", *MYPY_TARGETS])
    if code not in (0, 1):
        raise GateToolCrashedError(f"mypy --strict exited {code}:\n{out}")
    summary = next(
        (line for line in out.splitlines() if line.startswith(("Found ", "Success:"))),
        None,
    )
    if summary is None:
        raise GateToolCrashedError(f"mypy --strict produced no Found/Success summary line:\n{out}")
    digits = "".join(c if c.isdigit() else " " for c in summary.split("source file")[0])
    tokens = digits.split()
    if not tokens:
        raise GateToolCrashedError(f"mypy --strict summary has no file count: {summary!r}")
    checked = int(tokens[-1])
    errors = sorted(line for line in out.splitlines() if ": error:" in line)
    return errors, checked


def _load_baseline(path: Path) -> set[str]:
    """Read a committed baseline file as a set of exact finding lines."""
    if not path.exists():
        print(f"::error::{path} is missing -- run with --write-baseline to create it")
        raise GateToolCrashedError(f"{path} missing")
    return {line for line in path.read_text(encoding="utf-8").splitlines() if line}


def _write_baseline(path: Path, lines: list[str]) -> None:
    """Commit the current finding set as the new baseline, one line each, sorted."""
    path.write_text("\n".join(sorted(lines)) + ("\n" if lines else ""), encoding="utf-8")
    print(f"lint-gate: wrote {len(lines)} lines to {path}")


def main() -> int:
    """Run both gates (ruff, mypy); print examined/new counts; return the exit code."""
    write_baseline = "--write-baseline" in sys.argv[1:]

    ruff_files = _ruff_show_files()
    if len(ruff_files) == 0:
        print("::error::ruff check examined 0 files -- zero examined is a FAIL, not a pass")
        return 1
    ruff_current = _ruff_check_findings() + _ruff_format_findings()

    mypy_errors, mypy_checked = _mypy_findings()
    if mypy_checked == 0:
        print("::error::mypy --strict examined 0 source files -- zero examined is a FAIL")
        return 1

    if write_baseline:
        _write_baseline(RUFF_BASELINE_FILE, ruff_current)
        _write_baseline(MYPY_BASELINE_FILE, mypy_errors)
        return 0

    ruff_baseline = _load_baseline(RUFF_BASELINE_FILE)
    mypy_baseline = _load_baseline(MYPY_BASELINE_FILE)

    new_ruff = sorted(set(ruff_current) - ruff_baseline)
    new_mypy = sorted(set(mypy_errors) - mypy_baseline)

    print(
        f"lint-gate: ruff examined {len(ruff_files)} files, "
        f"{len(ruff_current)} known findings (baseline: {len(ruff_baseline)}), "
        f"{len(new_ruff)} new"
    )
    print(
        f"lint-gate: mypy examined {mypy_checked} source files, "
        f"{len(mypy_errors)} known errors (baseline: {len(mypy_baseline)}), "
        f"{len(new_mypy)} new"
    )

    if not new_ruff and not new_mypy:
        print("lint-gate: PASS -- no new findings")
        return 0

    if new_ruff:
        print("lint-gate: FAIL -- new ruff findings not present in .ruff-baseline.txt:")
        for line in new_ruff:
            print(f"  {line}")
    if new_mypy:
        print("lint-gate: FAIL -- new mypy errors not present in .mypy-baseline.txt:")
        for line in new_mypy:
            print(f"  {line}")
    print(
        "lint-gate: fix the finding(s) above, or if intentional, regenerate the "
        "baseline: python3 scripts/lint_gate.py --write-baseline"
    )
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GateToolCrashedError as exc:
        print(f"::error::{exc}")
        sys.exit(1)
