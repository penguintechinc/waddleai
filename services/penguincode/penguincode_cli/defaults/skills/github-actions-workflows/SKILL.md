---
name: github-actions-workflows
description: "GitHub Actions workflow creation, debugging, and secrets management"
model: qwen2.5-coder:7b
---

# GitHub Actions Workflows

## Overview
Create and debug GitHub Actions CI/CD workflows with proper secrets management.

## Workflow Structure
```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build
        run: make build
      - name: Test
        run: make test
```

## Common Patterns
- **Matrix builds**: test across multiple versions/platforms
- **Caching**: cache dependencies for faster builds
- **Conditional steps**: skip steps based on branch/event
- **Artifacts**: upload build outputs for later jobs

## Secrets Management
```yaml
env:
  API_KEY: ${{ secrets.API_KEY }}
```
- Store secrets in repo Settings > Secrets
- Never echo secrets in logs
- Use environments for different deploy targets

## Debugging
```bash
# View workflow runs
gh run list

# View specific run
gh run view <run-id>

# View logs
gh run view <run-id> --log

# Re-run failed workflow
gh run rerun <run-id>
```

## Multi-Arch Builds
```yaml
- uses: docker/setup-buildx-action@v3
- uses: docker/build-push-action@v5
  with:
    platforms: linux/amd64,linux/arm64
    push: true
    tags: registry/app:${{ github.sha }}
```
