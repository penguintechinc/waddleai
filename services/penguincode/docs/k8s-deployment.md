# PenguinCode Kubernetes Deployment Guide

Complete guide for deploying PenguinCode to Kubernetes clusters using both Helm and Kustomize.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Understanding the Architecture](#understanding-the-architecture)
3. [Quick Deployment](#quick-deployment)
4. [Using Helm](#using-helm)
5. [Using Kustomize](#using-kustomize)
6. [Using the Deploy Script](#using-the-deploy-script)
7. [Environment Configurations](#environment-configurations)
8. [Advanced Usage](#advanced-usage)
9. [Troubleshooting](#troubleshooting)

## Prerequisites

### Required Tools
- `kubectl` (v1.24+) - Kubernetes command-line tool
- `helm` (v3.10+) - Package manager for Kubernetes
- `docker` - Container engine for building images
- `kustomize` (v4.0+) - Template-free customization tool (optional, kubectl has built-in support)

### Cluster Requirements
- Kubernetes v1.24+
- Access to a container registry (for beta: `registry-dal2.penguintech.io`)
- Appropriate RBAC permissions
- Storage provisioner (for persistence, if enabled)

### Check Prerequisites
```bash
# Verify kubectl
kubectl version --client

# Verify helm
helm version

# Verify docker
docker --version

# Verify kubectl context (should see dal2-beta or similar)
kubectl config get-contexts

# Verify cluster access
kubectl cluster-info
```

## Understanding the Architecture

### Components

**PenguinCode Server**
- gRPC API on port 50051
- REST health endpoint on port 8080
- Processes AI code generation tasks
- Requires sufficient resources for model inference

### Configuration Hierarchy

```
Helm Templates (templates/)
        ↓
    ↓ values.yaml (defaults)
    ↓ values-alpha.yaml (overrides)
    ↓ values-beta.yaml (overrides)
        ↓
Kustomize Base (manifests/)
        ↓
    ↓ Namespace
    ↓ ServiceAccount
    ↓ Deployment
    ↓ Service
        ↓
Kustomize Overlays (overlays/)
    ↓ alpha/
    ↓ beta/
        ↓
Applied Resources
```

## Quick Deployment

### Deploy to Alpha (Testing)

```bash
# Option 1: Using Kustomize (Recommended)
kubectl apply -k k8s/kustomize/overlays/alpha

# Option 2: Using Helm
helm install penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-alpha.yaml \
  --namespace penguincode-alpha --create-namespace

# Verify deployment
kubectl get pods -n penguincode-alpha
kubectl logs -n penguincode-alpha -l app=penguincode
```

### Deploy to Beta (Production-like)

```bash
# Option 1: Using Kustomize (Recommended)
kubectl apply -k k8s/kustomize/overlays/beta

# Option 2: Using the deploy script
./scripts/deploy-beta.sh

# Option 3: Using Helm
helm install penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-beta.yaml \
  --namespace penguincode-beta --create-namespace

# Verify deployment
kubectl get pods -n penguincode-beta
kubectl logs -n penguincode-beta -l app=penguincode
```

## Using Helm

### Basic Helm Commands

#### Install
```bash
# Install with default values
helm install penguincode k8s/helm/penguincode

# Install with specific values file
helm install penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-beta.yaml

# Install with namespace creation
helm install penguincode k8s/helm/penguincode \
  --namespace penguincode-beta \
  --create-namespace

# Install with additional overrides
helm install penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-beta.yaml \
  --set image.tag=v1.2.3 \
  --set server.replicas=3
```

#### Upgrade
```bash
# Upgrade to new version
helm upgrade penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-beta.yaml

# Upgrade with wait for ready
helm upgrade penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-beta.yaml \
  --wait --timeout 5m

# Upgrade with atomic rollback on failure
helm upgrade penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-beta.yaml \
  --atomic
```

#### Rollback
```bash
# See release history
helm history penguincode

# Rollback to previous release
helm rollback penguincode

# Rollback to specific revision
helm rollback penguincode 2
```

#### Status and Information
```bash
# Check release status
helm status penguincode

# Get values of release
helm get values penguincode

# Get manifest of release
helm get manifest penguincode

# Lint chart for errors
helm lint k8s/helm/penguincode
```

### Helm Values

#### Default Values (values.yaml)
- Namespace: `penguincode-prod`
- Replicas: 2
- CPU request/limit: 500m / 1000m
- Memory request/limit: 1Gi / 2Gi
- Image: `penguincode/server:latest`

#### Alpha Values Override (values-alpha.yaml)
- Namespace: `penguincode-alpha`
- Replicas: 1
- CPU request/limit: 100m / 200m
- Memory request/limit: 128Mi / 256Mi
- Image pull policy: Never (local)
- SECURITY_LEVEL: 1
- VRAM: 4096 MB

#### Beta Values Override (values-beta.yaml)
- Namespace: `penguincode-beta`
- Replicas: 2
- CPU request/limit: 500m / 1000m
- Memory request/limit: 1Gi / 2Gi
- Image pull policy: Always (registry)
- Image repository: `registry-dal2.penguintech.io/penguincode`
- Image tag: `beta-latest`
- SECURITY_LEVEL: 2
- VRAM: 8192 MB

## Using Kustomize

### Basic Kustomize Commands

#### Apply
```bash
# Apply alpha overlay
kubectl apply -k k8s/kustomize/overlays/alpha

# Apply beta overlay
kubectl apply -k k8s/kustomize/overlays/beta

# Dry-run to see what will be applied
kubectl apply -k k8s/kustomize/overlays/beta --dry-run=client -o yaml

# Build and output to file
kubectl kustomize k8s/kustomize/overlays/beta > release.yaml
```

#### Update
```bash
# Update alpha deployment
kubectl apply -k k8s/kustomize/overlays/alpha

# Replace instead of merge
kubectl replace -k k8s/kustomize/overlays/beta
```

#### Delete
```bash
# Delete alpha resources
kubectl delete -k k8s/kustomize/overlays/alpha

# Delete beta resources
kubectl delete -k k8s/kustomize/overlays/beta
```

### Kustomize Structure

#### Base (k8s/kustomize/base/)
Common resources shared by all environments:
- `kustomization.yaml` - References manifests
- Applies common labels
- Sets base namespace

#### Overlays
Environment-specific customizations:
- `alpha/` - Testing environment (1 replica, debug logs)
- `beta/` - Production-like (2 replicas, info logs)

#### Customizations Applied
- `namePrefix` - Adds prefix to resource names (e.g., `alpha-`, `beta-`)
- `namespace` - Sets/overrides namespace
- `replicas` - Adjusts deployment replicas
- `images` - Updates image registry and tags
- `patches` - Modifies specific fields (resources, env vars)
- `commonLabels` - Adds labels to all resources
- `commonAnnotations` - Adds annotations to all resources

## Using the Deploy Script

### Overview

The `scripts/deploy-beta.sh` script provides automated deployment with:
- Docker build and push
- Helm deployment
- Health verification
- Rollback capability
- Progress reporting

### Usage

#### Basic Deployment
```bash
# Deploy with auto-generated tag (beta-YYYYMMDD-HHMMSS)
./scripts/deploy-beta.sh

# Deploy with specific tag
./scripts/deploy-beta.sh --tag v1.2.3

# Deploy with specific tag and registry
./scripts/deploy-beta.sh --tag beta-2024-01-15
```

#### Advanced Options
```bash
# Skip Docker build (use existing image)
./scripts/deploy-beta.sh --skip-build

# Dry-run to preview changes
./scripts/deploy-beta.sh --dry-run

# Deploy specific service
./scripts/deploy-beta.sh --service server

# Verbose output
./scripts/deploy-beta.sh --verbose

# Show help
./scripts/deploy-beta.sh --help
```

#### Rollback
```bash
# Rollback to previous release
./scripts/deploy-beta.sh --rollback penguincode-1

# Rollback to specific revision
./scripts/deploy-beta.sh --rollback penguincode-3
```

### Script Workflow

1. **Prerequisite Check**
   - Verifies docker, kubectl, helm are installed
   - Confirms Kubernetes context (dal2-beta)
   - Checks namespace existence
   - Validates project structure

2. **Generate Tag**
   - Uses provided tag or generates: `beta-YYYYMMDD-HHMMSS`

3. **Build and Push**
   - Builds Docker image with tag
   - Pushes to `registry-dal2.penguintech.io`
   - Tags and pushes `beta-latest`

4. **Deploy with Helm**
   - Creates namespace if needed
   - Installs/upgrades Helm release
   - Uses `values-beta.yaml` overrides
   - Waits for deployment ready

5. **Verify**
   - Checks rollout status
   - Displays pod information
   - Performs health check
   - Shows deployment summary

### Configuration Constants

```bash
RELEASE_NAME="penguincode"
NAMESPACE="penguincode"
CHART_PATH="./k8s/helm/penguincode"
IMAGE_REGISTRY="registry-dal2.penguintech.io"
KUBE_CONTEXT="dal2-beta"
APP_HOST="penguincode.penguintech.io"
```

## Environment Configurations

### Alpha Environment

**Purpose**: Development and testing
**Cluster**: Local development or alpha cluster

**Configuration**:
```yaml
Namespace: penguincode-alpha
Replicas: 1
CPU Request: 100m
CPU Limit: 200m
Memory Request: 128Mi
Memory Limit: 256Mi
Image: penguincode/server:latest (local)
Image Pull Policy: Never
Log Level: DEBUG
Security Level: 1
VRAM: 4096 MB
Max Concurrent: 1
```

**Use Cases**:
- Feature development
- Testing changes locally
- Debugging issues
- CI/CD testing

### Beta Environment

**Purpose**: Production-like staging
**Cluster**: dal2-beta Kubernetes cluster

**Configuration**:
```yaml
Namespace: penguincode-beta
Replicas: 2
CPU Request: 500m
CPU Limit: 1000m
Memory Request: 1Gi
Memory Limit: 2Gi
Image: registry-dal2.penguintech.io/penguincode:beta-latest
Image Pull Policy: Always
Log Level: INFO
Security Level: 2
VRAM: 8192 MB
Max Concurrent: 2
Auth: Enabled
Ingress: Enabled (penguincode.penguintech.io)
```

**Use Cases**:
- Pre-production validation
- Load testing
- Integration testing
- Staging deployments

### Adding New Environments

To add a new environment (e.g., `prod`):

1. **Create Helm values file**:
   ```bash
   cp k8s/helm/penguincode/values-beta.yaml k8s/helm/penguincode/values-prod.yaml
   # Edit values-prod.yaml with production settings
   ```

2. **Create Kustomize overlay**:
   ```bash
   cp -r k8s/kustomize/overlays/beta k8s/kustomize/overlays/prod
   # Edit kustomization.yaml and env.yaml
   ```

3. **Deploy**:
   ```bash
   # Using Kustomize
   kubectl apply -k k8s/kustomize/overlays/prod

   # Or using Helm
   helm install penguincode k8s/helm/penguincode \
     -f k8s/helm/penguincode/values-prod.yaml \
     --namespace penguincode-prod --create-namespace
   ```

## Advanced Usage

### Custom Configuration

#### Override at Deploy Time
```bash
# Using Helm
helm install penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-beta.yaml \
  --set server.replicas=3 \
  --set image.tag=custom-tag \
  --set server.env.LOG_LEVEL=DEBUG

# Using deploy script
./scripts/deploy-beta.sh --tag custom-tag
```

#### Kustomize Strategic Merge Patch
```yaml
# k8s/kustomize/overlays/custom/kustomization.yaml
bases:
- ../../base

patches:
- target:
    kind: Deployment
    name: penguincode-server
  patch: |-
    - op: replace
      path: /spec/replicas
      value: 5
```

### Multi-Cluster Deployment

Deploy to multiple clusters:
```bash
# Deploy to alpha
kubectl config use-context alpha-cluster
kubectl apply -k k8s/kustomize/overlays/alpha

# Deploy to beta
kubectl config use-context dal2-beta
./scripts/deploy-beta.sh

# Deploy to production
kubectl config use-context prod-cluster
kubectl apply -k k8s/kustomize/overlays/prod
```

### Canary Deployments

Using Kustomize for staged rollout:
```bash
# Create canary overlay
mkdir k8s/kustomize/overlays/canary

# Deploy canary (1 replica)
kubectl apply -k k8s/kustomize/overlays/canary

# Monitor metrics

# Scale to production (update replicas)
kubectl apply -k k8s/kustomize/overlays/beta
```

### Secret Management

Store secrets separately (not in git):
```bash
# Create secret manually
kubectl create secret generic penguincode-secrets \
  --from-literal=JWT_SECRET=your-secret \
  --from-literal=API_KEY=your-key \
  -n penguincode

# Or use sealed-secrets, external-secrets, or Vault
```

### Resource Limits and Requests

Adjust for your cluster capacity:
```bash
# For high-performance deployment
kubectl set resources deployment penguincode-server \
  -n penguincode \
  --limits=cpu=2000m,memory=4Gi \
  --requests=cpu=1000m,memory=2Gi
```

## Troubleshooting

### Deployment Issues

#### Pods not starting
```bash
# Check pod status
kubectl get pods -n penguincode -o wide

# Describe pod for events
kubectl describe pod <pod-name> -n penguincode

# Check logs
kubectl logs -n penguincode <pod-name>

# Check events
kubectl get events -n penguincode --sort-by='.lastTimestamp'
```

#### Image pull errors
```bash
# Check image registry access
kubectl get nodes -o wide

# Check image pull secrets
kubectl get secrets -n penguincode

# Verify image exists in registry
docker pull registry-dal2.penguintech.io/penguincode:beta-latest
```

#### Resource constraints
```bash
# Check node resources
kubectl top nodes

# Check pod resource usage
kubectl top pod -n penguincode

# Check requests vs available
kubectl describe nodes
```

### Service Connectivity

#### Test service connectivity
```bash
# Get service details
kubectl get svc -n penguincode

# Port forward for local testing
kubectl port-forward -n penguincode svc/penguincode-server 50051:50051

# Test with grpcurl
grpcurl -plaintext localhost:50051 list
```

#### Check endpoints
```bash
# Verify endpoints exist
kubectl get endpoints -n penguincode

# Check service selector matches pods
kubectl get pods -n penguincode -l app=penguincode
```

### Helm Troubleshooting

#### Debug Helm install
```bash
# Dry-run to see manifests
helm install penguincode k8s/helm/penguincode \
  --dry-run --debug \
  -f k8s/helm/penguincode/values-beta.yaml

# Check template rendering
helm template penguincode k8s/helm/penguincode

# Lint for errors
helm lint k8s/helm/penguincode
```

### Kustomize Troubleshooting

#### Debug Kustomize build
```bash
# Build and output manifests
kubectl kustomize k8s/kustomize/overlays/beta

# Dry-run apply
kubectl apply -k k8s/kustomize/overlays/beta --dry-run=client -o yaml

# Check if resources are valid
kubectl apply -k k8s/kustomize/overlays/beta --validate=true
```

### Performance Issues

#### Monitor deployment
```bash
# Watch pod deployment
kubectl get pods -n penguincode -w

# Monitor resource usage
kubectl top pod -n penguincode

# Check logs for errors
kubectl logs -n penguincode -l app=penguincode --all-containers=true -f
```

## Next Steps

- Review the [k8s README](../k8s/README.md) for quick reference
- Check [Helm documentation](https://helm.sh/docs/)
- Explore [Kustomize guide](https://kustomize.io/)
- Set up monitoring and logging
- Configure automatic scaling
- Implement GitOps workflow
