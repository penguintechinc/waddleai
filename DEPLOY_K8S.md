# WaddleAI Beta K8s Deployment Guide

## Quick Deploy

```bash
./scripts/deploy-beta.sh
```

## What Gets Deployed

### Services
- **WaddleAI Management** (2 replicas) - Flask API on port 8001
- **PostgreSQL** (1 replica) - Database on port 5432
- **Redis** (1 replica) - Cache on port 6379

### Ingress URLs
- `https://waddleai.beta.k8s.cluster` - Main application
- `https://api.waddleai.beta.k8s.cluster` - API endpoint
- `https://mgmt.waddleai.beta.k8s.cluster` - Management interface

### Storage
- PostgreSQL: 10Gi PVC (Longhorn)
- Redis: 5Gi PVC (Longhorn)

### TLS
- Wildcard certificate via cert-manager
- Nginx ingress controller
- Automatic HTTPS redirect

## Prerequisites Checklist

- [x] kubectl configured and connected to beta cluster
- [x] cert-manager installed in cluster
- [x] Nginx ingress controller running
- [x] Longhorn storage class available
- [ ] Update secrets with production values (see below)

## Post-Deployment Configuration

### 1. Update Secrets (CRITICAL!)

```bash
# Edit secrets
kubectl edit secret waddleai-secrets -n waddleai

# Update these values:
# - POSTGRES_PASSWORD
# - JWT_SECRET (32+ random characters)
# - FLASK_SECRET_KEY (32+ random characters)
# - WEBHOOK_SECRET (shared with MarchProxy)
```

### 2. Configure MarchProxy Integration

Update the ConfigMap if MarchProxy is in a different namespace:

```bash
kubectl edit configmap waddleai-config -n waddleai

# Update:
# - MARCHPROXY_AILB_HOST
# - MARCHPROXY_AILB_GRPC_PORT
# - MARCHPROXY_AILB_HTTP_PORT
```

### 3. Add DNS Entries

Get the ingress IP address:

```bash
kubectl get ingress -n waddleai
```

Add to `/etc/hosts` or DNS server:

```
<INGRESS_IP> waddleai.beta.k8s.cluster
<INGRESS_IP> api.waddleai.beta.k8s.cluster
<INGRESS_IP> mgmt.waddleai.beta.k8s.cluster
```

## Verification

### Check All Pods Running

```bash
kubectl get pods -n waddleai
```

Expected output:
```
NAME                             READY   STATUS    RESTARTS   AGE
postgres-xxxxxxxxxx-xxxxx        1/1     Running   0          2m
redis-xxxxxxxxxx-xxxxx           1/1     Running   0          2m
waddleai-mgmt-xxxxxxxxxx-xxxxx   1/1     Running   0          2m
waddleai-mgmt-xxxxxxxxxx-xxxxx   1/1     Running   0          2m
```

### Test Health Endpoint

```bash
# Via ingress
curl https://waddleai.beta.k8s.cluster/healthz

# Via port-forward
kubectl port-forward svc/waddleai-mgmt 8001:8001 -n waddleai
curl http://localhost:8001/healthz
```

Expected response:
```json
{
  "status": "healthy",
  "version": "1.0.0",
  "database": "connected",
  "redis": "connected"
}
```

### View Logs

```bash
# Management service
kubectl logs -f -l app=waddleai,component=management -n waddleai

# All services
kubectl logs -f -l app=waddleai -n waddleai --all-containers
```

## Common Operations

### Scale Management Service

```bash
# Scale up
kubectl scale deployment waddleai-mgmt -n waddleai --replicas=3

# Scale down
kubectl scale deployment waddleai-mgmt -n waddleai --replicas=1
```

### Update Image

```bash
# Update to specific tag
kubectl set image deployment/waddleai-mgmt -n waddleai \
  management=ghcr.io/penguintechinc/waddleai/management:beta-<epoch64>

# Rollout status
kubectl rollout status deployment/waddleai-mgmt -n waddleai
```

### Restart Service

```bash
kubectl rollout restart deployment/waddleai-mgmt -n waddleai
```

### Access Database

```bash
# Port forward
kubectl port-forward svc/postgres 5432:5432 -n waddleai

# Connect with psql
PGPASSWORD=$(kubectl get secret waddleai-secrets -n waddleai -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d) \
psql -h localhost -U waddleai -d waddleai
```

## Troubleshooting

### Pods in CrashLoopBackOff

```bash
# Check pod logs
kubectl logs <pod-name> -n waddleai --previous

# Describe pod for events
kubectl describe pod <pod-name> -n waddleai
```

### Database Connection Errors

```bash
# Test database connectivity
kubectl exec -it deployment/waddleai-mgmt -n waddleai -- \
  nc -zv postgres.waddleai.svc.cluster.local 5432

# Check PostgreSQL logs
kubectl logs -l app=postgres -n waddleai
```

### TLS Certificate Not Ready

```bash
# Check certificate status
kubectl get certificate -n waddleai
kubectl describe certificate waddleai-tls-wildcard -n waddleai

# Check cert-manager logs
kubectl logs -n cert-manager -l app=cert-manager

# Force renewal
kubectl delete certificate waddleai-tls-wildcard -n waddleai
./scripts/deploy-beta.sh
```

### Ingress 404 Errors

```bash
# Check ingress configuration
kubectl describe ingress waddleai-ingress -n waddleai

# Check nginx controller logs
kubectl logs -n ingress-nginx -l app.kubernetes.io/component=controller

# Test service directly
kubectl port-forward svc/waddleai-mgmt 8001:8001 -n waddleai
curl http://localhost:8001/healthz
```

## Cleanup

### Remove Deployment (Keep Data)

```bash
helm uninstall waddleai --kube-context=dal2-beta -n waddleai-beta
```

### Complete Removal (Data Loss!)

```bash
# Delete everything including PVCs
kubectl delete namespace waddleai
```

## Architecture Details

```
┌─────────────────────────────────────────────────────────┐
│ Internet                                                 │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│ Nginx Ingress Controller                                │
│ - TLS Termination (Wildcard Cert)                       │
│ - HTTPS Redirect                                         │
│ - CORS Headers                                           │
│ - Rate Limiting                                          │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│ WaddleAI Management Service (ClusterIP)                 │
│ - 2 Replicas (Load Balanced)                            │
│ - Flask + Flask-Security-Too                            │
│ - Port 8001                                              │
│ - Health Checks: /healthz                               │
└─────────┬─────────────────────┬─────────────────────────┘
          │                     │
          ▼                     ▼
┌─────────────────────┐   ┌─────────────────────┐
│ PostgreSQL          │   │ Redis               │
│ - 1 Replica         │   │ - 1 Replica         │
│ - Port 5432         │   │ - Port 6379         │
│ - 10Gi PVC          │   │ - 5Gi PVC           │
│ - ClusterIP         │   │ - ClusterIP         │
└─────────────────────┘   └─────────────────────┘
```

## Integration with MarchProxy

WaddleAI expects MarchProxy AILB to be running in the `marchproxy` namespace:

```
┌─────────────────────┐       gRPC        ┌─────────────────────┐
│   WaddleAI Mgmt     │◄─────────────────►│  MarchProxy AILB    │
│   (waddleai ns)     │  Route Sync       │  (marchproxy ns)    │
└─────────────────────┘                   └─────────────────────┘
```

Configuration:
- `MARCHPROXY_AILB_HOST`: `proxy-ailb.marchproxy.svc.cluster.local`
- `MARCHPROXY_AILB_GRPC_PORT`: `50051`
- `MARCHPROXY_AILB_HTTP_PORT`: `8080`

## Next Steps

1. ✅ Deploy to beta cluster
2. ✅ Update secrets with production values
3. ✅ Configure DNS entries
4. ⬜ Test API endpoints
5. ⬜ Configure MarchProxy integration
6. ⬜ Set up monitoring and alerts
7. ⬜ Configure backups
8. ⬜ Load test and scale as needed

## Support

- Documentation: `/docs/` folder
- K8s Manifests: `/k8s/helm/waddleai/` (Helm, beta/prod) and `/k8s/kustomize/` (Kustomize, alpha)
- Issues: support@penguintech.io
