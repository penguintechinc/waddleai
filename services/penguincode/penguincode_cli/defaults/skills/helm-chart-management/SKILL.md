---
name: helm-chart-management
description: "Helm chart creation, values management, and upgrade workflows"
model: qwen2.5-coder:7b
---

# Helm Chart Management

## Overview
Create, manage, and deploy Helm charts for Kubernetes applications.

## Chart Structure
```
charts/app/
├── Chart.yaml          # Chart metadata
├── values.yaml         # Default values
├── templates/
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── ingress.yaml
│   ├── configmap.yaml
│   └── _helpers.tpl
└── values/
    ├── dev.yaml
    ├── staging.yaml
    └── production.yaml
```

## Common Commands
```bash
# Install a chart
helm install app ./charts/app -f values/production.yaml

# Upgrade
helm upgrade app ./charts/app -f values/production.yaml

# Rollback
helm rollback app 1

# Check history
helm history app

# Dry run (preview)
helm install app ./charts/app --dry-run --debug

# Template rendering
helm template app ./charts/app -f values/dev.yaml
```

## Values Management
- Use separate values files per environment
- Override specific values: `helm upgrade app ./chart --set image.tag=v1.2.3`
- Keep secrets out of values files — use external secrets

## Best Practices
- Version your charts in Chart.yaml
- Use `helm lint` before deploying
- Always use `--dry-run` before applying changes
- Keep charts in version control
- Use Helm hooks for migrations (see waddlepowers:deploying-to-kubernetes)
