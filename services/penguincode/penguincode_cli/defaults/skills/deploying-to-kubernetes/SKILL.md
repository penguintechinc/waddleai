---
name: deploying-to-kubernetes
description: "kubectl apply, rollout strategy, and deployment verification"
model: qwen2.5-coder:7b
---

# Deploying to Kubernetes

## Overview
Deploy applications to Kubernetes clusters with proper rollout strategies and verification.

## Deployment Workflow
1. **Build image** (see waddlepowers:building-docker-images)
2. **Push to registry**: `docker push registry/app:tag`
3. **Apply manifests**: `kubectl apply -f k8s/`
4. **Monitor rollout**: `kubectl rollout status deployment/<name>`
5. **Verify**: check pods, logs, and endpoints

## Common Commands
```bash
# Apply manifests
kubectl apply -f k8s/manifests/

# Check rollout status
kubectl rollout status deployment/app -n default

# View pods
kubectl get pods -n default

# Check events
kubectl get events --sort-by=.metadata.creationTimestamp

# Port forward for testing
kubectl port-forward svc/app 8080:80
```

## Rollout Strategies
- **RollingUpdate** (default): gradually replaces pods
- **Recreate**: kills all old pods, then creates new ones

## Kustomize
```bash
# Apply with kustomize overlays
kubectl apply -k k8s/kustomize/overlays/production/
```

## Verification
- All pods in Running state
- No CrashLoopBackOff or Error states
- Health checks passing
- Service endpoints populated

## Rollback
```bash
kubectl rollout undo deployment/<name>
```
See waddlepowers:deployment-rollback for detailed rollback procedures.
