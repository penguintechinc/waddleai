#!/usr/bin/env bash
# Workspace coverage gate: single source of the excludes + threshold, shared by CI and local runs.
# Generated SeaORM entities are excluded (verified by the entity_drift gate, not unit tests).
set -euo pipefail
cd "$(dirname "$0")/.."
exec cargo llvm-cov --workspace --locked --fail-under-lines 90 \
  --ignore-filename-regex "${COV_IGNORE_REGEX:-crates/db/src/entities/}" "$@"
