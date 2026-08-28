#!/bin/bash
# Deploy to Beta - waddleai
#
# Beta images are CI-built only -- built and pushed on every merge to main, to
# ghcr.io/penguintechinc/waddleai/<service> by .github/workflows/docker-build.yml
# (tag: beta-<epoch64>). This script never builds or pushes images -- it only
# deploys a CI-built tag via Helm. Pass the tag to deploy with --tag; the image
# registry itself is chart-managed (k8s/helm/waddleai/values-beta.yaml
# global.imageRegistry). dal2-beta may be temporarily offline -- see
# docs/docs-site/docs/deployment/kubernetes.md Release pipeline for status.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

KUBE_CONTEXT="${KUBE_CONTEXT:-dal2-beta}"
NAMESPACE="${NAMESPACE:-waddleai}"
RELEASE_NAME="waddleai"
CHART_PATH="$PROJECT_ROOT/k8s/helm/waddleai"
VALUES_FILE="$CHART_PATH/values-beta.yaml"
APP_HOST="waddleai.penguintech.cloud"
SERVICES=(management proxy webui)

DRY_RUN=0
ROLLBACK=0
SERVICE=""
IMAGE_TAG=""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_section() { echo ""; echo -e "${BLUE}========================================${NC}"; echo -e "${BLUE}$1${NC}"; echo -e "${BLUE}========================================${NC}"; echo ""; }

check_prerequisites() {
    log_section "Checking Prerequisites"
    for cmd in kubectl helm; do
        if ! command -v "$cmd" &>/dev/null; then
            log_error "$cmd is not installed"
            return 1
        fi
        log_info "$cmd: found"
    done

    if ! kubectl config get-contexts "$KUBE_CONTEXT" &>/dev/null; then
        log_error "Kubernetes context '$KUBE_CONTEXT' not found"
        echo "Available contexts:"
        kubectl config get-contexts --output=name
        return 1
    fi
    log_info "context: $KUBE_CONTEXT"

    if [ ! -f "$VALUES_FILE" ]; then
        log_error "Helm values file not found: $VALUES_FILE"
        return 1
    fi
}

do_deploy() {
    log_section "Deploying with Helm"

    if [ -z "$IMAGE_TAG" ]; then
        log_error "No image tag supplied. Beta images are CI-built only (ghcr.io/penguintechinc/waddleai/<service>:beta-<epoch64>) -- pass one with --tag"
        return 1
    fi

    local helm_args=("upgrade" "--install" "$RELEASE_NAME" "$CHART_PATH" \
        "--kube-context=$KUBE_CONTEXT" "--namespace=$NAMESPACE" "--create-namespace" \
        "--values=$VALUES_FILE" "--wait" "--timeout=10m")

    for svc in "${SERVICES[@]}"; do
        if [ -n "$SERVICE" ] && [ "$SERVICE" != "$svc" ]; then continue; fi
        helm_args+=("--set=${svc}.image.tag=$IMAGE_TAG")
    done

    if [ "$DRY_RUN" -eq 1 ]; then
        helm_args+=("--dry-run")
        log_warn "DRY RUN MODE"
    fi

    log_info "Running: helm ${helm_args[*]}"
    helm "${helm_args[@]}"
}

do_rollback() {
    log_section "Rolling Back"
    log_info "Running: helm rollback $RELEASE_NAME --kube-context=$KUBE_CONTEXT -n $NAMESPACE"
    helm rollback "$RELEASE_NAME" --kube-context="$KUBE_CONTEXT" -n "$NAMESPACE"
    log_info "Rollback completed"
}

verify_deployment() {
    if [ "$DRY_RUN" -eq 1 ]; then return 0; fi
    log_section "Verifying Deployment"
    kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" rollout status deployment/"$RELEASE_NAME" --timeout=300s
    kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" get pods
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --tag=*) IMAGE_TAG="${1#*=}"; shift;;
        --tag) IMAGE_TAG="$2"; shift 2;;
        --service=*) SERVICE="${1#*=}"; shift;;
        --service) SERVICE="$2"; shift 2;;
        --dry-run) DRY_RUN=1; shift;;
        --rollback) ROLLBACK=1; shift;;
        -h|--help) echo "Usage: $0 --tag=TAG [--service=SVC] [--dry-run] [--rollback]"; exit 0;;
        *) log_error "Unknown: $1"; exit 1;;
    esac
done

main() {
    log_section "WaddleAI - Beta Deployment"
    check_prerequisites || exit 1
    if [ "$ROLLBACK" -eq 1 ]; then do_rollback; exit $?; fi
    do_deploy || exit 3
    verify_deployment
    log_section "Deployment Summary"
    echo -e "${GREEN}✓${NC} Release: $RELEASE_NAME"
    echo -e "${GREEN}✓${NC} Namespace: $NAMESPACE"
    echo -e "${GREEN}✓${NC} Tag: $IMAGE_TAG"
    echo -e "${GREEN}✓${NC} URL: https://$APP_HOST"
    log_info "Deployment complete!"
}

main
