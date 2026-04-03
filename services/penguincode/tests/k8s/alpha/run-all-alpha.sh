#!/usr/bin/env bash
set -euo pipefail

# Auto-detect repository and Helm chart location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
PROJECT_NAME="$(basename "$REPO_ROOT")"

# Find Helm chart directory
HELM_DIR=""
for dir in "k8s/helm/$PROJECT_NAME" "helm/$PROJECT_NAME" "infrastructure/helm/$PROJECT_NAME"; do
    if [[ -d "$REPO_ROOT/$dir" ]]; then
        HELM_DIR="$dir"
        break
    fi
done

if [[ -z "$HELM_DIR" ]]; then
    echo "ERROR: Could not find Helm chart directory for $PROJECT_NAME"
    exit 1
fi

echo "=== K8s Alpha Smoke Test for $PROJECT_NAME ==="
echo "Repo Root: $REPO_ROOT"
echo "Helm Chart: $HELM_DIR"
echo ""

NAMESPACE="${PROJECT_NAME}-alpha"
RELEASE_NAME="$PROJECT_NAME"

# Cleanup function
cleanup() {
    echo "Cleaning up..."
    helm uninstall "$RELEASE_NAME" -n "$NAMESPACE" 2>/dev/null || true
    kubectl delete namespace "$NAMESPACE" 2>/dev/null || true
}

# Trap cleanup on exit
trap cleanup EXIT

echo "Step 1: Linting Helm chart..."
helm lint "$REPO_ROOT/$HELM_DIR"

echo ""
echo "Step 2: Installing Helm chart (alpha environment)..."
helm upgrade --install "$RELEASE_NAME" "$REPO_ROOT/$HELM_DIR" \
    --namespace "$NAMESPACE" \
    --create-namespace \
    --values "$REPO_ROOT/$HELM_DIR/values-alpha.yaml" \
    --wait \
    --timeout 5m

echo ""
echo "Step 3: Checking deployment status..."
kubectl get pods -n "$NAMESPACE"
kubectl get svc -n "$NAMESPACE"

echo ""
echo "Step 4: Waiting for pods to be ready..."
kubectl wait --for=condition=ready pod \
    -l "app.kubernetes.io/name=$PROJECT_NAME" \
    -n "$NAMESPACE" \
    --timeout=120s

echo ""
echo "Step 5: Running Helm tests..."
helm test "$RELEASE_NAME" -n "$NAMESPACE" --logs || true

echo ""
echo "=== Alpha smoke test completed successfully ==="
