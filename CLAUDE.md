# WaddleAI - Claude Code Context

## Project Overview

WaddleAI is an AI proxy system designed to route and manage AI/LLM requests across multiple backends. It provides enterprise-grade request handling, authentication, rate limiting, and management interfaces.

**Core Features:**
- OpenAI-compatible API proxy
- Multi-backend routing and load balancing
- Request rate limiting and caching
- Administrative dashboard
- User and API key management
- Security scanning and vulnerability monitoring

## Technology Stack

### Services

| Service | Language | Purpose | Port |
|---------|----------|---------|------|
| **Proxy** | Python 3.13 | OpenAI-compatible API endpoint, routing, caching | 8000 |
| **Management** | Python 3.13 | Admin dashboard, user management, monitoring | 8001 |

### Core Technologies

**Language & Framework:**
- **Python**: 3.13.x (required)
- **Web Framework**: Flask with Flask-Security-Too
- **Database ORM**: PyDAL
- **Async Support**: asyncio for high-concurrency operations

**Dependencies:**
- Flask + gunicorn (WSGI)
- Redis (caching, rate limiting)
- PostgreSQL (data persistence)
- Celery (background tasks)
- plotly + pandas (analytics)

**DevOps & Infrastructure:**
- **Containers**: Docker (multi-architecture: amd64, arm64)
- **Orchestration**: Docker Compose (local), Kubernetes (production)
- **CI/CD**: GitHub Actions
- **Registry**: GitHub Container Registry (ghcr.io)
- **Monitoring**: Prometheus metrics, Grafana dashboards

## Project Structure

```
WaddleAI/
├── .github/
│   ├── workflows/
│   │   ├── docker-build.yml          # Main CI/CD pipeline
│   │   ├── version-release.yml       # Version-triggered releases
│   │   └── deploy-cloudflare-pages.yml # Website deployment
│   └── dependabot.yml
├── proxy/                             # Proxy service (Python)
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── apps/
│   │   ├── __init__.py
│   │   ├── routes.py                 # API endpoints
│   │   ├── middleware.py             # Request handling
│   │   └── cache.py                  # Redis caching
│   └── config/
├── management/                        # Management service (Python)
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── apps/
│   │   ├── __init__.py
│   │   ├── admin.py                  # Admin interface
│   │   ├── users.py                  # User management
│   │   └── analytics.py              # Reporting
│   ├── templates/
│   ├── static/
│   └── config/
├── shared/                            # Shared utilities
│   ├── auth.py                        # Authentication
│   ├── db.py                          # Database connection
│   ├── cache.py                       # Cache utilities
│   ├── errors.py                      # Error definitions
│   └── logging.py                     # Logging setup
├── tests/                             # Test suites
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── docs/                              # Documentation
│   ├── WORKFLOWS.md                   # CI/CD documentation
│   ├── STANDARDS.md                   # Development standards
│   ├── ARCHITECTURE.md                # Architecture design
│   └── API.md                         # API reference
├── docker-compose.yml                 # Production environment
├── docker-compose.testing.yml         # Testing environment
├── .version                           # Version file (SemVer)
├── requirements.txt                   # Root-level dependencies
├── README.md                          # Project overview
├── NETWORK-ARCHITECTURE.md            # Network design
├── CLAUDE.md                          # This file
└── .env.dev                           # Development environment
```

## API Endpoints

### Proxy Service (Port 8000)

**OpenAI-Compatible Endpoints:**
- `POST /v1/chat/completions` - Chat completion requests
- `POST /v1/embeddings` - Embedding generation
- `GET /health` - Health check
- `GET /metrics` - Prometheus metrics

**Authentication:**
- All endpoints require Bearer token in Authorization header
- Format: `Authorization: Bearer <api-key>`

### Management Service (Port 8001)

**Admin Dashboard:**
- `GET /` - Dashboard home
- `GET /users` - User management
- `GET /analytics` - Analytics and reporting
- `GET /health` - Health check
- `GET /metrics` - Prometheus metrics

**API Endpoints:**
- `GET /api/users` - List users
- `POST /api/users` - Create user
- `GET /api/users/<id>` - Get user details
- `PUT /api/users/<id>` - Update user
- `DELETE /api/users/<id>` - Delete user

**Authentication:**
- Admin panel: Session-based (Flask-Security-Too)
- API: JWT tokens required

## CI/CD & Workflows

### Build Naming Convention

Container images follow automatic naming based on branch and version:

| Scenario | Main Branch | Other Branches |
|----------|------------|-----------------|
| Regular build | `beta-<epoch64>` | `alpha-<epoch64>` |
| Version release | `vX.X.X-beta` | `vX.X.X-alpha` |
| Tagged release | `vX.X.X` + `latest` | N/A |

### Main Workflows

**1. docker-build.yml** (Main CI/CD Pipeline)
- Triggers: Push to main/v1.x, PRs, version tags
- Jobs: test → build-and-push → security-scan → integration-test → release → cleanup
- Security: bandit, Trivy scanning
- Coverage: Codecov integration
- Documentation: [See WORKFLOWS.md](docs/WORKFLOWS.md)

**2. version-release.yml** (Version Management)
- Triggers: `.version` file changes on main branch
- Creates GitHub pre-releases automatically
- Parses semantic version from `.version` file
- Documentation: [See WORKFLOWS.md](docs/WORKFLOWS.md)

**3. deploy-cloudflare-pages.yml** (Website)
- Triggers: Changes to website/ directory
- Builds and deploys to Cloudflare Pages
- npm audit for dependency scanning
- PR comments with preview URLs

### Security Scanning

**Code Security:**
- **bandit**: Python security issues (severity: low+)
- **npm audit**: JavaScript/Node.js dependencies
- **Trivy**: Container image scanning

**Container Registry:**
- Registry: ghcr.io (GitHub Container Registry)
- Authentication: GITHUB_TOKEN provided by Actions
- Multi-arch: linux/amd64, linux/arm64

## Development Workflow

### Local Setup

```bash
# Clone and setup
git clone https://github.com/penguintechinc/WaddleAI.git
cd WaddleAI

# Install dependencies
pip install -r requirements.txt

# Setup environment
cp .env.dev .env

# Start services
docker-compose -f docker-compose.testing.yml up -d

# Run tests
pytest tests/unit/ -v --cov=proxy,management,shared
```

### Pre-Commit Checklist

**Code Quality:**
- [ ] `flake8 proxy management shared` passes
- [ ] `black --check proxy management shared` passes
- [ ] `isort --check-only proxy management shared` passes
- [ ] `mypy proxy management shared` passes

**Security:**
- [ ] `bandit -r proxy management shared -ll` passes
- [ ] No hardcoded secrets or credentials
- [ ] No debug logging of sensitive data

**Testing:**
- [ ] `pytest tests/unit/ -v` passes
- [ ] Coverage >= 80%
- [ ] New tests for new features

**Documentation:**
- [ ] Docstrings updated
- [ ] API docs updated if endpoints changed
- [ ] WORKFLOWS.md updated if CI/CD changed

**Version:**
- [ ] `.version` updated for releases
- [ ] CHANGELOG.md updated

### Git Workflow

**Branch Strategy:**
- Feature: `feature/<description>`
- Bugfix: `bugfix/<description>`
- CI/CD: `ci/<description>`
- Docs: `docs/<description>`

**Before Commit:**
1. Run all pre-commit checks locally
2. Ensure tests pass
3. Update documentation
4. Update `.version` if releasing

**Commit Message Format:**
```
<type>(<scope>): <subject>

<body>

<footer>
```

**Example:**
```
feat(proxy): add request timeout configuration

- Add REQUEST_TIMEOUT environment variable
- Default timeout: 30 seconds
- Configurable per-request via headers
- Update integration tests

Closes #123
```

## Configuration

### Environment Variables

**Shared:**
```bash
# Logging
LOG_LEVEL=INFO

# Database
DB_TYPE=postgres
DB_HOST=localhost
DB_PORT=5432
DB_NAME=waddleai
DB_USER=waddleai
DB_PASS=<password>

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0

# Flask
FLASK_ENV=development
SECRET_KEY=<secure-key>
SECURITY_PASSWORD_SALT=<secure-salt>
```

**Proxy Service:**
```bash
PROXY_PORT=8000
REQUEST_TIMEOUT=30
RATE_LIMIT_REQUESTS=1000
RATE_LIMIT_WINDOW=3600
```

**Management Service:**
```bash
MANAGEMENT_PORT=8001
ENABLE_ANALYTICS=true
ANALYTICS_RETENTION_DAYS=90
```

### Version File

**Location**: `.version` (repository root)

**Format**: Semantic versioning
```
MAJOR.MINOR.PATCH
```

**Example**: `1.2.3`

**Usage**:
- Edit `.version` and commit to trigger release workflow
- Both docker-build and version-release detect changes
- Images automatically tagged with version

## Testing Strategy

### Unit Tests
- Framework: pytest
- Location: `tests/unit/`
- Isolation: Mocked dependencies, no network
- Coverage: >= 80% required

### Integration Tests
- Location: `tests/integration/`
- Services: Real database, Redis, docker-compose
- Health checks before testing
- Cleanup on completion

### E2E Tests
- Location: `tests/e2e/`
- Full workflow testing
- API endpoint validation
- User flow verification

## Monitoring & Observability

### Health Endpoints

**Proxy** (`GET /health`):
```json
{
  "status": "healthy",
  "service": "proxy",
  "version": "1.2.3",
  "timestamp": "2023-12-11T14:30:00Z"
}
```

**Management** (`GET /health`):
```json
{
  "status": "healthy",
  "service": "management",
  "version": "1.2.3",
  "timestamp": "2023-12-11T14:30:00Z"
}
```

### Metrics Endpoints

Both services expose Prometheus metrics at `GET /metrics`:
- HTTP request counts
- Request duration histograms
- Cache hit/miss ratios
- API usage statistics
- Error rates by endpoint

### Logging

**Format**: Structured JSON logging
```python
logger.info("Request processed", extra={
    "service": "proxy",
    "endpoint": "/v1/chat/completions",
    "status": 200,
    "duration_ms": 145,
    "timestamp": "2023-12-11T14:30:00Z"
})
```

## Documentation

**Primary Documentation:**
- `README.md` - Project overview and quick start
- `CLAUDE.md` - This file (development context)
- `docs/WORKFLOWS.md` - CI/CD pipeline documentation
- `docs/STANDARDS.md` - Development standards and requirements
- `docs/ARCHITECTURE.md` - System architecture and design
- `docs/API.md` - API reference and examples
- `NETWORK-ARCHITECTURE.md` - Network topology and flow

## Critical Development Rules

### Safety & Stability First

**NEVER take shortcuts - ALWAYS prioritize safety, stability, and completeness**

#### Red Flags (Never Do These)
- ❌ Hardcoding credentials or configuration
- ❌ Skipping input validation
- ❌ Commenting out failing tests
- ❌ Deploying without testing
- ❌ Ignoring security warnings
- ❌ Assuming data is valid without verification
- ❌ Leaving debug code in production

#### Quality Checklist
- ✅ All error cases handled properly
- ✅ Unit tests cover all code paths
- ✅ Input validation on all endpoints
- ✅ Security scanning passes
- ✅ No hardcoded secrets
- ✅ Performance acceptable
- ✅ Documentation complete
- ✅ Code review approved

### Security Requirements

**Before Commit:**
- Run `bandit -r proxy management shared -ll`
- Check `npm audit` (if website changes)
- No hardcoded credentials or API keys
- No secrets in logs or error messages

**Before Merge:**
- All security scans passed
- Code review completed
- All tests passing
- CI/CD pipeline green

### Code Standards

**Python Requirements:**
- PEP 8 compliance (120 char lines)
- Type hints on all functions
- Google-style docstrings
- 80%+ test coverage

**Linting:**
- flake8: Style compliance
- black: Code formatting
- isort: Import organization
- mypy: Type checking
- bandit: Security scanning

## Deployment

### Local Development

```bash
docker-compose -f docker-compose.testing.yml up -d
curl http://localhost:8000/health
curl http://localhost:8001/health
```

### Production Deployment

**Via Kubernetes:**
1. Update `.version` file
2. Commit and push to main
3. docker-build workflow tags images
4. Pull images in production environment
5. Deploy via Helm or kubectl

**Container Registry:**
- Images: ghcr.io/penguintechinc/waddleai/{proxy,management}
- Tags: version + beta tags

## Support & Resources

**Documentation:**
- Development Standards: [docs/STANDARDS.md](docs/STANDARDS.md)
- Workflow Reference: [docs/WORKFLOWS.md](docs/WORKFLOWS.md)
- Architecture Guide: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- API Reference: [docs/API.md](docs/API.md)
- Network Design: [NETWORK-ARCHITECTURE.md](NETWORK-ARCHITECTURE.md)

**Troubleshooting:**
- Check workflow logs in GitHub Actions
- Review container logs: `docker logs <container-name>`
- Verify environment variables are set
- Ensure dependencies are installed

---

**Last Updated**: 2025-12-11
**Version**: 0.0.0-beta
**Maintained by**: WaddleAI Development Team
**Repository**: https://github.com/penguintechinc/WaddleAI
