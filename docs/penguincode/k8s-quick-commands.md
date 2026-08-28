# PenguinCode Kubernetes Quick Commands Reference

Quick command reference for common Kubernetes operations with PenguinCode.

## Deployment Commands

### Deploy to Alpha (Testing)
```bash
# Using Kustomize (recommended)
kubectl apply -k k8s/kustomize/overlays/alpha

# Using Helm
helm install penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-alpha.yaml \
  -n penguincode --create-namespace

# Dry-run first
kubectl apply -k k8s/kustomize/overlays/alpha --dry-run=client -o yaml
```

### Deploy to Beta (Production-like)
```bash
# Using deploy script (recommended, includes build/push)
./scripts/deploy-beta.sh

# Using Kustomize
kubectl apply -k k8s/kustomize/overlays/beta

# Using Helm
helm install penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-beta.yaml \
  -n penguincode --create-namespace

# Dry-run first
kubectl apply -k k8s/kustomize/overlays/beta --dry-run=client -o yaml
```

### Deploy with Custom Tag
```bash
# Using script
./scripts/deploy-beta.sh --tag v1.2.3
./scripts/deploy-beta.sh --tag my-feature-tag

# Using Helm
helm install penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-beta.yaml \
  -n penguincode --create-namespace \
  --set image.tag=v1.2.3
```

### Skip Docker Build (Use Existing Image)
```bash
./scripts/deploy-beta.sh --skip-build
./scripts/deploy-beta.sh --tag v1.2.3 --skip-build
```

### Dry-Run (Preview Without Applying)
```bash
./scripts/deploy-beta.sh --dry-run
kubectl apply -k k8s/kustomize/overlays/beta --dry-run=client -o yaml
```

## Status and Information Commands

### Check Deployment Status
```bash
# Check if pods are running
kubectl get pods -n penguincode

# Watch deployment progress
kubectl get pods -n penguincode -w

# Get detailed deployment status
kubectl get deployment -n penguincode -o wide

# Check rollout status
kubectl rollout status deployment/penguincode-server -n penguincode

# Get all resources in namespace
kubectl get all -n penguincode
```

### View Logs
```bash
# Current logs
kubectl logs -n penguincode -l app=penguincode

# Tail logs in real-time
kubectl logs -n penguincode -l app=penguincode -f

# Specific pod logs
kubectl logs -n penguincode <pod-name>

# Previous pod logs (if crash)
kubectl logs -n penguincode <pod-name> --previous

# All containers (if multiple)
kubectl logs -n penguincode <pod-name> --all-containers=true

# With timestamps
kubectl logs -n penguincode -l app=penguincode --timestamps=true

# Last N lines
kubectl logs -n penguincode <pod-name> --tail=50
```

### Check Events
```bash
# All events in namespace
kubectl get events -n penguincode

# Sorted by time
kubectl get events -n penguincode --sort-by='.lastTimestamp'

# Watch events
kubectl get events -n penguincode -w

# Describe pod (includes events)
kubectl describe pod <pod-name> -n penguincode
```

### Helm Status
```bash
# Check release status
helm status penguincode -n penguincode

# List releases
helm list -n penguincode

# View release history
helm history penguincode -n penguincode

# Get current values
helm get values penguincode -n penguincode

# Get rendered manifests
helm get manifest penguincode -n penguincode
```

## Verification and Health Commands

### Test Health Endpoint
```bash
# Port forward
kubectl port-forward -n penguincode svc/penguincode-server 8080:8080 &

# Test health in another terminal
curl http://localhost:8080/api/v1/health

# Clean up port forward
pkill -f "port-forward"
```

### Test gRPC Service
```bash
# Port forward gRPC port
kubectl port-forward -n penguincode svc/penguincode-server 50051:50051 &

# List services (requires grpcurl)
grpcurl -plaintext localhost:50051 list

# Clean up
pkill -f "port-forward"
```

### Check Resource Usage
```bash
# Pod resource usage
kubectl top pod -n penguincode

# Node resource usage
kubectl top nodes

# Specific pod resources
kubectl top pod <pod-name> -n penguincode

# Watch resource usage
kubectl top pod -n penguincode -w
```

### Check Service and Endpoints
```bash
# List services
kubectl get svc -n penguincode

# Check endpoints
kubectl get endpoints -n penguincode

# Describe service
kubectl describe svc penguincode-server -n penguincode

# Check which pods are behind service
kubectl get pods -n penguincode -l app=penguincode -o wide
```

## Debugging and Troubleshooting Commands

### Describe Resources
```bash
# Describe pod (shows events, conditions, container details)
kubectl describe pod <pod-name> -n penguincode

# Describe deployment
kubectl describe deployment penguincode-server -n penguincode

# Describe service
kubectl describe svc penguincode-server -n penguincode
```

### Exec into Pod
```bash
# Get shell access
kubectl exec -it <pod-name> -n penguincode -- /bin/sh

# Run command
kubectl exec <pod-name> -n penguincode -- echo "test"

# View environment variables
kubectl exec <pod-name> -n penguincode -- env
```

### Check Pod Details
```bash
# Get pod YAML
kubectl get pod <pod-name> -n penguincode -o yaml

# Get pod JSON
kubectl get pod <pod-name> -n penguincode -o json

# Get specific field
kubectl get pod <pod-name> -n penguincode -o jsonpath='{.status.containerStatuses[0].image}'

# Get all pod info
kubectl describe pod <pod-name> -n penguincode
```

### Check Image and Resources
```bash
# See running image
kubectl get pods -n penguincode -o wide

# Get image digest
kubectl get pod <pod-name> -n penguincode -o jsonpath='{.status.containerStatuses[0].imageID}'

# Check resource requests/limits
kubectl get pod <pod-name> -n penguincode -o jsonpath='{.spec.containers[0].resources}'

# Get all pod images
kubectl get pods -n penguincode -o jsonpath='{.items[*].spec.containers[*].image}'
```

## Update and Management Commands

### Update Deployment
```bash
# Set new image
kubectl set image deployment/penguincode-server \
  penguincode-server=ghcr.io/penguintechinc/penguincode:new-tag \
  -n penguincode

# Watch rollout
kubectl rollout status deployment/penguincode-server -n penguincode -w

# Update via Helm
helm upgrade penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-beta.yaml \
  -n penguincode

# Scale replicas
kubectl scale deployment penguincode-server \
  --replicas=3 \
  -n penguincode

# Edit deployment directly (use with caution)
kubectl edit deployment penguincode-server -n penguincode
```

### Restart and Rollout Management
```bash
# Rollout undo (revert to previous)
kubectl rollout undo deployment/penguincode-server -n penguincode

# Rollout to specific revision
kubectl rollout history deployment/penguincode-server -n penguincode
kubectl rollout undo deployment/penguincode-server -n penguincode --to-revision=2

# Restart deployment (rolling restart)
kubectl rollout restart deployment/penguincode-server -n penguincode

# Check rollout history
kubectl rollout history deployment/penguincode-server -n penguincode
```

### Helm Rollback
```bash
# Rollback to previous release
helm rollback penguincode -n penguincode

# Rollback to specific revision
helm rollback penguincode 1 -n penguincode

# Check history
helm history penguincode -n penguincode
```

### Using Deploy Script
```bash
# Rollback using script
./scripts/deploy-beta.sh --rollback penguincode-1

# Get help
./scripts/deploy-beta.sh --help

# Verbose output
./scripts/deploy-beta.sh --verbose
```

## Deletion and Cleanup Commands

### Delete Deployments
```bash
# Delete using Kustomize
kubectl delete -k k8s/kustomize/overlays/beta

# Delete using Helm
helm uninstall penguincode -n penguincode

# Delete deployment directly
kubectl delete deployment penguincode-server -n penguincode

# Delete all in namespace
kubectl delete all -n penguincode
```

### Delete Pods (for restart)
```bash
# Delete single pod (will be recreated)
kubectl delete pod <pod-name> -n penguincode

# Delete all pods in label
kubectl delete pods -n penguincode -l app=penguincode

# Force delete stuck pod
kubectl delete pod <pod-name> -n penguincode --grace-period=0 --force
```

### Delete Namespace
```bash
# Delete entire namespace (WARNING: removes everything)
kubectl delete namespace penguincode

# Delete namespace (force, immediate)
kubectl delete namespace penguincode --grace-period=0 --force
```

## Configuration and Template Commands

### Kustomize
```bash
# Build Kustomize configuration (show YAML without applying)
kubectl kustomize k8s/kustomize/overlays/beta

# Build and save to file
kubectl kustomize k8s/kustomize/overlays/beta > release.yaml

# Validate output
kubectl apply -k k8s/kustomize/overlays/beta --dry-run=client --validate=true
```

### Helm
```bash
# Lint chart for errors
helm lint k8s/helm/penguincode

# Template render (show rendered YAML)
helm template penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-beta.yaml

# Dry-run install
helm install penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-beta.yaml \
  -n penguincode \
  --dry-run --debug

# Show differences
helm diff upgrade penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-beta.yaml \
  -n penguincode
```

## Context and Access Commands

### Kubernetes Context
```bash
# Check current context
kubectl config current-context

# List contexts
kubectl config get-contexts

# Switch context
kubectl config use-context dal2-beta

# Get cluster info
kubectl cluster-info
```

### Namespace Management
```bash
# Get all namespaces
kubectl get namespaces

# Create namespace
kubectl create namespace penguincode-custom

# Set default namespace
kubectl config set-context --current --namespace=penguincode

# Get current namespace
kubectl config view --minify -o jsonpath='{.contexts[0].context.namespace}'
```

### Service Account and RBAC
```bash
# List service accounts
kubectl get serviceaccounts -n penguincode

# Describe service account
kubectl describe sa penguincode -n penguincode

# Check RBAC permissions
kubectl auth can-i get pods -n penguincode --as=system:serviceaccount:penguincode:penguincode
```

## Network and Port Forwarding

### Port Forwarding
```bash
# Forward service port to local
kubectl port-forward -n penguincode svc/penguincode-server 50051:50051

# Forward to local port (background)
kubectl port-forward -n penguincode svc/penguincode-server 8080:8080 &

# Multiple ports
kubectl port-forward -n penguincode svc/penguincode-server 8080:8080 50051:50051

# Port forward to specific pod
kubectl port-forward -n penguincode <pod-name> 8080:8080

# Stop background port-forward
pkill -f "port-forward"
```

### Service Access
```bash
# Get service details
kubectl get svc -n penguincode -o wide

# Get service endpoint
kubectl get svc penguincode-server -n penguincode -o jsonpath='{.status.loadBalancer.ingress[0].ip}'

# DNS name in cluster
# From inside cluster: penguincode-server.penguincode.svc.cluster.local

# Describe service
kubectl describe svc penguincode-server -n penguincode
```

## Tips and Tricks

### Watch Multiple Resources
```bash
# Watch pods
kubectl get pods -n penguincode -w

# Watch deployment
kubectl get deployment -n penguincode -w

# Watch events
kubectl get events -n penguincode -w

# Watch resources with wide output
kubectl get pods -n penguincode -w -o wide
```

### One-Liners
```bash
# Get all pod names
kubectl get pods -n penguincode -o name

# Get pod image
kubectl get pods -n penguincode -o jsonpath='{.items[*].spec.containers[*].image}'

# Get restart count
kubectl get pods -n penguincode -o jsonpath='{.items[*].status.containerStatuses[*].restartCount}'

# Get pod IPs
kubectl get pods -n penguincode -o jsonpath='{.items[*].status.podIP}'

# Follow logs from all pods
kubectl logs -n penguincode -l app=penguincode -f --all-containers=true
```

### Configuration and Secrets
```bash
# List ConfigMaps
kubectl get configmap -n penguincode

# View ConfigMap
kubectl get configmap <name> -n penguincode -o yaml

# List Secrets
kubectl get secrets -n penguincode

# View Secret (base64 decoded)
kubectl get secret <name> -n penguincode -o jsonpath='{.data.KEY}' | base64 -d
```

## Common Troubleshooting One-Liners

```bash
# Check if pods are stuck
kubectl get pods -n penguincode -o jsonpath='{.items[?(@.status.phase!="Running")].metadata.name}'

# Find crashing pods
kubectl get pods -n penguincode --field-selector=status.phase!=Running

# Get pod events
kubectl describe pod <pod-name> -n penguincode | tail -20

# Check resource limits exceeded
kubectl top pod -n penguincode | awk '{if(NR>1) print $1 " CPU: " $2 " Mem: " $3}'

# Show pod creation timestamps
kubectl get pods -n penguincode -o jsonpath='{.items[*].metadata.creationTimestamp}'

# Get failed pod logs
kubectl logs -n penguincode --previous <pod-name> 2>/dev/null || echo "No previous logs"
```

## See Also

- For comprehensive deployment guide: `docs/k8s-deployment.md`
- For deployment checklist: `docs/k8s-deployment-checklist.md`
- For quick reference: `k8s/README.md`
- For script options: `./scripts/deploy-beta.sh --help`

---

All commands use the `penguincode` namespace — it is the same in every cluster (alpha,
beta, prod); only the `--context`/cluster changes between environments, never the
namespace suffix.
