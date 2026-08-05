# WaddleAI Kubernetes Deployment

This directory contains Kubernetes manifests for deploying WaddleAI to kubernetes clusters.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Ingress (Nginx)                          │
│  waddleai.beta.k8s.cluster (TLS Wildcard Cert)              │
│  api.waddleai.beta.k8s.cluster                               │
│  mgmt.waddleai.beta.k8s.cluster                              │
└────────────────────────┬────────────────────────────────────┘
                         │
         ┌───────────────┴───────────────┐
         │                               │
┌────────▼─────────┐          ┌──────────▼─────────┐
│  WaddleAI Mgmt   │          │    PostgreSQL      │
│  (2 replicas)    │◄─────────┤    (1 replica)     │
│  Port: 8001      │          │    Port: 5432      │
└────────┬─────────┘          └────────────────────┘
         │
         │                    ┌────────────────────┐
         └────────────────────┤      Redis         │
                              │    (1 replica)     │
                              │    Port: 6379      │
                              └────────────────────┘
```

## Directory Structure

```
infrastructure/kubernetes/
├── base/                           # Base configuration (environment-agnostic)
│   ├── namespace.yaml              # waddleai namespace
│   ├── configmap.yaml              # Configuration values
│   ├── secret.yaml                 # Secrets (passwords, keys)
│   ├── postgres-deployment.yaml    # PostgreSQL database
│   ├── redis-deployment.yaml       # Redis cache
│   ├── management-deployment.yaml  # WaddleAI management service
│   ├── ingress.yaml                # Ingress with TLS
│   └── kustomization.yaml          # Kustomize base config
├── overlays/
│   └── beta/                       # Beta environment overlay
│       ├── kustomization.yaml      # Beta-specific config
│       └── replica-patch.yaml      # Replica counts
├── deploy-beta.sh                  # Deployment script
└── README.md                       # This file
```

## Prerequisites

1. **kubectl**: Kubernetes command-line tool installed and configured
2. **Access to beta k8s cluster**: Cluster accessible via kubectl
3. **cert-manager**: Installed in cluster for TLS certificates
4. **Ingress Controller**: Nginx ingress controller installed
5. **Storage Class**: Longhorn storage class available for PVCs

## Quick Start

### Deploy to Beta Cluster

```bash
# From the project root
cd infrastructure/kubernetes

# Deploy using the script
./deploy-beta.sh
```

### Manual Deployment

```bash
# Apply base configuration with beta overlay
kubectl apply -k overlays/beta

# Check deployment status
kubectl get all -n waddleai

# Watch pod status
kubectl get pods -n waddleai -w
```

## Configuration

### Secrets (IMPORTANT - Update Before Production Use!)

Edit the secrets to set production values:

```bash
kubectl edit secret waddleai-secrets -n waddleai
```

**Required updates:**
- `POSTGRES_PASSWORD`: Strong database password
- `JWT_SECRET`: Random 32+ character string
- `FLASK_SECRET_KEY`: Random 32+ character string
- `WEBHOOK_SECRET`: Shared secret with MarchProxy AILB

### ConfigMap

Edit configuration values:

```bash
kubectl edit configmap waddleai-config -n waddleai
```

**Common settings:**
- `MARCHPROXY_AILB_HOST`: MarchProxy AILB service hostname
- `ENABLE_OLLAMA_MANAGEMENT`: true/false
- `ENABLE_USAGE_WEBHOOKS`: true/false

## Accessing the Application

### Ingress URLs

- **Main**: https://waddleai.beta.k8s.cluster
- **API**: https://api.waddleai.beta.k8s.cluster
- **Management**: https://mgmt.waddleai.beta.k8s.cluster

### DNS Configuration

Add to `/etc/hosts` or configure DNS:

```bash
# Get ingress IP
kubectl get ingress -n waddleai

# Add to /etc/hosts (replace <IP> with actual ingress IP)
<IP> waddleai.beta.k8s.cluster api.waddleai.beta.k8s.cluster mgmt.waddleai.beta.k8s.cluster
```

## Scaling

### Scale Management Service

```bash
# Scale to 3 replicas
kubectl scale deployment waddleai-mgmt -n waddleai --replicas=3

# Auto-scale based on CPU
kubectl autoscale deployment waddleai-mgmt -n waddleai --min=2 --max=10 --cpu-percent=80
```

## Monitoring

### View Logs

```bash
# Management service logs
kubectl logs -f -l app=waddleai,component=management -n waddleai

# PostgreSQL logs
kubectl logs -f -l app=postgres -n waddleai

# Redis logs
kubectl logs -f -l app=redis -n waddleai

# Follow logs from all pods
kubectl logs -f -l app=waddleai -n waddleai --all-containers
```

### Check Pod Status

```bash
# Get all pods
kubectl get pods -n waddleai

# Describe pod for details
kubectl describe pod <pod-name> -n waddleai

# Get pod events
kubectl get events -n waddleai --sort-by='.lastTimestamp'
```

### Health Checks

```bash
# Check service endpoints
kubectl get endpoints -n waddleai

# Test health endpoint
kubectl port-forward svc/waddleai-mgmt 8001:8001 -n waddleai
curl http://localhost:8001/healthz
```

## Database Management

### Access PostgreSQL

```bash
# Port forward to local machine
kubectl port-forward svc/postgres 5432:5432 -n waddleai

# Connect using psql
PGPASSWORD=$(kubectl get secret waddleai-secrets -n waddleai -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d) \
psql -h localhost -U waddleai -d waddleai

# Or exec into pod
kubectl exec -it deployment/postgres -n waddleai -- psql -U waddleai -d waddleai
```

### Backup Database

```bash
# Create backup
kubectl exec deployment/postgres -n waddleai -- pg_dump -U waddleai waddleai > backup.sql

# Restore backup
kubectl exec -i deployment/postgres -n waddleai -- psql -U waddleai -d waddleai < backup.sql
```

## Troubleshooting

### Pods Not Starting

```bash
# Check pod status
kubectl describe pod <pod-name> -n waddleai

# Check events
kubectl get events -n waddleai --sort-by='.lastTimestamp'

# Check logs
kubectl logs <pod-name> -n waddleai
```

### Database Connection Issues

```bash
# Test database connectivity from management pod
kubectl exec -it deployment/waddleai-mgmt -n waddleai -- nc -zv postgres.waddleai.svc.cluster.local 5432

# Check database logs
kubectl logs -l app=postgres -n waddleai
```

### Ingress Not Working

```bash
# Check ingress status
kubectl describe ingress waddleai-ingress -n waddleai

# Check cert-manager certificate
kubectl get certificate -n waddleai
kubectl describe certificate waddleai-tls-wildcard -n waddleai

# Check nginx ingress logs
kubectl logs -n ingress-nginx -l app.kubernetes.io/component=controller
```

### TLS Certificate Issues

```bash
# Check certificate status
kubectl get certificate -n waddleai
kubectl describe certificate waddleai-tls-wildcard -n waddleai

# Check cert-manager logs
kubectl logs -n cert-manager -l app=cert-manager

# Force certificate renewal
kubectl delete certificate waddleai-tls-wildcard -n waddleai
kubectl apply -k overlays/beta
```

## Updating

### Rolling Update

```bash
# Update image tag in kustomization
vim overlays/beta/kustomization.yaml

# Apply changes
kubectl apply -k overlays/beta

# Watch rollout
kubectl rollout status deployment/waddleai-mgmt -n waddleai
```

### Rollback

```bash
# Rollback to previous version
kubectl rollout undo deployment/waddleai-mgmt -n waddleai

# Rollback to specific revision
kubectl rollout undo deployment/waddleai-mgmt -n waddleai --to-revision=2

# Check rollout history
kubectl rollout history deployment/waddleai-mgmt -n waddleai
```

## Cleanup

### Remove Deployment

```bash
# Delete all resources
kubectl delete -k overlays/beta

# Delete namespace (removes everything)
kubectl delete namespace waddleai
```

### Remove PVCs (Caution: Data Loss!)

```bash
# List PVCs
kubectl get pvc -n waddleai

# Delete specific PVC
kubectl delete pvc postgres-pvc -n waddleai
kubectl delete pvc redis-pvc -n waddleai
```

## Production Considerations

### High Availability

1. **Database**: Consider using external managed PostgreSQL (RDS, Cloud SQL, etc.)
2. **Redis**: Consider Redis Sentinel or Redis Cluster for HA
3. **Multiple Replicas**: Run 3+ replicas of management service across nodes
4. **Pod Disruption Budgets**: Configure PDBs to ensure availability during updates

### Security

1. **Update Secrets**: Generate strong, unique passwords and keys
2. **Network Policies**: Implement network policies to restrict traffic
3. **RBAC**: Configure appropriate role-based access control
4. **Pod Security Policies**: Enable PSPs or Pod Security Standards
5. **TLS**: Ensure all internal communication uses TLS where possible

### Monitoring

1. **Prometheus**: Expose metrics on `/metrics` endpoint
2. **Grafana Dashboards**: Create dashboards for key metrics
3. **Alerting**: Configure alerts for critical issues
4. **Log Aggregation**: Send logs to centralized logging system

### Backup Strategy

1. **Database Backups**: Automated daily backups with retention policy
2. **PVC Snapshots**: Volume snapshots for data persistence
3. **Configuration Backups**: Version control for manifests and configs

## Support

For issues or questions:
- Technical Documentation: See `/docs/` folder
- Integration Support: support@penguintech.io
- Status Page: https://status.penguintech.io
