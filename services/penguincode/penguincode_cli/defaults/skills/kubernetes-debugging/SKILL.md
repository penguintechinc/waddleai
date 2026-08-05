---
name: kubernetes-debugging
description: "Pod logs, describe, events, exec for Kubernetes troubleshooting"
model: qwen2.5-coder:7b
---

# Kubernetes Debugging

## Overview
Systematically diagnose issues with Kubernetes deployments, pods, and services.

## Debugging Flow
1. **Check pod status**: `kubectl get pods -n <namespace>`
2. **Describe the pod**: `kubectl describe pod <name>`
3. **Check logs**: `kubectl logs <pod>`
4. **Check events**: `kubectl get events --sort-by=.metadata.creationTimestamp`
5. **Exec into pod**: `kubectl exec -it <pod> -- /bin/sh`

## Common Issues

### CrashLoopBackOff
```bash
kubectl logs <pod> --previous  # Logs from crashed container
kubectl describe pod <pod>      # Check events for OOMKilled, etc.
```

### ImagePullBackOff
- Verify image exists in registry
- Check imagePullSecrets
- Verify network access to registry

### Pending Pods
```bash
kubectl describe pod <pod>  # Check events for scheduling issues
kubectl get nodes            # Verify node resources
```

### Service Not Reachable
```bash
kubectl get endpoints <service>   # Check if endpoints are populated
kubectl get svc <service>          # Verify service type and ports
kubectl run debug --image=busybox --rm -it -- wget -qO- http://service:port
```

## Resource Inspection
```bash
# Node resources
kubectl top nodes

# Pod resources
kubectl top pods -n <namespace>

# Describe everything in namespace
kubectl get all -n <namespace>
```
