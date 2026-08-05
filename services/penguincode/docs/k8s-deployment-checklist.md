# PenguinCode Kubernetes Deployment Checklist

Complete checklist for deploying PenguinCode to Kubernetes environments.

## Pre-Deployment Checklist

### Environment Setup
- [ ] Docker installed and running (`docker version`)
- [ ] kubectl installed (`kubectl version --client`)
- [ ] Helm installed (`helm version`)
- [ ] kustomize available (or use built-in kubectl support)
- [ ] Kubernetes cluster accessible (`kubectl cluster-info`)
- [ ] Correct kubeconfig configured (`kubectl config current-context`)

### Cluster Verification
- [ ] Kubernetes version 1.24+ (`kubectl version --server`)
- [ ] Access to appropriate Kubernetes context (dal2-beta for beta)
- [ ] Cluster resource availability (`kubectl top nodes`)
- [ ] Storage provisioner available (if using persistence)
- [ ] Container registry access (`docker login registry-dal2.penguintech.io`)

### Code Repository
- [ ] Latest code pulled (`git status`)
- [ ] Clean working directory (no uncommitted changes)
- [ ] Correct branch checked out (main for production)
- [ ] Dockerfile.server present and valid (`ls -l Dockerfile.server`)
- [ ] All manifests files present

### Configuration Review
- [ ] Helm values reviewed (values.yaml, values-alpha.yaml, values-beta.yaml)
- [ ] Kustomize overlays reviewed (alpha/ and beta/)
- [ ] Deploy script reviewed and understood (`./scripts/deploy-beta.sh --help`)
- [ ] Target namespace confirmed (penguincode-alpha or penguincode-beta)
- [ ] Image registry configured correctly (registry-dal2.penguintech.io for beta)

## Alpha Deployment Checklist

### Pre-Deployment
- [ ] Using correct kubeconfig for alpha cluster
- [ ] No conflicts with existing deployments
- [ ] Local Docker images available or will be built
- [ ] DEBUG logging desired (alpha default)
- [ ] Small resource footprint acceptable (1 replica, 100m/200m CPU)

### Deployment Steps
- [ ] Option 1 - Kustomize:
  ```bash
  kubectl apply -k k8s/kustomize/overlays/alpha
  ```
  - [ ] Dry-run first: `kubectl apply -k k8s/kustomize/overlays/alpha --dry-run=client -o yaml`
  - [ ] Review output
  - [ ] Apply changes

- [ ] Option 2 - Helm:
  ```bash
  helm install penguincode k8s/helm/penguincode \
    -f k8s/helm/penguincode/values-alpha.yaml \
    -n penguincode-alpha --create-namespace
  ```
  - [ ] Lint first: `helm lint k8s/helm/penguincode`
  - [ ] Dry-run first: add `--dry-run --debug`
  - [ ] Review output

### Post-Deployment Verification
- [ ] Namespace created: `kubectl get ns | grep penguincode-alpha`
- [ ] ServiceAccount created: `kubectl get sa -n penguincode-alpha`
- [ ] Pods running: `kubectl get pods -n penguincode-alpha`
- [ ] Deployment ready: `kubectl get deployment -n penguincode-alpha`
- [ ] Service created: `kubectl get svc -n penguincode-alpha`
- [ ] Pod logs clean: `kubectl logs -n penguincode-alpha -l app=penguincode`
- [ ] Health endpoint responds:
  ```bash
  kubectl port-forward -n penguincode-alpha svc/penguincode-server 8080:8080 &
  curl http://localhost:8080/api/v1/health
  ```

### Testing
- [ ] Port-forward to service: `kubectl port-forward -n penguincode-alpha svc/penguincode-server 50051:50051`
- [ ] Test gRPC connectivity with grpcurl or client
- [ ] Check logs for errors: `kubectl logs -n penguincode-alpha -l app=penguincode -f`
- [ ] Monitor resource usage: `kubectl top pods -n penguincode-alpha`

### Cleanup (if needed)
- [ ] Delete deployment: `kubectl delete -k k8s/kustomize/overlays/alpha`
- [ ] Or uninstall helm: `helm uninstall penguincode -n penguincode-alpha`
- [ ] Verify cleanup: `kubectl get all -n penguincode-alpha`

## Beta Deployment Checklist

### Pre-Deployment
- [ ] Using correct kubeconfig for dal2-beta cluster
- [ ] Connected to dal2-beta context: `kubectl config current-context`
- [ ] Docker image will be built and pushed
- [ ] Registry credentials available for push
- [ ] Helm release doesn't already exist (check with `helm list`)
- [ ] High availability desired (2 replicas, standard resources)
- [ ] Auth enabled in values confirmed

### Image Building and Pushing
- [ ] Option 1 - Use deploy script:
  ```bash
  ./scripts/deploy-beta.sh --tag <tag>
  ```
  - [ ] Script is executable: `ls -l scripts/deploy-beta.sh`
  - [ ] Review script with `--dry-run`: `./scripts/deploy-beta.sh --dry-run`

- [ ] Option 2 - Manual build and push:
  - [ ] Build image:
    ```bash
    docker build -t registry-dal2.penguintech.io/penguincode:beta-latest \
      -f Dockerfile.server .
    ```
    - [ ] Build succeeds without errors
    - [ ] Image is reasonably sized

  - [ ] Push image:
    ```bash
    docker push registry-dal2.penguintech.io/penguincode:beta-latest
    ```
    - [ ] Login to registry first if needed
    - [ ] Push completes successfully

### Deployment Steps
- [ ] Option 1 - Deploy script (recommended):
  ```bash
  ./scripts/deploy-beta.sh --tag <tag-or-default>
  ```
  - [ ] Verify prerequisites pass
  - [ ] Image builds and pushes
  - [ ] Helm upgrade succeeds
  - [ ] Deployment reaches ready state
  - [ ] Health check passes

- [ ] Option 2 - Kustomize:
  ```bash
  kubectl apply -k k8s/kustomize/overlays/beta
  ```
  - [ ] Dry-run first: `kubectl apply -k k8s/kustomize/overlays/beta --dry-run=client -o yaml`
  - [ ] Review namespace (penguincode-beta)
  - [ ] Review resource names (beta- prefix)
  - [ ] Review image registry (registry-dal2.penguintech.io)

- [ ] Option 3 - Helm:
  ```bash
  helm install penguincode k8s/helm/penguincode \
    -f k8s/helm/penguincode/values-beta.yaml \
    -f k8s/helm/penguincode/values.yaml \
    -n penguincode-beta --create-namespace \
    --set image.tag=<your-tag>
  ```
  - [ ] Lint chart: `helm lint k8s/helm/penguincode`
  - [ ] Template render: `helm template penguincode k8s/helm/penguincode -f k8s/helm/penguincode/values-beta.yaml`
  - [ ] Review rendered manifests
  - [ ] Apply with --wait: add `--wait --timeout 5m`

### Deployment Verification
- [ ] Namespace created: `kubectl get ns | grep penguincode-beta`
- [ ] ServiceAccount created: `kubectl get sa -n penguincode-beta`
- [ ] Deployment created: `kubectl get deployment -n penguincode-beta`
- [ ] Replicas: 2 running and ready: `kubectl get deployment -n penguincode-beta`
- [ ] Pods healthy: `kubectl get pods -n penguincode-beta`
- [ ] Service endpoints: `kubectl get endpoints -n penguincode-beta`
- [ ] Recent image deployed: `kubectl get pods -n penguincode-beta -o wide`
- [ ] Logs showing startup:
  ```bash
  kubectl logs -n penguincode-beta -l app=penguincode -f
  ```
- [ ] No error events:
  ```bash
  kubectl get events -n penguincode-beta --sort-by='.lastTimestamp'
  ```

### Health and Connectivity Verification
- [ ] Pod health probes passing:
  ```bash
  kubectl get pods -n penguincode-beta -o jsonpath='{.items[*].status.conditions}'
  ```

- [ ] Health endpoint responding:
  ```bash
  kubectl port-forward -n penguincode-beta svc/penguincode-server 8080:8080
  curl http://localhost:8080/api/v1/health
  ```

- [ ] Service connectivity:
  ```bash
  kubectl port-forward -n penguincode-beta svc/penguincode-server 50051:50051
  grpcurl -plaintext localhost:50051 list
  ```

- [ ] Resource usage acceptable:
  ```bash
  kubectl top pods -n penguincode-beta
  kubectl top nodes
  ```

- [ ] Ingress working (if enabled):
  ```bash
  curl https://penguincode.penguintech.io/api/v1/health
  ```

### Monitoring Setup (Optional)
- [ ] Configure pod monitoring (Prometheus, etc.)
- [ ] Set up log aggregation (ELK, Loki, etc.)
- [ ] Configure alerts for pod crashes
- [ ] Set up dashboard for deployment metrics

### Helm Release Management
- [ ] Check release status: `helm status penguincode -n penguincode-beta`
- [ ] View release history: `helm history penguincode -n penguincode-beta`
- [ ] Get deployment values: `helm get values penguincode -n penguincode-beta`
- [ ] Document release notes for your team

### Testing
- [ ] Connectivity test: Connect to gRPC service
- [ ] Health check: HTTP GET to /api/v1/health
- [ ] Load test: Send test requests
- [ ] Error handling: Test failure scenarios
- [ ] Performance baseline: Measure response times

## Post-Deployment Checklist

### Documentation
- [ ] Update deployment documentation with actual values used
- [ ] Document any custom configurations
- [ ] Note any issues encountered and their solutions
- [ ] Add team notes to deployment runbook
- [ ] Update release notes

### Monitoring
- [ ] Confirm alerts are firing
- [ ] Check log aggregation is working
- [ ] Verify dashboards show data
- [ ] Set up on-call procedures

### Backup and Recovery
- [ ] Document current state: `helm get manifest penguincode -n penguincode-beta > backup.yaml`
- [ ] Test rollback procedure: `./scripts/deploy-beta.sh --rollback penguincode-1`
- [ ] Verify rollback worked
- [ ] Document rollback steps

### Team Communication
- [ ] Notify team of deployment completion
- [ ] Share access information and commands
- [ ] Schedule any necessary training
- [ ] Gather feedback on deployment process

## Rollback Checklist

### Decision to Rollback
- [ ] Issues confirmed in current release
- [ ] Rollback approved by appropriate team member
- [ ] Communication plan in place

### Execution
- [ ] Option 1 - Using deploy script:
  ```bash
  ./scripts/deploy-beta.sh --rollback penguincode-<revision>
  ```

- [ ] Option 2 - Using Helm directly:
  ```bash
  helm rollback penguincode -n penguincode-beta
  helm rollout status deployment/penguincode-server -n penguincode-beta
  ```

- [ ] Option 3 - Using kubectl:
  ```bash
  kubectl rollout undo deployment/penguincode-server -n penguincode-beta
  kubectl rollout status deployment/penguincode-server -n penguincode-beta
  ```

### Verification After Rollback
- [ ] Previous version confirmed running
- [ ] Pods healthy and ready
- [ ] Health endpoints responding
- [ ] Services responding normally
- [ ] Monitoring shows green status
- [ ] Team notified of rollback

### Post-Rollback Analysis
- [ ] Analyze cause of issues in failed release
- [ ] Review deployment logs and events
- [ ] Update deployment checklist if needed
- [ ] Plan fixes for next deployment attempt
- [ ] Document lessons learned

## Troubleshooting Decision Tree

### Pods not starting?
- [ ] Check pod status: `kubectl get pods -n penguincode-beta`
- [ ] Describe pod: `kubectl describe pod <name> -n penguincode-beta`
- [ ] Check events: `kubectl get events -n penguincode-beta`
- [ ] Check logs: `kubectl logs <pod-name> -n penguincode-beta`
- [ ] Resolution checklist:
  - [ ] Image exists and is correct
  - [ ] Image pull secrets configured
  - [ ] Resources available on nodes
  - [ ] Security context not blocking

### Image pull failing?
- [ ] Verify registry credentials: `docker login registry-dal2.penguintech.io`
- [ ] Check image exists: `docker pull registry-dal2.penguintech.io/penguincode:tag`
- [ ] Verify pull policy: `kubectl get deployment -n penguincode-beta -o yaml | grep imagePullPolicy`
- [ ] Check pull secrets: `kubectl get secrets -n penguincode-beta`

### Pods crashing?
- [ ] Check pod logs: `kubectl logs <pod-name> -n penguincode-beta`
- [ ] Check previous logs: `kubectl logs <pod-name> -n penguincode-beta --previous`
- [ ] Describe pod: `kubectl describe pod <pod-name> -n penguincode-beta`
- [ ] Check resource limits: `kubectl top pod <pod-name> -n penguincode-beta`
- [ ] Check for OOM: Look for "Out of memory" in events

### Service not responding?
- [ ] Check service exists: `kubectl get svc -n penguincode-beta`
- [ ] Check endpoints: `kubectl get endpoints -n penguincode-beta`
- [ ] Check pod labels: `kubectl get pods -n penguincode-beta -L app,component`
- [ ] Test port-forward: `kubectl port-forward svc/penguincode-server 8080:8080 -n penguincode-beta`

### Deployment stuck?
- [ ] Check deployment status: `kubectl get deployment -n penguincode-beta`
- [ ] Check rollout status: `kubectl rollout status deployment/penguincode-server -n penguincode-beta`
- [ ] Increase timeout: `kubectl rollout status deployment/penguincode-server -n penguincode-beta --timeout=10m`
- [ ] Force rollout: `kubectl rollout restart deployment/penguincode-server -n penguincode-beta`

## Emergency Procedures

### Immediate Rollback (Service Down)
```bash
# Option 1 - Using script (fastest)
./scripts/deploy-beta.sh --rollback penguincode-1

# Option 2 - Using Helm
helm rollback penguincode 0 -n penguincode-beta

# Verify
kubectl rollout status deployment/penguincode-server -n penguincode-beta
```

### Delete Stuck Deployment
```bash
# Force delete pods
kubectl delete pods -n penguincode-beta -l app=penguincode --grace-period=0 --force

# Delete deployment
kubectl delete deployment penguincode-server -n penguincode-beta --grace-period=0 --force

# Redeploy
kubectl apply -k k8s/kustomize/overlays/beta
```

### Scale Down (Resource Issues)
```bash
# Reduce replicas
kubectl scale deployment penguincode-server -n penguincode-beta --replicas=1

# Investigate
# ...

# Scale back up
kubectl scale deployment penguincode-server -n penguincode-beta --replicas=2
```

### Clear All and Restart
```bash
# Delete everything
kubectl delete -k k8s/kustomize/overlays/beta

# Wait for cleanup
sleep 10

# Redeploy
kubectl apply -k k8s/kustomize/overlays/beta

# Monitor
kubectl rollout status deployment/penguincode-server -n penguincode-beta --watch
```

## Sign-Off

- [ ] All checklist items completed
- [ ] Deployment verified successful
- [ ] Team notified
- [ ] Documentation updated

**Deployment Date**: _______________
**Deployed By**: _______________
**Reviewed By**: _______________
**Notes**: _______________________________________________
