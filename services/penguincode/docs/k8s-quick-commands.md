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
  -n penguincode-alpha --create-namespace

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
  -n penguincode-beta --create-namespace

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
  -n penguincode-beta --create-namespace \
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
kubectl get pods -n penguincode-alpha
kubectl get pods -n penguincode-beta

# Watch deployment progress
kubectl get pods -n penguincode-beta -w

# Get detailed deployment status
kubectl get deployment -n penguincode-beta -o wide

# Check rollout status
kubectl rollout status deployment/penguincode-server -n penguincode-beta

# Get all resources in namespace
kubectl get all -n penguincode-beta
```

### View Logs
```bash
# Current logs
kubectl logs -n penguincode-beta -l app=penguincode

# Tail logs in real-time
kubectl logs -n penguincode-beta -l app=penguincode -f

# Specific pod logs
kubectl logs -n penguincode-beta <pod-name>

# Previous pod logs (if crash)
kubectl logs -n penguincode-beta <pod-name> --previous

# All containers (if multiple)
kubectl logs -n penguincode-beta <pod-name> --all-containers=true

# With timestamps
kubectl logs -n penguincode-beta -l app=penguincode --timestamps=true

# Last N lines
kubectl logs -n penguincode-beta <pod-name> --tail=50
```

### Check Events
```bash
# All events in namespace
kubectl get events -n penguincode-beta

# Sorted by time
kubectl get events -n penguincode-beta --sort-by='.lastTimestamp'

# Watch events
kubectl get events -n penguincode-beta -w

# Describe pod (includes events)
kubectl describe pod <pod-name> -n penguincode-beta
```

### Helm Status
```bash
# Check release status
helm status penguincode -n penguincode-beta

# List releases
helm list -n penguincode-beta

# View release history
helm history penguincode -n penguincode-beta

# Get current values
helm get values penguincode -n penguincode-beta

# Get rendered manifests
helm get manifest penguincode -n penguincode-beta
```

## Verification and Health Commands

### Test Health Endpoint
```bash
# Port forward
kubectl port-forward -n penguincode-beta svc/penguincode-server 8080:8080 &

# Test health in another terminal
curl http://localhost:8080/api/v1/health

# Clean up port forward
pkill -f "port-forward"
```

### Test gRPC Service
```bash
# Port forward gRPC port
kubectl port-forward -n penguincode-beta svc/penguincode-server 50051:50051 &

# List services (requires grpcurl)
grpcurl -plaintext localhost:50051 list

# Clean up
pkill -f "port-forward"
```

### Check Resource Usage
```bash
# Pod resource usage
kubectl top pod -n penguincode-beta

# Node resource usage
kubectl top nodes

# Specific pod resources
kubectl top pod <pod-name> -n penguincode-beta

# Watch resource usage
kubectl top pod -n penguincode-beta -w
```

### Check Service and Endpoints
```bash
# List services
kubectl get svc -n penguincode-beta

# Check endpoints
kubectl get endpoints -n penguincode-beta

# Describe service
kubectl describe svc penguincode-server -n penguincode-beta

# Check which pods are behind service
kubectl get pods -n penguincode-beta -l app=penguincode -o wide
```

## Debugging and Troubleshooting Commands

### Describe Resources
```bash
# Describe pod (shows events, conditions, container details)
kubectl describe pod <pod-name> -n penguincode-beta

# Describe deployment
kubectl describe deployment penguincode-server -n penguincode-beta

# Describe service
kubectl describe svc penguincode-server -n penguincode-beta
```

### Exec into Pod
```bash
# Get shell access
kubectl exec -it <pod-name> -n penguincode-beta -- /bin/sh

# Run command
kubectl exec <pod-name> -n penguincode-beta -- echo "test"

# View environment variables
kubectl exec <pod-name> -n penguincode-beta -- env
```

### Check Pod Details
```bash
# Get pod YAML
kubectl get pod <pod-name> -n penguincode-beta -o yaml

# Get pod JSON
kubectl get pod <pod-name> -n penguincode-beta -o json

# Get specific field
kubectl get pod <pod-name> -n penguincode-beta -o jsonpath='{.status.containerStatuses[0].image}'

# Get all pod info
kubectl describe pod <pod-name> -n penguincode-beta
```

### Check Image and Resources
```bash
# See running image
kubectl get pods -n penguincode-beta -o wide

# Get image digest
kubectl get pod <pod-name> -n penguincode-beta -o jsonpath='{.status.containerStatuses[0].imageID}'

# Check resource requests/limits
kubectl get pod <pod-name> -n penguincode-beta -o jsonpath='{.spec.containers[0].resources}'

# Get all pod images
kubectl get pods -n penguincode-beta -o jsonpath='{.items[*].spec.containers[*].image}'
```

## Update and Management Commands

### Update Deployment
```bash
# Set new image
kubectl set image deployment/penguincode-server \
  penguincode-server=registry-dal2.penguintech.io/penguincode:new-tag \
  -n penguincode-beta

# Watch rollout
kubectl rollout status deployment/penguincode-server -n penguincode-beta -w

# Update via Helm
helm upgrade penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-beta.yaml \
  -n penguincode-beta

# Scale replicas
kubectl scale deployment penguincode-server \
  --replicas=3 \
  -n penguincode-beta

# Edit deployment directly (use with caution)
kubectl edit deployment penguincode-server -n penguincode-beta
```

### Restart and Rollout Management
```bash
# Rollout undo (revert to previous)
kubectl rollout undo deployment/penguincode-server -n penguincode-beta

# Rollout to specific revision
kubectl rollout history deployment/penguincode-server -n penguincode-beta
kubectl rollout undo deployment/penguincode-server -n penguincode-beta --to-revision=2

# Restart deployment (rolling restart)
kubectl rollout restart deployment/penguincode-server -n penguincode-beta

# Check rollout history
kubectl rollout history deployment/penguincode-server -n penguincode-beta
```

### Helm Rollback
```bash
# Rollback to previous release
helm rollback penguincode -n penguincode-beta

# Rollback to specific revision
helm rollback penguincode 1 -n penguincode-beta

# Check history
helm history penguincode -n penguincode-beta
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
helm uninstall penguincode -n penguincode-beta

# Delete deployment directly
kubectl delete deployment penguincode-server -n penguincode-beta

# Delete all in namespace
kubectl delete all -n penguincode-beta
```

### Delete Pods (for restart)
```bash
# Delete single pod (will be recreated)
kubectl delete pod <pod-name> -n penguincode-beta

# Delete all pods in label
kubectl delete pods -n penguincode-beta -l app=penguincode

# Force delete stuck pod
kubectl delete pod <pod-name> -n penguincode-beta --grace-period=0 --force
```

### Delete Namespace
```bash
# Delete entire namespace (WARNING: removes everything)
kubectl delete namespace penguincode-beta

# Delete namespace (force, immediate)
kubectl delete namespace penguincode-beta --grace-period=0 --force
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
  -n penguincode-beta \
  --dry-run --debug

# Show differences
helm diff upgrade penguincode k8s/helm/penguincode \
  -f k8s/helm/penguincode/values-beta.yaml \
  -n penguincode-beta
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
kubectl config set-context --current --namespace=penguincode-beta

# Get current namespace
kubectl config view --minify -o jsonpath='{.contexts[0].context.namespace}'
```

### Service Account and RBAC
```bash
# List service accounts
kubectl get serviceaccounts -n penguincode-beta

# Describe service account
kubectl describe sa penguincode -n penguincode-beta

# Check RBAC permissions
kubectl auth can-i get pods -n penguincode-beta --as=system:serviceaccount:penguincode-beta:penguincode
```

## Network and Port Forwarding

### Port Forwarding
```bash
# Forward service port to local
kubectl port-forward -n penguincode-beta svc/penguincode-server 50051:50051

# Forward to local port (background)
kubectl port-forward -n penguincode-beta svc/penguincode-server 8080:8080 &

# Multiple ports
kubectl port-forward -n penguincode-beta svc/penguincode-server 8080:8080 50051:50051

# Port forward to specific pod
kubectl port-forward -n penguincode-beta <pod-name> 8080:8080

# Stop background port-forward
pkill -f "port-forward"
```

### Service Access
```bash
# Get service details
kubectl get svc -n penguincode-beta -o wide

# Get service endpoint
kubectl get svc penguincode-server -n penguincode-beta -o jsonpath='{.status.loadBalancer.ingress[0].ip}'

# DNS name in cluster
# From inside cluster: penguincode-server.penguincode-beta.svc.cluster.local

# Describe service
kubectl describe svc penguincode-server -n penguincode-beta
```

## Tips and Tricks

### Watch Multiple Resources
```bash
# Watch pods
kubectl get pods -n penguincode-beta -w

# Watch deployment
kubectl get deployment -n penguincode-beta -w

# Watch events
kubectl get events -n penguincode-beta -w

# Watch resources with wide output
kubectl get pods -n penguincode-beta -w -o wide
```

### One-Liners
```bash
# Get all pod names
kubectl get pods -n penguincode-beta -o name

# Get pod image
kubectl get pods -n penguincode-beta -o jsonpath='{.items[*].spec.containers[*].image}'

# Get restart count
kubectl get pods -n penguincode-beta -o jsonpath='{.items[*].status.containerStatuses[*].restartCount}'

# Get pod IPs
kubectl get pods -n penguincode-beta -o jsonpath='{.items[*].status.podIP}'

# Follow logs from all pods
kubectl logs -n penguincode-beta -l app=penguincode -f --all-containers=true
```

### Configuration and Secrets
```bash
# List ConfigMaps
kubectl get configmap -n penguincode-beta

# View ConfigMap
kubectl get configmap <name> -n penguincode-beta -o yaml

# List Secrets
kubectl get secrets -n penguincode-beta

# View Secret (base64 decoded)
kubectl get secret <name> -n penguincode-beta -o jsonpath='{.data.KEY}' | base64 -d
```

## Common Troubleshooting One-Liners

```bash
# Check if pods are stuck
kubectl get pods -n penguincode-beta -o jsonpath='{.items[?(@.status.phase!="Running")].metadata.name}'

# Find crashing pods
kubectl get pods -n penguincode-beta --field-selector=status.phase!=Running

# Get pod events
kubectl describe pod <pod-name> -n penguincode-beta | tail -20

# Check resource limits exceeded
kubectl top pod -n penguincode-beta | awk '{if(NR>1) print $1 " CPU: " $2 " Mem: " $3}'

# Show pod creation timestamps
kubectl get pods -n penguincode-beta -o jsonpath='{.items[*].metadata.creationTimestamp}'

# Get failed pod logs
kubectl logs -n penguincode-beta --previous <pod-name> 2>/dev/null || echo "No previous logs"
```

## See Also

- For comprehensive deployment guide: `docs/k8s-deployment.md`
- For deployment checklist: `docs/k8s-deployment-checklist.md`
- For quick reference: `k8s/README.md`
- For script options: `./scripts/deploy-beta.sh --help`

---

All commands use the penguincode namespace. Update namespace names (penguincode-alpha, penguincode-beta) as needed for your environment.
