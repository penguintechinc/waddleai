# Development Standards

This document consolidates all development standards, patterns, and requirements for the WaddleAI project, extending the gold standard template from Penguin Tech Inc.

## Table of Contents

1. [Code Quality Standards](#code-quality-standards)
2. [Language Selection](#language-selection)
3. [Python Development Standards](#python-development-standards)
4. [Flask-Security-Too Integration](#flask-security-too-integration)
5. [Database Standards](#database-standards)
6. [API Versioning](#api-versioning)
7. [Protocol Support](#protocol-support)
8. [Testing Requirements](#testing-requirements)
9. [Security Standards](#security-standards)
10. [CI/CD Standards](#cicd-standards)
11. [Documentation Standards](#documentation-standards)
12. [Git Workflow](#git-workflow)
13. [WaddleAI-Specific Standards](#waddleai-specific-standards)

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

4. **Dataclasses with Slots (MANDATORY)**

   ```python
   from dataclasses import dataclass, field

   @dataclass(slots=True, frozen=True)
   class User:
       """User model with slots for 30-50% memory reduction."""
       id: int
       email: str
       name: str
       metadata: dict = field(default_factory=dict)
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

---

## Language Selection

### Python 3.13 (Default Choice)

**Use Python for all WaddleAI applications:**
- Web services (Flask-based)
- Background workers (Celery)
- API servers and proxies
- Data processing and analytics
- Management interfaces

**Advantages:**
- Rapid development and iteration
- Rich ecosystem of libraries
- Strong support for async/await
- Excellent for network services

---

## Python Development Standards

### Concurrency Patterns

**asyncio** - For I/O-bound operations:
- Database queries and connections
- HTTP/REST API calls
- File I/O operations
- Network communication
- Best for operations that wait on external resources

**threading.Thread** - For I/O-bound operations with blocking libraries:
- Legacy libraries without async support
- Blocking I/O operations
- Moderate parallelism (10-100 threads)
- Use ThreadPoolExecutor for managed thread pools

**multiprocessing** - For CPU-bound operations:
- Data processing and transformations
- Cryptographic operations
- Heavy computational tasks
- Bypasses GIL for true parallelism

---

## Flask-Security-Too Integration

**MANDATORY for ALL Flask applications - provides comprehensive security framework**

### Core Features
- User authentication and session management
- Role-based access control (RBAC)
- Password hashing with bcrypt
- Email confirmation and password reset
- Two-factor authentication (2FA)
- Token-based authentication for APIs
- Login tracking and session management

### Integration with PyDAL

```python
from flask import Flask
from flask_security import Security, auth_required, hash_password
from pydal import DAL, Field
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'super-secret')
app.config['SECURITY_PASSWORD_SALT'] = os.getenv('SECURITY_PASSWORD_SALT', 'salt')
app.config['SECURITY_REGISTERABLE'] = True
app.config['SECURITY_PASSWORD_HASH'] = 'bcrypt'

# PyDAL database setup
db = DAL(
    f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASS')}@"
    f"{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}",
    pool_size=10,
    migrate=True
)

# Define user and role tables
db.define_table('auth_user',
    Field('email', 'string', requires=IS_EMAIL(), unique=True),
    Field('password', 'string'),
    Field('active', 'boolean', default=True),
    Field('fs_uniquifier', 'string', unique=True),
    migrate=True
)

db.define_table('auth_role',
    Field('name', 'string', unique=True),
    Field('description', 'text'),
    migrate=True
)

# Initialize Flask-Security-Too
from flask_security import PyDALUserDatastore
user_datastore = PyDALUserDatastore(db, db.auth_user, db.auth_role)
security = Security(app, user_datastore)

# Protected route example
@app.route('/api/protected')
@auth_required()
def protected_endpoint():
    return {'message': 'Access granted'}
```

### WaddleAI Roles

**WaddleAI implements the following roles:**

| Role | Permissions |
|------|-------------|
| **Admin** | Full system access, user management, configuration |
| **Resource Manager** | Organization quota management, user management |
| **Reporter** | Read-only analytics and reporting |
| **User** | API access only, personal usage statistics |

---

## Database Standards

### Hybrid Approach (SQLAlchemy + PyDAL)

**SQLAlchemy**: Database initialization only
- Used for initial schema creation
- Runs once at startup
- Handles all supported databases reliably

**PyDAL**: Day-to-day operations and migrations (MANDATORY)
- All CRUD operations
- All queries and data access
- Schema migrations
- Thread-safe connection pooling

### PyDAL Configuration

```python
from pydal import DAL, Field

def get_db_connection():
    """Initialize PyDAL database connection with pooling."""
    db_type = os.getenv('DB_TYPE', 'postgresql')
    db_uri = f"{db_type}://{os.getenv('DB_USER')}:{os.getenv('DB_PASS')}@" \
             f"{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"

    db = DAL(
        db_uri,
        pool_size=int(os.getenv('DB_POOL_SIZE', '10')),
        migrate_enabled=True,
        check_reserved=['all'],
        lazy_tables=True
    )

    return db
```

### Supported Databases

- **postgres** - PostgreSQL (recommended)
- **mysql** - MySQL/MariaDB
- **sqlite** - SQLite (development only)

### Thread Safety Requirements

**PyDAL MUST be used in a thread-safe manner:**

```python
import threading
from pydal import DAL

thread_local = threading.local()

def get_thread_db():
    """Get thread-local database connection."""
    if not hasattr(thread_local, 'db'):
        thread_local.db = DAL(db_uri, pool_size=10, migrate_enabled=True)
    return thread_local.db
```

---

## API Versioning

**ALL REST APIs MUST use versioning in the URL path**

### URL Structure

**Required Format:** `/api/v{major}/endpoint`

**Examples:**
- `/api/v1/chat/completions` - Chat completion
- `/api/v1/embeddings` - Embeddings
- `/api/v1/users` - User management
- `/api/v2/analytics` - Version 2 of analytics

**Key Rules:**
1. Always include version prefix in URL path
2. Semantic versioning: v1, v2, v3, etc.
3. Major version only in URL
4. Consistent prefix across all endpoints

### WaddleAI API Standards

**Proxy Service Endpoints:**
```python
@app.route('/v1/chat/completions', methods=['POST'])
@auth_required()
def chat_completions():
    """OpenAI-compatible chat completions endpoint."""
    pass

@app.route('/v1/embeddings', methods=['POST'])
@auth_required()
def embeddings():
    """OpenAI-compatible embeddings endpoint."""
    pass
```

**Management Service Endpoints:**
```python
@app.route('/api/v1/users', methods=['GET'])
@auth_required()
@roles_required('admin', 'resource_manager')
def list_users():
    """List users (admin/resource_manager only)."""
    pass

@app.route('/api/v1/analytics/tokens', methods=['GET'])
@auth_required()
def get_token_usage():
    """Get token usage analytics."""
    pass
```

---

## Protocol Support

**All WaddleAI services MUST support:**

1. **REST API**: RESTful HTTP endpoints (GET, POST, PUT, DELETE)
   - JSON request/response format
   - Proper HTTP status codes
   - OpenAI-compatible format

2. **gRPC**: High-performance RPC protocol (optional for future)
   - Protocol Buffers for serialization
   - Health checking via gRPC health protocol

3. **HTTP/1.1**: Standard HTTP protocol
   - Keep-alive connections
   - Chunked transfer encoding
   - Compression (gzip)

4. **HTTP/2**: Modern HTTP protocol
   - Multiplexing multiple requests
   - Header compression

### Configuration via Environment Variables

```bash
HTTP1_ENABLED=true
HTTP2_ENABLED=true
HTTP_PORT=8000
```

---

## Testing Requirements

### Unit Testing

**Minimum Coverage**: 80% code coverage required

**Framework**: pytest

**Requirements**:
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
async def test_proxy_request_valid_token(mock_db):
    """Test proxy request with valid authentication token."""
    # Test implementation
    pass

@pytest.mark.parametrize("input_data", [
    {"model": "gpt-4", "messages": []},
    {"model": "gpt-3.5", "messages": []},
])
def test_chat_completion_models(input_data):
    """Test chat completions with different models."""
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

### E2E Testing

**Scope**:
- Full workflow testing
- API endpoint validation
- User flow verification

---

## Security Standards

### Input Validation

- ALL inputs MUST have appropriate validators
- Use Flask validators or shared validation libraries
- Implement XSS and SQL injection prevention
- Server-side validation for all client input

```python
from wtforms.validators import InputRequired, Email, Length

class LoginForm(FlaskForm):
    email = StringField(validators=[InputRequired(), Email()])
    password = PasswordField(validators=[InputRequired(), Length(min=12)])
```

### SQL Injection Prevention

**Use PyDAL for all database queries:**

```python
# Good: Using PyDAL
users = db(db.users.email == user_email).select()

# Bad: String concatenation (NEVER DO THIS)
# query = f"SELECT * FROM users WHERE email = '{user_email}'"
```

### Authentication & Authorization

- Multi-factor authentication support
- Role-based access control (RBAC)
- API key management with rotation
- JWT token validation with proper expiration
- Session management with secure cookies

### TLS/Encryption

- **TLS enforcement**: TLS 1.2 minimum, prefer TLS 1.3
- **Connection security**: Use HTTPS where possible
- **JWT**: For API authentication
- **MFA**: Multi-factor authentication standard

### Dependency Security

- **ALWAYS check for Dependabot alerts** before commits
- **Monitor vulnerabilities via Socket.dev**
- **Mandatory security scanning** before dependency changes
- **Fix all security alerts immediately**
- **Regular security audits**: `pip audit`, `bandit`

---

## CI/CD Standards

### Build Naming Convention

Container images follow automatic naming based on branch and version:

| Scenario | Main Branch | Other Branches |
|----------|------------|-----------------|
| Regular build | `beta-<epoch64>` | `alpha-<epoch64>` |
| Version release | `vX.X.X-beta` | `vX.X.X-alpha` |
| Tagged release | `vX.X.X` + `latest` | N/A |

### Path Filter Requirements

**EVERY build workflow MUST include `.version` in its path filter:**

```yaml
on:
  push:
    branches: [main, develop]
    paths:
      - '.version'                              # MANDATORY
      - 'proxy/**'                              # Service path
      - 'management/**'                         # Service path
      - '.github/workflows/docker-build.yml'    # Workflow
```

### Security Scanning

**Mandatory for all builds:**

1. **bandit** (Python security)
   ```bash
   bandit -r proxy management shared -ll
   ```

2. **Trivy** (Container scanning)
   ```yaml
   - uses: aquasecurity/trivy-action@master
     with:
       image-ref: ${{ image }}
       severity: 'HIGH,CRITICAL'
   ```

3. **CodeQL** (Code analysis)
   ```yaml
   - uses: github/codeql-action/init@v2
   - uses: github/codeql-action/analyze@v2
   ```

---

## Documentation Standards

### Code Documentation

1. **Module Docstrings**
   ```python
   """Authentication module for user login and session management.

   This module provides user authentication, token generation, and validation.
   """
   ```

2. **Class Docstrings**
   ```python
   class WaddleAIProxy:
       """OpenAI-compatible API proxy for WaddleAI.

       Handles request routing, authentication, and response translation.
       """
   ```

3. **Function/Method Docstrings**
   ```python
   def validate_request(request_data: Dict) -> bool:
       """Validate incoming API request.

       Args:
           request_data: Request payload dictionary

       Returns:
           True if valid, False otherwise

       Raises:
           ValueError: If required fields missing
       """
   ```

### Project Documentation

**Required Documentation Files:**

| File | Purpose |
|------|---------|
| `README.md` | Project overview and quick start |
| `CLAUDE.md` | Claude Code context (39K max) |
| `docs/STANDARDS.md` | Development standards (this file) |
| `docs/WORKFLOWS.md` | CI/CD pipeline documentation |
| `docs/ARCHITECTURE.md` | System architecture and design |
| `docs/API.md` | API endpoints and usage |
| `RELEASE_NOTES.md` | Version release notes |
| `NETWORK-ARCHITECTURE.md` | Network design and flow |

---

## Git Workflow

### Branch Strategy

**Branch Naming**: `<type>/<description>`
- `feature/add-model-routing`
- `bugfix/fix-rate-limiting`
- `docs/add-api-documentation`
- `ci/update-workflows`

### Commit Guidelines

**Commit Message Format:**
```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation
- `style`: Formatting (no code changes)
- `refactor`: Code refactoring
- `test`: Test additions/changes
- `ci`: CI/CD changes

**Example:**
```
feat(proxy): add request timeout configuration

- Add REQUEST_TIMEOUT environment variable
- Default timeout: 30 seconds
- Configurable per request via headers

Closes #123
```

### Before Committing

**Pre-Commit Checklist:**

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

4. **Documentation**
   - [ ] Updated docstrings
   - [ ] Updated README.md if needed
   - [ ] API documentation updated

5. **Version Management**
   - [ ] Update `.version` if releasing
   - [ ] Update `RELEASE_NOTES.md` for version changes

---

## WaddleAI-Specific Standards

### OpenAI Compatibility

All proxy endpoints MUST be OpenAI API compatible:

```python
# Request format matches OpenAI
{
    "model": "gpt-4",
    "messages": [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello!"}
    ],
    "max_tokens": 100
}

# Response format matches OpenAI
{
    "id": "chatcmpl-xxx",
    "object": "chat.completion",
    "created": 1234567890,
    "model": "gpt-4",
    "choices": [{
        "index": 0,
        "message": {
            "role": "assistant",
            "content": "Hello! How can I help?"
        }
    }],
    "usage": {
        "prompt_tokens": 20,
        "completion_tokens": 10,
        "total_tokens": 30
    }
}
```

### Token System

**WaddleAI maintains dual token counting:**

1. **LLM Tokens**: Raw provider token counts
   - `prompt_tokens`: Input tokens from provider
   - `completion_tokens`: Output tokens from provider
   - `total_tokens`: Sum of above

2. **WaddleAI Tokens**: Normalized billing units
   - Consistent across all LLM providers
   - Used for quota enforcement
   - Used for cost calculation

### Rate Limiting

**Proxy must implement rate limiting:**

```python
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="redis://localhost:6379"
)

@app.route('/v1/chat/completions', methods=['POST'])
@limiter.limit("10/minute")
@auth_required()
def chat_completions():
    pass
```

### Caching

**Redis caching for frequent requests:**

```python
import redis

cache = redis.Redis(
    host=os.getenv('REDIS_HOST', 'localhost'),
    port=int(os.getenv('REDIS_PORT', 6379)),
    db=int(os.getenv('REDIS_DB', 0))
)

# Cache embeddings (they rarely change)
cache_key = f"embedding:{model}:{text_hash}"
cached = cache.get(cache_key)
if cached:
    return json.loads(cached)
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

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0

# Flask
FLASK_ENV=development
SECRET_KEY=<secure-key>
SECURITY_PASSWORD_SALT=<secure-salt>

# Services
PROXY_PORT=8000
MANAGEMENT_PORT=8001
LOG_LEVEL=INFO

# License (optional, development mode)
LICENSE_KEY=PENG-XXXX-XXXX-XXXX-XXXX-ABCD
RELEASE_MODE=false
```

---

## Quality Checklist

Before marking any task complete, verify:
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

---

## Related Documentation

- [WaddleAI CLAUDE.md](../CLAUDE.md)
- [CI/CD Workflows](WORKFLOWS.md)
- [Network Architecture](../NETWORK-ARCHITECTURE.md)
- [API Reference](API.md)
- [Project Template Standards](../../project-template/docs/STANDARDS.md)

---

**Last Updated**: 2025-12-18
**Version**: 1.0.0
**Maintained by**: WaddleAI Development Team
