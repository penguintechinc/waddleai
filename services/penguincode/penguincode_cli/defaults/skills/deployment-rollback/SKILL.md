---
name: deployment-rollback
description: "Rollback procedures, canary verification, and incident recovery"
model: qwen2.5-coder:7b
---

# Deployment Rollback

## Overview
Quickly and safely roll back deployments when issues are detected in production.

## When to Rollback
- Error rate spike after deployment
- Health checks failing
- User-facing functionality broken
- Performance degradation beyond SLA

## Kubernetes Rollback
```bash
# View rollout history
kubectl rollout history deployment/<name>

# Rollback to previous version
kubectl rollout undo deployment/<name>

# Rollback to specific revision
kubectl rollout undo deployment/<name> --to-revision=<N>

# Monitor rollback progress
kubectl rollout status deployment/<name>
```

## Docker Compose Rollback
```bash
# Re-deploy with previous image tag
docker-compose pull
docker-compose up -d

# Or use specific image tag
IMAGE_TAG=v1.2.2 docker-compose up -d
```

## Verification After Rollback
1. All pods/containers healthy
2. Health endpoints returning 200
3. Error rate returning to baseline
4. No data corruption

## Post-Rollback Actions
1. Confirm service is stable
2. Investigate root cause (see waddlepowers:monitoring-and-logging)
3. Fix the issue in a new branch
4. Add tests to prevent recurrence
5. Write postmortem (see waddlepowers:incident-response)
