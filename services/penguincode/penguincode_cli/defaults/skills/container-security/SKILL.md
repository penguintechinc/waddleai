---
name: container-security
description: "Image scanning, non-root users, secrets management for containers"
model: qwen2.5-coder:7b
---

# Container Security

## Overview
Secure container images and runtime environments following Docker security best practices.

## Image Security
1. **Use minimal base images**: `*-slim` or `distroless`
2. **Pin versions**: `python:3.13.1-slim` not `python:latest`
3. **Run as non-root**: add `USER nonroot` in Dockerfile
4. **No secrets in images**: use runtime env vars or secrets management
5. **Scan for vulnerabilities**: `docker scout cves <image>`

## Scanning
```bash
# Docker Scout
docker scout cves <image>

# Trivy
trivy image <image>

# Grype
grype <image>
```

## Dockerfile Hardening
```dockerfile
# Use non-root user
RUN addgroup --system app && adduser --system --ingroup app app
USER app

# Read-only filesystem
# In docker-compose: read_only: true

# Drop capabilities
# In docker-compose: cap_drop: [ALL]
```

## Secrets Management
- NEVER bake secrets into images
- Use Docker secrets for Swarm
- Use environment variables with `.env` files (not committed)
- Use external secret managers (Vault, AWS Secrets Manager)

## Runtime Security
- Use read-only root filesystem where possible
- Drop all capabilities, add only needed ones
- Set memory and CPU limits
- Use `no-new-privileges` security option
