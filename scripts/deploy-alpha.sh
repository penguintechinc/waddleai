#!/usr/bin/env bash
# Retired: alpha is CI + ArgoCD now.
# CI (.github/workflows/alpha-build.yml, runner dal2-alpha) builds and pushes
# ghcr.io/penguintechinc/waddleai/<svc>:alpha-<epoch64>; ArgoCD deploys to dal2alpha.
set -euo pipefail
echo "ERROR: scripts/deploy-alpha.sh is retired. Alpha is built by CI (alpha-build.yml) and deployed by ArgoCD to dal2alpha." >&2
echo "       See docs/docs-site/docs/deployment/kubernetes.md (Alpha deploy flow)." >&2
exit 1
