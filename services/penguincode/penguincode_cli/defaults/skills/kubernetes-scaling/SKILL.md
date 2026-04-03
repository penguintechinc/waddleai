---
name: kubernetes-scaling
description: "HPA, VPA, resource limits, and scaling strategies"
model: qwen2.5-coder:7b
---

# Kubernetes Scaling

## Overview
Configure auto-scaling, resource limits, and scaling strategies for Kubernetes workloads.

## Horizontal Pod Autoscaler (HPA)
```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: app-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: app
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
```

## Resource Limits
```yaml
resources:
  requests:
    memory: "128Mi"
    cpu: "250m"
  limits:
    memory: "512Mi"
    cpu: "1000m"
```

## Manual Scaling
```bash
# Scale deployment
kubectl scale deployment/<name> --replicas=5

# Check HPA status
kubectl get hpa

# Describe HPA
kubectl describe hpa <name>
```

## Best Practices
- Always set resource requests and limits
- Use HPA for stateless workloads
- Set appropriate min/max replicas
- Monitor scaling events: `kubectl get events`
- Use PodDisruptionBudgets for availability during scaling
