#!/usr/bin/env bash
# Runs `pytest tests/unit` and fails if either pytest itself fails, or the
# number of items it collected drops below tests/COLLECTED_FLOOR.
#
# Why this exists: tests/unit/test_memory_integration.py and
# tests/unit/test_token_manager.py used to guard a broken first-party import
# behind try/except ImportError -> pytest.skip(allow_module_level=True). The
# whole file vanished from collection, reported as one ordinary "skipped"
# line, and `pytest tests/unit` kept exiting 0 for months. A per-file guard
# (tests/unit/test_collection_guard.py) closes the specific pattern; this
# script is the second, independent layer -- a coarse denominator check that
# catches the same failure mode even if a *new* silent-collection bug shows
# up in a shape the per-file guard doesn't recognize yet.
#
# Used by both `make test-unit` (local dev) and the `test (3.13)` job in
# .github/workflows/docker-build.yml, so the two never drift.
#
# Usage: check_collected_floor.sh <python-binary> <pytest-args...>
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <python-binary> <pytest-args...>" >&2
  exit 2
fi

PY_BIN="$1"
shift

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLOOR_FILE="$SCRIPT_DIR/../tests/COLLECTED_FLOOR"
FLOOR="$(tr -d '[:space:]' < "$FLOOR_FILE")"

OUT_FILE="$(mktemp)"
trap 'rm -f "$OUT_FILE"' EXIT

# -rs (report skip reasons) is added here rather than in every caller, so
# both `make test-unit` and CI get skip visibility for free.
"$PY_BIN" -m pytest "$@" -rs 2>&1 | tee "$OUT_FILE"
PYTEST_STATUS="${PIPESTATUS[0]}"

# Not anchored to line start: -q prints "collected N items" as its own line,
# -v prints "collecting ... collected N items" on one line -- both must match.
COLLECTED="$(grep -m1 -E 'collected [0-9]+ item' "$OUT_FILE" | grep -oE '[0-9]+' | head -1 || true)"

if [ -z "$COLLECTED" ]; then
  echo "ERROR: no 'collected N items' line found in pytest output -- treating as a" >&2
  echo "failure (zero items examined is not a pass), not silently trusting pytest's exit code." >&2
  exit 1
fi

echo "collection floor check: collected $COLLECTED items (floor: $FLOOR, from tests/COLLECTED_FLOOR)"

if [ "$COLLECTED" -lt "$FLOOR" ]; then
  echo "ERROR: pytest collected only $COLLECTED items, below the floor of $FLOOR." >&2
  echo "A drop this large usually means a test module silently failed to collect" >&2
  echo "(e.g. a first-party import being swallowed by a module-level skip guard)." >&2
  echo "Investigate before lowering tests/COLLECTED_FLOOR." >&2
  exit 1
fi

exit "$PYTEST_STATUS"
