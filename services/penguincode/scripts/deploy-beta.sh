#!/bin/bash
set -euo pipefail

###############################################################################
# PenguinCode Beta Deployment Script
#
# Comprehensive Kubernetes deployment with Docker build, push, and rollback
# support for the PenguinCode server on the beta (dal2) cluster.
#
# Usage:
#   ./scripts/deploy-beta.sh [OPTIONS]
#
# Options:
#   --tag <tag>           Container image tag (default: beta-<timestamp>)
#   --service <service>   Specific service to deploy (default: all)
#   --skip-build          Skip docker build and push
#   --dry-run            Show what would be deployed without applying
#   --rollback <release> Rollback to previous release
#   --help               Show this help message
#
###############################################################################

# Configuration
readonly RELEASE_NAME="penguincode"
readonly NAMESPACE="penguincode"
readonly CHART_PATH="./k8s/helm/penguincode"
readonly IMAGE_REGISTRY="registry-dal2.penguintech.io"
readonly KUBE_CONTEXT="dal2-beta"
readonly APP_HOST="penguincode.penguintech.cloud"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

# Default values
TAG=""
SERVICE=""
SKIP_BUILD=false
DRY_RUN=false
ROLLBACK_RELEASE=""
VERBOSE=false

###############################################################################
# Color Output Helpers
###############################################################################

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

print_header() {
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}${1}${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

print_info() {
    echo -e "${CYAN}[INFO]${NC} ${1}"
}

print_success() {
    echo -e "${GREEN}[✓]${NC} ${1}"
}

print_warning() {
    echo -e "${YELLOW}[⚠]${NC} ${1}"
}

print_error() {
    echo -e "${RED}[✗]${NC} ${1}"
}

###############################################################################
# Usage and Help
###############################################################################

show_help() {
    cat << EOF
$(basename "$0") - Deploy PenguinCode to Beta (dal2) Kubernetes cluster

USAGE:
  $(basename "$0") [OPTIONS]

OPTIONS:
  --tag <tag>           Container image tag (default: beta-YYYYMMDD-HHMMSS)
  --service <service>   Specific service to deploy: server (default: all)
  --skip-build          Skip docker build and push phases
  --dry-run            Show kubectl commands without applying
  --rollback <release> Rollback to previous helm release (e.g., penguincode-1)
  --verbose            Enable verbose output
  --help               Display this help message

EXAMPLES:
  # Deploy with auto-generated tag
  $(basename "$0")

  # Deploy specific tag
  $(basename "$0") --tag v1.2.3

  # Skip build and push (use existing image)
  $(basename "$0") --skip-build

  # Dry run to see what would be deployed
  $(basename "$0") --dry-run

  # Rollback to previous release
  $(basename "$0") --rollback penguincode-1

EOF
}

###############################################################################
# Argument Parsing
###############################################################################

parse_arguments() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --tag)
                TAG="$2"
                shift 2
                ;;
            --service)
                SERVICE="$2"
                shift 2
                ;;
            --skip-build)
                SKIP_BUILD=true
                shift
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            --rollback)
                ROLLBACK_RELEASE="$2"
                shift 2
                ;;
            --verbose)
                VERBOSE=true
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
}

###############################################################################
# Prerequisite Checks
###############################################################################

check_prerequisites() {
    print_header "Checking Prerequisites"

    # Check for required tools
    local required_tools=("docker" "kubectl" "helm" "git")
    for tool in "${required_tools[@]}"; do
        if ! command -v "$tool" &> /dev/null; then
            print_error "$tool is not installed"
            exit 1
        fi
        print_success "$tool is installed"
    done

    # Check kubectl context
    print_info "Checking Kubernetes context: $KUBE_CONTEXT"
    if ! kubectl config get-contexts | grep -q "$KUBE_CONTEXT"; then
        print_error "Kubernetes context '$KUBE_CONTEXT' not found"
        echo "Available contexts:"
        kubectl config get-contexts
        exit 1
    fi

    # Switch to correct context
    kubectl config use-context "$KUBE_CONTEXT" > /dev/null
    print_success "Using context: $KUBE_CONTEXT"

    # Check namespace exists or can be created
    if ! kubectl get namespace "$NAMESPACE" &> /dev/null; then
        print_warning "Namespace '$NAMESPACE' does not exist, will be created"
    else
        print_success "Namespace '$NAMESPACE' exists"
    fi

    # Verify Docker is running
    if ! docker ps &> /dev/null; then
        print_error "Docker is not running or not accessible"
        exit 1
    fi
    print_success "Docker daemon is accessible"

    # Check project structure
    if [[ ! -f "${PROJECT_ROOT}/Dockerfile.server" ]]; then
        print_error "Dockerfile.server not found at ${PROJECT_ROOT}"
        exit 1
    fi
    print_success "Project structure verified"

    echo ""
}

###############################################################################
# Generate Image Tag
###############################################################################

generate_tag() {
    if [[ -z "$TAG" ]]; then
        TAG="beta-$(date +%Y%m%d-%H%M%S)"
    fi
    print_info "Using image tag: $TAG"
}

###############################################################################
# Build and Push Docker Image
###############################################################################

build_and_push() {
    if [[ "$SKIP_BUILD" == true ]]; then
        print_header "Skipping Docker Build and Push"
        print_info "Using existing image: ${IMAGE_REGISTRY}/penguincode:${TAG}"
        return 0
    fi

    print_header "Building and Pushing Docker Image"

    local image_ref="${IMAGE_REGISTRY}/penguincode:${TAG}"

    print_info "Building image: $image_ref"
    if ! docker build \
        --tag "$image_ref" \
        --tag "${IMAGE_REGISTRY}/penguincode:beta-latest" \
        --file "${PROJECT_ROOT}/Dockerfile.server" \
        "${PROJECT_ROOT}"; then
        print_error "Docker build failed"
        exit 1
    fi
    print_success "Docker image built successfully"

    print_info "Pushing image to registry: ${IMAGE_REGISTRY}"
    if ! docker push "$image_ref"; then
        print_error "Failed to push image: $image_ref"
        exit 1
    fi
    print_success "Image pushed: $image_ref"

    if ! docker push "${IMAGE_REGISTRY}/penguincode:beta-latest"; then
        print_error "Failed to push latest tag"
        exit 1
    fi
    print_success "Latest tag pushed"

    echo ""
}

###############################################################################
# Deploy with Helm
###############################################################################

deploy_helm() {
    print_header "Deploying with Helm"

    # Ensure namespace exists
    print_info "Creating namespace if it doesn't exist"
    kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

    # Prepare Helm command
    local helm_cmd=(
        "helm" "upgrade" "--install"
        "${RELEASE_NAME}"
        "${CHART_PATH}"
        "--namespace" "${NAMESPACE}"
        "--values" "${CHART_PATH}/values.yaml"
        "--values" "${CHART_PATH}/values-beta.yaml"
        "--set" "image.tag=${TAG}"
        "--set" "image.repository=${IMAGE_REGISTRY}/penguincode"
        "--set" "global.namespace=${NAMESPACE}"
        "--wait"
        "--timeout" "5m"
    )

    # Add service filter if specified
    if [[ -n "$SERVICE" ]]; then
        helm_cmd+=("--set" "server.enabled=true")
        print_info "Deploying service: $SERVICE"
    fi

    # Add dry-run flag if specified
    if [[ "$DRY_RUN" == true ]]; then
        helm_cmd+=("--dry-run" "--debug")
        print_warning "Running in DRY-RUN mode (no changes will be applied)"
    fi

    print_info "Helm command: ${helm_cmd[*]}"

    if ! "${helm_cmd[@]}"; then
        print_error "Helm deployment failed"
        exit 1
    fi

    print_success "Helm deployment completed successfully"
    echo ""
}

###############################################################################
# Verify Deployment
###############################################################################

verify_deployment() {
    print_header "Verifying Deployment"

    # Check deployment status
    print_info "Checking deployment status..."

    if ! kubectl rollout status deployment/penguincode-server \
        -n "$NAMESPACE" --timeout=2m; then
        print_error "Deployment did not reach ready state"
        echo ""
        print_info "Recent pod events:"
        kubectl describe pod -n "$NAMESPACE" -l app=penguincode
        return 1
    fi

    print_success "Deployment is ready"

    # Get service info
    print_info "Service information:"
    kubectl get svc -n "$NAMESPACE" -l app=penguincode

    # Get pod info
    print_info "Pod information:"
    kubectl get pods -n "$NAMESPACE" -l app=penguincode

    # Check app health
    print_info "Checking application health..."
    local pod_name=$(kubectl get pods -n "$NAMESPACE" -l app=penguincode \
        -o jsonpath='{.items[0].metadata.name}')

    if [[ -n "$pod_name" ]]; then
        # Port forward and check health
        print_info "Forwarding port to check health endpoint..."
        kubectl port-forward -n "$NAMESPACE" "pod/$pod_name" 8080:8080 &
        local pf_pid=$!
        sleep 2

        if curl -s http://localhost:8080/api/v1/health | grep -q "ok"; then
            print_success "Application health check passed"
        else
            print_warning "Health check endpoint may not be immediately ready"
        fi

        kill $pf_pid 2>/dev/null || true
    fi

    # Display deployment summary
    print_header "Deployment Summary"
    echo "Release Name:    ${RELEASE_NAME}"
    echo "Namespace:       ${NAMESPACE}"
    echo "Image:           ${IMAGE_REGISTRY}/penguincode:${TAG}"
    echo "App Host:        ${APP_HOST}"
    echo "Kube Context:    ${KUBE_CONTEXT}"
    echo "Status:          $(kubectl get deployment penguincode-server -n "$NAMESPACE" -o jsonpath='{.status.conditions[?(@.type=="Available")].status}')"
    echo ""
    print_success "Deployment completed successfully!"
    echo ""
}

###############################################################################
# Rollback Function
###############################################################################

rollback_deployment() {
    local release="$1"

    print_header "Rolling Back Deployment"
    print_info "Rolling back release: $release"

    if ! helm rollback "$release" -n "$NAMESPACE"; then
        print_error "Rollback failed for release: $release"
        exit 1
    fi

    print_success "Rollback initiated"

    print_info "Waiting for rollback to complete..."
    if ! kubectl rollout status deployment/penguincode-server \
        -n "$NAMESPACE" --timeout=2m; then
        print_error "Rollback did not complete successfully"
        exit 1
    fi

    print_success "Rollback completed successfully"
    echo ""
}

###############################################################################
# Cleanup on Exit
###############################################################################

cleanup() {
    # Any cleanup operations if needed
    :
}

trap cleanup EXIT

###############################################################################
# Main Execution Flow
###############################################################################

main() {
    print_header "PenguinCode Beta Deployment"
    echo "Time: $(date)"
    echo ""

    # Parse arguments
    parse_arguments "$@"

    # Handle rollback mode
    if [[ -n "$ROLLBACK_RELEASE" ]]; then
        check_prerequisites
        rollback_deployment "$ROLLBACK_RELEASE"
        exit 0
    fi

    # Normal deployment flow
    check_prerequisites
    generate_tag
    build_and_push
    deploy_helm
    verify_deployment
}

main "$@"
