# WaddleAI - Claude Code Context

## Project Overview

WaddleAI is an AI proxy and management system that provides OpenAI-compatible APIs with advanced routing, security, and token management. It enables organizations to manage AI/LLM requests across multiple backends with enterprise-grade request handling, authentication, rate limiting, and comprehensive management interfaces.

**Core Features:**
- OpenAI-compatible API proxy with multi-backend routing
- Advanced model routing and load balancing
- Dual token system (WaddleAI tokens + LLM tokens) for accurate billing
- Role-based access control (Admin, Resource Manager, Reporter, User)
- Request rate limiting, caching, and quota management
- Comprehensive administrative dashboard and analytics
- Security scanning, threat detection, and vulnerability monitoring
- Prometheus metrics and observability integration

## Technology Stack

### Services & Containers

**WaddleAI consists of two core services:**

| Service | Language | Purpose | Port |
|---------|----------|---------|------|
| **Proxy** | Python 3.13 | OpenAI-compatible API endpoint, routing, caching, request handling | 8000 |
| **Management** | Python 3.13 | Administrative dashboard, user management, analytics, quota control | 8001 |

**Container Architecture:**
- Each service runs in a separate Docker container
- Independent scaling and deployment
- Shared PostgreSQL/MariaDB database and Redis cache
- Multi-architecture builds (amd64, arm64)

### Languages & Frameworks

**Language Selection:**
- **Python**: 3.13.x for all applications (3.12+ minimum)
- **Web Framework**: Flask + Flask-Security-Too (mandatory for all Flask applications)
- **Database ORM**: Hybrid approach
  - **SQLAlchemy**: Database initialization and schema creation
  - **PyDAL**: Day-to-day operations and migrations (mandatory)
- **Performance**: Dataclasses with slots, type hints, async/await required

### Infrastructure & DevOps
- **Containers**: Docker with multi-stage builds, multi-architecture (amd64, arm64)
- **Orchestration**: Docker Compose (local), Kubernetes (production ready)
- **CI/CD**: GitHub Actions with comprehensive pipelines
- **Container Registry**: GitHub Container Registry (ghcr.io)
- **Monitoring**: Prometheus metrics, Grafana dashboards
- **Logging**: Structured JSON logging with configurable levels
- **Security**: Pre-commit scanning (bandit, CodeQL, npm audit)

### Databases & Storage
- **Primary**: PostgreSQL (default, configurable via DB_TYPE)
- **Production**: MariaDB Galera cluster (with WSREP support)
- **Cache**: Redis/Valkey with optional TLS
- **Database Strategy**: Hybrid (SQLAlchemy init + PyDAL operations)
- **Supported DB_TYPE Values**: `postgres`, `mysql`, `sqlite`

## Project Structure

```
WaddleAI/
├── .github/
│   ├── workflows/
│   │   ├── docker-build.yml          # Main CI/CD pipeline
│   │   ├── version-release.yml       # Version-triggered releases
│   │   └── deploy-cloudflare-pages.yml # Website deployment
│   └── dependabot.yml
├── proxy/                             # API/Backend service (Python)
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── apps/
│   │   ├── __init__.py
│   │   ├── routes.py                 # API endpoints
│   │   ├── middleware.py             # Request handling
│   │   └── cache.py                  # Redis caching
│   ├── config/
│   └── .plan                          # Recovery plan (crash recovery)
├── management/                        # Management/Dashboard service (Python)
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── apps/
│   │   ├── __init__.py
│   │   ├── admin.py                  # Admin interface
│   │   ├── users.py                  # User management
│   │   └── analytics.py              # Reporting
│   ├── templates/
│   ├── static/
│   ├── config/
│   └── .todo                          # Todo tracking (crash recovery)
├── shared/                            # Shared utilities
│   ├── auth.py                        # Authentication
│   ├── db.py                          # Database connection (SQLAlchemy/PyDAL)
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
├── README.md                          # Project overview (max 25K chars)
├── RELEASE_NOTES.md                   # Version release notes
├── NETWORK-ARCHITECTURE.md            # Network design (max 25K chars)
├── CLAUDE.md                          # This file (39K exception allowed)
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
- `GET /api/v1/users` - List users
- `POST /api/v1/users` - Create user
- `GET /api/v1/users/<id>` - Get user details
- `PUT /api/v1/users/<id>` - Update user
- `DELETE /api/v1/users/<id>` - Delete user

**Authentication:**
- Admin panel: Session-based (Flask-Security-Too)
- API: JWT tokens required

## Security & Authentication

### Flask-Security-Too Integration
- **MANDATORY for all Flask applications**
- Role-based access control (RBAC) with configurable roles
- User authentication and session management
- Password hashing with bcrypt (mandatory)
- Email confirmation and password reset
- Two-factor authentication (2FA) support
- Token-based authentication for APIs (JWT)

### API Authentication
- All endpoints require Bearer token in Authorization header
- Format: `Authorization: Bearer <api-key>`
- JWT tokens with configurable expiration
- API key management with rotation support

### Security Standards
- **TLS**: Enforce TLS 1.2 minimum, prefer TLS 1.3
- **Input Validation**: ALL API endpoints MUST validate user input
- **SQL Injection Prevention**: Use PyDAL for all database queries (never construct SQL strings)
- **Secrets Management**: Never hardcode credentials - use environment variables
- **Security Scanning**: Mandatory bandit, CodeQL, npm audit analysis
- **Dependency Scanning**: Trivy container scanning and Dependabot monitoring

## PenguinTech License Server Integration

All projects integrate with the centralized PenguinTech License Server at `https://license.penguintech.io` for feature gating and enterprise functionality.

**IMPORTANT: License enforcement is ONLY enabled when project is marked as release-ready**
- Development phase: All features available, no license checks
- Release phase: License validation required, feature gating active

**License Key Format**: `PENG-XXXX-XXXX-XXXX-XXXX-ABCD`

**Core Endpoints**:
- `POST /api/v2/validate` - Validate license
- `POST /api/v2/features` - Check feature entitlements
- `POST /api/v2/keepalive` - Report usage statistics

**Environment Variables**:
```bash
LICENSE_KEY=PENG-XXXX-XXXX-XXXX-XXXX-ABCD
LICENSE_SERVER_URL=https://license.penguintech.io
PRODUCT_NAME=waddleai
RELEASE_MODE=false  # Development (default)
RELEASE_MODE=true   # Production (explicitly set)
```

## Version Management System

**Format**: `vMajor.Minor.Patch.build`
- **Major**: Breaking changes, API changes, removed features
- **Minor**: Significant new features and functionality additions
- **Patch**: Minor updates, bug fixes, security patches
- **Build**: Epoch64 timestamp of build time

**Update Commands**:
```bash
./scripts/version/update-version.sh          # Increment build timestamp
./scripts/version/update-version.sh patch    # Increment patch version
./scripts/version/update-version.sh minor    # Increment minor version
./scripts/version/update-version.sh major    # Increment major version
```

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
- Documentation: [See docs/WORKFLOWS.md](docs/WORKFLOWS.md)

**2. version-release.yml** (Version Management)
- Triggers: `.version` file changes on main branch
- Creates GitHub pre-releases automatically
- Parses semantic version from `.version` file
- Documentation: [See docs/WORKFLOWS.md](docs/WORKFLOWS.md)

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
- [ ] `bandit -r proxy management shared -ll` passes (Python security)
- [ ] `npm audit` passes (JavaScript dependencies)
- [ ] `CodeQL` analysis passes (GitHub advanced security)
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
- [ ] RELEASE_NOTES.md updated

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

### CRITICAL: Git Workflow Rules

- **NEVER commit automatically** unless explicitly requested by the user
- **NEVER push to remote repositories** under any circumstances
- **ONLY commit when explicitly asked** - never assume commit permission
- Always use feature branches for development
- Require pull request reviews for main branch
- Automated testing must pass before merge

**Before Every Commit - Security Scanning**:
- **Run security audits on all modified packages**:
  - **Python packages**: Run `bandit -r proxy management shared -ll`
  - **Node.js packages**: Run `npm audit` (if applicable)
- **Do NOT commit if security vulnerabilities are found** - fix all issues first

## Configuration

### Environment Variables

**Shared:**
```bash
# Logging
LOG_LEVEL=INFO

# Database (supports: postgres, mysql, sqlite)
# Local development: postgres or sqlite
# Production: MariaDB Galera cluster required
DB_TYPE=postgres
DB_HOST=localhost
DB_PORT=5432
DB_NAME=waddleai
DB_USER=waddleai
DB_PASS=<password>

# MariaDB Galera (production only)
DB_GALERA_CLUSTER_NODES=node1,node2,node3
DB_GALERA_WSREP_PROVIDER=/usr/lib/galera/libgalera_smm.so

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

### Local State Management

**Crash Recovery Files:**
- **`.plan`** (proxy service): Recovery plan for API/Backend service
- **`.todo`** (management service): Todo tracking for state recovery

**Purpose**: Enable system recovery from unexpected shutdowns

## Critical Development Rules

### Development Philosophy: Safe, Stable, and Feature-Complete

**NEVER take shortcuts or the "easy route" - ALWAYS prioritize safety, stability, and feature completeness**

#### Core Principles
- **No Quick Fixes**: Resist quick workarounds or partial solutions
- **Complete Features**: Fully implemented with proper error handling and validation
- **Safety First**: Security, data integrity, and fault tolerance are non-negotiable
- **Stable Foundations**: Build on solid, tested components
- **Future-Proof Design**: Consider long-term maintainability and scalability
- **No Technical Debt**: Address issues properly the first time

#### Red Flags (Never Do These)
- ❌ Skipping input validation "just this once"
- ❌ Writing custom validators instead of using shared libraries
- ❌ Hardcoding credentials or configuration
- ❌ Ignoring error returns or exceptions
- ❌ Commenting out failing tests to make CI pass
- ❌ Deploying without proper testing
- ❌ Using deprecated or unmaintained dependencies
- ❌ Implementing partial features with "TODO" placeholders
- ❌ Bypassing security checks for convenience
- ❌ Assuming data is valid without verification
- ❌ Leaving debug code or backdoors in production

#### Quality Checklist Before Completion
- ✅ All error cases handled properly
- ✅ Unit tests cover all code paths
- ✅ Integration tests verify component interactions
- ✅ Security requirements fully implemented
- ✅ Performance meets acceptable standards
- ✅ Documentation complete and accurate
- ✅ Code review standards met
- ✅ No hardcoded secrets or credentials
- ✅ Logging and monitoring in place
- ✅ Build passes in containerized environment
- ✅ No security vulnerabilities in dependencies
- ✅ Edge cases and boundary conditions tested

### Linting & Code Quality Requirements
- **ALL code must pass linting** before commit - no exceptions
- **Python**: flake8, black, isort, mypy (type checking), bandit (security)
- **Docker**: hadolint
- **YAML**: yamllint
- **Markdown**: markdownlint
- **Shell**: shellcheck
- **CodeQL**: All code must pass CodeQL security analysis
- **PEP Compliance**: Python code must follow PEP 8, PEP 257 (docstrings), PEP 484 (type hints)

### Build & Deployment Requirements
- **NEVER mark tasks as completed until successful build verification**
- All Python builds MUST be executed within Docker containers
- Use containerized builds for local development and CI/CD pipelines
- Build failures must be resolved before task completion

### Documentation Standards
- **README.md**: Keep as overview and pointer to comprehensive docs/ folder
- **docs/ folder**: Create comprehensive documentation for all aspects
- **RELEASE_NOTES.md**: Maintain in docs/ folder, prepend new version releases to top
- Update CLAUDE.md when adding significant context
- **Build status badges**: Always include in README.md
- **Company homepage**: Point to www.penguintech.io

### File Size Limits
- **Maximum file size**: 25,000 characters for ALL code and markdown files
- **Split large files**: Decompose into modules, libraries, or separate documents
- **CLAUDE.md exception**: Maximum 39,000 characters (only exception to 25K rule)

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

## WaddleAI Integration

WaddleAI is a **proxy and management system** for AI/LLM request orchestration. It serves as a critical infrastructure component that abstracts and manages communication with multiple AI backends (OpenAI, Claude, local models, etc.).

**Role as AI Proxy/Management System:**
- Central gateway for all AI/LLM requests across the organization
- Provides unified OpenAI-compatible API interface
- Manages authentication, rate limiting, quota enforcement
- Implements token accounting and cost tracking (dual token system)
- Handles request routing to appropriate backend based on model availability, load, cost
- Enables seamless backend switching and fallback mechanisms
- Centralized monitoring, analytics, and security scanning
- Reduces vendor lock-in by supporting multiple LLM providers

**Self-Reference Integration:**
WaddleAI IS the AI proxy layer. This CLAUDE.md documents:
- How WaddleAI components interact (proxy ↔ management)
- API contract and authentication mechanisms
- Token management and billing system
- Multi-backend routing architecture
- Integration with external systems

**Integration with Other Systems:**
- Projects can integrate with WaddleAI by pointing to proxy service (port 8000)
- Use Bearer token authentication
- Send requests to OpenAI-compatible endpoints
- WaddleAI handles backend selection and request proxying

**When to Build on WaddleAI vs. Use Standalone:**
- Use WaddleAI: Multi-user environments, billing/quota tracking, multi-backend support needed
- Use standalone: Single-user development, direct LLM provider access acceptable

## Development Standards

Comprehensive development standards for WaddleAI are documented in [docs/STANDARDS.md](docs/STANDARDS.md).

**Key Standards Reference:**
- Dual token system: WaddleAI tokens (user-facing) + LLM tokens (actual usage)
- Multi-backend routing with fallback mechanisms
- PyDAL for all runtime database operations
- Flask-Security-Too for admin panel and API authentication
- Prometheus metrics on `/metrics` endpoint
- Structured JSON logging for observability
- Rate limiting and quota enforcement patterns

**Database Standards (PyDAL):**
- All database operations use PyDAL ORM (mandatory)
- SQLAlchemy used only for schema initialization
- Support PostgreSQL, MySQL, MariaDB Galera, SQLite
- Thread-safe connection pooling with retry logic
- Environment variable configuration via `DB_TYPE`

**Authentication Patterns (Flask-Security-Too + PyDAL):**
```python
# User authentication with Flask-Security-Too
from flask_security import Security, SQLAlchemyUserDatastore
from pydal import DAL

# Session-based for admin panel
# JWT/Bearer tokens for API endpoints
# Role-based access control (Admin, Resource Manager, Reporter, User)
```

**Multi-Backend Routing Patterns:**
- Route request based on model availability and load
- Implement fallback to secondary backends
- Track token usage per backend
- Log routing decisions for analytics

## Application Architecture

WaddleAI uses a **dual-container proxy/management microservices architecture**:

### Service Architecture

**Proxy Service** (Port 8000):
- OpenAI-compatible API endpoint
- Request routing engine with load balancing
- Token accounting and tracking
- Request validation and security scanning
- Cache layer (Redis) for response caching
- Middleware for authentication, rate limiting, request/response transformation

**Management Service** (Port 8001):
- Administrative dashboard (Flask + Jinja2 templates)
- User and API key management
- Analytics and reporting dashboard
- Quota and billing management
- Backend configuration interface
- System health and metrics dashboard

### Communication Flow

```
Client
  ↓ (Bearer Token)
┌─────────────────────────────────────────┐
│  Proxy Service (8000)                   │
│  - Route /v1/chat/completions           │
│  - Validate token + rate limit          │
│  - Select backend + route request       │
│  - Cache responses in Redis             │
│  - Track token usage                    │
└────────────┬────────────────────────────┘
             ↓
┌──────────────────────────────────────────┐
│  Backend Selection                       │
│  - OpenAI API                            │
│  - Anthropic Claude                      │
│  - Local LLM (ollama, vLLM)             │
│  - Other providers                       │
└──────────────────────────────────────────┘
```

### Data Flow: Token Accounting

```
1. Request arrives → WaddleAI token consumed
2. Request routed to backend → LLM tokens consumed
3. Response returned → Both token types recorded
4. Billing calculated → Dual-token accuracy
```

### Shared Components

- **shared/auth.py**: Token validation, API key management
- **shared/db.py**: SQLAlchemy + PyDAL database abstraction
- **shared/cache.py**: Redis caching utilities
- **shared/logging.py**: Structured JSON logging

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed architectural diagrams.

## Common Integration Patterns

### Flask-Security-Too Authentication + PyDAL

**Admin Panel (Session-Based):**
```python
from flask_security import Security, SQLAlchemyUserDatastore, login_required, roles_required

@app.route('/dashboard')
@login_required
@roles_required('admin')
def admin_dashboard():
    # Session-based authentication
    # Role checking handled automatically
    pass
```

**API Endpoints (JWT/Bearer Token):**
```python
from flask import request
from shared.auth import validate_token

@app.route('/api/v1/models', methods=['GET'])
def list_models():
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    user = validate_token(token)
    # Database queries use PyDAL
    pass
```

**Database Integration (PyDAL):**
```python
from pydal import DAL, Field

# Initialize in shared/db.py
db = DAL(f'{db_type}://{db_host}:{db_port}/{db_name}', user=db_user, password=db_pass)

# Define tables
db.define_table('api_keys',
    Field('user_id', 'reference auth_user'),
    Field('key_hash', 'string'),
    Field('name', 'string'),
    Field('created_on', 'datetime'))

# Use in business logic
# Query: records = db(db.api_keys.user_id == user_id).select()
# Insert: db.api_keys.insert(user_id=user_id, key_hash=hash, name='My Key')
# Update: db(db.api_keys.id == key_id).update(name='New Name')
```

### Multi-Backend Routing

**Backend Configuration:**
```python
BACKENDS = {
    'openai': {
        'url': 'https://api.openai.com/v1',
        'api_key': os.getenv('OPENAI_API_KEY'),
        'models': ['gpt-4', 'gpt-3.5-turbo'],
        'priority': 1,
        'token_ratio': 1.0
    },
    'claude': {
        'url': 'https://api.anthropic.com/v1',
        'api_key': os.getenv('ANTHROPIC_API_KEY'),
        'models': ['claude-3-opus', 'claude-3-sonnet'],
        'priority': 2,
        'token_ratio': 1.2
    }
}
```

**Routing Decision Logic:**
```python
def select_backend(model: str, load_preference: str = 'balanced'):
    available = [b for b in BACKENDS.values() if model in b['models']]
    if not available:
        raise ValueError(f'Model {model} not available')

    if load_preference == 'lowest_latency':
        return min(available, key=lambda x: x['priority'])
    elif load_preference == 'lowest_cost':
        return min(available, key=lambda x: x['token_ratio'])
    else:  # balanced (default)
        return available[0]  # Round-robin via priority
```

## Website Integration Requirements

WaddleAI requires integrated marketing and documentation websites for enterprise positioning.

**Website Components:**
1. **Marketing Website** (Node.js/Next.js)
   - Overview of WaddleAI capabilities
   - Use cases and benefits
   - Pricing and feature tiers
   - Getting started guide
   - Contact/sales information

2. **Documentation Website** (Markdown-based)
   - API reference documentation
   - Integration guides
   - Best practices and examples
   - Troubleshooting guides
   - Admin panel documentation

**Repository Structure:**
- Sparse checkout from `github.com/penguintechinc/website`
- `waddleai/` folder for marketing site
- `waddleai-docs/` folder for documentation site
- Deployed via deploy-cloudflare-pages.yml workflow

**Design Requirements:**
- Modern, professional aesthetic
- Responsive design (mobile-first)
- Performance optimized (Core Web Vitals)
- Subtle gradients and smooth transitions
- Clear call-to-action for API keys/signup
- Live API examples and documentation
- Integration with license server for account management

## Template Customization

WaddleAI's architecture can be extended for specific deployment scenarios:

### Adding Custom Backends

1. **Register Backend** in configuration
   ```python
   BACKENDS['custom'] = {
       'url': 'https://custom-llm.example.com',
       'api_key': os.getenv('CUSTOM_LLM_KEY'),
       'models': ['custom-model-v1'],
       'token_ratio': 0.8
   }
   ```

2. **Add Adapter** if API differs from OpenAI format
   ```python
   def adapt_openai_to_custom(openai_request):
       # Transform request format
       return custom_format_request
   ```

3. **Update Tests** and Documentation

### Extending Management Features

**Add Custom Analytics:**
- Extend `/api/v1/analytics` endpoint
- Add new PyDAL tables for tracking
- Create dashboard widgets

**Add Custom User Quotas:**
- Modify rate limiting logic
- Add quota enforcement in middleware
- Create quota management UI

**Add Custom Security Scanning:**
- Add request inspection middleware
- Integrate with security scanning services
- Add threat detection alerts

### Multi-Region Deployment

- Deploy proxy and management services in multiple regions
- Use shared PostgreSQL/MariaDB Galera cluster
- Implement Redis cluster for cache
- Use DNS routing or load balancer for region selection

## Documentation

**Primary Documentation:**
- `README.md` - Project overview and quick start
- `CLAUDE.md` - This file (development context)
- `docs/WORKFLOWS.md` - CI/CD pipeline documentation
- `docs/STANDARDS.md` - Development standards and requirements
- `docs/ARCHITECTURE.md` - System architecture and design
- `docs/API.md` - API reference and examples
- `NETWORK-ARCHITECTURE.md` - Network topology and flow

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

**Last Updated**: 2025-12-18
**Version**: 1.0.0
**Maintained by**: WaddleAI Development Team
**Repository**: https://github.com/penguintechinc/WaddleAI
**License Server**: https://license.penguintech.io
