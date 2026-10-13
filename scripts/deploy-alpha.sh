#!/usr/bin/env bash
# =============================================================================
# waddleai Alpha Deployment Script
# Local MicroK8s Deployment via Helm
#
# Serves both the pre-alpha tier (local, uncommitted work -- this script's default
# local build+import path) and the alpha tier (release/v{Major}.{Minor}.X branches,
# CI tag alpha-<epoch64>) -- both deploy to the shared local-alpha context and are
# destroyed and recreated on every deploy, never upgraded in place.
#
# Usage:
#   ./scripts/deploy-alpha.sh [OPTIONS]
#
# Options:
#   --build               Build Docker images and push to the local registry (default)
#   --skip-build          Skip Docker build, use existing images
#   --tag TAG             Image tag to use (default: alpha-latest; must match values-alpha.yaml)
#   --service SERVICE     Build/deploy specific service only
#   --dry-run             Show what would be deployed without applying
#   --rollback            Rollback deployments to previous revision
#   --help                Show this help message
#
# Environment:
#   KUBE_CONTEXT          Kubernetes context (default: local-alpha)
#   NAMESPACE             Target namespace (default: waddleai — product name only,
#                          never environment-suffixed)
#   APP_HOST              Application hostname (default: waddleai.localhost.local)
#   HELM_CHART_PATH        Helm chart path (default: k8s/helm/waddleai)
#   HELM_VALUES_FILE       Helm values file (default: k8s/helm/waddleai/values-alpha.yaml)
#
# =============================================================================

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"
readonly PROJECT_ROOT

readonly APP_NAME="${APP_NAME:-waddleai}"
readonly KUBE_CONTEXT="${KUBE_CONTEXT:-local-alpha}"
readonly NAMESPACE="${NAMESPACE:-waddleai}"
readonly APP_HOST="${APP_HOST:-waddleai.localhost.local}"
readonly HELM_CHART_PATH="${HELM_CHART_PATH:-k8s/helm/waddleai}"
readonly HELM_VALUES_FILE="${HELM_VALUES_FILE:-k8s/helm/waddleai/values-alpha.yaml}"

# Services with Dockerfiles (customize per repo)
declare -a SERVICES=("management" "proxy" "webui")

# Image name prefix (used for docker build tags)
readonly IMAGE_PREFIX="${APP_NAME}"

# Defaults
declare TAG="alpha-latest"
declare SERVICE_FILTER=""
declare SKIP_BUILD=false
declare DRY_RUN=false
declare DO_ROLLBACK=false

# =============================================================================
# Color output helpers
# =============================================================================

readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly NC='\033[0m'

print_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

print_success() {
    echo -e "${GREEN}[OK]${NC} $*"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $*" >&2
}

# =============================================================================
# kubectl wrapper (always uses --context)
# =============================================================================

kctl() {
    kubectl --context "${KUBE_CONTEXT}" "$@"
}

# =============================================================================
# Prerequisite checks
# =============================================================================

check_prerequisites() {
    print_info "Checking prerequisites..."
    local missing=()

    for cmd in kubectl docker helm; do
        if ! command -v "${cmd}" &>/dev/null; then
            missing+=("${cmd}")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        print_error "Missing required tools: ${missing[*]}"
        exit 1
    fi

    # Verify context exists
    if ! kubectl config get-contexts "${KUBE_CONTEXT}" &>/dev/null; then
        print_error "Kubernetes context '${KUBE_CONTEXT}' not found"
        echo "Available contexts:"
        kubectl config get-contexts --output=name
        exit 1
    fi

    # Verify cluster reachable
    if ! kctl cluster-info &>/dev/null; then
        print_error "Cannot reach cluster via context '${KUBE_CONTEXT}'"
        print_error "Is MicroK8s running? Try: microk8s status"
        exit 1
    fi

    # Verify Helm chart exists
    if [[ ! -f "${PROJECT_ROOT}/${HELM_CHART_PATH}/Chart.yaml" ]]; then
        print_error "Helm chart not found: ${HELM_CHART_PATH}"
        exit 1
    fi

    # Verify Helm values file exists
    if [[ ! -f "${PROJECT_ROOT}/${HELM_VALUES_FILE}" ]]; then
        print_error "Helm values file not found: ${HELM_VALUES_FILE}"
        exit 1
    fi

    print_success "All prerequisites satisfied"
}

# =============================================================================
# Docker build and push to the local registry (sudo-free)
# =============================================================================

# The cluster pulls from the local registry (values-alpha.yaml:
# global.imageRegistry = localhost:32000), so images are `docker push`ed there
# instead of imported with `microk8s ctr` (which needs sudo -- never used here).
readonly LOCAL_REGISTRY="${LOCAL_REGISTRY:-localhost:32000}"

# External postgres image: pgvector build, pinned by digest -- KEEP IN SYNC with
# k8s/helm/waddleai/values.yaml postgres.image. Mirrored (tag only, no digest) to
# the local registry as values-alpha.yaml expects.
readonly PGVECTOR_SRC="pgvector/pgvector:pg17@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d"
readonly PGVECTOR_DST="${LOCAL_REGISTRY}/pgvector/pgvector:pg17"

# Echo "<build-context>|<dockerfile>" (both relative to PROJECT_ROOT) for a service.
# management and proxy COPY shared/ and their own tree by repo-root-relative
# path (same as CI, .github/workflows/docker-build.yml), so their context is the
# repo root; webui is self-contained and builds from its own directory.
service_build_spec() {
    case "$1" in
        management) echo ".|services/management/Dockerfile" ;;
        proxy)      echo ".|proxy/Dockerfile" ;;
        webui)      echo "services/webui|services/webui/Dockerfile" ;;
        *)          return 1 ;;
    esac
}

build_and_import() {
    local service="$1"
    local tag="$2"
    local spec context dockerfile

    if ! spec="$(service_build_spec "${service}")"; then
        print_error "No build spec for service '${service}'"
        return 1
    fi
    context="${PROJECT_ROOT}/${spec%%|*}"
    dockerfile="${PROJECT_ROOT}/${spec##*|}"

    if [[ ! -f "${dockerfile}" ]]; then
        print_error "Dockerfile not found: ${dockerfile}"
        return 1
    fi

    local image_name="${IMAGE_PREFIX}/${service}:${tag}"
    local registry_image="${LOCAL_REGISTRY}/${image_name}"

    print_info "Building image: ${image_name} (context: ${context}, file: ${dockerfile})"
    if ! docker build \
        --file "${dockerfile}" \
        --tag "${image_name}" \
        --tag "${registry_image}" \
        --label "environment=alpha" \
        --label "timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "${context}"; then
        print_error "Failed to build ${service}"
        return 1
    fi

    print_info "Pushing ${registry_image}..."
    if ! docker push "${registry_image}"; then
        print_error "Failed to push ${registry_image} (is the MicroK8s registry addon enabled on ${LOCAL_REGISTRY}?)"
        return 1
    fi

    print_success "Built and pushed: ${registry_image}"
}

# Mirror the pgvector postgres image into the local registry (idempotent).
mirror_postgres_image() {
    print_info "Mirroring ${PGVECTOR_SRC} -> ${PGVECTOR_DST}"
    # pgvector/pgvector is a multi-arch manifest list. Pull+tag+push of the index
    # produces a local image record that `docker push` cannot resolve to a
    # platform ("found but does not provide any platform"). Building a one-line
    # FROM the same pinned digest yields a concrete single-platform image with
    # real layers that pushes cleanly. The digest pin is preserved.
    printf 'FROM %s\n' "${PGVECTOR_SRC}" \
        | docker build --platform linux/amd64 -t "${PGVECTOR_DST}" - \
        || { print_error "Failed to build ${PGVECTOR_DST} from ${PGVECTOR_SRC}"; return 1; }
    docker push "${PGVECTOR_DST}" || { print_error "Failed to push ${PGVECTOR_DST}"; return 1; }
    print_success "Mirrored: ${PGVECTOR_DST}"
}

# =============================================================================
# Helm deployment
# =============================================================================

do_deploy() {
    print_info "Deploying to local MicroK8s cluster..."
    print_info "  Context:   ${KUBE_CONTEXT}"
    print_info "  Namespace: ${NAMESPACE}"
    print_info "  Chart:     ${HELM_CHART_PATH}"
    print_info "  Values:    ${HELM_VALUES_FILE}"
    print_info "  Host:      ${APP_HOST}"

    # Subchart tarballs (gpu-operator) are gitignored (*.tgz), but Helm refuses to
    # render/install while a Chart.yaml dependency is missing from charts/ -- even
    # when its condition is off. Chart.lock pins the exact versions. Needs network.
    print_info "Building Helm chart dependencies (Chart.lock pinned)..."
    if ! helm dependency build "${PROJECT_ROOT}/${HELM_CHART_PATH}" >/dev/null; then
        print_error "helm dependency build failed (network access to helm.ngc.nvidia.com required)"
        return 1
    fi

    # Do NOT pre-create the namespace with kubectl: the chart templates its own
    # Namespace resource (PSA labels), and a kubectl-created one has no Helm
    # ownership metadata -> "cannot be imported into the current release".
    # `helm --create-namespace` below owns creation.

    # Render/apply the Helm chart
    if [[ "${DRY_RUN}" == "true" ]]; then
        print_info "DRY-RUN: Rendering Helm chart output..."
        helm template "${APP_NAME}" "${PROJECT_ROOT}/${HELM_CHART_PATH}" \
            --namespace "${NAMESPACE}" \
            --values "${PROJECT_ROOT}/${HELM_VALUES_FILE}" \
            --set "namespace=${NAMESPACE}"
        return 0
    fi

    if ! helm upgrade --install "${APP_NAME}" "${PROJECT_ROOT}/${HELM_CHART_PATH}" \
        --kube-context "${KUBE_CONTEXT}" \
        --namespace "${NAMESPACE}" \
        --create-namespace \
        --values "${PROJECT_ROOT}/${HELM_VALUES_FILE}" \
        --set "namespace=${NAMESPACE}"; then
        print_error "Failed to apply Helm chart"
        return 1
    fi

    print_success "Helm chart applied"
}

# =============================================================================
# Rollout verification
# =============================================================================

wait_for_rollout() {
    print_info "Waiting for deployments to roll out..."

    # A kubectl failure or an empty list is an ERROR, never "nothing to wait for":
    # a gate that cannot fail is not a gate (critical-rules.md Verification Integrity).
    local deployments
    if ! deployments=$(kctl get deployments -n "${NAMESPACE}" -o jsonpath='{.items[*].metadata.name}'); then
        print_error "Failed to list deployments in namespace ${NAMESPACE}"
        return 1
    fi

    if [[ -z "${deployments}" ]]; then
        print_error "No deployments found in namespace ${NAMESPACE} (0 examined) -- the install did not create any"
        return 1
    fi

    local failed=false
    for deploy in ${deployments}; do
        print_info "Waiting for deployment/${deploy}..."
        if ! kctl rollout status "deployment/${deploy}" -n "${NAMESPACE}" --timeout=300s; then
            print_error "Deployment ${deploy} failed to roll out"
            failed=true
        fi
    done

    # Also check statefulsets
    local statefulsets
    if ! statefulsets=$(kctl get statefulsets -n "${NAMESPACE}" -o jsonpath='{.items[*].metadata.name}'); then
        print_error "Failed to list statefulsets in namespace ${NAMESPACE}"
        return 1
    fi

    for sts in ${statefulsets}; do
        print_info "Waiting for statefulset/${sts}..."
        if ! kctl rollout status "statefulset/${sts}" -n "${NAMESPACE}" --timeout=300s; then
            print_error "StatefulSet ${sts} failed to roll out"
            failed=true
        fi
    done

    if [[ "${failed}" == "true" ]]; then
        return 1
    fi

    print_success "All workloads rolled out successfully ($(echo ${deployments} | wc -w | tr -d ' ') deployments checked)"
}

# =============================================================================
# Show status
# =============================================================================

show_status() {
    echo ""
    print_info "Pod Status:"
    kctl get pods -n "${NAMESPACE}" -o wide
    echo ""
    print_info "Services:"
    kctl get svc -n "${NAMESPACE}"
    echo ""
    print_info "Access URL: https://${APP_HOST}"
    echo ""
    print_info "Quick commands:"
    echo "  View pods:   kubectl --context ${KUBE_CONTEXT} get pods -n ${NAMESPACE}"
    echo "  View logs:   kubectl --context ${KUBE_CONTEXT} logs -n ${NAMESPACE} -l environment=alpha -f"
    echo "  Describe:    kubectl --context ${KUBE_CONTEXT} describe pods -n ${NAMESPACE}"
}

# =============================================================================
# Rollback
# =============================================================================

do_rollback() {
    print_warning "Rolling back deployments in ${NAMESPACE}..."

    local deployments
    if ! deployments=$(kctl get deployments -n "${NAMESPACE}" -o jsonpath='{.items[*].metadata.name}'); then
        print_error "Failed to list deployments in namespace ${NAMESPACE}"
        return 1
    fi

    if [[ -z "${deployments}" ]]; then
        print_error "No deployments found in namespace ${NAMESPACE}"
        return 1
    fi

    for deploy in ${deployments}; do
        print_info "Rolling back deployment/${deploy}..."
        kctl rollout undo "deployment/${deploy}" -n "${NAMESPACE}"
    done

    print_success "Rollback initiated"
    wait_for_rollout
}

# =============================================================================
# Help
# =============================================================================

show_help() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Deploy ${APP_NAME} to local MicroK8s alpha environment using Helm.

OPTIONS:
    --build               Build images and push to the local registry (default)
    --skip-build          Skip Docker build, use existing images
    --tag TAG             Image tag (default: alpha-latest; must match values-alpha.yaml)
    --service SERVICE     Build specific service only (e.g., management)
    --dry-run             Render manifests without applying
    --rollback            Rollback deployments to previous revision
    --help                Show this help message

ENVIRONMENT:
    KUBE_CONTEXT:      ${KUBE_CONTEXT}
    NAMESPACE:         ${NAMESPACE}
    APP_HOST:          ${APP_HOST}
    HELM_CHART_PATH:   ${HELM_CHART_PATH}
    HELM_VALUES_FILE:  ${HELM_VALUES_FILE}
    SERVICES:          ${SERVICES[*]}

EXAMPLES:
    # Full build and deploy
    $(basename "$0")

    # Deploy without rebuilding images
    $(basename "$0") --skip-build

    # Build and deploy only one service
    $(basename "$0") --service management

    # Preview what would be applied
    $(basename "$0") --skip-build --dry-run

    # Rollback to previous deployment
    $(basename "$0") --rollback
EOF
}

# =============================================================================
# Main
# =============================================================================

main() {
    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --build)
                SKIP_BUILD=false
                shift
                ;;
            --skip-build)
                SKIP_BUILD=true
                shift
                ;;
            --tag)
                TAG="$2"
                shift 2
                ;;
            --service)
                SERVICE_FILTER="$2"
                shift 2
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            --rollback)
                DO_ROLLBACK=true
                shift
                ;;
            --help)
                show_help
                exit 0
                ;;
            *)
                print_error "Unknown option: $1"
                show_help
                exit 1
                ;;
        esac
    done

    echo ""
    print_info "=========================================="
    print_info "  ${APP_NAME} — Alpha Deployment"
    print_info "=========================================="
    echo ""

    check_prerequisites

    # Handle rollback
    if [[ "${DO_ROLLBACK}" == "true" ]]; then
        local rc=0
        do_rollback || rc=$?
        show_status || true
        exit "${rc}"
    fi

    # Build images
    if [[ "${SKIP_BUILD}" != "true" ]]; then
        print_info "Building and pushing Docker images to ${LOCAL_REGISTRY}..."
        for service in "${SERVICES[@]}"; do
            if [[ -z "${SERVICE_FILTER}" ]] || [[ "${SERVICE_FILTER}" == "${service}" ]]; then
                build_and_import "${service}" "${TAG}" || {
                    print_error "Failed to build ${service}"
                    exit 1
                }
            fi
        done
        if [[ -z "${SERVICE_FILTER}" ]]; then
            mirror_postgres_image || exit 1
        fi
    else
        print_info "Skipping build (--skip-build)"
    fi

    # Deploy
    do_deploy || exit 1

    if [[ "${DRY_RUN}" != "true" ]]; then
        if ! wait_for_rollout; then
            show_status || true
            print_error "Alpha deployment FAILED: workloads did not roll out"
            exit 1
        fi
        show_status
        print_success "Alpha deployment complete!"
    else
        print_success "Dry-run complete!"
    fi
}

main "$@"
