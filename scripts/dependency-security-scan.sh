#!/bin/bash
set -euo pipefail

# Dependency & secrets security gate: gitleaks (secrets), pip-audit (Python
# CVEs), npm audit (Node CVEs), gosec/govulncheck (Go CVEs, when a go.mod is
# present). Deliberately excludes bandit (SAST) and pip-licenses (OSI gate)
# -- both already run as their own gates elsewhere (root `make test-security`
# bandit block; CI docker-build.yml `test` job) -- so this script is the
# single source of truth for the dependency/secret half of `make
# test-security` and is also invoked directly by CI's `dependency-security`
# job, which previously ran none of these three scanners at all
# (critical-rules.md Verification Integrity: a gate CI never calls is not a
# gate).
#
# Usage: scripts/dependency-security-scan.sh
# Env:
#   VENV              - path to the venv providing pip-audit (default .venv)
#   PIP_AUDIT_IGNORES - extra --ignore-vuln args passed through to pip-audit

VENV="${VENV:-.venv}"
PIP_AUDIT_IGNORES="${PIP_AUDIT_IGNORES:-}"
fail=0

command -v gitleaks >/dev/null 2>&1 || { echo "!! MISSING TOOL: gitleaks -- cannot verify, counting as FAILURE"; fail=1; }

if command -v gitleaks >/dev/null 2>&1; then
  echo "-- gitleaks --"
  gitleaks detect --source . --no-git --redact --config .gitleaks.toml \
    --exit-code 1 --log-level error || fail=1
fi

echo "-- pip-audit --"
# Prefer the project venv (local dev / `make test-security`); fall back to
# whatever `pip-audit` is on PATH (CI's dependency-security job installs it
# directly into the runner's Python, no venv) -- so this one script serves
# both callers without either duplicating it or forcing CI to build a full
# venv just to run one tool.
if [ -x "$VENV/bin/pip-audit" ]; then
  PIP_AUDIT_BIN="$VENV/bin/pip-audit"
elif command -v pip-audit >/dev/null 2>&1; then
  PIP_AUDIT_BIN="$(command -v pip-audit)"
else
  PIP_AUDIT_BIN=""
fi
if [ -n "$PIP_AUDIT_BIN" ]; then
  for r in requirements.txt proxy/requirements.txt services/management/requirements.txt; do
    tmp=$(mktemp)
    counts=$(awk -v target="en-core-web-lg" -v outfile="$tmp" '{is_start=(length($0)>0 && substr($0,1,1) !~ /[ \t#]/); if (is_start) {name=$0; sub(/[ \t@=\[].*/,"",name); gsub(/_/,"-",name); name=tolower(name); skip=(name==target); if (skip) excluded++; else count++} if (!skip) print > outfile} END{print count+0, excluded+0}' "$r")
    set -- $counts; audited=$1; excluded=$2
    echo "pip-audit: $audited requirements audited, $excluded excluded ($r) -- en_core_web_lg is a spaCy model wheel from github.com/explosion release, hash-pinned in $r, no PyPI entry"
    if [ "$audited" -eq 0 ]; then echo "!! pip-audit: 0 requirements audited in $r -- filter produced an empty file, counting as FAILURE"; fail=1; fi
    "$PIP_AUDIT_BIN" -r "$tmp" --strict $PIP_AUDIT_IGNORES || fail=1
    rm -f "$tmp"
  done
else
  echo "!! pip-audit not found in $VENV/bin or PATH -- run 'make venv' (local) or install pip-audit (CI); counting as FAILURE"; fail=1
fi

if [ -n "$(find . -name go.mod -not -path './.venv/*' -not -path '*/vendor/*' -not -path './.worktrees/*' -not -path './services/penguincode/*')" ]; then
  for t in gosec govulncheck; do
    command -v "$t" >/dev/null 2>&1 || { echo "!! Go present but $t MISSING -- FAILURE"; fail=1; }
  done
  for t in gosec govulncheck; do
    command -v "$t" >/dev/null 2>&1 && find . -name go.mod -not -path './.venv/*' -not -path '*/vendor/*' -not -path './.worktrees/*' -not -path './services/penguincode/*' -print0 \
      | xargs -0 -r -I{} dirname {} | xargs -r -I{} sh -c "cd {} && $t ./..." || fail=1
  done
else
  echo "-- gosec/govulncheck -- (no go.mod outside vendor; skipped legitimately)"
fi

echo "-- npm audit --"
pkg_count=0
while IFS= read -r -d '' pkg_json; do
  d="$(dirname "$pkg_json")"
  pkg_count=$((pkg_count + 1))
  if [ -f "$d/package-lock.json" ]; then
    (cd "$d" && npm audit --audit-level=high) || fail=1
  else
    echo "!! $d has package.json but NO package-lock.json -- dependency pinning violation (critical-rules.md); counting as FAILURE"; fail=1
  fi
done < <(find . -name package.json -maxdepth 3 -not -path './.git/*' -not -path './.venv/*' -not -path './.worktrees/*' -not -path '*/node_modules/*' -print0)
echo "npm audit: $pkg_count package.json director$([ "$pkg_count" -eq 1 ] && echo y || echo ies) examined"
if [ "$pkg_count" -eq 0 ]; then echo "!! npm audit: 0 package.json found -- find scoped wrong, counting as FAILURE"; fail=1; fi

[ "$fail" -eq 0 ] || { echo "=== DEPENDENCY SECURITY SCAN FAILED ==="; exit 1; }
echo "=== dependency security scan clean ==="
