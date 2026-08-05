# WaddleAI - Claude Code Context

## Project Overview

WaddleAI is an AI proxy and management system that provides OpenAI-compatible APIs with advanced routing, security, and token management.

**Key Features:**
- OpenAI-compatible API proxy
- Advanced model routing
- Dual token system (WaddleAI tokens + LLM tokens)
- Role-based access control (Admin, Resource Manager, Reporter, User)
- Security scanning and threat detection
- Quota and usage management
- Prometheus metrics and monitoring

## Technology Stack

### Languages & Frameworks

**Python Stack:**
- **Python**: 3.13 for all applications (3.12+ minimum)
- **Web Framework**: Flask + Flask-Security-Too (mandatory)
- **Database ORM**: PyDAL (mandatory for all Python applications)
- **Performance**: Dataclasses with slots, type hints, async/await required

**Frontend Stack:**
- **React**: ReactJS for all frontend applications
- **Node.js**: 18+ for build tooling and React development

### Infrastructure & DevOps
- **Containers**: Docker with multi-stage builds, Docker Compose
- **CI/CD**: GitHub Actions
- **Monitoring**: Prometheus metrics, Grafana dashboards
- **Logging**: Structured logging with configurable levels

### Databases & Storage
- **Primary**: PostgreSQL (default)
- **Database Abstraction**: PyDAL for cross-database support

### Security & Authentication
- **Flask-Security-Too**: Role-based access control (RBAC)
- **JWT**: Token-based authentication
- **TLS**: Enforce TLS 1.2 minimum
- **Security Scanning**: Prompt injection detection, jailbreak prevention

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
- Skipping input validation "just this once"
- Hardcoding credentials or configuration
- Ignoring error returns or exceptions
- Commenting out failing tests to make CI pass
- Deploying without proper testing
- Using deprecated or unmaintained dependencies
- Implementing partial features with "TODO" placeholders
- Bypassing security checks for convenience
- Assuming data is valid without verification
- Leaving debug code or backdoors in production

#### Quality Checklist Before Completion
- All error cases handled properly
- Unit tests cover all code paths
- Integration tests verify component interactions
- Security requirements fully implemented
- Performance meets acceptable standards
- Documentation complete and accurate
- Code review standards met
- No hardcoded secrets or credentials
- Logging and monitoring in place
- Build passes in containerized environment
- No security vulnerabilities in dependencies
- Edge cases and boundary conditions tested

### Git Workflow
- **NEVER commit automatically** unless explicitly requested by the user
- **NEVER push to remote repositories** under any circumstances
- **ONLY commit when explicitly asked** - never assume commit permission
- Always use feature branches for development
- Require pull request reviews for main branch
- Automated testing must pass before merge

### Local State Management (Crash Recovery)
- **ALWAYS maintain local .PLAN and .TODO files** for crash recovery
- **Keep .PLAN file updated** with current implementation plans and progress
- **Keep .TODO file updated** with task lists and completion status
- **Update these files in real-time** as work progresses
- **Add to .gitignore**: Both .PLAN and .TODO files must be in .gitignore
- **File format**: Use simple text format for easy recovery
- **Automatic recovery**: Upon restart, check for existing files to resume work

### Dependency Security Requirements
- **ALWAYS check for Dependabot alerts** before every commit
- **Monitor vulnerabilities via Socket.dev** for all dependencies
- **Mandatory security scanning** before any dependency changes
- **Fix all security alerts immediately** - no commits with outstanding vulnerabilities
- **Regular security audits**: `npm audit`, `safety check`

### Linting & Code Quality Requirements
- **ALL code must pass linting** before commit - no exceptions
- **Python**: flake8, black, isort, mypy (type checking), bandit (security)
- **JavaScript/TypeScript**: ESLint, Prettier
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
- **License**: All projects use Limited AGPL3 with preamble for fair use

### File Size Limits
- **Maximum file size**: 25,000 characters for ALL code and markdown files
- **Split large files**: Decompose into modules, libraries, or separate documents
- **CLAUDE.md exception**: Maximum 39,000 characters (only exception to 25K rule)
- **Use Task Agents**: Utilize task agents (subagents) for large file operations

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

## Project Structure

```
WaddleAI/
├── proxy/              # OpenAI-compatible proxy server
├── management/         # Management API server
├── docs/               # Documentation
├── tests/              # Test suites
├── config/             # Configuration files
├── docker-compose.yml  # Production environment
└── CLAUDE.md           # This file
```

## Quick Start for Applications

### Using OpenAI-Compatible API

WaddleAI provides a fully compatible OpenAI API that can be used as a drop-in replacement:

```python
import openai

client = openai.OpenAI(
    api_key="wa-your-api-key-here",
    base_url="https://your-waddleai-proxy.com/v1"
)

response = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello, how are you?"}]
)

print(response.choices[0].message.content)
```

### Node.js Integration
```javascript
import OpenAI from 'openai';

const openai = new OpenAI({
    apiKey: 'wa-your-api-key-here',
    baseURL: 'https://your-waddleai-proxy.com/v1'
});

const completion = await openai.chat.completions.create({
    messages: [{ role: 'user', content: 'Hello!' }],
    model: 'gpt-4',
});
```

## Management API

### Authentication
```python
import requests

auth_response = requests.post(
    "https://your-waddleai-mgmt.com/auth/login",
    json={"username": "admin", "password": "your-password"}
)
token = auth_response.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}
```

### Token Usage Analytics
```python
usage = requests.get(
    "https://your-waddleai-mgmt.com/analytics/tokens/waddleai",
    headers=headers
).json()
```

### Quota Management
```python
quota_update = requests.post(
    "https://your-waddleai-mgmt.com/analytics/quotas/user123",
    headers=headers,
    json={"monthly_limit": 200000, "daily_limit": 20000}
)
```

## Role-Based Access Control

### Admin
- Full system access via management API
- All endpoints and functionality available
- Cross-organization visibility and control

### Resource Manager
- Organization-scoped quota management
- User management within assigned organizations
- Token limit control for assigned organizations

### Reporter
- Read-only analytics and reporting for assigned organizations
- Usage trend analysis and reporting
- Security incident reporting

### User
- OpenAI-compatible API access only
- Personal API key management
- Own usage statistics

## Dual Token System

WaddleAI uses a dual token system for accurate billing and analytics:

### WaddleAI Tokens
- Normalized billing units across all LLM providers
- Used for quota enforcement and cost calculation

### LLM Tokens
- Raw provider token counts (input/output)
- Used for detailed analytics and optimization

```python
{
    "usage": {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "waddleai_tokens": 15
    }
}
```

## Advanced Features

### Model Routing
```python
# Automatic routing
response = client.chat.completions.create(
    model="smart-router",
    messages=[{"role": "user", "content": "Complex reasoning task..."}]
)

# Force specific provider
response = client.chat.completions.create(
    model="ollama:llama2",
    messages=[{"role": "user", "content": "Local processing needed"}]
)
```

### Memory Integration
```python
response = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Remember my preferences"}],
    extra_headers={
        "X-WaddleAI-Memory": "user-session-123",
        "X-WaddleAI-Memory-Type": "conversation"
    }
)
```

### Security Features
- Prompt injection detection
- Jailbreak attempt prevention
- Data extraction blocking
- Credential harvesting protection

## Configuration

### Environment Variables
```bash
# Proxy Server
export PROXY_HOST=0.0.0.0
export PROXY_PORT=8000
export DATABASE_URL=postgresql://user:pass@localhost/waddleai
export JWT_SECRET=your-jwt-secret
export SECURITY_POLICY=balanced

# Management Server
export MGMT_HOST=0.0.0.0
export MGMT_PORT=8001
export ADMIN_PASSWORD=secure-admin-password
```

### Docker Compose
```yaml
version: '3.8'
services:
  waddleai-proxy:
    build: ./proxy
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql://postgres:password@db:5432/waddleai
    depends_on:
      - db

  waddleai-mgmt:
    build: ./management
    ports:
      - "8001:8001"
    environment:
      - DATABASE_URL=postgresql://postgres:password@db:5432/waddleai
    depends_on:
      - db

  db:
    image: postgres:15
    environment:
      - POSTGRES_DB=waddleai
      - POSTGRES_PASSWORD=password
```

## Health Monitoring

### Endpoints
```bash
curl https://your-waddleai-proxy.com/healthz
curl https://your-waddleai-proxy.com/api/status
curl https://your-waddleai-proxy.com/metrics
```

## Error Handling

### Common Error Codes
- `401` - Invalid or expired API key/token
- `403` - Insufficient permissions
- `429` - Rate limit or quota exceeded
- `400` - Blocked by security scanning
- `503` - Service unavailable

## Troubleshooting

### Common Issues
1. **Port Conflicts**: Check docker-compose port mappings
2. **Database Connections**: Verify connection strings
3. **License Validation**: Check license key format and network
4. **Build Failures**: Check dependency versions
5. **Quota Exceeded**: Monitor WaddleAI token consumption

### Debug Commands
```bash
docker-compose logs -f waddleai-proxy
docker exec -it waddleai-proxy /bin/bash
```

## Best Practices

### API Key Security
- Use environment variables for API keys
- Rotate keys regularly
- Use minimal required permissions
- Monitor usage patterns

### Performance Optimization
- Implement connection pooling
- Cache frequently used data
- Monitor response times
- Use appropriate models for tasks

### Cost Management
- Monitor WaddleAI token consumption
- Set appropriate quotas
- Use cheaper models when possible
- Implement usage alerts

## Support

- **Technical Documentation**: See `/docs/` folder
- **Integration Support**: support@penguintech.io
- **Sales Inquiries**: sales@penguintech.io
- **License Server Status**: https://status.penguintech.io

---

**Version**: 1.0.0
**Last Updated**: 2025-11-23
**Maintained by**: Penguin Tech Inc
**License Server**: https://license.penguintech.io
