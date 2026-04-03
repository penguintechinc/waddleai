# PenguinCode Security Documentation

## Overview

PenguinCode implements a comprehensive security model for local and remote execution scenarios. This document covers authentication, authorization, encryption, and best practices for safe code generation and deployment.

## Table of Contents

- [Security Levels](#security-levels)
- [JWT Authentication](#jwt-authentication-remote-server-mode)
- [API Key Management](#api-key-management)
- [TLS Configuration](#tls-configuration)
- [Local Tool Execution Model](#local-tool-execution-model)
- [Code Generation Safety](#safe-code-generation-guidelines)
- [OWASP Top 10 Compliance](#owasp-top-10-compliance)
- [Production Deployment](#production-deployment-best-practices)

---

## Security Levels

PenguinCode supports three security levels configured in `config.yaml`:

| Level | Name | Behavior | Use Case |
|-------|------|----------|----------|
| **1** | Always Prompt | Prompts for confirmation on ALL operations | Development/Testing |
| **2** | Destructive Prompt | Prompts only for destructive operations (delete, modify, execute) | Default Production |
| **3** | No Prompts | Automatic approval of all operations | High-Trust, Automated |

```yaml
security:
  level: 2  # Recommended default
```

**Destructive Operations** include: file deletion, code execution, database modifications, configuration changes, and external network calls.

---

## JWT Authentication (Remote Server Mode)

PenguinCode uses JWT (JSON Web Tokens) for client-server authentication when running in remote or standalone modes.

### Authentication Flow

1. **Client authenticates** with API key → Server validates against whitelist
2. **Server generates** access token (1 hour expiry) + refresh token (24 hours)
3. **Client stores** tokens securely at `~/.penguincode/token` (mode 0600)
4. **Interceptor validates** JWT on each request via `JWTValidationInterceptor`
5. **Automatic refresh** when access token expires

### Token Structure

**Access Token (HS256 signed)**:
```json
{
  "sub": "client_id",
  "iat": 1704067200,
  "exp": 1704070800,
  "scopes": ["chat", "tools"],
  "type": "access"
}
```

**Refresh Token**: Cryptographically random 32-byte URL-safe string, single-use with server-side tracking.

### Configuration

```yaml
auth:
  enabled: false                    # Set to true for remote mode
  jwt_secret: "${PENGUINCODE_JWT_SECRET}"
  token_expiry: 3600                # 1 hour
  refresh_expiry: 86400             # 24 hours
  api_keys:
    - "${PENGUINCODE_API_KEY}"

client:
  server_url: "grpc://server:50051"
  token_path: "~/.penguincode/token"
```

**Environment Variables**:
- `PENGUINCODE_JWT_SECRET`: Min 32 bytes (use `openssl rand -hex 32`)
- `PENGUINCODE_API_KEY`: Complex string, min 32 characters

---

## API Key Management

API keys provide initial authentication to obtain JWT tokens.

### Best Practices

1. **Generate Strong Keys**: Min 32 chars, alphanumeric + special
2. **Environment Variables**: Store in `.env` or secret manager, never in code
3. **Rotation**: Change quarterly or after exposure
4. **Validation**: Server-side whitelist comparison (timing-safe)
5. **Scope Limiting**: Future feature for granular permissions

**Generation**:
```bash
openssl rand -base64 32  # API key
openssl rand -hex 32     # JWT secret
```

---

## TLS Configuration

TLS protects gRPC traffic from eavesdropping and MITM attacks.

### Enabling TLS

```yaml
server:
  tls_enabled: true
  tls_cert_path: "/etc/penguincode/certs/server.crt"
  tls_key_path: "/etc/penguincode/certs/server.key"

client:
  server_url: "grpcs://server:50051"  # grpcs for TLS
```

### Certificate Setup (Self-Signed Development)

```bash
openssl genrsa -out server.key 2048
openssl req -new -key server.key -out server.csr
openssl x509 -req -days 365 -in server.csr -signkey server.key -out server.crt
```

**Production**: Use CA-signed certificates, TLS 1.3, enable OCSP stapling, rotate every 90 days.

---

## Local Tool Execution Model

PenguinCode uses **security by architecture**: sensitive tools execute locally on the client.

### Why Local Tools?

**Local Tools** (`config.yaml`):
- `read`: File reading (no risk)
- `write`: File writing (user controls target)
- `edit`: File editing (granular, auditable)
- `bash`: Shell execution (direct system access)
- `grep`/`glob`: File search (info gathering only)

**Security Benefits**:
1. **User Control**: Operations directly affect user's filesystem
2. **Data Privacy**: Server cannot read private files
3. **Malicious Server Protection**: Even if compromised, cannot execute arbitrary code
4. **Audit Trail**: All commands in shell history

---

## Safe Code Generation Guidelines

### Pre-Execution Checks

1. **Type Checking**: mypy (Python), vet (Go)
2. **Linting**: flake8, ESLint, golangci-lint
3. **Security Scanning**: Trivy, CodeQL, bandit
4. **Manual Review**: Always review AI-generated code
5. **Containerization**: Isolated Docker execution

### Generated Code Standards

```python
# ✅ DO: Validate inputs
def process_user_input(data: str) -> dict:
    if not isinstance(data, str) or len(data) > 10000:
        raise ValueError("Invalid input")
    return parse_input(data)

# ✅ DO: Use parameterized queries
result = db.query("SELECT * FROM users WHERE id = ?", [user_id])

# ✅ DO: Explicit error handling
try:
    result = risky_operation()
except SpecificException as e:
    logger.error(f"Operation failed: {e}")
    raise
```

---

## OWASP Top 10 Compliance

The Executor agent follows OWASP Top 10 (2021) guidelines when generating code:

### A01: Broken Access Control

**What we do:**
- Generate proper authorization checks before sensitive operations
- Implement deny-by-default access patterns
- Use indirect object references (UUIDs) instead of sequential IDs
- Server-side permission validation

**Example - FastAPI:**
```python
from fastapi import Depends, HTTPException, status
from uuid import UUID

async def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    user = await verify_token(token)
    if not user:
        raise HTTPException(status_code=401)
    return user

@app.get("/users/{user_id}")
async def get_user(
    user_id: UUID,  # UUID, not int
    current_user: User = Depends(get_current_user)
):
    if not current_user.can_view_user(user_id):
        raise HTTPException(status_code=403)
    return await get_user_by_id(user_id)
```

### A02: Cryptographic Failures

**What we do:**
- Never hardcode secrets in generated code
- Use environment variables or secret managers
- Modern encryption algorithms (AES-256, bcrypt/argon2)
- Proper password hashing with salts

**Example - Password hashing:**
```python
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)
```

### A03: Injection

**What we do:**
- Parameterized queries for all database operations
- Input validation and sanitization
- Context-aware output escaping
- No shell command construction with user input

**Example - SQL (SQLAlchemy):**
```python
# GOOD: Parameterized query
user = session.execute(
    select(User).where(User.email == email)
).scalar_one_or_none()

# BAD: String concatenation (never generated)
# query = f"SELECT * FROM users WHERE email = '{email}'"
```

### A04: Insecure Design

**What we do:**
- Rate limiting on authentication endpoints
- CSRF protection for state-changing operations
- Secure session management
- Principle of least privilege

### A05: Security Misconfiguration

**What we do:**
- No debug info in production responses
- Secure HTTP headers by default
- Minimal service exposure

**Example - FastAPI security headers:**
```python
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.sessions import SessionMiddleware

app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*.example.com"])
app.add_middleware(SessionMiddleware, secret_key=os.environ["SESSION_SECRET"])

@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response
```

### A06: Vulnerable Components

**What we do:**
- Recommend well-maintained libraries
- Suggest version pinning
- Warn about known vulnerabilities

### A07: Authentication Failures

**What we do:**
- Strong password policies
- Account lockout mechanisms
- Secure session tokens
- Proper token expiration

### A08: Data Integrity Failures

**What we do:**
- Validate all external data
- Suggest integrity checks

### A09: Logging & Monitoring

**What we do:**
- Log security events
- Never log sensitive data
- Structured logging with context

**Example:**
```python
import structlog

logger = structlog.get_logger()

async def login(email: str, password: str):
    user = await get_user(email)
    if not user or not verify_password(password, user.password_hash):
        logger.warning("login_failed", email=email)  # Log email, not password
        raise HTTPException(status_code=401)

    logger.info("login_success", user_id=str(user.id))
    return create_token(user)
```

### A10: SSRF

**What we do:**
- URL validation before requests
- Domain allowlists
- Block internal IP ranges

---

## Planned Security Features

### Prompt Injection Protection

Future releases will include:
- Input sanitization (special tokens/delimiters)
- Instruction separation (system/user boundary)
- Template validation (prompt structure)
- Pattern detection (manipulation attempts)

**Current Mitigation**: Run code in isolated containers, review before execution.

### Audit Logging

Planned comprehensive audit trail:
- Request logging (user, method, parameters)
- Authorization events (role changes, permissions)
- Code execution (generated code, results)
- Authentication events (logins, token refresh)
- Data access (file I/O, database queries)
- System changes (configuration, secrets)

```json
{
  "timestamp": "2024-01-15T10:30:45Z",
  "user_id": "client_abc123",
  "event": "code_execution",
  "action": "bash",
  "parameters": {"command": "ls -la"},
  "result": "success"
}
```

---

## Production Deployment Best Practices

### Pre-Deployment Checklist

- [ ] Environment variables set (JWT secret, API keys)
- [ ] TLS certificates installed and tested
- [ ] JWT secret: min 32 bytes from cryptographic RNG
- [ ] API keys: complex, unique per client
- [ ] Security level: 2 recommended
- [ ] Firewall: only expose gRPC port
- [ ] Network: isolated on private network
- [ ] Scanning: dependencies/containers (Trivy)
- [ ] Code review: all custom code reviewed
- [ ] Monitoring: Prometheus metrics enabled
- [ ] Alerting: failed auth, security events

### Network Security

```bash
# Firewall rules (Linux iptables)
iptables -A INPUT -p tcp --dport 50051 -j ACCEPT  # gRPC

# Source IP whitelist
iptables -A INPUT -p tcp --dport 50051 -s 192.168.1.0/24 -j ACCEPT
```

### Secrets Management

```bash
# Generate secrets
export PENGUINCODE_JWT_SECRET=$(openssl rand -hex 32)
export PENGUINCODE_API_KEY=$(openssl rand -base64 32)

# Use Kubernetes Secrets
kubectl create secret generic penguincode \
  --from-literal=jwt-secret=<value> \
  --from-literal=api-key=<value>
```

### Incident Response

1. **Detect**: Monitor auth failures, unusual patterns
2. **Isolate**: Disconnect compromised client/server
3. **Investigate**: Check audit logs, review access
4. **Revoke**: Invalidate tokens, rotate secrets
5. **Patch**: Update vulnerabilities
6. **Notify**: Alert users/admins
7. **Review**: Post-incident analysis

### Key Rotation

```bash
# JWT secret rotation (invalidates all tokens immediately)
NEW_SECRET=$(openssl rand -hex 32)
# Update server config, restart with new secret

# API key rotation (grace period)
# 1. Add new key to whitelist
# 2. Update clients
# 3. Remove old key after grace period
```

---

## Language-Specific Security

### Python

| Risk | Mitigation |
|------|------------|
| Insecure random | Use `secrets` module, not `random` |
| Command injection | Use `subprocess` with `shell=False` and list args |
| Code injection | Avoid `eval()`, `exec()`, `pickle.loads()` |
| XSS | Use `html.escape()` for HTML output |

### JavaScript/TypeScript

| Risk | Mitigation |
|------|------------|
| XSS | Use `textContent` instead of `innerHTML` |
| Input validation | Use Zod, Yup, or similar |
| URL injection | Use `encodeURIComponent()` |
| Cookie theft | Set `httpOnly` and `secure` flags |

### Go

| Risk | Mitigation |
|------|------------|
| SQL injection | Use `database/sql` with `?` placeholders |
| Command injection | Use `exec.Command()` with separate args |
| Path traversal | Use `filepath.Clean()` and validate paths |

### Rust

| Risk | Mitigation |
|------|------------|
| Memory safety | Leverage ownership system (automatic) |
| SQL injection | Use `sqlx` or `diesel` with parameters |
| Untrusted input | Use `serde` with validation |

---

## Infrastructure as Code Security

### OpenTofu/Terraform

**Secrets Management:**
```hcl
# GOOD: Use variables with sensitive flag
variable "db_password" {
  type      = string
  sensitive = true
}

resource "aws_db_instance" "main" {
  password = var.db_password
}

# BAD: Hardcoded (never generated)
# password = "supersecret123"
```

**State Security:**
```hcl
# Use encrypted backend
terraform {
  backend "s3" {
    bucket         = "my-terraform-state"
    key            = "prod/terraform.tfstate"
    region         = "us-west-2"
    encrypt        = true
    dynamodb_table = "terraform-locks"
  }
}
```

**Resource Security:**
```hcl
# Enable encryption by default
resource "aws_s3_bucket" "data" {
  bucket = "my-secure-bucket"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Use private subnets
resource "aws_instance" "app" {
  subnet_id                   = aws_subnet.private.id
  associate_public_ip_address = false
}
```

### Ansible

**Vault Usage:**
```yaml
# Create encrypted variables
# ansible-vault create secrets.yml

# Reference in playbook
- name: Deploy application
  hosts: webservers
  vars_files:
    - secrets.yml
  tasks:
    - name: Configure database
      template:
        src: db.conf.j2
        dest: /etc/app/db.conf
      no_log: true  # Don't log secrets
```

**Privilege Escalation:**
```yaml
# Use become only when necessary
- name: Install packages
  become: true
  ansible.builtin.apt:
    name: nginx
    state: present

- name: Copy config (no root needed)
  become: false
  ansible.builtin.copy:
    src: nginx.conf
    dest: /etc/nginx/nginx.conf
```

---

## Security Configuration

### Enable Security Scanning

```yaml
# config.yaml
security:
  enabled: true
  scan_on_write: true      # Scan generated code
  block_insecure: false    # Warn but don't block

  rules:
    - no_hardcoded_secrets
    - no_sql_injection
    - no_command_injection
    - use_parameterized_queries
    - use_secure_random
```

### Custom Rules

```yaml
security:
  custom_rules:
    - pattern: "password\\s*=\\s*['\"][^'\"]+['\"]"
      message: "Hardcoded password detected"
      severity: high

    - pattern: "eval\\(.*\\$"
      message: "Eval with variable input"
      severity: critical
```

---

## Reporting Vulnerabilities

If you discover a security vulnerability in PenguinCode:

1. **Do not** open a public issue
2. Email security@penguintech.io
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

We will respond within 48 hours and work with you on responsible disclosure.

---

## Compliance & Standards

PenguinCode follows:
- **OWASP Top 10 2021**: Mitigations for all A1-A10 risks
- **NIST Cybersecurity Framework**: Identify, Protect, Detect, Respond, Recover
- **CWE Top 25**: Common weakness enumeration prevention
- **OWASP API Security**: API-first design, authentication/authorization

## References

- [OWASP Top 10 2021](https://owasp.org/Top10/)
- [JWT Best Practices (RFC 8725)](https://tools.ietf.org/html/rfc8725)
- [gRPC Security](https://grpc.io/docs/guides/auth/)
- [NIST Cybersecurity Framework](https://www.nist.gov/cyberframework)
- [CWE/SANS Top 25](https://cwe.mitre.org/top25/)

## Reporting Vulnerabilities

If you discover a security vulnerability in PenguinCode:

1. **Do not** open a public issue
2. Email security@penguintech.io
3. Include: description, reproduction steps, impact, suggested fix

We will respond within 48 hours and work on responsible disclosure.

---

**Last Updated**: 2025-12-28
**See Also**: [AGENTS.md](AGENTS.md), [STANDARDS.md](STANDARDS.md), [LICENSE_SERVER_INTEGRATION.md](licensing/license-server-integration.md)
