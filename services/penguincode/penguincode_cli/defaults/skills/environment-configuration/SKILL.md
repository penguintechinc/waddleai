---
name: environment-configuration
description: "Environment variables, .env files, and secrets management"
model: qwen2.5-coder:7b
---

# Environment Configuration

## Overview
Manage environment variables, .env files, and secrets across development, staging, and production environments.

## .env File Pattern
```bash
# .env.example (committed to git)
DATABASE_URL=postgresql://user:password@localhost:5432/mydb
REDIS_URL=redis://localhost:6379
JWT_SECRET=change-me-in-production
LOG_LEVEL=INFO
```

## Rules
- **NEVER commit .env files** — add to .gitignore
- **ALWAYS provide .env.example** — template with placeholder values
- **Validate required vars at startup** — fail fast if missing
- **Use typed parsing** — convert strings to int, bool, etc.

## Python Pattern
```python
import os

DATABASE_URL = os.environ["DATABASE_URL"]  # Required, fails if missing
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")  # Optional with default
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
```

## Docker Compose
```yaml
services:
  app:
    environment:
      - DATABASE_URL=${DATABASE_URL}
    env_file:
      - .env
```

## Secrets Management
- **Development**: `.env` file
- **CI/CD**: GitHub Secrets, GitLab CI variables
- **Production**: Kubernetes Secrets, Vault, AWS SSM
- **Never** log environment variables containing secrets
