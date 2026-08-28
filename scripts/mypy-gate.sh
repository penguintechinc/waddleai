#!/bin/bash
# mypy-gate.sh — ratchet gate for mypy: fail on any type error mypy did not
# already report at HEAD, mirroring the coverage ratchet in .coveragerc.
#
# `mypy-baseline.txt` (repo root) is the committed set of currently-known
# error lines. This script re-runs mypy, diffs its "error:" lines against
# that baseline, and fails if any line is new. Baseline entries are kept as
# full "path:line: error: message [code]" text -- line numbers are NOT
# stripped, so an unrelated edit that shifts a downstream error's line
# number will read as "new" and require regenerating the baseline. That is
# a deliberate simplicity/false-positive tradeoff, not an oversight: fuzzy
# matching (message-only, or code-only per file) hides real regressions as
# often as it avoids busywork re-baselines.
#
# Bash 3.2 compatible: no `declare -A`, no `mapfile`, no `&>>`.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

PY="${PY:-.venv/bin/python}"
if [ ! -x "$PY" ]; then
  PY="python3"
fi

BASELINE_FILE="mypy-baseline.txt"
if [ ! -f "$BASELINE_FILE" ]; then
  echo "mypy-gate: FAIL -- $BASELINE_FILE is missing; run mypy and commit a baseline first" >&2
  exit 1
fi

RAW_OUT="$(mktemp)"
trap 'rm -f "$RAW_OUT"' EXIT

# mypy exits 1 when it finds type errors -- that is the expected, common
# case here and is handled below via the baseline diff, not treated as a
# hard command failure.
set +e
"$PY" -m mypy proxy services shared scripts tests --ignore-missing-imports >"$RAW_OUT" 2>&1
set -e

SUMMARY_LINE="$(grep -E '^(Found|Success)' "$RAW_OUT" | tail -1 || true)"

if [ -z "$SUMMARY_LINE" ]; then
  echo "mypy-gate: FAIL -- mypy did not complete a run (no Found/Success summary line)" >&2
  tail -20 "$RAW_OUT" >&2
  exit 1
fi

CHECKED_COUNT="$(printf '%s\n' "$SUMMARY_LINE" | grep -oE 'checked [0-9]+ source file' | grep -oE '[0-9]+' || true)"

if [ -z "$CHECKED_COUNT" ] || [ "$CHECKED_COUNT" -eq 0 ]; then
  echo "mypy-gate: FAIL -- mypy examined zero source files (module-path error or a broken lint-path/exclude config)" >&2
  tail -20 "$RAW_OUT" >&2
  exit 1
fi

CURRENT_ERRORS_FILE="$(mktemp)"
BASELINE_SORTED_FILE="$(mktemp)"
NEW_ERRORS_FILE="$(mktemp)"
trap 'rm -f "$RAW_OUT" "$CURRENT_ERRORS_FILE" "$BASELINE_SORTED_FILE" "$NEW_ERRORS_FILE"' EXIT

grep -E ': error:' "$RAW_OUT" | sort >"$CURRENT_ERRORS_FILE" || true
sort "$BASELINE_FILE" >"$BASELINE_SORTED_FILE"

comm -23 "$CURRENT_ERRORS_FILE" "$BASELINE_SORTED_FILE" >"$NEW_ERRORS_FILE" || true

CURRENT_ERROR_COUNT="$(wc -l <"$CURRENT_ERRORS_FILE" | tr -d ' ')"
NEW_ERROR_COUNT="$(wc -l <"$NEW_ERRORS_FILE" | tr -d ' ')"

echo "mypy-gate: examined $CHECKED_COUNT source files, $CURRENT_ERROR_COUNT known errors (baseline: $(wc -l <"$BASELINE_SORTED_FILE" | tr -d ' ')), $NEW_ERROR_COUNT new"

if [ "$NEW_ERROR_COUNT" -gt 0 ]; then
  echo "mypy-gate: FAIL -- new type errors not present in $BASELINE_FILE:" >&2
  cat "$NEW_ERRORS_FILE" >&2
  echo "mypy-gate: fix the error(s) above, or if intentional, regenerate the baseline:" >&2
  echo "  $PY -m mypy proxy services shared scripts tests --ignore-missing-imports 2>&1 | grep -E ': error:' | sort > $BASELINE_FILE" >&2
  exit 1
fi

echo "mypy-gate: PASS -- no new errors"
