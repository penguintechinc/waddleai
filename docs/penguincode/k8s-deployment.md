# PenguinCode Kubernetes Deployment Guide

Complete guide for deploying PenguinCode to Kubernetes clusters using Helm.
**Helm v4 is the only supported deployment method** -- Kustomize and raw
manifest directories were retired; Docker Compose is deprecated for every
environment (local dev included).

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Understanding the Architecture](#understanding-the-architecture)
3. [Quick Deployment](#quick-deployment)
4. [Using Helm](#using-helm)
5. [Using the Deploy Script](#using-the-deploy-script)
6. [Environment Configurations](#environment-configurations)
7. [Advanced Usage](#advanced-usage)
8. [Troubleshooting](#troubleshooting)

## Prerequisites

### Required Tools
- `kubectl` (v1.24+) - Kubernetes command-line tool
- `helm` (v3.10+, v4 preferred) - Package manager for Kubernetes
- `docker` - Container engine for building images

### Cluster Requirements
- Kubernetes v1.24+
- Access to a container registry (beta/gamma/prod: `ghcr.io/penguintechinc/penguincode`)
- Appropriate RBAC permissions
- A shared WaddleAI Postgres instance reachable from the cluster (see
  Postgres Role Bootstrap below)

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
Helm Templates (k8s/helm/penguincode/templates/)
        |
    values.yaml (Helm-implicit default, prod-safe baseline)
        |
    alpha.yml / beta.yml / gamma.yml / production.yml (env overrides)
        |
Applied Resources (namespace: penguincode, in every environment)
```

The namespace is always `penguincode` -- the environment lives in the
values filename, never in the namespace name.

### Postgres Role Bootstrap (spec §9)

Every environment runs two Helm pre-install/pre-upgrade hook Jobs before the
server Deployment rolls out:

1. **role-bootstrap** (`templates/role-bootstrap-job.yaml`, hook-weight -10) --
   idempotently creates the least-privilege `penguincode_app` Postgres role,
   the `penguincode` schema owned by it, and the `vector` extension, using an
   ADMIN DSN.
2. **migrate** (`templates/migration-job.yaml`, hook-weight -5) -- applies
   `penguincode_cli/db/migrations/*.sql` using the same ADMIN DSN (DDL/`CREATE
   EXTENSION` need elevated privileges the app role never holds).

The server Deployment itself uses only the least-privilege app-role DSN. See
`k8s/helm/penguincode/values.yaml` (`postgres:` block) for the full 3-secret
runbook operators must provision before `helm install`.

## Quick Deployment

### Deploy to Alpha (Testing)

```bash
helm upgrade --install penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/alpha.yml \
  --namespace penguincode --create-namespace

# Verify deployment
kubectl get pods -n penguincode
kubectl logs -n penguincode -l app.kubernetes.io/name=penguincode
```

Or via the smoke-test wrapper: `make k8s-alpha-deploy` (see
`tests/k8s/alpha/run-all-alpha.sh`).

### Deploy to Beta (Production-like)

```bash
# Option 1: Using the deploy script (CI/CD)
./scripts/deploy-beta.sh

# Option 2: Using Helm directly
helm upgrade --install penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/beta.yml \
  --namespace penguincode --create-namespace

# Verify deployment
kubectl get pods -n penguincode
kubectl logs -n penguincode -l app.kubernetes.io/name=penguincode
```

## Using Helm

### Basic Helm Commands

#### Install
```bash
# Install with default (prod-safe baseline) values
helm install penguincode k8s/helm/penguincode

# Install with a specific environment values file
helm install penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/beta.yml \
  --namespace penguincode --create-namespace

# Install with additional overrides
helm install penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/beta.yml \
  --set server.replicas=3
```

#### Upgrade
```bash
# Upgrade to new version
helm upgrade penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/beta.yml

# Upgrade with wait for ready
helm upgrade penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/beta.yml \
  --wait --timeout 5m

# Upgrade with atomic rollback on failure
helm upgrade penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/beta.yml \
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
- Namespace: `penguincode`
- Replicas: 2
- CPU request/limit: 500m / 1000m
- Memory request/limit: 1Gi / 2Gi
- Image: `penguincode/server:latest` (placeholder -- every real environment
  values file overrides this with a pinned tier tag or SHA256 digest)

#### Alpha Values Override (alpha.yml)
- Namespace: `penguincode`
- Replicas: 1
- CPU request/limit: 100m / 200m
- Memory request/limit: 128Mi / 256Mi
- Image pull policy: `IfNotPresent`; tag: `alpha-<epoch64>` (local build)
- SECURITY_LEVEL: 1
- VRAM: 4096 MB

#### Beta Values Override (beta.yml)
- Namespace: `penguincode`
- Replicas: 2
- CPU request/limit: 500m / 1000m
- Memory request/limit: 1Gi / 2Gi
- Image repository: `ghcr.io/penguintechinc/penguincode`
- Image tag: `beta-<epoch64>` (CI-set on merge to `main`)
- SECURITY_LEVEL: 2
- VRAM: 8192 MB

#### Gamma Values Override (gamma.yml)
- Same shape as beta -- upgrade-in-place validation tier on DigitalOcean
- Image tag: `gamma-<epoch64>` (CI-set on GitHub pre-release)

#### Production Values Override (production.yml)
- Replicas: 3
- Image pinned by SHA256 digest (`image.digest`), never a mutable tag

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
# Deploy with auto-generated tag (beta-<epoch64>)
./scripts/deploy-beta.sh

# Deploy with specific tag
./scripts/deploy-beta.sh --tag beta-1727308800
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

1. **Prerequisite Check** -- verifies docker, kubectl, helm are installed;
   confirms Kubernetes context; checks namespace existence.
2. **Generate Tag** -- uses provided tag or generates `beta-<epoch64>`.
3. **Build and Push** -- builds the Docker image, pushes to
   `ghcr.io/penguintechinc/penguincode`.
4. **Deploy with Helm** -- creates namespace if needed, installs/upgrades
   using `beta.yml` overrides, waits for deployment ready.
5. **Verify** -- checks rollout status, pod info, health check.

### Configuration Constants

```bash
RELEASE_NAME="penguincode"
NAMESPACE="penguincode"
CHART_PATH="./k8s/helm/penguincode"
IMAGE_REGISTRY="ghcr.io/penguintechinc"
KUBE_CONTEXT="dal2-beta"
```

## Environment Configurations

### Alpha Environment

**Purpose**: Development and testing
**Cluster**: Local development (MicroK8s/Docker Desktop)

**Configuration**:
```yaml
Namespace: penguincode
Replicas: 1
CPU Request: 100m
CPU Limit: 200m
Memory Request: 128Mi
Memory Limit: 256Mi
Image: penguincode/server:alpha-<epoch64> (local build)
Image Pull Policy: IfNotPresent
Log Level: DEBUG
Security Level: 1
VRAM: 4096 MB
Max Concurrent: 1
```

### Beta Environment

**Purpose**: Production-like staging
**Cluster**: dal2-beta Kubernetes cluster

**Configuration**:
```yaml
Namespace: penguincode
Replicas: 2
CPU Request: 500m
CPU Limit: 1000m
Memory Request: 1Gi
Memory Limit: 2Gi
Image: ghcr.io/penguintechinc/penguincode:beta-<epoch64>
Image Pull Policy: IfNotPresent
Log Level: INFO
Security Level: 2
VRAM: 8192 MB
Max Concurrent: 2
Auth: Enabled
Ingress: Enabled (penguincode.penguintech.cloud)
```

### Gamma Environment

**Purpose**: Upgrade-in-place validation before a release is cut
**Cluster**: DigitalOcean

Same resource shape as beta; image tag `gamma-<epoch64>`, host
`penguincode-gamma.penguintech.cloud`.

### Production Environment

**Purpose**: Live traffic
**Cluster**: DigitalOcean (separate cluster from gamma)

3 replicas, image pinned by SHA256 digest, flags OFF by default.

### Adding New Environments

Copy the closest existing values file (e.g. `beta.yml`) to a new bare
`<env>.yml` (no `values-` prefix), edit it for the new environment, and
deploy with `helm upgrade --install ... -f k8s/helm/penguincode/<env>.yml`.
Never create a Kustomize overlay -- Helm v4 is the only supported deployment
method.

## Advanced Usage

### Custom Configuration

```bash
helm install penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/beta.yml \
  --set server.replicas=3 \
  --set image.tag=custom-tag \
  --set server.env.LOG_LEVEL=DEBUG
```

### Multi-Cluster Deployment

```bash
# Deploy to alpha (local context)
kubectl config use-context local-prealpha
helm upgrade --install penguincode k8s/helm/penguincode -f k8s/helm/penguincode/alpha.yml -n penguincode --create-namespace

# Deploy to beta
kubectl config use-context dal2-beta
./scripts/deploy-beta.sh
```

### Secret Management

Never commit secrets to git -- use `kubectl create secret`, sealed-secrets,
External Secrets Operator, or Vault. See `k8s/helm/penguincode/values.yaml`
(`postgres:` block) for the 3-secret Postgres role-bootstrap contract
(admin DSN, app-role password, app DSN).

```bash
kubectl create secret generic penguincode-secrets \
  --from-literal=JWT_SECRET=your-secret \
  --from-literal=API_KEY=your-key \
  -n penguincode
```

### Ollama-Embedding Bulkhead (Cross-Chart)

A bulk `index_docs`/`index_code` job can saturate the Ollama instance also
serving live chat if both workloads share one instance -- see
[`docs/penguincode/ARCHITECTURE.md`](ARCHITECTURE.md#ollama-embedding-bulkhead)
for the full picture. The dedicated embedding deployment lives in the
**WaddleAI chart**, not this one: `k8s/helm/waddleai`'s optional
`ollamaEmbeddings.enabled` value (default `false`) renders a second Ollama
Deployment/Service (`<release>-ollama-embeddings`).

To route this penguincode deployment's embedding calls there once that's
enabled, set `PENGUINCODE_EMBEDDING_OLLAMA_URL` in **this** chart's `env:`
map (`values.yaml`/`alpha.yml`/`beta.yml`, same key you'd use for
`OLLAMA_HOST` today) to the WaddleAI release's Service DNS name, e.g.
`http://waddleai-ollama-embeddings.waddleai.svc.cluster.local:11434`.
Leaving it unset is always safe -- embedding calls fall back to
`OLLAMA_HOST`/`OLLAMA_API_URL` exactly as before. See
[`docs/penguincode/CONFIGURATION.md`](CONFIGURATION.md#ollama-embedding-bulkhead-penguincode_embedding_ollama_url)
for the full env var reference.

### Resource Limits and Requests

```bash
kubectl set resources deployment penguincode-server \
  -n penguincode \
  --limits=cpu=2000m,memory=4Gi \
  --requests=cpu=1000m,memory=2Gi
```

### Health Checks, Graceful Shutdown, Autoscaling & Disruption Budgets

| `values.yaml` key | Default | Notes |
|---|---|---|
| `server.startupProbe` | `enabled: true`, 5s + 24×5s ≈ 125s grace | Gates liveness/readiness until the server's gRPC channel accepts connections once — absorbs a slow first-start model-registry/config load |
| `server.terminationGracePeriodSeconds` / `.preStopSleepSeconds` | `120` / `10` | Pod-level SIGTERM→SIGKILL window and a `preStop sleep` before SIGTERM, covering an in-flight agent/chat RPC plus Service/EndpointSlice deregistration time |
| `server.podDisruptionBudget.enabled` / `.minAvailable` | `true` / `1` | Renders `templates/pdb.yaml` only when the effective replica count (`autoscaling.minReplicas` when autoscaling is on, else `server.replicas`) is greater than 1 — alpha (`replicas: 1`) renders nothing, beta/gamma/production (`replicas: 2+`) render normally |
| `autoscaling.enabled` / `.minReplicas` / `.maxReplicas` | `false` / `2` / `10` | `templates/hpa.yaml` (CPU + memory targets) honors this block — previously defined in `values.yaml` with no template ever reading it. `server.replicas` is omitted from the Deployment spec whenever this is on, so `helm upgrade` never fights the HPA |
| `server.healthCheck.nativeGrpcProbe` | `false` | Switches both probes from the exec-based `grpc.channel_ready_future()` check to K8s's native `grpc:` probe type (GA since 1.27) against the standard `grpc.health.v1.Health` service — readiness targets `KnowledgeService` specifically, liveness targets overall server health. **Leave this `false` until `penguincode_cli/server/main.py` actually registers `grpc.health.v1.Health`** (it currently doesn't; `shared/py_libs/py_libs/grpc/server.py`'s `register_health_check` exists but is unused) — flipping it on before then fails every probe closed |
| `server.grpcMaxWorkers` | `10` | Sets `PENGUINCODE_GRPC_MAX_WORKERS` — chart-side plumbing is ready for when the server reads it (currently hardcoded `max_workers=10` in `main.py`, a separate non-Helm change) |

## Troubleshooting

### Deployment Issues

#### Pods not starting
```bash
kubectl get pods -n penguincode -o wide
kubectl describe pod <pod-name> -n penguincode
kubectl logs -n penguincode <pod-name>
kubectl get events -n penguincode --sort-by='.lastTimestamp'
```

#### Role-bootstrap or migration Job failing
```bash
# Check the bootstrap/migration hook Jobs specifically
kubectl get jobs -n penguincode
kubectl logs -n penguincode job/penguincode-role-bootstrap
kubectl logs -n penguincode job/penguincode-migrate
```

#### Image pull errors
```bash
kubectl get nodes -o wide
kubectl get secrets -n penguincode
docker pull ghcr.io/penguintechinc/penguincode:beta-<epoch64>
```

#### Resource constraints
```bash
kubectl top nodes
kubectl top pod -n penguincode
kubectl describe nodes
```

### Service Connectivity

```bash
kubectl get svc -n penguincode
kubectl port-forward -n penguincode svc/penguincode-server 50051:50051
grpcurl -plaintext localhost:50051 list
kubectl get endpoints -n penguincode
```

### Helm Troubleshooting

```bash
# Dry-run to see manifests
helm install penguincode k8s/helm/penguincode \
  --dry-run --debug \
  -f k8s/helm/penguincode/beta.yml

# Check template rendering
helm template penguincode k8s/helm/penguincode

# Lint for errors
helm lint k8s/helm/penguincode
```

### Performance Issues

```bash
kubectl get pods -n penguincode -w
kubectl top pod -n penguincode
kubectl logs -n penguincode -l app.kubernetes.io/name=penguincode --all-containers=true -f
```

## Next Steps

- Check [Helm documentation](https://helm.sh/docs/)
- Set up monitoring and logging
- Configure automatic scaling
- Implement GitOps workflow
