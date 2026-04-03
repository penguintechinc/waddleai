---
name: building-docker-images
description: "Multi-arch Docker builds, layer optimization, and tagging"
model: qwen2.5-coder:7b
---

# Building Docker Images

## Overview
Build optimized, multi-architecture Docker images following best practices for layer caching and image size.

## Multi-Arch Build
```bash
# Create builder (once)
docker buildx create --name multiarch --use

# Build and push multi-arch
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t registry/app:tag \
  --push .
```

## Layer Optimization
1. **Order matters** — put rarely-changing layers first
2. **Combine RUN commands** — reduce layer count
3. **Multi-stage builds** — separate build and runtime
4. **Use .dockerignore** — exclude unnecessary files

## Dockerfile Best Practices
```dockerfile
# Multi-stage build
FROM python:3.13-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.13-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY . .
USER nonroot
CMD ["python", "main.py"]
```

## Tagging Convention
- `beta-<epoch64>` — development builds from main
- `alpha-<epoch64>` — feature branch builds
- `vX.X.X-beta` — version release candidates
- `vX.X.X` — tagged production releases

## Security
- Use non-root user
- Scan images for vulnerabilities (see waddlepowers:container-security)
- Pin base image versions
- Don't copy secrets into images
