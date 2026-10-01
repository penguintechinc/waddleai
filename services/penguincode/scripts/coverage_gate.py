#!/usr/bin/env python3
"""Two-tier coverage gate for penguincode.

Reads the coverage.py data file already produced by `pytest --cov
--cov-branch` (never re-runs the test suite itself) and enforces two
independent thresholds against it:

  Tier A (platform, 90% line + 90% branch): the explicit knowledge-platform
  module list in TIER_A_PATHS below -- not a directory glob. An earlier
  version of this gate used whole-directory globs (e.g. `server/**`,
  `client/**`) and silently swept in legacy, non-platform code sharing
  those directories (server/main.py, server/services/{chat,health,tools}.py,
  client/{grpc_client,model_manager,org_manager}.py, docs_rag/fetcher.py --
  none of it this cycle's work), which understated Tier A's true number.
  TIER_A_PATHS is the single source of truth for scope; this is the real,
  committed quality bar for new knowledge-platform work.

  Tier B (whole-package ratchet floor): everything else, gated against the
  value in `.coverage-floor` next to this script. That file can only move
  up as legacy modules (core/repl.py and friends) are brought under test --
  it exists so the one unavoidably-low whole-package number never regresses
  further, without pretending the legacy debt is paid off today.

A gate that cannot fail is not a gate (critical-rules.md Verification
Integrity): both tiers assert a non-zero file/statement denominator before
trusting any percentage, and the script exits non-zero on any failure --
no `|| true`, no swallowed exit code.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import coverage
from coverage.exceptions import NoDataError

SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPT_DIR.parent
FLOOR_FILE = PACKAGE_ROOT / ".coverage-floor"

TIER_A_MIN_PERCENT = 90.0

# The knowledge-platform module set for this cycle -- the ONE place Tier A
# scope is defined. Entries ending in "/" are directory prefixes (whole
# subtree in scope); everything else is an exact file path. New
# knowledge-platform modules MUST be added here explicitly -- do not widen
# an entry back into a bare top-level directory glob (e.g. "server/",
# "client/", "docs_rag/"), which is exactly what previously swept in
# unrelated legacy code (see module docstring above).
TIER_A_PATHS: tuple[str, ...] = (
    "penguincode_cli/stores/",
    "penguincode_cli/graphs/",
    "penguincode_cli/retrieval/",
    "penguincode_cli/lessons/",
    "penguincode_cli/auth/",
    "penguincode_cli/flags/",
    "penguincode_cli/observability/",
    "penguincode_cli/db/",
    "penguincode_cli/docs_rag/indexer.py",
    "penguincode_cli/docs_rag/injector.py",
    "penguincode_cli/server/services/knowledge.py",
    "penguincode_cli/server/services/lessons.py",
    "penguincode_cli/server/interceptors.py",
    "penguincode_cli/client/knowledge_client.py",
    "penguincode_cli/client/lessons_client.py",
    "penguincode_cli/client/waddleai_auth.py",
    "penguincode_cli/tools/memory.py",
)


def is_tier_a(relpath: str) -> bool:
    """Return True if `relpath` (coverage.json key, repo-relative) matches a TIER_A_PATHS entry."""
    for entry in TIER_A_PATHS:
        if entry.endswith("/"):
            if relpath.startswith(entry):
                return True
        elif relpath == entry:
            return True
    return False


def load_coverage_json() -> dict[str, Any]:
    """Load the current coverage data file and render it as the `coverage json` structure.

    Honours `COVERAGE_FILE` exactly as coverage.py does natively, so this
    script never collides with another process's data file. Uses the
    `[tool.coverage]` settings from pyproject.toml (branch=True, source,
    omit) via `config_file=True`, matching how pytest-cov collected the data.
    """
    cov = coverage.Coverage(config_file=True)
    cov.load()
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="penguincode-coverage-gate-", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        try:
            cov.json_report(outfile=str(tmp_path))
        except NoDataError as exc:
            # coverage.py deletes its own output file on this failure path
            # (report_core.render_report's file_be_gone) -- nothing left to
            # clean up here, and this is itself the zero-examined failure
            # Verification Integrity calls out, not a bug in this script.
            print(f"::error::coverage data file has no data to report: {exc}")
            sys.exit(1)
        with tmp_path.open(encoding="utf-8") as fh:
            data: dict[str, Any] = json.load(fh)
            return data
    finally:
        tmp_path.unlink(missing_ok=True)


def combined_percent(
    covered_lines: int, statements: int, covered_branches: int, branches: int
) -> float:
    """Compute coverage.py's own combined line+branch percentage.

    This is the exact formula behind `coverage report --fail-under` and
    `totals.percent_covered` in `coverage json` -- (covered_lines +
    covered_branches) / (statements + branches) * 100 -- so both tiers here
    gate on the same number a human would see running `coverage report`.
    """
    denominator = statements + branches
    if denominator == 0:
        return 0.0
    return (covered_lines + covered_branches) / denominator * 100


def summarize_tier_a(files: dict[str, Any]) -> tuple[int, int, int, int, int]:
    """Sum statement/branch counts across every Tier-A file.

    Returns (files_examined, statements, covered_statements, branches,
    covered_branches).
    """
    files_examined = 0
    statements = 0
    covered_statements = 0
    branches = 0
    covered_branches = 0
    for relpath, entry in files.items():
        if not is_tier_a(relpath):
            continue
        summary = entry["summary"]
        files_examined += 1
        statements += summary["num_statements"]
        covered_statements += summary["covered_lines"]
        branches += summary["num_branches"]
        covered_branches += summary["covered_branches"]
    return files_examined, statements, covered_statements, branches, covered_branches


def read_floor() -> int:
    """Read the whole-package ratchet floor from `.coverage-floor`.

    A missing or malformed floor file is a configuration error, not a
    pass -- fail loudly rather than silently defaulting to 0.
    """
    if not FLOOR_FILE.exists():
        print(f"::error::{FLOOR_FILE} is missing -- Tier B has no floor to enforce")
        sys.exit(1)
    raw = FLOOR_FILE.read_text(encoding="utf-8").strip()
    try:
        return int(raw)
    except ValueError:
        print(f"::error::{FLOOR_FILE} contents {raw!r} is not an integer percentage")
        sys.exit(1)


def main() -> int:
    """Run both tiers and return the process exit code (0 pass, 1 fail)."""
    report = load_coverage_json()
    files = report["files"]
    totals = report["totals"]

    overall_ok = True

    # --- Tier A: platform modules, 90% line + 90% branch ---
    a_files, a_statements, a_covered, a_branches, a_covered_branches = summarize_tier_a(files)
    print("=== Tier A: knowledge-platform modules (90% line + 90% branch) ===")
    print(f"scope: {len(TIER_A_PATHS)} TIER_A_PATHS entries -> {a_files} files examined")
    if a_files == 0 or a_statements == 0:
        print(
            f"::error::Tier A examined {a_files} files / {a_statements} statements -- "
            "zero examined is a FAIL, not a pass"
        )
        overall_ok = False
    else:
        a_combined_pct = combined_percent(a_covered, a_statements, a_covered_branches, a_branches)
        a_branch_pct = (a_covered_branches / a_branches * 100) if a_branches else 0.0
        print(f"statements examined: {a_statements} ({a_covered} covered)")
        print(
            f"combined coverage:    {a_combined_pct:.2f}%  "
            f"(threshold {TIER_A_MIN_PERCENT:.0f}%)  -- matches `coverage report`'s percent_covered"
        )
        print(f"branches examined:   {a_branches} ({a_covered_branches} covered)")
        print(f"branch coverage:      {a_branch_pct:.2f}%  (threshold {TIER_A_MIN_PERCENT:.0f}%)")
        if a_combined_pct < TIER_A_MIN_PERCENT or a_branch_pct < TIER_A_MIN_PERCENT:
            print("::error::Tier A FAILED -- platform modules below the 90% line/branch bar")
            overall_ok = False
        else:
            print("Tier A PASSED")

    # --- Tier B: whole-package ratchet floor ---
    floor = read_floor()
    b_statements = totals["num_statements"]
    b_covered = totals["covered_lines"]
    b_branches = totals["num_branches"]
    b_covered_branches = totals["covered_branches"]
    b_files = len(files)
    print(f"\n=== Tier B: whole-package ratchet floor ({floor}%) ===")
    if b_files == 0 or b_statements == 0:
        print(
            f"::error::Tier B examined {b_files} files / {b_statements} statements -- "
            "zero examined is a FAIL, not a pass"
        )
        overall_ok = False
    else:
        b_combined_pct = combined_percent(b_covered, b_statements, b_covered_branches, b_branches)
        print(f"files examined:      {b_files}")
        print(f"statements examined: {b_statements} ({b_covered} covered)")
        print(
            f"combined coverage:    {b_combined_pct:.2f}%  (floor {floor}%)  -- matches "
            "`coverage report`'s percent_covered"
        )
        print(
            f"ratchet floor {floor}% -- raise {FLOOR_FILE.name} as legacy debt is "
            "paid down; target 90"
        )
        if b_combined_pct < floor:
            print(
                "::error::Tier B FAILED -- whole-package coverage dropped below its ratchet floor"
            )
            overall_ok = False
        else:
            print("Tier B PASSED")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
