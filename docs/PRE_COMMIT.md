# Pre-Commit Checklist

**CRITICAL: This checklist MUST be followed before every commit.**

## Automated Pre-Commit Script

**Run the automated pre-commit script to execute all checks:**

```bash
./scripts/pre-commit/pre-commit.sh
```

This script will:
1. Run all checks in the correct order
2. Log output to `/tmp/pre-commit-waddleai-<epoch>.log`
3. Provide a summary of pass/fail status
4. Echo the log file location for review

**Individual check scripts** (run separately if needed):
- `./scripts/pre-commit/check-python.sh` - Python linting & security
- `./scripts/pre-commit/check-security.sh` - All security scans
- `./scripts/pre-commit/check-secrets.sh` - Secret detection
- `./scripts/pre-commit/check-docker.sh` - Docker build & validation
- `./scripts/pre-commit/check-tests.sh` - Unit tests
- `./scripts/pre-commit/check-tokens.sh` - Dual token system validation
- `./scripts/pre-commit/check-routing.sh` - Multi-backend routing validation

---

## Required Steps (In Order)

Before committing, run in this order (or use `./scripts/pre-commit/pre-commit.sh`):

### Foundation Checks
- [ ] **Linters**: `flake8 proxy management shared` or equivalent
- [ ] **Code formatting**: `black --check proxy management shared`
- [ ] **Import sorting**: `isort --check-only proxy management shared`
- [ ] **Type checking**: `mypy proxy management shared`
- [ ] **Security scans**: `bandit -r proxy management shared -ll`

### Build & Integration Verification
- [ ] **Build & Run**: Verify code compiles and containers start successfully
- [ ] **Smoke tests** (mandatory, <2 min): `make smoke-test`
  - Proxy service health check (HTTP 200)
  - Management service health check (HTTP 200)
  - Dual token validation working
  - Backend routing responds correctly
  - See: [Testing Documentation - Smoke Tests](TESTING.md#smoke-tests)

### Feature Testing & Documentation
- [ ] **Mock data** (for testing features): Ensure 3-4 test items per feature via `make seed-mock-data`
  - Populate development database with realistic test data for dual token testing
  - Needed before capturing screenshots and UI testing
  - See: [Testing Documentation - Mock Data Scripts](TESTING.md#mock-data-scripts)
- [ ] **API testing** (for endpoint changes): Create and run API testing scripts
  - Location: `tests/api/` directory with service-specific subdirectories
  - Test coverage: Health checks, authentication, dual token validation, routing
  - Run before commit: Each test script should be executable and pass completely

### Comprehensive Testing
- [ ] **Unit tests**: `pytest tests/unit/ -v`
  - Network isolated, mocked dependencies
  - Must pass before committing
  - Token validation tests included
  - Routing logic tests included
- [ ] **Integration tests**: Component interaction verification
  - Tests with real database and service communication
  - Dual token consumption tracking validation
  - Multi-backend routing decision logic validation
  - See: [Testing Documentation - Integration Tests](TESTING.md#integration-tests)

### Finalization
- [ ] **Version updates**: Update `.version` if releasing new version
- [ ] **Documentation**: Update docs if adding/changing workflows or token system
- [ ] **Docker builds**: Verify Dockerfile uses debian-slim base (no alpine)
- [ ] **Cross-architecture**: (Optional) Test alternate architecture with QEMU
  - `docker buildx build --platform linux/arm64 proxy/` (if on amd64)
  - `docker buildx build --platform linux/amd64 proxy/` (if on arm64)
  - See: [Testing Documentation - Cross-Architecture Testing](TESTING.md#cross-architecture-testing)

---

## Language-Specific Commands

### Python

```bash
# Linting
flake8 proxy management shared --max-line-length=100
black --check proxy management shared
isort --check-only proxy management shared
mypy proxy management shared --ignore-missing-imports

# Security
bandit -r proxy management shared -ll
safety check

# Build & Run
python -m py_compile proxy/**/*.py management/**/*.py shared/**/*.py  # Syntax check
pip install -r requirements.txt    # Dependencies
python proxy/apps/proxy_server/main.py &  # Verify proxy starts (then kill)
python management/apps/management_server/main.py &  # Verify management starts

# Tests
pytest tests/unit/ -v --cov=proxy,management,shared
pytest tests/integration/ -v
pytest tests/unit/proxy/test_token_validation.py -v  # Token tests
pytest tests/unit/proxy/test_routing.py -v  # Routing tests
```

### Docker / Containers

```bash
# Lint Dockerfiles
hadolint proxy/Dockerfile management/Dockerfile

# Verify base image (debian-slim, NOT alpine)
grep -E "^FROM.*slim" proxy/Dockerfile management/Dockerfile

# Build & Run
docker build -t waddleai-proxy:test proxy/             # Build proxy
docker build -t waddleai-management:test management/   # Build management

# Start containers
docker run -d --name test-proxy waddleai-proxy:test
docker run -d --name test-management waddleai-management:test

# Check logs
docker logs test-proxy
docker logs test-management

# Cleanup
docker stop test-proxy test-management
docker rm test-proxy test-management

# Docker Compose (if applicable)
docker-compose -f docker-compose.dev.yml build  # Build all services
docker-compose -f docker-compose.dev.yml up -d  # Start all services
docker-compose -f docker-compose.dev.yml logs   # Check logs
docker-compose -f docker-compose.dev.yml down   # Cleanup
```

### YAML Linting

```bash
yamllint .github/workflows/*.yml docker-compose*.yml
```

### Markdown

```bash
markdownlint docs/*.md README.md
```

---

## Commit Rules

- **NEVER commit automatically** unless explicitly requested by the user
- **NEVER push to remote repositories** under any circumstances
- **ONLY commit when explicitly asked** - never assume commit permission
- **Wait for approval** before running `git commit`

---

## Security Scanning Requirements

### Before Every Commit
- **Run security audits on all modified packages**:
  - **Python packages**: Run `bandit -r proxy management shared -ll`
  - **Dependency checks**: Run `safety check` for vulnerable packages
  - **CodeQL**: All code must pass CodeQL security analysis (GitHub)
- **Do NOT commit if security vulnerabilities are found** - fix all issues first
- **Document vulnerability fixes** in commit message if applicable

### Vulnerability Response
1. Identify affected packages and severity
2. Update to patched versions immediately
3. Test updated dependencies thoroughly
4. Document security fixes in commit messages
5. Verify no new vulnerabilities introduced

---

## API Testing Requirements

Before committing changes to proxy/management services:

- **Create and run API testing scripts** for each modified service
- **Testing scope**: All new endpoints and modified functionality
- **Test files location**: `tests/api/` directory with service-specific subdirectories
  - `tests/api/proxy/` - Proxy service API tests
  - `tests/api/management/` - Management service API tests
- **Run before commit**: Each test script should be executable and pass completely
- **Test coverage**: Health checks, authentication, token validation, routing, error cases

**Example API Test Script**:
```bash
#!/bin/bash
# tests/api/proxy/test_dual_tokens.sh

set -e

API_URL="http://localhost:8000"
ADMIN_TOKEN="wa-admin-token"

# Test 1: Token validation
echo "Testing token validation..."
curl -X POST "$API_URL/v1/chat/completions" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4", "messages": [{"role": "user", "content": "test"}]}'

# Test 2: Dual token consumption
echo "Testing dual token consumption..."
curl -X GET "http://localhost:8001/api/v1/analytics/tokens/$ADMIN_TOKEN" \
  -H "Authorization: Bearer $ADMIN_TOKEN"

# Test 3: Backend routing
echo "Testing backend routing..."
curl -X POST "$API_URL/v1/chat/completions" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4", "messages": [{"role": "user", "content": "test"}], "waddleai_route": "openai"}'

echo "✓ All API tests passed"
```

---

## Dual Token System Requirements

Before committing changes to token handling:

- [ ] **Token validation works**: `pytest tests/unit/proxy/test_token_validation.py -v`
- [ ] **Quota enforcement works**: `pytest tests/unit/proxy/test_quota_management.py -v`
- [ ] **Token consumption tracked**: `pytest tests/integration/test_token_consumption.py -v`
- [ ] **WaddleAI tokens calculated correctly**: Conversion from LLM tokens verified
- [ ] **LLM tokens tracked separately**: Input/output tokens tracked independently
- [ ] **Usage analytics accurate**: Billing data is correct

---

## Multi-Backend Routing Requirements

Before committing changes to routing logic:

- [ ] **Routing selection works**: `pytest tests/unit/proxy/test_routing.py -v`
- [ ] **Backend fallback works**: Test fallback scenarios when backend unavailable
- [ ] **Explicit backend selection works**: `waddleai_route` parameter tested
- [ ] **Smart routing works**: Automatic backend selection based on model
- [ ] **Rate limiting enforced**: Requests per minute limits tested
- [ ] **Error handling**: Invalid backend errors handled gracefully

---

## Smoke Tests Requirements

Mandatory smoke tests must pass before committing:

```bash
# Run all smoke tests
make smoke-test

# Or run individually:
./tests/smoke/health/test-proxy-health.sh
./tests/smoke/health/test-management-health.sh
./tests/smoke/tokens/test-token-validation.sh
./tests/smoke/routing/test-backend-routing.sh
```

**Required to pass**:
- ✅ Proxy service responds with health 200
- ✅ Management service responds with health 200
- ✅ Dual token validation passes
- ✅ Backend routing responds correctly
- ✅ API authentication required and working

---

## Pre-Commit Checklist Template

Use this checklist before every commit:

```
Before EVERY commit, verify:

[ ] Linting passes
    - Python: flake8, black, isort, mypy, bandit
    - Docker: hadolint
    - YAML: yamllint

[ ] Security checks pass
    - Python: bandit, safety check
    - CodeQL analysis passes (GitHub)
    - No hardcoded secrets

[ ] Tests pass locally
    - Unit tests complete successfully
    - Integration tests pass
    - No test failures or errors

[ ] Smoke tests pass
    - Proxy health: HTTP 200
    - Management health: HTTP 200
    - Dual token validation works
    - Backend routing works

[ ] Manual testing complete
    - Token validation endpoints respond
    - Routing selection works correctly
    - Logs show expected output
    - No debug code left in

[ ] No debug code left in
    - No console.log, print(), println! statements
    - No commented code blocks
    - No debug flags enabled

[ ] Configuration is correct
    - Environment variables correct
    - Database connections working
    - API endpoints accessible
    - Backend connections configured

[ ] Documentation updated
    - Comments added/updated if needed
    - API docs updated if endpoints changed
    - README updated if major changes
    - DEVELOPMENT.md updated if workflow changed

[ ] Version file updated (if releasing)
    - .version file has new version
    - RELEASE_NOTES.md updated
    - Format: MAJOR.MINOR.PATCH
```

---

**Last Updated**: 2026-01-06
**Maintained by**: Penguin Tech Inc
