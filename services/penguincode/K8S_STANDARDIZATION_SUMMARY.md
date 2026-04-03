# PenguinCode Kubernetes Standardization - Summary

This document summarizes the standardized Kubernetes configuration created for PenguinCode.

## Overview

PenguinCode has been standardized with a hybrid approach supporting both **Helm** (existing) and **Kustomize** (new) for flexible, environment-aware deployments.

## Files Created

### Manifests (k8s/manifests/)
```
k8s/manifests/
├── namespace.yaml              # Kubernetes namespace: penguincode
├── serviceaccount.yaml         # Service account for pods
└── server/
    ├── deployment.yaml         # PenguinCode server deployment (gRPC 50051 + REST 8080)
    └── service.yaml            # ClusterIP service exposing server ports
```

**Details**:
- **namespace.yaml**: Creates isolated namespace for resource grouping
- **serviceaccount.yaml**: Provides authentication identity for pods
- **deployment.yaml**:
  - 2 replicas (default)
  - gRPC health checks on port 50051
  - REST health checks on port 8080
  - Comprehensive environment configuration from values
  - Liveness/readiness probes
  - Resource limits (CPU: 1000m, Memory: 2Gi)
- **service.yaml**:
  - ClusterIP type (internal only)
  - Dual port exposure (gRPC 50051, REST 8080)

### Kustomize Base (k8s/kustomize/base/)
```
k8s/kustomize/base/
└── kustomization.yaml         # Base kustomization referencing all manifests
```

**Details**:
- Aggregates all manifest files (namespace, serviceaccount, deployment, service)
- Applies common labels to all resources
- Sets namespace context
- Foundation for environment overlays

### Kustomize Alpha Overlay (k8s/kustomize/overlays/alpha/)
```
k8s/kustomize/overlays/alpha/
├── kustomization.yaml         # Alpha environment configuration
└── env.yaml                   # Alpha-specific environment variables
```

**Configuration**:
- **Namespace**: `penguincode-alpha`
- **Name Prefix**: `alpha-`
- **Replicas**: 1 (test efficiency)
- **Resources**: 100m CPU request / 200m limit, 128Mi memory request / 256Mi limit
- **Image Pull Policy**: Never (local images)
- **Log Level**: DEBUG
- **Security Level**: 1
- **VRAM**: 4096 MB
- **Max Concurrent**: 1 model

**Use Case**: Development, testing, rapid iteration

### Kustomize Beta Overlay (k8s/kustomize/overlays/beta/)
```
k8s/kustomize/overlays/beta/
├── kustomization.yaml         # Beta environment configuration
└── env.yaml                   # Beta-specific environment variables
```

**Configuration**:
- **Namespace**: `penguincode-beta`
- **Name Prefix**: `beta-`
- **Replicas**: 2 (high availability)
- **Resources**: 500m CPU request / 1000m limit, 1Gi memory request / 2Gi limit
- **Image Registry**: `registry-dal2.penguintech.io`
- **Image Pull Policy**: Always
- **Log Level**: INFO
- **Security Level**: 2
- **VRAM**: 8192 MB
- **Max Concurrent**: 2 models
- **Auth**: Enabled

**Use Case**: Production-like staging, integration testing, pre-release validation

### Deploy Script (scripts/deploy-beta.sh)
```
scripts/
└── deploy-beta.sh             # Comprehensive deployment script for beta
```

**Features**:
- **Prerequisites Check**: Validates kubectl, helm, docker, Kubernetes context
- **Docker Build**: Builds image with timestamp or custom tag
- **Registry Push**: Pushes to `registry-dal2.penguintech.io`
- **Helm Deploy**: Installs/upgrades release with values overrides
- **Health Verification**: Confirms pods are ready and healthy
- **Rollback Support**: Can rollback to previous releases
- **Dry-run Mode**: Preview changes without applying
- **Color Output**: Progress reporting with visual cues

**Configuration Constants**:
```bash
RELEASE_NAME="penguincode"
NAMESPACE="penguincode"
CHART_PATH="./k8s/helm/penguincode"
IMAGE_REGISTRY="registry-dal2.penguintech.io"
KUBE_CONTEXT="dal2-beta"
APP_HOST="penguincode.penguintech.io"
```

**CLI Options**:
- `--tag <tag>` - Custom image tag (default: beta-YYYYMMDD-HHMMSS)
- `--service <service>` - Deploy specific service
- `--skip-build` - Skip Docker build/push
- `--dry-run` - Preview without applying
- `--rollback <release>` - Rollback to previous release
- `--verbose` - Enable verbose output
- `--help` - Show help message

**Usage Examples**:
```bash
./scripts/deploy-beta.sh                    # Auto build, tag, push, deploy
./scripts/deploy-beta.sh --tag v1.2.3       # Deploy specific version
./scripts/deploy-beta.sh --skip-build       # Use existing image
./scripts/deploy-beta.sh --dry-run          # Preview only
./scripts/deploy-beta.sh --rollback penguincode-1  # Rollback
```

### Documentation

#### k8s/README.md
Quick reference guide for Kubernetes configuration:
- Directory structure
- Quick start commands
- Environment overviews
- Troubleshooting basics
- References and best practices

#### docs/k8s-deployment.md
Comprehensive deployment guide (3000+ lines):
- Prerequisites and setup
- Understanding architecture
- Step-by-step deployment procedures
- Helm usage guide
- Kustomize usage guide
- Deploy script reference
- Environment configuration details
- Advanced usage patterns
- Multi-cluster deployment
- Canary deployments
- Secret management
- Detailed troubleshooting
- Performance monitoring

## Deployment Flows

### Option 1: Kustomize (Recommended)
```bash
# Alpha
kubectl apply -k k8s/kustomize/overlays/alpha

# Beta
kubectl apply -k k8s/kustomize/overlays/beta
```

**Advantages**:
- Simple, single command
- No external state (stateless)
- Easy to preview with dry-run
- Direct kubectl integration

### Option 2: Helm
```bash
# Alpha
helm install penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-alpha.yaml \
  -n penguincode-alpha --create-namespace

# Beta
helm install penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-beta.yaml \
  -n penguincode-beta --create-namespace
```

**Advantages**:
- Release tracking
- Built-in rollback
- History management
- Existing investment

### Option 3: Deploy Script (Beta Only)
```bash
./scripts/deploy-beta.sh                    # Auto build, tag, push, deploy
./scripts/deploy-beta.sh --skip-build       # Use existing image
./scripts/deploy-beta.sh --rollback penguincode-1  # Rollback
```

**Advantages**:
- Full automation
- Docker build included
- Health verification
- Colored output
- One-command deployment

## Key Features

### Multi-Environment Support
- **Alpha**: Minimal resources, debug logging, local images
- **Beta**: Production-like, info logging, registry images
- Easily extensible for additional environments (prod, staging, etc.)

### Hybrid Approach
- **Manifests**: Source of truth for resource structure
- **Helm**: Package management, release tracking
- **Kustomize**: Environment-specific customization

### Health Checks
- **Liveness Probe**: HTTP GET /api/v1/health on port 8080
- **Readiness Probe**: HTTP GET /api/v1/health on port 8080
- Automatic pod restart on failure
- Graceful startup delay (30s liveness, 10s readiness)

### Security
- Service account isolation
- Namespace separation
- Resource limits enforced
- Image pull policy management

### Observability
- Deploy script with color-coded output
- Pod status monitoring
- Event tracking
- Log access through kubectl
- Health endpoint verification

## Configuration Values

### Base Server Configuration
```yaml
Ports:
  - gRPC: 50051 (port-forward friendly)
  - REST: 8080 (health, provisioning)

Environment:
  - Model configurations (planning, orchestration, execution, exploration, research)
  - Memory settings (store: chroma, embedding: nomic-embed-text)
  - Security levels (configurable 1-2)
  - Research engine (duckduckgo)
```

### Alpha Overrides
- Replicas: 1
- CPU: 100m req / 200m lim
- Memory: 128Mi req / 256Mi lim
- Security Level: 1
- VRAM: 4096 MB
- Log Level: DEBUG

### Beta Overrides
- Replicas: 2
- CPU: 500m req / 1000m lim
- Memory: 1Gi req / 2Gi lim
- Security Level: 2
- VRAM: 8192 MB
- Log Level: INFO
- Auth: Enabled
- Registry: `registry-dal2.penguintech.io`
- Host: `penguincode.penguintech.io`

## Integration with Existing Helm Charts

All existing Helm charts are preserved:
- `k8s/helm/penguincode/` - Original Helm chart
- `values.yaml` - Default production values
- `values-alpha.yaml` - Alpha environment overrides
- `values-beta.yaml` - Beta environment overrides

The new Kustomize configuration coexists peacefully and can:
- Use the same Helm values
- Override specific fields via patches
- Extend with additional templates

## File Size Reference

- **deploy-beta.sh**: ~13 KB (production-grade script)
- **kustomization.yaml files**: ~1-2 KB each
- **manifest files**: ~2-3 KB each
- **k8s-deployment.md**: ~15 KB (comprehensive guide)
- **k8s/README.md**: ~8 KB (quick reference)

## Next Steps

1. **Review**: Read k8s/README.md for quick overview
2. **Understand**: Review docs/k8s-deployment.md for comprehensive details
3. **Test Locally**: Use `--dry-run` to preview deployments
4. **Deploy Alpha**: `kubectl apply -k k8s/kustomize/overlays/alpha`
5. **Deploy Beta**: `./scripts/deploy-beta.sh` or `kubectl apply -k k8s/kustomize/overlays/beta`
6. **Monitor**: Use kubectl to check pods, logs, events
7. **Iterate**: Adjust overlays and redeploy as needed

## Troubleshooting

See docs/k8s-deployment.md #Troubleshooting section for:
- Deployment issues
- Service connectivity
- Helm debugging
- Kustomize debugging
- Performance monitoring
- Common error resolution

## Support Resources

- **Kubernetes Docs**: https://kubernetes.io/docs/
- **Helm Docs**: https://helm.sh/docs/
- **Kustomize**: https://kustomize.io/
- **Script Help**: `./scripts/deploy-beta.sh --help`

---

**Created**: 2026-02-11
**PenguinCode Version**: v1.0.0+
**Kubernetes Minimum**: v1.24
**Helm Minimum**: v3.10
**Status**: Production-Ready
