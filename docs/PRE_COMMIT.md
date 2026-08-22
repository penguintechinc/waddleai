# Pre-Commit Checklist

**CRITICAL: This checklist MUST be followed before every commit.**

## Automated Pre-Commit Target

**Run the Makefile target to execute the checks in order:**

```bash
make pre-commit
```

This runs, in order: `lint` → `test-security` → `test` (currently `test-unit` only — see
[Required Steps](#required-steps-in-order)). There is no standalone
`scripts/pre-commit/*.sh` suite; `make pre-commit` and the individual `make` targets below
are the only supported entry points.

**Individual targets** (run separately if needed — all defined in the root `Makefile`):
- `make lint` — flake8, black, isort, mypy (best-effort; each step is skipped with a warning
  if the tool isn't installed, so a clean run is not proof the tool actually executed —
  confirm the tool ran before trusting a pass)
- `make test-security` — bandit, pip-audit, npm audit, gitleaks, OSI license gate
  (`scripts/check-licenses.sh`)
- `make test-unit` — `pytest tests/unit`
- `make test-integration` — `pytest tests/integration` (directory currently has no test
  files; the target passes trivially — see [TESTING.md](TESTING.md#integration-tests))
- `make test-contract` — `pytest tests/contract` (request/response snapshot tests)
- `make smoke-test` — `pytest tests/smoke` (directory currently has no `pytest`-discoverable
  tests, only standalone shell scripts — see
  [TESTING.md](TESTING.md#smoke-tests))
- `make seed-mock-data` — currently a no-op placeholder (no seeder scripts exist yet)

---

## Required Steps (In Order)

Before committing, run in this order (or use `make pre-commit` for the first three):

### Foundation Checks
- [ ] **Linters**: `make lint` (flake8, black --check, isort --check-only, mypy — see
      `pyproject.toml` for black/isort/mypy config, `.flake8` for flake8 config)
- [ ] **Security scans**: `make test-security` (bandit, pip-audit, npm audit, gitleaks,
      license gate)

### Build & Integration Verification
- [ ] **Build & Run**: Verify each service's container builds (see
      [Docker / Containers](#docker--containers) below)
- [ ] **Smoke tests**: `make smoke-test`
  - Currently collects zero tests (`tests/smoke/` has two standalone bash scripts,
    `test-production.sh` and `test_management_build.sh`, but no `pytest`-discoverable
    `test_*.py` files) — run the scripts directly if you need smoke coverage today
  - See: [Testing Documentation - Smoke Tests](TESTING.md#smoke-tests)

### Feature Testing & Documentation
- [ ] **Mock data**: `make seed-mock-data` — currently a no-op placeholder; no
      `scripts/mock-data/` seeders exist yet. If you add a feature that needs seed data,
      write the seeder and wire it into this target
- [ ] **API/contract testing** (for endpoint changes): `pytest tests/contract -v` covers
      request/response shape for proxy and management endpoints via stored snapshots in
      `tests/contract/snapshots/`. There is no separate `tests/api/` directory

### Comprehensive Testing
- [ ] **Unit tests**: `pytest tests/unit/ -v`
  - Mocked dependencies; ~1141 tests today (a handful skipped) — see
    [TESTING.md](TESTING.md#unit-tests)
  - Coverage gate: 60% (`.coveragerc`, `fail_under = 60`) — see
    [TESTING.md](TESTING.md#unit-tests)
- [ ] **Integration tests**: `pytest tests/integration/ -v`
  - 35 tests across 4 modules (Claude, llama.cpp, mem0, Ollama). They hit live
    backends, so they need the relevant service reachable; skip locally if it
    isn't. CI does not run them — the `integration-test` job is gated on
    non-pull-request events
- [ ] **Web UI tests** (if `services/webui` changed): `npm test` from `services/webui/`
  (runs `vitest run --coverage`); coverage gate 90% (see
  [TESTING.md](TESTING.md#unit-tests))

### Finalization
- [ ] **Version updates**: Update `.version` if releasing new version (only increment
      Major/Minor/Patch once the current version is tagged — see `versioning` skill)
- [ ] **Documentation**: Update docs if adding/changing workflows or token system
- [ ] **Docker builds**: Verify Dockerfiles use a Debian bookworm-slim base (no alpine) and
      pin external images by SHA256 digest
- [ ] **Cross-architecture**: (Optional) Test alternate architecture with QEMU —
      `docker buildx build --platform linux/arm64 -f proxy/Dockerfile proxy/` (if on amd64)
  - See: [Testing Documentation - Cross-Architecture Testing](TESTING.md#cross-architecture-testing)

---

## Language-Specific Commands

### Python

```bash
# Linting (paths match .flake8 / pyproject.toml exclusions)
flake8 . --max-line-length=120
black --check .
isort --check-only .
mypy . --ignore-missing-imports

# Security (paths match CI's docker-build.yml test job)
bandit -r proxy services/management shared -ll
pip-audit -r requirements.txt

# Build & Run
python3 -m py_compile proxy/apps/proxy_server/main.py services/management/app/__init__.py
pip install -r requirements.txt
pip install -r services/management/requirements.txt

# Verify proxy starts (Quart app, hypercorn ASGI server, port 8080), then kill
cd proxy && hypercorn apps.proxy_server.main:app --bind 0.0.0.0:8080 &

# Verify management starts (Quart app, hypercorn ASGI server, port 8001), then kill
cd services/management && hypercorn asgi:app --bind 0.0.0.0:8001 &

# Tests
pytest tests/unit/ -v                      # coverage config comes from .coveragerc via pytest.ini
pytest tests/contract/ -v --no-cov         # request/response snapshot tests
pytest tests/unit/test_token_manager.py -v      # token accounting tests
pytest tests/unit/test_request_router.py -v     # routing tests
```

Database access at runtime goes through `penguin-dal`; SQLAlchemy + Alembic are schema/
migration only (`services/management/alembic/`) — never used for runtime queries. See
`docs/TESTING.md` and `.claude/rules` `backend-database.md` for the full pattern.

### Docker / Containers

```bash
# Lint Dockerfiles
hadolint proxy/Dockerfile services/management/Dockerfile services/webui/Dockerfile

# Verify base image (Debian bookworm-slim, NOT alpine)
grep -E "^FROM.*bookworm" proxy/Dockerfile services/management/Dockerfile

# Build (note differing build contexts)
docker build -t waddleai-proxy:test -f proxy/Dockerfile proxy/
docker build -t waddleai-management:test -f services/management/Dockerfile .   # context = repo root (COPYs shared/)
docker build -t waddleai-webui:test -f services/webui/Dockerfile services/webui/

# Start containers
docker run -d --name test-proxy -p 8080:8080 waddleai-proxy:test
docker run -d --name test-management -p 8001:8001 waddleai-management:test

# Check logs
docker logs test-proxy
docker logs test-management

# Cleanup
docker stop test-proxy test-management
docker rm test-proxy test-management
```

**Docker Compose is deprecated for every environment** — there is no root
`docker-compose.yml`/`docker-compose.dev.yml` in this repo. Local multi-service run and
deployment go through the Helm chart at `k8s/helm/waddleai` (see the `deploying-app` skill).
CI's `integration-test` job generates its own throwaway `docker-compose.test.yml` purely to
stand up proxy + management + Postgres + Valkey for that one job — that file is not
committed and is not a local-dev artifact.

### YAML Linting

```bash
yamllint .github/workflows/*.yml k8s/helm/waddleai/*.yml k8s/helm/waddleai/*.yaml
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
  - **Python packages**: `bandit -r proxy services/management shared -ll` (CI additionally
    gates the build on `bandit -r proxy services/management shared -lll`, HIGH severity only)
  - **Dependency checks**: `pip-audit -r requirements.txt` (and per-service
    `requirements.txt` for any service you touched)
  - **CodeQL**: All code must pass CodeQL security analysis (`.github/workflows/codeql.yml`,
    languages: python, javascript-typescript, actions)
- **Do NOT commit if security vulnerabilities are found** - fix all issues first
- **Document vulnerability fixes** in commit message if applicable

### Vulnerability Response
1. Identify affected packages and severity
2. Update to patched versions immediately
3. Test updated dependencies thoroughly
4. Document security fixes in commit messages
5. Verify no new vulnerabilities introduced

---

## API / Contract Testing Requirements

Before committing changes to proxy/management endpoints:

- **There is no `tests/api/` directory.** Endpoint-shape coverage lives in
  `tests/contract/` (`test_proxy_contract.py`, `test_proxy_health.py`,
  `test_management_contract.py`, `test_management_mutations.py`), which asserts responses
  against stored snapshots in `tests/contract/snapshots/`
- **Run before commit**: `pytest tests/contract -v --no-cov`
- **Test coverage**: health checks, auth, CRUD, error cases, memory/mem0 scoping — see the
  snapshot filenames in `tests/contract/snapshots/` for the current surface

**Example manual API check (proxy, port 8080)**:
```bash
#!/bin/bash
set -e

PROXY_URL="http://localhost:8080"
MGMT_URL="http://localhost:8001"
TOKEN="$WADDLEAI_API_KEY"

# Health
curl -sf "$PROXY_URL/healthz"
curl -sf "$MGMT_URL/healthz"

# Chat completion (expect 401 without a valid key, 200 with one)
curl -X POST "$PROXY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4", "messages": [{"role": "user", "content": "test"}]}'
```

---

## Dual Token System Requirements

Before committing changes to token handling:

- [ ] **Token accounting works**: `pytest tests/unit/test_token_manager.py tests/unit/test_token_manager_costmodel.py tests/unit/test_token_limiter.py -v`
- [ ] **Metering works**: `pytest tests/unit/test_metering.py -v`
- [ ] **WaddleAI tokens calculated correctly**: Conversion from LLM tokens verified
- [ ] **LLM tokens tracked separately**: Input/output tokens tracked independently
- [ ] **Consumption/analytics integration coverage**: `tests/integration/` covers
      the provider and memory paths but not token-consumption accounting — add
      coverage alongside any change that touches consumption tracking

---

## Multi-Backend Routing Requirements

Before committing changes to routing logic:

- [ ] **Routing selection works**: `pytest tests/unit/test_request_router.py tests/unit/test_request_router_merge.py tests/unit/test_routing_matrix.py -v`
- [ ] **Backend fallback / circuit breaking works**: `pytest tests/unit/test_request_router_breaker.py -v`
- [ ] **Pipeline stages work**: `pytest tests/unit/proxy/test_pipeline.py tests/unit/proxy/test_pipeline_stages.py -v`
- [ ] **Endpoint parity**: `pytest tests/unit/proxy/test_endpoint_parity.py -v`

---

## Smoke Tests Requirements

```bash
# Via Makefile — currently a no-op (no pytest-discoverable tests in tests/smoke/)
make smoke-test

# The two smoke scripts that do exist are standalone bash, run directly:
BASE_URL=https://waddleai.penguintech.io ./tests/smoke/test-production.sh
./tests/smoke/test_management_build.sh
```

`test_management_build.sh` predates the Phase-1 consolidation and checks for `docker-compose.yml`,
`flask`, `flask-security-too`, and `pydal` in `services/management/requirements.txt` — none
of which are current (the service uses Quart/hypercorn and `penguin-dal`). Treat its output
with that in mind until it's updated; don't use it as a source of truth for the current stack.

---

## Pre-Commit Checklist Template

Use this checklist before every commit:

```
Before EVERY commit, verify:

[ ] Linting passes
    - Python: flake8, black, isort, mypy
    - Docker: hadolint
    - YAML: yamllint

[ ] Security checks pass
    - Python: bandit, pip-audit
    - CodeQL analysis passes (GitHub)
    - No hardcoded secrets (gitleaks)

[ ] Tests pass locally
    - Unit tests complete successfully (integration tests need live backends — not a gate)
    - Contract/snapshot tests pass

[ ] Web UI tests pass (if services/webui touched)
    - npm test (vitest) from services/webui/, coverage ≥90%

[ ] Manual testing complete
    - Endpoints respond as expected
    - Logs show expected output
    - No debug code left in

[ ] No debug code left in
    - No console.log, print(), println! statements
    - No commented code blocks
    - No debug flags enabled

[ ] Configuration is correct
    - Environment variables correct
    - Database connections working (penguin-dal at runtime)
    - API endpoints accessible

[ ] Documentation updated
    - Comments added/updated if needed
    - README updated if major changes
    - DEVELOPMENT.md updated if workflow changed

[ ] Version file updated (if releasing)
    - .version file has new version
    - RELEASE_NOTES.md updated
    - Format: MAJOR.MINOR.PATCH
```

---

**Last Updated**: 2026-08-10
**Maintained by**: Penguin Tech Inc
