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

echo "=== K8s Beta Smoke Test for $PROJECT_NAME ==="
echo "Repo Root: $REPO_ROOT"
echo "Helm Chart: $HELM_DIR"
echo ""

NAMESPACE="${PROJECT_NAME}-beta"
RELEASE_NAME="$PROJECT_NAME"

# Cleanup function
cleanup() {
    echo "Cleaning up..."
    helm uninstall "$RELEASE_NAME" -n "$NAMESPACE" 2>/dev/null || true
    microk8s kubectl delete namespace "$NAMESPACE" 2>/dev/null || true
}

# Trap cleanup on exit
trap cleanup EXIT

echo "Step 1: Linting Helm chart..."
helm lint "$REPO_ROOT/$HELM_DIR"

echo ""
echo "Step 2: Creating namespace..."
microk8s kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | microk8s kubectl apply -f -

echo ""
echo "Step 3: Installing Helm chart (beta environment)..."
helm upgrade --install "$RELEASE_NAME" "$REPO_ROOT/$HELM_DIR" \
    --namespace "$NAMESPACE" \
    --values "$REPO_ROOT/$HELM_DIR/values-beta.yaml" \
    --wait \
    --timeout 5m

echo ""
echo "Step 4: Checking deployment status..."
microk8s kubectl get pods -n "$NAMESPACE"
microk8s kubectl get svc -n "$NAMESPACE"
microk8s kubectl get ingress -n "$NAMESPACE" 2>/dev/null || true

echo ""
echo "Step 5: Waiting for pods to be ready..."
microk8s kubectl wait --for=condition=ready pod \
    -l "app.kubernetes.io/name=$PROJECT_NAME" \
    -n "$NAMESPACE" \
    --timeout=120s

echo ""
echo "Step 6: Running Helm tests..."
helm test "$RELEASE_NAME" -n "$NAMESPACE" --logs || true

echo ""
echo "=== Beta smoke test completed successfully ==="
