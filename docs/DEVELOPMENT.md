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
- **Docker Desktop** 4.0+ (or Docker Engine 20.10+) — used to run standalone dependency containers (database, cache) and to build service images; not used for orchestrating the app itself
- **Git** 2.30+
- **Python** 3.13
- **Node.js** 24.x (webui)
- **kubectl** + **MicroK8s** (Linux) or **Docker Desktop Kubernetes** (macOS/Windows) — only needed if you want to run the full stack in a local cluster instead of running services directly (see [Full Stack via Local Kubernetes](#full-stack-via-local-kubernetes))

### Optional Tools

- **Docker Buildx** (for multi-architecture builds)
- **Helm v4** (chart at `k8s/helm/waddleai`, used for beta/prod deploys)
- **psql** client (for inspecting the local database directly)

### Installation

**macOS (Homebrew)**:
```bash
brew install git python@3.13 node@24 kubectl helm
brew install --cask docker
```

**Ubuntu/Debian**:
```bash
sudo apt-get update
sudo apt-get install -y git python3.13 python3.13-venv nodejs npm postgresql-client
# kubectl: https://kubernetes.io/docs/tasks/tools/#install-kubectl-linux
# Helm v4: https://helm.sh/docs/intro/install/
# MicroK8s (optional, local Kubernetes): sudo snap install microk8s --classic
```

**Verify Installation**:
```bash
docker --version       # Docker 20.10+
git --version
python3.13 --version   # Python 3.13.x
node --version          # v24.x
kubectl version --client
```

---

## Initial Setup

### Clone Repository

```bash
git clone <repository-url> waddleai
cd waddleai
```

### Install Python Dependencies

The proxy and management services share `shared/`, so tests and most local dev work run out of a single virtual environment at the repo root — this mirrors what CI installs:

```bash
python3.13 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

pip install --upgrade pip
pip install -r requirements.txt
pip install -r services/management/requirements.txt
```

### Install Web UI Dependencies

```bash
cd services/webui
npm ci
cd ../..
```

### Environment Configuration

There is no `.env.example` in this repo — each service reads environment variables directly (see `services/management/app/config.py`, `proxy/apps/proxy_server/main.py`, and `k8s/helm/waddleai/values.yaml` for the full list and defaults). For local development, exporting these is enough to get both backend services running:

```bash
# Database — always a postgresql:// URI (PostgreSQL + pgvector; no other DB is supported)
export DATABASE_URL=postgresql://waddleai:waddleai-dev@localhost:5432/waddleai

# Used to sign JWTs issued by the management service and verified by the proxy
export JWT_SECRET=$(openssl rand -hex 32)

export LOG_LEVEL=DEBUG
```

The cache (Valkey) needs no configuration if it's reachable at `localhost:6379` — both services default to that address. To point them elsewhere, note that the two services read *different* variables:

| Service | Variable(s) |
|---|---|
| Proxy (`proxy/apps/proxy_server/main.py`) | `REDIS_URL` — a full URL, e.g. `redis://cache-host:6379/0` |
| Management (`services/management/app/config.py`) | `CACHE_HOST` / `CACHE_PORT` / `CACHE_USER` / `CACHE_PASS`, and also honours `REDIS_URL` |

The `REDIS_URL` name is historical — the deployed cache is Valkey, not Redis. Setting only `CACHE_HOST` will not move the proxy.

### Start Dependency Containers

Postgres and Valkey run as two standalone containers — there is no Compose file in this repo (Docker Compose is deprecated here; Kubernetes/Helm is the only supported way to run the full application, see [Full Stack via Local Kubernetes](#full-stack-via-local-kubernetes)):

```bash
docker run -d --name waddleai-postgres \
  -e POSTGRES_DB=waddleai \
  -e POSTGRES_USER=waddleai \
  -e POSTGRES_PASSWORD=waddleai-dev \
  -p 5432:5432 \
  pgvector/pgvector:pg16

docker run -d --name waddleai-valkey \
  -p 6379:6379 \
  valkey/valkey:8-bookworm
```

### Database Schema

No manual step is needed the first time you start the management service: `init_schema()` (`services/management/app/models_sqlalchemy.py`) enables the `pgvector` extension and creates any missing tables automatically and idempotently on startup.

Alembic (`services/management/alembic/`) is the schema of record for applying versioned migrations after that — it is run manually, never automatically at app startup:

```bash
cd services/management
DATABASE_URL=postgresql://waddleai:waddleai-dev@localhost:5432/waddleai alembic upgrade head
cd ../..
```

---

## Starting Development Environment

### Quick Start (All Services)

Run each service in its own terminal from the repo root, with the virtual environment activated. `PYTHONPATH` includes both the repo root (for the shared `shared` package) and the service's own subdirectory (matching how each Dockerfile lays out `/app` at runtime).

**Management API** (port 8001):
```bash
export PYTHONPATH="$(pwd):$(pwd)/services/management"
export DATABASE_URL=postgresql://waddleai:waddleai-dev@localhost:5432/waddleai
export JWT_SECRET=$(openssl rand -hex 32)
cd services/management
hypercorn asgi:app --bind 0.0.0.0:8001 --reload
```

**Proxy** (port 8080, gRPC 50051) — start after the management service is up, since the proxy talks to it for OIDC/route discovery:
```bash
export PYTHONPATH="$(pwd):$(pwd)/proxy"
export DATABASE_URL=postgresql://waddleai:waddleai-dev@localhost:5432/waddleai
cd proxy
hypercorn apps.proxy_server.main:app --bind 0.0.0.0:8080 --reload
```

**Web UI** (port 3000, proxies `/api/*` to `localhost:8001`):
```bash
cd services/webui
npm run dev
```

**Access the services**:
```
Proxy API:        http://localhost:8080
Management API:   http://localhost:8001
Web UI:           http://localhost:3000
Proxy health:      http://localhost:8080/healthz
Management health: http://localhost:8001/healthz  (also /readyz)
```

> `make dev` currently shells out to `docker-compose up`, but there is no `docker-compose.yml` in this repo, so that target does not work as-is. Use the commands above until the Makefile is updated.

### Full Stack via Local Kubernetes

For testing against a real cluster instead of directly-run processes, this repo ships a MicroK8s-based alpha deploy script:

```bash
./scripts/deploy-alpha.sh          # builds proxy/management/webui images, imports into MicroK8s, helm upgrade --install's the alpha release
./scripts/deploy-alpha.sh --help   # all options: --skip-build, --service, --dry-run, --rollback
```

It requires a `local-alpha` kubectl context pointing at a running MicroK8s (or Docker Desktop Kubernetes) cluster — see the `deploying-app` skill for cluster setup. Under the hood it runs `helm upgrade --install waddleai k8s/helm/waddleai --kube-context local-alpha --namespace waddleai --create-namespace --values k8s/helm/waddleai/values-alpha.yaml`. The Helm chart at `k8s/helm/waddleai` is the only supported deployment path for every environment — it's what `./scripts/deploy-beta.sh` deploys to the shared beta cluster too.

---

## Development Workflow

### 1. Start Development Environment

Start Postgres + Valkey, then the management and proxy services and the web UI, as shown above.

### 2. Make Code Changes

- **Management / Proxy (Python)**: `hypercorn ... --reload` restarts the process on file save
- **Web UI**: Vite's dev server hot-reloads on save
- **Changes in `shared/`**: picked up automatically by `--reload` since it re-imports on restart; no separate rebuild step needed for direct-run dev

### 3. Verify Changes

```bash
# Quick smoke tests
make smoke-test

# Run linters
make lint

# Run unit tests
make test-unit
# or directly:
PYTHONPATH="$(pwd)" pytest tests/unit -v

# Run all tests
make test
```

### 4. Populate Data for Feature Testing

`make seed-mock-data` is currently a no-op placeholder in the Makefile. One real seed script exists and takes `DATABASE_URL` as an env var:

```bash
DATABASE_URL=postgresql://waddleai:waddleai-dev@localhost:5432/waddleai \
  python3 scripts/seed_security_rag.py
```

Model-assignment seeding (formerly `scripts/seed_routing_matrix.py`, tied to
the retired tool_type x complexity x region routing_matrix grid, §7.6) is now
handled by migration 010's built-in internal-function rows plus the
management API's `POST /api/v1/routing/assignments/seed` convenience
endpoint (upserts a small default spec) or ad-hoc `POST
/api/v1/routing/assignments` calls -- the smart-routing engine (spec §7)
resolves complexity from the stage-2 classifier at request time rather than
from a pre-seeded per-complexity row.

When adding mock data for a new feature, follow this same pattern (a standalone, re-runnable `scripts/seed_*.py` script driven by `DATABASE_URL`) rather than introducing a new directory convention.

### 5. Test Multi-Backend Routing

WaddleAI's dual token system and multi-backend routing, exercised through the proxy's OpenAI-compatible API:

```bash
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer wa-test-token" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Hello"}]
  }'

curl http://localhost:8080/v1/models \
  -H "Authorization: Bearer wa-test-token"
```

### 6. Run Pre-Commit Checklist

```bash
make pre-commit
```

Runs, in order: `make lint`, `make test-security`, `make test`. See [Pre-Commit Documentation](PRE_COMMIT.md) for details on each step and how to fix common failures.

### 7. Testing & Validation

Complete guide: [Testing Documentation](TESTING.md).

```bash
# Smoke tests only (fast)
make smoke-test

# Unit tests only
make test-unit

# Integration tests
make test-integration

# All tests
make test

# Specific test file
pytest tests/unit/proxy/test_proxy_auth.py -v
```

### 8. Create Pull Request

```bash
# Push branch
git push origin feature-branch-name

# Create PR via GitHub CLI
gh pr create --title "Brief feature description" \
  --body "Detailed description of changes"
```

---

## Common Tasks

### Adding a New Python Dependency

Dependencies are pinned with hashes via `uv` (`uv pip compile --generate-hashes`), not edited by hand:

```bash
# Add to the appropriate requirements.in (root, or services/management/requirements.in)
echo "new-package>=1.0.0" >> requirements.in

# Recompile the pinned, hashed requirements.txt
uv pip compile requirements.in --generate-hashes --python-version 3.13 -o requirements.txt

# Reinstall in your venv
pip install -r requirements.txt

# Verify the import works
python3 -c "import new_package"
```

### Adding a New Environment Variable

Add the `os.getenv(...)` call in the relevant service's config (`services/management/app/config.py` for management, or directly in `proxy/apps/proxy_server/main.py` for the proxy), export it in your shell before starting that service, and document the default in the Helm chart's `values.yaml` if it's needed beyond local dev.

### Debugging a Service

Each service logs to stdout/stderr of the terminal it's running in — no separate log aggregation step for direct-run local dev.

**Check service health directly**:
```bash
curl http://localhost:8080/healthz    # proxy
curl http://localhost:8001/healthz    # management
curl http://localhost:8001/readyz     # management readiness
```

**Attach a debugger / run a one-off script against the same environment**:
```bash
PYTHONPATH="$(pwd):$(pwd)/services/management" python3
>>> from app import create_app
```

### Database Operations

**Connect to the database**:
```bash
psql postgresql://waddleai:waddleai-dev@localhost:5432/waddleai
```
```sql
\dt   -- list tables
```

**Reset the database** (deletes all data):
```bash
docker rm -f waddleai-postgres
docker run -d --name waddleai-postgres \
  -e POSTGRES_DB=waddleai -e POSTGRES_USER=waddleai -e POSTGRES_PASSWORD=waddleai-dev \
  -p 5432:5432 pgvector/pgvector:pg16
# Restart the management service — init_schema() recreates all tables automatically
```

**Apply pending migrations**:
```bash
cd services/management
DATABASE_URL=postgresql://waddleai:waddleai-dev@localhost:5432/waddleai alembic upgrade head
```

### Working with Git Branches

```bash
# Create feature branch
git checkout -b feature/new-feature-name

# Keep branch updated with the release branch you branched from
git fetch origin
git rebase origin/release/vX.Y.X

# Push branch
git push origin feature/new-feature-name
```

---

## Troubleshooting

### Services Won't Start

**Check if ports are already in use**:
```bash
lsof -i :8080   # proxy
lsof -i :8001   # management
lsof -i :3000   # webui

kill -9 <PID>

# Or bind to a different port
hypercorn apps.proxy_server.main:app --bind 0.0.0.0:8888 --reload
```

**Docker daemon not running** (needed for the Postgres/Valkey containers):
```bash
# macOS
open /Applications/Docker.app

# Linux
sudo systemctl start docker
```

### Database Connection Error

```bash
# Verify the container is running
docker ps --filter name=waddleai-postgres

# Check DATABASE_URL is exported in the shell running the service
echo "$DATABASE_URL"

# Connect directly to confirm credentials/reachability
psql "$DATABASE_URL"

# View container logs
docker logs waddleai-postgres
```

### Proxy or Management Service Won't Start

```bash
# Re-run in the foreground to see the traceback directly (no --reload)
cd proxy && PYTHONPATH="$(pwd)/..:$(pwd)" hypercorn apps.proxy_server.main:app --bind 0.0.0.0:8080

# Common cause: PYTHONPATH not exported before the hypercorn invocation,
# causing "ModuleNotFoundError: No module named 'shared'" or 'apps'
```

### Smoke Tests Failing

```bash
make smoke-test
```

**Common issues**:
- Service not healthy — check the terminal running that service for the traceback
- Wrong port — confirm the service is bound to the port the test expects (8080 proxy, 8001 management)
- Missing environment variables — `DATABASE_URL` / `JWT_SECRET` not exported in the shell that started the service

See [Testing Documentation](TESTING.md) for detailed troubleshooting.

### Git Merge Conflicts

```bash
git status

# Edit conflicted files (marked with <<<<, ====, >>>>)
# Remove conflict markers and keep desired code

git add <resolved-file>
git commit -m "Resolve merge conflicts"
```

### Slow Docker Builds

```bash
docker system df
docker system prune
docker build --no-cache -f proxy/Dockerfile -t waddleai/proxy:local .
```

---

## Tips & Best Practices

### Fast Iteration

For the fastest edit-test loop, skip containers entirely and run services directly with `--reload` (Python) / `npm run dev` (webui) as shown in [Starting Development Environment](#starting-development-environment). Only fall back to Docker builds or the local Kubernetes path when you specifically need to validate the built image.

### Environment-Specific Configuration

```bash
# Local shell exports (this guide)      — development
# Kubernetes Secret/ConfigMap           — beta/gamma/prod (see k8s/helm/waddleai)
```

### Code Organization

```bash
# Remove old branches
git branch -D old-branch

# Clean local Docker images
docker image prune -a
docker container prune
```

---

## Related Documentation

- **Testing**: [Testing Documentation](TESTING.md)
  - Smoke tests for proxy/management
  - Unit/integration/E2E tests
  - Cross-architecture testing

- **Testing Setup (OpenWebUI)**: [Testing Setup](TESTING_SETUP.md)
  - Local proxy + OpenWebUI environment for manual LLM testing

- **Pre-Commit**: [Pre-Commit Checklist](PRE_COMMIT.md)
  - Linting requirements
  - Security scanning
  - Build verification
  - Test requirements

- **Deployment**: [Helm Chart](../k8s/helm/waddleai/)
  - Helm chart at `k8s/helm/waddleai`
  - Beta cluster deployment via `./scripts/deploy-beta.sh`

- **Workflows**: [CI/CD Workflows](WORKFLOWS.md)
  - GitHub Actions pipelines
  - Build automation
  - Test automation

---

**Last Updated**: 2026-08-10
**Maintained by**: Penguin Tech Inc
