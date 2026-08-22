# WaddleAI - Claude Code Context

## Project Overview

WaddleAI is an AI proxy and management system that provides OpenAI-compatible and
Anthropic-compatible APIs with advanced routing, security, and dual-token usage
management.

**Key Features:**
- OpenAI-compatible (`/v1/chat/completions`) and Anthropic-compatible (`/v1/messages`)
  proxy endpoints, sharing one ordered pipeline (auth → rate limit → security → memory →
  dispatch → metering)
- Multi-provider routing: OpenAI, xAI, Anthropic, Google Gemini, Ollama, llama.cpp, AWS
  Bedrock — SSE streaming across all of them
- Dual token system (WaddleAI tokens + raw LLM tokens)
- Role-based access control (Admin, Resource Manager, Reporter, User)
- 4-tier content filtering pipeline (regex → org rules → NER → LLM auditor)
- Quota and usage management, OpenTelemetry `gen_ai.*` spans, Prometheus metrics

## Technology Stack

### Languages & Frameworks

**Python Stack:**
- **Python**: 3.13 (see `.python-version` / CI matrix)
- **Web framework**: **Quart** (async-native), served by **hypercorn** — Flask and
  Flask-Security-Too are retired, not present in this repo
- **Database at runtime**: **`penguin-dal`** in `services/management/` (the control
  plane). The proxy (`proxy/apps/proxy_server/main.py`) is the one exception: its
  synchronous API-key auth path (`RBACManager.authenticate_api_key` in
  `shared/auth/rbac.py`) still uses raw PyDAL, offloaded to a thread pool via
  `asyncio.to_thread` so it doesn't block the event loop. This is tracked debt, not the
  target state — fix-on-sight per `backend-database.md` if you touch that code path.
- **Schema & migrations**: SQLAlchemy models + Alembic (`services/management/alembic/`)
  — schema/migration authority only, never used for runtime queries.
  `init_schema()` (`services/management/app/models_sqlalchemy.py`) also
  idempotently creates missing tables on startup; Alembic upgrades are run manually or
  via a Helm-templated Kubernetes Job, never automatically at app boot.
- **Auth**: `penguin-aaa` (OIDC/JWT) — Flask-Security-Too is gone
- **Performance**: `@dataclass(slots=True)`, type hints, async/await throughout

**Frontend Stack:**
- **React** (TypeScript) at `services/webui/`, served via Express, built with Vite
- **Node.js**: 24.x for build tooling

### Infrastructure & DevOps
- **Deployment**: Kubernetes + Helm only, every environment — **Docker Compose is
  deprecated and not used anywhere in this repo**, including local development. Docker
  itself is still used to build service images and to run standalone dependency
  containers (Postgres, Valkey) for direct-run local dev — see `docs/DEVELOPMENT.md`.
- **CI/CD**: GitHub Actions — see [Release Pipeline](#release-pipeline--ci-image-tags)
  below
- **Monitoring**: Prometheus metrics, OpenTelemetry `gen_ai.*` span attributes on the
  dispatch span
- **Logging**: Structured logging, configurable levels; no secrets in span/log data

### Databases & Storage
- **Primary**: PostgreSQL + `pgvector` (memory/embedding storage) — the only supported
  database; no `DB_TYPE` switch in this repo today
- **Cache**: **Valkey** (Redis-protocol-compatible) — replaces Redis throughout; the
  `REDIS_URL` env var name is historical

### Security & Authentication
- **`penguin-aaa`**: OIDC/JWT authentication and scope-based authorization
- **TLS**: 1.2 minimum enforced
- **Security scanning**: prompt injection detection, jailbreak prevention, PII/PCI
  detection — see [Content Filtering & Licence Model](#content-filtering--licence-model)

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
- **Regular security audits**: `pip-audit`, `npm audit` (see `make test-security`)

### Linting & Code Quality Requirements
- **ALL code must pass `make lint`** before commit — no exceptions
- **Python**: `ruff` (lint + format + import sort — the only Python linter/formatter;
  flake8/black/isort are not used), `mypy` (advisory, not a hard gate today), `bandit`
  (security)
- **JavaScript/TypeScript**: ESLint (`services/webui/`)
- **Docker**: hadolint
- **YAML**: yamllint
- **OpenAPI**: `spectral lint openapi/v1.yaml` (`make openapi-lint`)
- **CodeQL**: Python, JavaScript/TypeScript and Actions — `.github/workflows/codeql.yml`
- **PEP Compliance**: PEP 8, PEP 257 (docstrings), PEP 484 (type hints)

### Build & Deployment Requirements
- **NEVER mark tasks as completed until successful build verification**
- All Python and Node builds MUST be executed within Docker containers (CI and, where
  practical, local)
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

## Content Filtering & Licence Model

The content filter (`shared/security/content_filter.py`) runs a 4-tier pipeline:

1. **Regex patterns** — 23 built-in PII/PCI patterns (credit cards, SSNs, phone
   numbers, emails, API keys, etc.), toggleable per organization — **never
   licence-gated**
2. **Custom organization rules** — org-defined pattern overrides — **never
   licence-gated**
3. **NER (Named Entity Recognition)** — Presidio + spaCy (`en_core_web_lg`), transformer
   fallback, for PERSON/LOCATION/NRP/MEDICAL_LICENSE and more — **licence-gated**: both
   a PostHog flag and a `penguin_licensing` entitlement
   (`NER_TIER_LICENSE_FEATURE = "pii_ner_detection"`) must pass; if either check is
   unavailable or fails, the NER tier is skipped and tiers 1–2 still run, so a caller
   without a licence client gets baseline PII protection rather than none
4. **LLM auditor** — ShieldGemma 2B default safety classifier (YES/NO policy format) —
   not licence-gated by tier, but governed by the same PostHog-flag-first pattern as
   every other feature

Tiers 1 and 2 are always available regardless of licence tier; tier 3 (NER) is the only
one gated on `penguin_licensing`/PostHog.

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
waddleai/
├── proxy/                    # OpenAI/Anthropic-compatible data-plane (Quart, port 8080)
├── services/
│   ├── management/           # Control plane API (Quart, port 8001)
│   ├── webui/                 # React/TypeScript frontend (Express-served)
│   └── penguincode/            # Vendored PenguinCode CLI/extension (own release story)
├── shared/                   # Shared library code (auth, security, DAL helpers)
├── k8s/helm/waddleai/         # The one Helm chart deploying every environment
├── docs/                      # Documentation (this tree + docs/docs-site/ mkdocs site)
├── tests/                     # unit/, contract/, integration/, e2e/, smoke/
├── openapi/v1.yaml            # Generated management API OpenAPI 3.x spec
├── Makefile                   # Build/test/lint/deploy automation
├── .version                   # Version tracking
└── CLAUDE.md                  # This file
```

There is no root `docker-compose.yml` — the Helm chart is the only supported way to run
more than one service together, in every environment including local dev (see
`docs/DEVELOPMENT.md` for the direct-run-processes alternative for fast local iteration).

## Real `make` Targets

The full, current target list, defined in the root `Makefile` (run `make help`-style
`grep -nE '^[a-z][a-z-]*:' Makefile` to regenerate this list):

| Target | Purpose |
|---|---|
| `make venv` | Create `.venv` (Python 3.13) from the hash-pinned lockfiles — published deps only |
| `make setup` | `install-hooks` |
| `make install-hooks` | Install the `pre-commit` framework + register pre-commit/pre-push hooks |
| `make verify-hooks` | Report whether the hooks are installed and non-empty |
| `make dev` | Start development services |
| `make build` | Build all services |
| `make docker-build` | Build container images |
| `make docker-push` | Push container images |
| `make lint` | Lint everything — fails on error, no `\|\| true`, no silent skips |
| `make generate-openapi` | Regenerate `openapi/v1.yaml` from the `quart-schema` annotations |
| `make openapi-lint` | `spectral lint openapi/v1.yaml` — gates on error |
| `make test` | `test-unit` |
| `make test-unit` | `pytest tests/unit` — 90% coverage gate, branch coverage on |
| `make test-integration` | `pytest tests/integration` |
| `make test-e2e` | `pytest tests/e2e` |
| `make test-functional` | (placeholder) |
| `make test-contract` | `pytest tests/contract` — request/response snapshot tests |
| `make test-security` | Security scans over first-party code — bandit, gitleaks, pip-audit, npm audit, licence gate; fails on findings |
| `make smoke-test` | Fast post-build verification |
| `make smoke-test-production` | Live prod checks (network + real deployment required) — not part of pre-commit |
| `make seed-mock-data` | Mock data seeding |
| `make clean` | Remove build artifacts |
| `make deploy-dev` | Deploy to dev/alpha |
| `make deploy-prod` | Deploy to production |
| `make pre-commit` | The pre-commit gate sequence |

See `docs/PRE_COMMIT.md`, `docs/TESTING.md`, and `docs/DEVELOPMENT.md` for the full
procedures behind each target.

## Coverage Gate

`.coveragerc`: `fail_under = 90`, `branch = True`. Coverage source is `shared` and
`services/management/app`; `pytest.ini` additionally instruments `proxy` for reporting.
Run `pytest tests/unit -v --cov-report=term-missing` to see exactly which lines are
uncounted before you add a test — the `Missing` column names the line ranges, not just a
percentage. CI also asserts a minimum collected-test count for `tests/unit`, so a
misconfigured path filter that silently collects zero tests fails loudly instead of
reporting a false "clean" 90%.

## Release Pipeline / CI Image Tags

Five tiers, each with its own source branch/event, tag, and cluster — canonical table
lives in `docs/docs-site/docs/deployment/kubernetes.md` (§Release pipeline):

| Tier | Source | Tag | Cluster / deploy |
|---|---|---|---|
| Pre-alpha | `feature/` `fix/` `chore/` `hotfix/` `docs/` `refactor/` branches | build-only in CI (not pushed) | Local K8s, destroy + fresh |
| Alpha | `release/v{Major}.{Minor}.X` branches | `alpha-<epoch64>` | Local K8s today, destroy + fresh |
| Beta | Merge to `main` | `beta-<epoch64>` | `dal2-beta` context, destroy + fresh |
| Gamma | GitHub release flagged pre-release | `gamma-<epoch64>` | DigitalOcean (context TBD), **upgrade in place** — the only upgrade-path test |
| Prod | GitHub release (non-pre-release), **v1.x+ only** | `v{Major}.{Minor}.{Patch}` | DigitalOcean, separate cluster (not built yet), upgrade in place |

Namespace is always `waddleai` in every context — never environment-suffixed; registry
is `ghcr.io/penguintechinc/waddleai/{proxy,management,webui,ollama}`.

## Quick Start for Applications

### Using OpenAI-Compatible API

WaddleAI provides a fully compatible OpenAI API that can be used as a drop-in replacement:

```python
import openai

client = openai.OpenAI(
    api_key="<your-waddleai-key>", base_url="https://your-waddleai-proxy.com/v1"
)

response = client.chat.completions.create(
    model="gpt-4", messages=[{"role": "user", "content": "Hello, how are you?"}]
)

print(response.choices[0].message.content)
```

### Node.js Integration
```javascript
import OpenAI from 'openai';

const openai = new OpenAI({
    apiKey: '<your-waddleai-key>',
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
    json={"username": "admin", "password": "your-password"},
)
token = auth_response.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}
```

### Token Usage Analytics
```python
usage = requests.get(
    "https://your-waddleai-mgmt.com/analytics/tokens/waddleai", headers=headers
).json()
```

### Quota Management
```python
quota_update = requests.post(
    "https://your-waddleai-mgmt.com/analytics/quotas/user123",
    headers=headers,
    json={"monthly_limit": 200000, "daily_limit": 20000},
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
        "waddleai_tokens": 15,
    }
}
```

## Advanced Features

### Model Routing
```python
# Automatic routing
response = client.chat.completions.create(
    model="smart-router", messages=[{"role": "user", "content": "Complex reasoning task..."}]
)

# Force specific provider
response = client.chat.completions.create(
    model="ollama:llama2", messages=[{"role": "user", "content": "Local processing needed"}]
)
```

### Memory Integration
```python
response = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Remember my preferences"}],
    extra_headers={
        "X-WaddleAI-Memory": "user-session-123",
        "X-WaddleAI-Memory-Type": "conversation",
    },
)
```

## Configuration

### Environment Variables
```bash
# Proxy Server -- binds 0.0.0.0 always; only the port is configurable
export HTTP_PORT=8080  # default; proxy/apps/proxy_server/main.py
export DATABASE_URL=postgresql://user:pass@localhost/waddleai
export JWT_SECRET=your-jwt-secret
export SECURITY_POLICY=balanced

# Management Server -- no host/port env var; bound via hypercorn CLI/Dockerfile
# (0.0.0.0:8001, see services/management/Dockerfile)
export ADMIN_INITIAL_PASSWORD=secure-admin-password
```

Full variable list and defaults: `services/management/app/config.py`,
`proxy/apps/proxy_server/main.py`, and `k8s/helm/waddleai/values.yaml`. There is no
`.env.example` in this repo — see `docs/DEVELOPMENT.md`.

### Deployment

Every environment deploys via the Helm chart at `k8s/helm/waddleai` — see
[Release Pipeline / CI Image Tags](#release-pipeline--ci-image-tags) above and
`docs/docs-site/docs/deployment/kubernetes.md` for the full procedure. Local multi-service
runs go through `./scripts/deploy-alpha.sh` against a MicroK8s/Docker Desktop cluster;
there is no Docker Compose path.

## Health Monitoring

### Endpoints (proxy, port 8080)
```bash
curl https://your-waddleai-proxy.com/healthz
curl https://your-waddleai-proxy.com/readyz
curl https://your-waddleai-proxy.com/api/status
curl https://your-waddleai-proxy.com/metrics
```

Management (port 8001) exposes the equivalent `/healthz` and `/readyz`.

## Error Handling

### Common Error Codes
- `401` - Invalid or expired API key/token
- `403` - Insufficient permissions
- `429` - Rate limit or quota exceeded
- `400` - Blocked by security scanning
- `503` - Service unavailable

## Troubleshooting

### Common Issues
1. **Port Conflicts**: Check what's bound to 8080 (proxy) / 8001 (management) / 3000 (webui)
2. **Database Connections**: Verify `DATABASE_URL` and that Postgres/pgvector is reachable
3. **License Validation**: Check license key format and network access to
   `license.penguintech.io`
4. **Build Failures**: Check dependency versions; builds run inside containers
5. **Quota Exceeded**: Monitor WaddleAI token consumption

### Debug Commands
```bash
kubectl --context <ctx> logs -n waddleai -l app=waddleai-proxy -f
kubectl --context <ctx> exec -it -n waddleai deploy/waddleai-proxy -- /bin/sh
```

For direct-run local dev (no cluster), each service logs to its own terminal — see
`docs/DEVELOPMENT.md`.

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

**Version**: 1.1.0
**Last Updated**: 2026-08-21
**Maintained by**: Penguin Tech Inc
**License Server**: https://license.penguintech.io
