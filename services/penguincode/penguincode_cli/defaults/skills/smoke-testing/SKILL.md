---
name: smoke-testing
description: "Quick build, run, and API verification in under 2 minutes"
model: qwen2.5-coder:7b
---

# Smoke Testing

## Overview
Run quick verification that the application builds, starts, and responds to basic requests. Target: under 2 minutes.

## Smoke Test Checklist
1. **Build passes**: `make build` or `docker-compose build`
2. **Services start**: `docker-compose up -d` and verify health
3. **API responds**: hit health/status endpoints
4. **Page loads**: verify frontend serves (if applicable)

## Quick Commands
```bash
# Build
make build || docker-compose build

# Start services
docker-compose up -d

# Check health
curl -s http://localhost:8080/health | jq .

# Check API
curl -s http://localhost:8080/api/v1/status | jq .

# Stop
docker-compose down
```

## Verification (see waddlepowers:verification-before-completion)
- All services healthy in `docker ps`
- Health endpoint returns 200
- No error logs: `docker-compose logs --tail=20`

## When to Run
- Before committing (see waddlepowers:committing-changes)
- After building images (see waddlepowers:building-docker-images)
- After deployment
- After dependency updates
