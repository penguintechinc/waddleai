#!/usr/bin/env bash
# Helm render tests with an anti-skip gate (A8): a skipped render is not a pass, and a run that
# examined zero tests is a failure. Usage: scripts/test_helm.sh [python]
set -euo pipefail
py="${1:-python3}"
out="$(mktemp)"
trap 'rm -f "$out"' EXIT

set +e
"$py" -m pytest tests/helm -o addopts="" -p no:warnings -q -rs 2>&1 | tee "$out"
rc="${PIPESTATUS[0]}"
set -e

summary="$(grep -E '^(=+ )?[0-9]+ (passed|failed|skipped|error).* in ' "$out" | tail -n1 || true)"
passed="$(grep -oE '[0-9]+ passed' <<<"$summary" | grep -oE '[0-9]+' || echo 0)"
skipped="$(grep -oE '[0-9]+ skipped' <<<"$summary" | grep -oE '[0-9]+' || echo 0)"
echo "test-helm: passed=${passed} skipped=${skipped} pytest-exit=${rc}"

[ "$rc" -eq 0 ] || { echo "FAIL: pytest exited ${rc}"; exit "$rc"; }
[ "$passed" -gt 0 ] || { echo "FAIL: zero helm tests passed (nothing was examined)"; exit 1; }
[ "$skipped" -eq 0 ] || { echo "FAIL: ${skipped} helm tests skipped (is helm installed?)"; exit 1; }
