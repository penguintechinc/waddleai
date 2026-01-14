#!/bin/bash
set -e

# WaddleAI Beta K8s Deployment Script
# Deploys WaddleAI to beta k8s cluster with wildcard TLS cert

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="waddleai"

echo "=========================================="
echo "WaddleAI Beta K8s Deployment"
echo "=========================================="
echo ""

# Check if kubectl is available
if ! command -v kubectl &> /dev/null; then
    echo "Error: kubectl not found. Please install kubectl first."
    exit 1
fi

# Check cluster connectivity
echo "Checking cluster connectivity..."
if ! kubectl cluster-info &> /dev/null; then
    echo "Error: Cannot connect to kubernetes cluster"
    exit 1
fi

echo "Connected to cluster: $(kubectl config current-context)"
echo ""

# Create namespace if it doesn't exist
echo "Ensuring namespace exists..."
kubectl create namespace $NAMESPACE --dry-run=client -o yaml | kubectl apply -f -
echo ""

# Apply base configuration
echo "Deploying WaddleAI to beta cluster..."
kubectl apply -k "$SCRIPT_DIR/overlays/beta"
echo ""

# Wait for deployment to be ready
echo "Waiting for deployments to be ready..."
kubectl rollout status deployment/postgres -n $NAMESPACE --timeout=300s
kubectl rollout status deployment/redis -n $NAMESPACE --timeout=300s
kubectl rollout status deployment/waddleai-mgmt -n $NAMESPACE --timeout=300s
echo ""

# Get ingress information
echo "=========================================="
echo "Deployment Complete!"
echo "=========================================="
echo ""
echo "Ingress URLs:"
kubectl get ingress -n $NAMESPACE -o custom-columns=NAME:.metadata.name,HOSTS:.spec.rules[*].host,ADDRESS:.status.loadBalancer.ingress[*].ip
echo ""

echo "Services:"
kubectl get svc -n $NAMESPACE
echo ""

echo "Pods:"
kubectl get pods -n $NAMESPACE
echo ""

echo "To view logs:"
echo "  kubectl logs -f -l app=waddleai,component=management -n $NAMESPACE"
echo ""

echo "To update secrets (IMPORTANT - do this before production use):"
echo "  kubectl edit secret waddleai-secrets -n $NAMESPACE"
echo ""

echo "To access the application:"
echo "  https://waddleai.beta.k8s.cluster"
echo "  https://api.waddleai.beta.k8s.cluster"
echo "  https://mgmt.waddleai.beta.k8s.cluster"
echo ""
