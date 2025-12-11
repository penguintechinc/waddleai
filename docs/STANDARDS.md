# WaddleAI Development Standards

This document outlines the development, testing, and deployment standards for the WaddleAI project.

## Table of Contents

1. [Code Quality Standards](#code-quality-standards)
2. [CI/CD Standards](#cicd-standards)
3. [Testing Standards](#testing-standards)
4. [Security Standards](#security-standards)
5. [Documentation Standards](#documentation-standards)
6. [Git Workflow](#git-workflow)

---

## Code Quality Standards

### Python Code Standards

**All Python code MUST comply with:**

1. **PEP 8 Style Guide**
   - Line length: 120 characters maximum
   - Indentation: 4 spaces (no tabs)
   - Naming conventions: lowercase with underscores for functions/variables

2. **Type Hints (PEP 484)**
   - All function signatures MUST include type hints
   - Function return types REQUIRED
   - Complex types use `typing` module

   ```python
   from typing import Optional, List, Dict

   def process_data(items: List[str], config: Optional[Dict[str, any]]) -> bool:
       """Process data items with optional configuration."""
       return True
   ```

3. **Docstrings (PEP 257)**
   - All modules require module-level docstring
   - All classes require docstring explaining purpose
   - All public functions/methods require docstring
   - Use Google-style docstrings for clarity

   ```python
   def authenticate_user(email: str, password: str) -> Dict[str, any]:
       """Authenticate user and return session token.

       Args:
           email: User email address
           password: User password (will be hashed)

       Returns:
           Dictionary with 'token' and 'expires' keys

       Raises:
           ValueError: If credentials are invalid
           DatabaseError: If user lookup fails
       """
   ```

4. **Import Organization**
   - Standard library imports first
   - Third-party imports second
   - Local imports third
   - Blank line between groups

   ```python
   import os
   import sys
   from typing import List

   import flask
   import redis

   from shared.auth import validate_token
   from shared.db import get_db_connection
   ```

### Linting Tools

All Python code MUST pass these linters before commit:

| Tool | Purpose | Command |
|------|---------|---------|
| **flake8** | Style compliance | `flake8 proxy management shared` |
| **black** | Code formatting | `black --check proxy management shared` |
| **isort** | Import sorting | `isort --check-only proxy management shared` |
| **mypy** | Type checking | `mypy proxy management shared` |
| **bandit** | Security scanning | `bandit -r proxy management shared -ll` |

### Configuration Files

**Python project configuration** (if applicable):

```ini
# setup.cfg or pyproject.toml
[flake8]
max-line-length = 120
ignore = E501, W503
exclude = .git,__pycache__,venv

[tool:pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
```

---

## CI/CD Standards

### Workflow Requirements

**All build workflows MUST:**

1. **Include Path Filters**
   ```yaml
   on:
     push:
       paths:
         - 'service/**'
         - '.version'
         - '.github/workflows/workflow-name.yml'
   ```

2. **Generate Epoch64 Timestamp**
   ```yaml
   - name: Generate epoch64 timestamp
     id: timestamp
     run: |
       EPOCH64=$(date +%s)
       echo "epoch64=$EPOCH64" >> $GITHUB_OUTPUT
   ```

3. **Detect Version Changes**
   ```yaml
   - name: Check version file
     id: version
     run: |
       if [ -f .version ]; then
         VERSION=$(cat .version | tr -d '[:space:]')
         SEMVER=$(echo "$VERSION" | cut -d'.' -f1-3)
         echo "semver=$SEMVER" >> $GITHUB_OUTPUT

         if git diff --name-only HEAD^ HEAD 2>/dev/null | grep -q "^.version$"; then
           echo "changed=true" >> $GITHUB_OUTPUT
         else
           echo "changed=false" >> $GITHUB_OUTPUT
         fi
       fi
   ```

4. **Use Conditional Tagging**
   ```yaml
   - name: Extract metadata
     id: meta
     uses: docker/metadata-action@v5
     with:
       tags: |
         type=raw,value=alpha-${{ steps.timestamp.outputs.epoch64 }},enable=${{ github.ref != 'refs/heads/main' && steps.version.outputs.changed != 'true' }}
         type=raw,value=beta-${{ steps.timestamp.outputs.epoch64 }},enable=${{ github.ref == 'refs/heads/main' && steps.version.outputs.changed != 'true' }}
         type=raw,value=v${{ steps.version.outputs.semver }}-alpha,enable=${{ github.ref != 'refs/heads/main' && steps.version.outputs.changed == 'true' }}
         type=raw,value=v${{ steps.version.outputs.semver }}-beta,enable=${{ github.ref == 'refs/heads/main' && steps.version.outputs.changed == 'true' }}
   ```

5. **Include Security Scanning**
   - Python: bandit
   - Go: gosec
   - JavaScript/Node.js: npm audit
   - Container images: Trivy

6. **Fetch Full History**
   ```yaml
   - name: Checkout code
     uses: actions/checkout@v4
     with:
       fetch-depth: 0
   ```

### Build Naming Convention

Container images follow automatic naming based on branch and version changes:

| Scenario | Main Branch | Other Branches |
|----------|------------|-----------------|
| Regular build (no `.version` change) | `beta-<epoch64>` | `alpha-<epoch64>` |
| Version release (`.version` changed) | `vX.X.X-beta` | `vX.X.X-alpha` |
| Tagged release | `vX.X.X` + `latest` | N/A |

**Example Workflow**:
1. Push feature branch: Image tagged `alpha-1702312159`
2. Update `.version` to 1.2.0 on feature branch: Image tagged `v1.2.0-alpha`
3. Merge to main: Image tagged `beta-1702312189`
4. Update `.version` to 1.2.0 on main: Image tagged `v1.2.0-beta`
5. Create git tag `v1.2.0`: Image tagged `v1.2.0` + `latest`

### Workflow Documentation

**New workflows MUST be documented in `docs/WORKFLOWS.md` including:**

- Trigger events (branches, tags, paths)
- Job descriptions and dependencies
- Environment variables and secrets
- Security scanning integration
- Tagging strategy

---

## Testing Standards

### Unit Testing

**Minimum Coverage**: 80% code coverage required

**Python Testing**:
- Framework: pytest
- Async testing: pytest-asyncio
- Coverage: pytest-cov
- Command: `pytest tests/unit/ -v --cov=<module>`

**Test File Organization**:
```
tests/
├── unit/
│   ├── test_proxy/
│   │   ├── __init__.py
│   │   ├── test_routes.py
│   │   ├── test_auth.py
│   │   └── test_cache.py
│   ├── test_management/
│   │   ├── __init__.py
│   │   ├── test_admin.py
│   │   └── test_users.py
│   └── test_shared/
│       ├── __init__.py
│       └── test_utils.py
├── integration/
│   ├── test_proxy_integration.py
│   └── test_management_integration.py
└── e2e/
    └── test_workflows.py
```

**Test Requirements**:
- Isolated unit tests (no network, mocked dependencies)
- Descriptive test names following `test_<function>_<scenario>`
- Docstrings explaining test purpose
- Setup/teardown for test isolation
- Parametrized tests for multiple scenarios

```python
import pytest
from unittest.mock import Mock, patch

@pytest.fixture
def mock_db():
    """Mock database connection."""
    return Mock()

@pytest.mark.asyncio
async def test_authenticate_user_valid_credentials(mock_db):
    """Test successful user authentication with valid credentials."""
    # Test implementation
    pass

@pytest.mark.parametrize("password", ["short", "", None])
def test_authenticate_user_invalid_password(mock_db, password):
    """Test authentication fails with invalid passwords."""
    # Test implementation
    pass
```

### Integration Testing

**Purpose**: Test component interactions

**Requirements**:
- Docker Compose for test environment
- Real database (PostgreSQL)
- Real cache (Redis)
- Health checks before testing
- Cleanup after tests

### Test Execution in CI/CD

**Location**: `docker-build.yml` → `test` job

```yaml
- name: Run unit tests
  run: |
    python -m pytest tests/unit/ -v --cov=shared --cov-report=xml
  env:
    PYTHONPATH: ${{ github.workspace }}

- name: Run security check (bandit)
  run: |
    bandit -r proxy management shared -ll -f json -o bandit-results.json || true
```

---

## Security Standards

### Code Security

**Mandatory Security Scanning**:

1. **bandit (Python)**: Detect security issues
   ```bash
   bandit -r proxy management shared -ll
   ```

2. **Input Validation**: All user inputs MUST be validated
   ```python
   from wtforms.validators import InputRequired, Email, Length

   class LoginForm(FlaskForm):
       email = StringField(validators=[InputRequired(), Email()])
       password = PasswordField(validators=[InputRequired(), Length(min=12)])
   ```

3. **SQL Injection Prevention**: Use PyDAL for all database queries
   - Never construct SQL strings with user input
   - Use parameterized queries via PyDAL
   ```python
   # Good: Using PyDAL
   users = db(db.users.email == user_email).select()

   # Bad: String concatenation (NEVER DO THIS)
   # query = f"SELECT * FROM users WHERE email = '{user_email}'"
   ```

4. **Authentication**: All protected endpoints MUST require authentication
   ```python
   from flask_security import auth_required

   @app.route('/api/protected', methods=['GET'])
   @auth_required()
   def protected_endpoint():
       return {'message': 'Protected content'}
   ```

5. **TLS**: All HTTPS connections MUST use TLS 1.2 minimum
   - Production: TLS 1.3 preferred
   - Configuration via environment variables

6. **Secrets Management**:
   - Never hardcode credentials
   - Use environment variables for all secrets
   - Use GitHub Secrets for CI/CD
   - No secrets in logs

### Dependency Security

**Vulnerability Scanning**:

| Tool | Purpose | Frequency |
|------|---------|-----------|
| **npm audit** | Node.js dependencies | Every build |
| **bandit** | Python security issues | Every build |
| **Trivy** | Container image scanning | After build |
| **Dependabot** | Dependency updates | Weekly |

### Container Security

**Image Standards**:
- Base image: debian-slim (never alpine)
- Non-root user required
- Read-only filesystem where possible
- Minimal attack surface
- Signed images (if applicable)

**Trivy Scanning**:
```yaml
- name: Run Trivy vulnerability scanner
  uses: aquasecurity/trivy-action@master
  with:
    image-ref: ghcr.io/penguintechinc/waddleai/proxy:latest
    format: 'sarif'
    output: 'trivy-results.sarif'
```

---

## Documentation Standards

### Code Documentation

1. **Module Docstrings**: Describe module purpose
   ```python
   """Authentication module for user login and session management.

   This module provides:
   - User authentication and validation
   - Session token generation
   - Token refresh and validation
   """
   ```

2. **Class Docstrings**: Explain class purpose and usage
   ```python
   class User(db.Model):
       """User model for authentication and profile management.

       Attributes:
           id: Unique user identifier
           email: User email address (unique)
           password: Hashed password
           active: Whether user account is active
       """
   ```

3. **Function/Method Docstrings**: Google-style format
   ```python
   def generate_session_token(user_id: int, expiry_hours: int = 24) -> str:
       """Generate JWT session token for user.

       Args:
           user_id: User database ID
           expiry_hours: Token expiration time in hours (default: 24)

       Returns:
           JWT token string

       Raises:
           ValueError: If user_id is invalid
           TokenGenerationError: If JWT generation fails

       Example:
           >>> token = generate_session_token(123, expiry_hours=1)
           >>> len(token) > 50
           True
       """
   ```

### Project Documentation

**Required Documentation Files**:

| File | Purpose |
|------|---------|
| `README.md` | Project overview and quick start |
| `docs/WORKFLOWS.md` | CI/CD workflow documentation |
| `docs/STANDARDS.md` | Development standards (this file) |
| `docs/ARCHITECTURE.md` | System architecture and design |
| `docs/API.md` | API endpoints and usage |
| `CHANGELOG.md` | Version release notes |

### README.md Structure

```markdown
# WaddleAI

Brief project description

## Quick Start

Installation and basic usage

## Services

- **Proxy**: Description
- **Management**: Description

## Development

Setup and running locally

## Documentation

Links to detailed docs

## License

License information
```

---

## Git Workflow

### Branch Strategy

**Branch Naming**: `<type>/<description>`
- `feature/add-user-roles`
- `bugfix/fix-cache-expiry`
- `docs/add-api-documentation`
- `ci/update-workflows`

**Main Branches**:
- `main`: Production-ready code
- `develop`: Development branch (if using)

### Commit Guidelines

**Commit Message Format**:

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types**:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation
- `style`: Formatting (no code changes)
- `refactor`: Code refactoring
- `test`: Test additions/changes
- `ci`: CI/CD changes
- `chore`: Build, dependencies

**Example**:
```
feat(proxy): add request timeout configuration

- Add REQUEST_TIMEOUT environment variable
- Default timeout: 30 seconds
- Configurable per request via headers

Closes #123
```

### Before Committing

**Pre-Commit Checklist**:

1. **Code Quality**
   - [ ] `flake8 proxy management shared` passes
   - [ ] `black --check proxy management shared` passes
   - [ ] `isort --check-only proxy management shared` passes
   - [ ] `mypy proxy management shared` passes

2. **Security**
   - [ ] `bandit -r proxy management shared -ll` passes
   - [ ] No hardcoded credentials or secrets
   - [ ] No debug logging of sensitive data

3. **Testing**
   - [ ] `pytest tests/unit/ -v` passes
   - [ ] Coverage >= 80%
   - [ ] New tests for new features
   - [ ] Integration tests for API changes

4. **Documentation**
   - [ ] Updated docstrings
   - [ ] Updated README.md if needed
   - [ ] API documentation updated

5. **Version Management**
   - [ ] Update `.version` if releasing
   - [ ] Update `CHANGELOG.md` for version changes

### Pull Request Process

1. Create feature branch from `main`
2. Make changes following all standards
3. Pass all pre-commit checks locally
4. Push to remote and create PR
5. Wait for GitHub Actions to pass
6. Request code review
7. Address review feedback
8. Merge after approval

**PR Template**:
```markdown
## Description
Brief description of changes

## Related Issues
Closes #123

## Changes Made
- Change 1
- Change 2
- Change 3

## Testing
- [ ] Unit tests added/updated
- [ ] Integration tests passed
- [ ] Manual testing completed

## Security
- [ ] No secrets committed
- [ ] Security scanning passed
- [ ] Dependencies updated if needed
```

---

## Version Management

### .version File

**Format**: Semantic versioning
```
MAJOR.MINOR.PATCH
```

**Example**: `1.2.3`

**Update Process**:
1. Edit `.version` file
2. Commit: `git add .version && git commit -m "Release v1.2.3"`
3. Push: `git push origin main`
4. docker-build tags images automatically
5. version-release creates GitHub pre-release

### Changelog Management

**File**: `CHANGELOG.md`

**Format**:
```markdown
# Changelog

## [1.2.3] - 2023-12-11

### Added
- New request timeout configuration
- User role management endpoint

### Fixed
- Cache expiration bug in proxy service

### Changed
- Updated authentication flow

### Security
- Fixed SQL injection vulnerability in user search

## [1.2.2] - 2023-12-01
...
```

---

## Environment Configuration

### Required Environment Variables

```bash
# Database
DB_TYPE=postgres
DB_HOST=localhost
DB_PORT=5432
DB_NAME=waddleai
DB_USER=waddleai
DB_PASS=<secure-password>

# Redis Cache
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0

# Flask
FLASK_ENV=development
SECRET_KEY=<secure-key>

# Proxy Service
PROXY_PORT=8000
LOG_LEVEL=INFO

# Management Service
MANAGEMENT_PORT=8001
```

### Development vs Production

**Development** (`.env.dev`):
- Debug mode enabled
- Verbose logging
- Mocked external services
- Local database/cache

**Production**:
- Debug mode disabled
- Structured logging
- Real external services
- Managed database/cache

---

## Monitoring and Observability

### Logging Standards

**Log Levels**:
- **DEBUG**: Detailed diagnostic information
- **INFO**: General informational messages
- **WARNING**: Warning messages for potential issues
- **ERROR**: Error messages for failures
- **CRITICAL**: Critical errors requiring immediate attention

**Log Format** (structured):
```python
import logging

logger = logging.getLogger(__name__)

# Good: Structured logging with context
logger.info("User authenticated", extra={
    "user_id": user.id,
    "email": user.email,
    "timestamp": datetime.utcnow().isoformat()
})

# Bad: String formatting
# logger.info(f"User {user.id} authenticated at {datetime.utcnow()}")
```

### Health Checks

**Proxy Service** (`/health`):
```json
{
  "status": "healthy",
  "service": "proxy",
  "version": "1.2.3",
  "timestamp": "2023-12-11T14:30:00Z"
}
```

**Management Service** (`/health`):
```json
{
  "status": "healthy",
  "service": "management",
  "version": "1.2.3",
  "timestamp": "2023-12-11T14:30:00Z"
}
```

### Metrics Endpoints

Both services expose Prometheus metrics at `/metrics`

```
# HELP http_requests_total Total HTTP requests
# TYPE http_requests_total counter
http_requests_total{method="GET",endpoint="/api/v1/users"} 1234

# HELP http_request_duration_seconds HTTP request duration
# TYPE http_request_duration_seconds histogram
http_request_duration_seconds_bucket{le="1.0"} 500
```

---

## Related Documentation

- [WaddleAI Workflows](WORKFLOWS.md)
- [Network Architecture](NETWORK-ARCHITECTURE.md)
- [API Documentation](API.md)
- [Project Template Standards](../../project-template/docs/STANDARDS.md)

---

**Last Updated**: 2025-12-11
**Version**: 1.0.0
**Maintained by**: WaddleAI Development Team
