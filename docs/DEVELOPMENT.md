# Local Development Guide

Complete guide to setting up a local development environment for WaddleAI, running the proxy and management services locally, and following the development workflow.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Initial Setup](#initial-setup)
3. [Starting Development Environment](#starting-development-environment)
4. [Development Workflow](#development-workflow)
5. [Common Tasks](#common-tasks)
6. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### System Requirements

- **macOS 12+**, **Linux (Ubuntu 20.04+)**, or **Windows 10+ with WSL2**
- **Docker Desktop** 4.0+ (or Docker Engine 20.10+)
- **Docker Compose** 2.0+
- **Git** 2.30+
- **Python** 3.13+ (for local development)
- **PostgreSQL** 13+ (optional: for local database, or use Docker)

### Optional Tools

- **Docker Buildx** (for multi-architecture builds)
- **Helm** (for Kubernetes deployments)
- **kubectl** (for Kubernetes clusters)

### Installation

**macOS (Homebrew)**:
```bash
brew install docker docker-compose git python postgresql
brew install --cask docker
```

**Ubuntu/Debian**:
```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose git python3.13 postgresql
sudo usermod -aG docker $USER  # Allow docker without sudo
newgrp docker                   # Activate group change
```

**Verify Installation**:
```bash
docker --version      # Docker 20.10+
docker-compose --version  # Docker Compose 2.0+
git --version
python3 --version     # Python 3.13+
```

---

## Initial Setup

### Clone Repository

```bash
git clone <repository-url>
cd WaddleAI
```

### Install Python Dependencies

```bash
# Create virtual environment
python3.13 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Environment Configuration

Copy and customize environment files:

```bash
# Copy example environment file
cp .env.example .env
```

**Key Environment Variables**:
```bash
# Database (supports: postgres, mysql, sqlite)
DB_TYPE=postgres
DB_HOST=localhost
DB_PORT=5432
DB_NAME=waddleai
DB_USER=waddleai
DB_PASSWORD=waddleai-dev

# Flask Backend
FLASK_ENV=development
FLASK_DEBUG=1
SECRET_KEY=dev-secret-key-change-in-production
SECURITY_PASSWORD_SALT=dev-salt-key

# Proxy Service
PROXY_PORT=8000
REQUEST_TIMEOUT=30
RATE_LIMIT_REQUESTS=1000
RATE_LIMIT_WINDOW=3600

# Management Service
MANAGEMENT_PORT=8001
ENABLE_ANALYTICS=true
ANALYTICS_RETENTION_DAYS=90

# Redis Cache
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0

# License (Development - all features available)
RELEASE_MODE=false
LICENSE_KEY=not-required-in-dev

# Logging
LOG_LEVEL=DEBUG
```

### Database Initialization

```bash
# Start PostgreSQL via Docker
docker run -d \
  --name waddleai-postgres \
  -e POSTGRES_DB=waddleai \
  -e POSTGRES_USER=waddleai \
  -e POSTGRES_PASSWORD=waddleai-dev \
  -p 5432:5432 \
  postgres:15

# Or use SQLite for lightweight development
export DB_TYPE=sqlite
export DATABASE_URL=sqlite:///waddleai.db

# Run migrations
cd shared && python -c "from db import init_db; init_db()"
```

---

## Starting Development Environment

### Quick Start (All Services)

```bash
# Start all services with Docker Compose
docker-compose -f docker-compose.dev.yml up -d

# This runs:
# - PostgreSQL database (port 5432)
# - Redis cache (port 6379)
# - Proxy service (port 8000)
# - Management service (port 8001)

# Access the services:
# Proxy API:        http://localhost:8000
# Management UI:    http://localhost:8001
# Health Check:     http://localhost:8000/health
# Metrics:          http://localhost:8000/metrics
```

### Individual Service Management

**Start specific services**:
```bash
# Start only Proxy service
docker-compose up -d proxy

# Start Proxy and Management
docker-compose up -d proxy management

# Start without detaching (see logs)
docker-compose up proxy
```

**View service logs**:
```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f proxy

# Last 100 lines, follow new entries
docker-compose logs -f --tail=100 management
```

**Stop services**:
```bash
# Stop all services (keep data)
docker-compose down

# Stop and remove volumes (clean slate)
docker-compose down -v

# Restart services
docker-compose restart

# Rebuild and restart (apply code changes)
docker-compose down && docker-compose up -d --build
```

### Development Docker Compose Files

- **`docker-compose.dev.yml`**: Local development (hot-reload, debug ports, fake SMTP)
- **`docker-compose.yml`**: Production-like (health checks, resource limits, no debug)

Use dev version locally:
```bash
docker-compose -f docker-compose.dev.yml up
```

---

## Development Workflow

### 1. Start Development Environment

```bash
make dev        # Start all services
make seed-data  # Populate with test data
```

### 2. Make Code Changes

Edit files in your favorite editor. Services auto-reload:

- **Python (Flask)**: Reload on file save (FLASK_DEBUG=1)
- **Proxy/Management**: Hot reload with watchdog
- **Changes in shared/**: Requires container restart

### 3. Verify Changes

```bash
# Quick smoke tests
make smoke-test

# Run linters
make lint

# Run unit tests (specific service)
cd proxy && pytest tests/unit/ -v

# Run all tests
make test
```

### 4. Populate Mock Data for Feature Testing

After implementing a new feature, create mock data scripts:

```bash
# Create mock data script for tokens
cat > scripts/mock-data/seed-tokens.py << 'EOF'
from shared.db import get_db

def seed_tokens():
    db = get_db()

    # Create test API keys for dual token system
    tokens = [
        {"name": "Test Token 1", "waddleai_quota": 100000, "llm_quota": 50000},
        {"name": "Test Token 2", "waddleai_quota": 200000, "llm_quota": 100000},
        {"name": "Limited Token", "waddleai_quota": 10000, "llm_quota": 5000},
        {"name": "Demo Token", "waddleai_quota": 50000, "llm_quota": 25000},
    ]

    for token in tokens:
        db.api_keys.insert(**token)

    print(f"✓ Seeded {len(tokens)} API tokens")

if __name__ == "__main__":
    seed_tokens()
EOF

# Run the mock data script
python scripts/mock-data/seed-tokens.py

# Add to seed-all.py orchestrator
echo "from seed_tokens import seed_tokens; seed_tokens()" >> scripts/mock-data/seed-all.py
```

### 5. Test Multi-Backend Routing

WaddleAI's dual token system and multi-backend routing:

```bash
# Create mock backend connections
python scripts/mock-data/seed-backends.py

# Test routing with different backends
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer wa-test-token" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Hello"}],
    "waddleai_route": "openai"  # Force backend selection
  }'

# Monitor dual token consumption
curl http://localhost:8001/analytics/tokens \
  -H "Authorization: Bearer admin-token"
```

### 6. Run Pre-Commit Checklist

Before committing, run the comprehensive pre-commit script:

```bash
./scripts/pre-commit/pre-commit.sh
```

**Steps**:
1. ✅ Linters (flake8, black, isort, mypy)
2. ✅ Security scans (bandit)
3. ✅ Secret detection (no API keys, passwords, tokens)
4. ✅ Build & Run (build containers, verify runtime)
5. ✅ Smoke tests (proxy health, management health, dual token validation)
6. ✅ Unit tests (isolated component testing)
7. ✅ Integration tests (service interactions with multi-backend routing)
8. ✅ Version update & Docker standards

**Troubleshooting Pre-Commit**:

See [Pre-Commit Documentation](PRE_COMMIT.md) for detailed guidance on:
- Fixing linting errors
- Resolving security vulnerabilities
- Excluding files from checks
- Bypassing specific checks (with justification)

### 7. Testing & Validation

Comprehensive testing guide for proxy/management architecture:

**Complete Testing Guide**: [Testing Documentation](TESTING.md)

**Quick Test Commands**:
```bash
# Smoke tests only (fast, <2 min)
make smoke-test

# Unit tests only
make test-unit

# Integration tests only (including dual token validation)
make test-integration

# All tests
make test

# Specific test file
pytest tests/unit/test_proxy_auth.py

# Cross-architecture testing (QEMU)
make test-multiarch
```

### 8. Create Pull Request

Once tests pass:

```bash
# Push branch
git push origin feature-branch-name

# Create PR via GitHub CLI
gh pr create --title "Brief feature description" \
  --body "Detailed description of changes"

# Or use web UI: https://github.com/penguintechinc/WaddleAI/compare
```

---

## Common Tasks

### Adding a New Python Dependency

```bash
# Add to requirements.txt
echo "new-package==1.0.0" >> requirements.txt

# Rebuild containers
docker-compose up -d --build

# Verify import works
docker-compose exec proxy python -c "import new_package"
```

### Adding a New Environment Variable

```bash
# Add to .env
echo "NEW_VAR=value" >> .env

# Restart services to pick up new variable
docker-compose restart

# Verify it's set
docker-compose exec proxy printenv | grep NEW_VAR
```

### Debugging a Service

**View logs in real-time**:
```bash
docker-compose logs -f proxy
docker-compose logs -f management
```

**Access container shell**:
```bash
# Proxy service
docker-compose exec proxy bash

# Management service
docker-compose exec management bash
```

**Execute commands in container**:
```bash
# Run Python script
docker-compose exec proxy python -c "print('hello')"

# Check service health
docker-compose exec proxy curl http://localhost:8000/health
```

### Database Operations

**Connect to database**:
```bash
# PostgreSQL
docker-compose exec postgres psql -U waddleai -d waddleai

# SQLite
sqlite3 waddleai.db

# View schema
\dt                    # PostgreSQL tables
.tables                # SQLite tables
```

**Reset database**:
```bash
# Full reset (deletes all data)
docker-compose down -v
make db-init
make seed-mock-data
```

**Run migrations**:
```bash
# Auto-migrate on startup
docker-compose restart proxy

# Or manually run migration
docker-compose exec proxy python -m shared.db migrate
```

### Working with Git Branches

```bash
# Create feature branch
git checkout -b feature/new-feature-name

# Keep branch updated with main
git fetch origin
git rebase origin/main

# Clean commit history before PR
git rebase -i origin/main  # Interactive rebase

# Push branch
git push origin feature/new-feature-name
```

### Testing Dual Token System

```bash
# Create test tokens with different quotas
python scripts/mock-data/seed-tokens.py

# Monitor token consumption
curl http://localhost:8001/api/v1/analytics/tokens \
  -H "Authorization: Bearer $ADMIN_TOKEN"

# View per-token breakdown (WaddleAI vs LLM tokens)
curl http://localhost:8001/api/v1/analytics/tokens/token-id \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

### Testing Multi-Backend Routing

```bash
# Configure backend connections
python scripts/mock-data/seed-backends.py

# Test smart routing
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer wa-token" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "smart-router",
    "messages": [{"role": "user", "content": "Complex reasoning"}]
  }'

# Force specific backend
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer wa-token" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Use OpenAI"}],
    "waddleai_route": "openai"
  }'
```

---

## Troubleshooting

### Services Won't Start

**Check if ports are already in use**:
```bash
# Find what's using port 8000
lsof -i :8000

# Kill the process
kill -9 <PID>

# Or use different ports in .env
PROXY_PORT=8080
MANAGEMENT_PORT=8082
```

**Docker daemon not running**:
```bash
# macOS
open /Applications/Docker.app

# Linux
sudo systemctl start docker

# Windows (Docker Desktop)
# Start Docker Desktop from Applications
```

### Database Connection Error

```bash
# Verify database container is running
docker-compose ps postgres

# Check database credentials in .env
cat .env | grep DB_

# Connect to database directly
docker-compose exec postgres psql -U waddleai -d waddleai

# View logs
docker-compose logs postgres
```

### Proxy Service Won't Start

```bash
# Check logs
docker-compose logs proxy

# Verify database migration
docker-compose exec proxy python -c "from shared.db import init_db; init_db()"

# Reset and rebuild
docker-compose down
docker-compose up -d --build proxy
```

### Smoke Tests Failing

**Check which test failed**:
```bash
# Run individually
./tests/smoke/health-check.sh
./tests/smoke/dual-token-validation.sh
./tests/smoke/backend-routing-check.sh
```

**Common issues**:
- Service not healthy (logs: `docker-compose logs <service>`)
- Port not exposed (check docker-compose.yml)
- API endpoint not implemented
- Missing environment variables

See [Testing Documentation - Smoke Tests](TESTING.md#smoke-tests) for detailed troubleshooting.

### Git Merge Conflicts

```bash
# View conflicts
git status

# Edit conflicted files (marked with <<<<, ====, >>>>)
# Remove conflict markers and keep desired code

# Mark as resolved
git add <resolved-file>

# Complete merge
git commit -m "Resolve merge conflicts"
```

### Slow Docker Builds

```bash
# Check Docker disk usage
docker system df

# Clean up unused images/containers
docker system prune

# Rebuild without cache (slow, but fresh)
docker-compose build --no-cache proxy
```

---

## Tips & Best Practices

### Hot Reload Development

For fastest iteration:
```bash
# Start services once
docker-compose up -d

# Edit Python files → auto-reload (FLASK_DEBUG=1)
# Changes in proxy/management auto-apply
# Changes in shared/ → restart service
```

### Environment-Specific Configuration

```bash
# Development settings (auto-loaded)
.env              # Default development config
.env.local        # Local machine overrides (gitignored)

# Production settings (via secret management)
Kubernetes secrets
AWS Secrets Manager
HashiCorp Vault
```

### Code Organization

Keep project clean:
```bash
# Remove old branches
git branch -D old-branch

# Clean local Docker images
docker image prune -a

# Clean unused containers
docker container prune
```

### Performance Tips

```bash
# Use specific services to reduce memory usage
docker-compose up postgres proxy  # Skip management, redis

# Use lightweight testing
make smoke-test  # Instead of full test suite while developing

# Cache Docker layers by building in order of frequency of change
Dockerfile: base → dependencies → code → entrypoint
```

---

## Related Documentation

- **Testing**: [Testing Documentation](TESTING.md)
  - Mock data scripts for dual token system
  - Smoke tests for proxy/management
  - Unit/integration/E2E tests
  - Performance tests
  - Cross-architecture testing

- **Pre-Commit**: [Pre-Commit Checklist](PRE_COMMIT.md)
  - Linting requirements
  - Security scanning
  - Build verification
  - Test requirements

- **Deployment**: [Deployment Guide](deployment/)
  - Containerization
  - Kubernetes deployment
  - Docker Compose production
  - Health checks

- **Standards**: [Development Standards](STANDARDS.md)
  - Architecture decisions
  - Code style
  - API conventions
  - Database patterns
  - Dual token system design
  - Multi-backend routing patterns

- **Workflows**: [CI/CD Workflows](WORKFLOWS.md)
  - GitHub Actions pipelines
  - Build automation
  - Test automation
  - Release processes

---

**Last Updated**: 2026-01-06
**Maintained by**: Penguin Tech Inc
