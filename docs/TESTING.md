# Testing Guide

Comprehensive testing documentation for WaddleAI, including unit tests, contract/snapshot
tests, smoke tests, and dual-token/multi-backend-routing verification.

## Overview

| Test Level | Purpose | Runner | Status Today |
|-----------|---------|--------|--------------|
| **Unit Tests** | Isolated function/method testing | `pytest tests/unit/` | ~1141 passed, 4 skipped |
| **Contract Tests** | Request/response snapshot assertions for proxy + management | `pytest tests/contract/` | 79 tests collected |
| **Web UI Tests** | React component/unit tests | `npm test` (vitest) in `services/webui/` | 244 tests, 90% gate met |
| **Smoke Tests** | Fast post-deploy verification | two standalone bash scripts | not `pytest`-discoverable — see [Smoke Tests](#smoke-tests) |
| **Integration Tests** | Component interaction against live backends | `pytest tests/integration/` | 35 tests across 4 modules; needs reachable services |
| **E2E Tests** | Critical workflows end-to-end | Playwright, `tests/e2e/` (separate npm project) | not wired into `make test-e2e` — see [End-to-End Tests](#end-to-end-tests) |
| **Performance Tests** | Scalability/throughput | — | no `make` target or script exists yet |

Runtime database access goes through **`penguin-dal`**. **SQLAlchemy + Alembic**
(`services/management/alembic/`) are schema/migration only — never used for runtime
queries in tests or application code. Raw PyDAL is being migrated away from; if you find
it in a test fixture, that's a fix-on-sight per `.claude/rules` `backend-database.md`.

---

## Mock Data Scripts

### Current State

**`make seed-mock-data` is a no-op placeholder today** (`@echo "No mock data seeding
defined"` in the `Makefile`) — there is no `scripts/mock-data/` directory in this repo yet.
The pattern below is the PenguinTech house standard for when seeders are added (see
`.claude/rules` `general.md`/`testing.md`, 3-4 items per feature) — treat it as the target
shape to build toward, not as something you can run today.

### Naming Convention (once implemented)

- **Python**: `scripts/mock-data/seed-{feature-name}.py`, orchestrated by a
  `scripts/mock-data/seed-all.py` that runs all seeders in order
- One seeder per logical entity/feature, 3-4 representative items each — enough to cover
  the feature's variations without bloating the dev database

### Implementation Pattern (target — not yet present)

Use `penguin-dal` for the insert, not raw PyDAL or SQLAlchemy:

```python
#!/usr/bin/env python3
"""Seed mock data for API keys with dual-token quotas."""

from penguin_dal import create_dal
import os

def seed_keys() -> None:
    db = create_dal(db_type=os.getenv("DB_TYPE", "postgresql"), connection_string=os.environ["DATABASE_URL"])

    keys = [
        {"name": "unlimited-key", "waddleai_quota": 1_000_000, "llm_quota": 500_000, "status": "active"},
        {"name": "limited-key", "waddleai_quota": 100_000, "llm_quota": 50_000, "status": "active"},
        {"name": "demo-key", "waddleai_quota": 10_000, "llm_quota": 5_000, "status": "active"},
        {"name": "revoked-key", "waddleai_quota": 0, "llm_quota": 0, "status": "revoked"},
    ]

    for key in keys:
        db.api_keys.insert(**key)

    print(f"Seeded {len(keys)} API keys")

if __name__ == "__main__":
    seed_keys()
```

### Execution (once implemented)

```bash
make seed-mock-data                       # Via Makefile, once wired up
python3 scripts/mock-data/seed-all.py     # Direct execution
```

### When to Create a Seeder

Add a `seed-{feature}.py` (and wire it into `seed-all.py` and the `seed-mock-data`
Makefile target) whenever a new feature needs realistic dev data — quotas, backend
connections, routing rules, users. This is currently outstanding for every existing
feature in this repo.

---

## Contract Tests

### Purpose

Request/response snapshot tests for the proxy and management APIs — the closest thing
this repo has to dedicated API tests today. There is **no `tests/api/` directory**;
contract tests are it.

### Location & Structure

```
tests/contract/
├── conftest.py
├── snapshot.py
├── test_proxy_contract.py
├── test_proxy_health.py
├── test_management_contract.py
├── test_management_mutations.py
└── snapshots/              # stored JSON responses, one per scenario
```

`tests/contract/snapshots/` covers auth (login/logout/refresh/verify/change-password),
orgs, users, keys, quotas, usage, providers, routing matrix, ollama/llamacpp deployments,
webhooks, mem0 memory scoping, and error responses (400/404/401/403) — see the filenames
in that directory for the exact current surface.

### Execution

```bash
make test-contract                    # Via Makefile
pytest tests/contract/ -v --no-cov    # Direct — 79 tests collected today
```

---

## Smoke Tests

### Purpose

Fast verification that basic functionality works after code changes or a deploy.

### Current State

`make smoke-test` runs `pytest tests/smoke -v`, but **`tests/smoke/` has no
`pytest`-discoverable `test_*.py` files today** — only two standalone bash scripts. Running
`make smoke-test` as-is collects zero tests. Run the scripts directly instead:

```
tests/smoke/
├── test-production.sh          # Hits a live BASE_URL (default: https://waddleai.penguintech.io)
└── test_management_build.sh    # Static/build checks for services/management — see caveat below
```

### Execution

```bash
# Against a live deployment
BASE_URL=https://waddleai.penguintech.io ./tests/smoke/test-production.sh

# Static file/build checks for the management service
./tests/smoke/test_management_build.sh
```

`test-production.sh` checks: WebUI homepage loads (200 or 403 — Cloudflare may challenge),
login page loads, `/healthz` returns `healthy`, `/readyz` returns 200/503.

**Caveat on `test_management_build.sh`**: it predates the Phase-1 services/ consolidation
and checks for a root `docker-compose.yml` and for `flask`/`flask-security-too`/`pydal` in
`services/management/requirements.txt`. None of those are current — the service runs
Quart/hypercorn with `penguin-dal`, and there is no root `docker-compose.yml` (Docker
Compose is deprecated for every environment here — deployment is Helm, `k8s/helm/waddleai`).
Treat its pass/fail as informative about file existence and Docker build success only,
not as confirmation of the current stack.

### Speed Requirement

Once a real `pytest`-based smoke suite exists under `tests/smoke/`, it must complete in
under 2 minutes, per the company-wide standard.

### Pre-Commit Integration

Smoke tests are step 3 of `make pre-commit` (`lint` → `test-security` → `test`); there is
no separate smoke gate wired into `pre-commit` today beyond what `make test` covers.

---

## Unit Tests

### Purpose

Isolated function/method tests with mocked dependencies.

### Location

```
tests/unit/
├── *.py                    # ~24 files: token accounting, routing, RBAC, security,
│                            # memory/mem0 scoping, metering, feature flags, gRPC auth
├── proxy/
│   ├── test_endpoint_parity.py
│   ├── test_pipeline.py
│   └── test_pipeline_stages.py
├── management/
│   ├── ~24 files covering auth, keys, orgs, quotas, usage, providers, ollama,
│   │   llamacpp, webhooks, and app init
└── security/
    └── test_content_filter_redaction.py
```

Token-accounting tests: `test_token_manager.py`, `test_token_manager_costmodel.py`,
`test_token_limiter.py`, `test_metering.py`. Routing tests: `test_request_router.py`,
`test_request_router_breaker.py`, `test_request_router_merge.py`, `test_routing_matrix.py`.

### Execution

```bash
make test-unit                              # All unit tests
pytest tests/unit/                          # Same, direct
pytest tests/unit/test_token_manager.py -v  # Specific file
```

### Requirements

- All external dependencies mocked (network, DB)
- Coverage gate: **60%** (`.coveragerc`, `fail_under = 60`) — actual coverage today is
  ~78%, comfortably above the gate; don't let it regress toward the floor
- Coverage source scope is `shared` and `services/management/app` only (see
  `[run] source =` in `.coveragerc`) — `proxy/` unit coverage isn't counted toward the gate

---

## Integration Tests

### Purpose

Component interaction verification against a real database.

### Current State

`tests/integration/` holds **35 tests across four modules**, all tracked in git:

| Module | Covers |
|---|---|
| `test_claude_integration.py` | Anthropic-compatible path |
| `test_llamacpp_integration.py` | llama.cpp connector and fleet |
| `test_mem0_integration.py` | mem0-compatible memory API over pgvector |
| `test_ollama_integration.py` | Ollama connector and lifecycle |

These exercise live backends, so they need the relevant service reachable — an
Ollama or llama.cpp endpoint, and a PostgreSQL instance with pgvector for the
memory tests. They are not part of the `test (3.13)` CI job, which runs
`tests/unit/` only; the `integration-test` job is gated on non-pull-request
events and reports `skipping` on PRs.

Note that running this suite on its own trips the 60% coverage gate configured
in `pytest.ini`, since 35 integration tests exercise only a narrow slice of the
codebase. That is a reporting artifact of running the suite in isolation, not a
failure of the tests. Run the full suite, or pass `--no-cov`, when you only want
the integration results.

### Execution

```bash
make test-integration          # pytest tests/integration -v
pytest tests/integration/ --no-cov   # skip the coverage gate when running alone
```

---

## End-to-End Tests

### Purpose

Critical user workflows against a real deployment, via Playwright.

### Current State

E2E tests are **not** run through `make test-e2e` — that target (`pytest tests/e2e -v`)
collects zero tests, because `tests/e2e/` is a separate Playwright/npm project, not a
pytest tree.

### Location

```
tests/e2e/
├── package.json          # scripts: test, test:ui, test:debug, report
├── playwright.config.js  # baseURL https://waddleai.penguintech.cloud, routes through
│                          # the dal2 internal LB IP to bypass Cloudflare bot protection
└── tests/
    └── smoke.spec.js
```

### Execution

```bash
cd tests/e2e
npm ci
npm test                # playwright test
npm run test:ui         # interactive UI mode
npm run report          # view the HTML report
```

Playwright artifacts (traces, HTML report) go to `/tmp/playwright-waddleai/` — clean up
after every run, pass or fail, per the company Playwright convention.

---

## Performance Tests

**No `make test-performance` target and no performance test script exist in this repo
today.** If you add performance/load testing, add both the script(s) and a `make` target
that runs them — don't reference this section as a working procedure until that lands.

---

## Cross-Architecture Testing

### Purpose

Ensure images build and run correctly on both amd64 and arm64 — CI's `build-platform` job
already does this on every push (see `docs/WORKFLOWS.md`); this section covers doing it
locally before a final commit.

### When to Test

Before every final commit, build the alternate architecture with QEMU:
- Developing on amd64 → build arm64
- Developing on arm64 → build amd64

### Setup (First Time)

```bash
docker buildx create --name multiarch --driver docker-container
docker buildx use multiarch
```

### Single Architecture Build (native, fast)

```bash
docker build -f proxy/Dockerfile -t waddleai-proxy:test proxy/
docker build -f services/management/Dockerfile -t waddleai-management:test .   # context = repo root
```

### Cross-Architecture Build (QEMU)

```bash
docker buildx build --platform linux/arm64 -f proxy/Dockerfile -t waddleai-proxy:test proxy/
docker buildx build --platform linux/amd64,linux/arm64 -f services/management/Dockerfile -t waddleai-management:test .
```

There is no committed `scripts/build/test-multiarch.sh` — the commands above are run
directly; write one if you find yourself repeating this often enough to justify it (see
`.claude/rules` `general.md` Repeatable Task Migration).

### Troubleshooting

**QEMU not available**:
```bash
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
```

**Slow builds with QEMU**: expect 2-5x slower builds under emulation; use for final
validation, not every iteration.

**Architecture-specific issues to watch for**: endianness in binary protocols,
floating-point precision, package availability per arch.

---

## Test Execution Order (Pre-Commit)

Matches `make pre-commit` plus the additional steps this doc covers:

1. **Linters** (`make lint`)
2. **Security scans** (`make test-security`)
3. **Unit tests** (`make test-unit` — the `test` target currently only runs this)
4. **Contract tests** (`make test-contract`)
5. **Web UI tests** (`npm test` in `services/webui/`, if that service changed)
6. **Integration tests** (`make test-integration` — currently a no-op, see above)
7. **E2E tests** (`npm test` in `tests/e2e/`, against a real deployment — not part of
   `make pre-commit`)
8. **Cross-architecture build** (optional, slow)

## CI/CD Integration

GitHub Actions (`docker-build.yml`) runs, on every push/PR touching `proxy/**`,
`services/**`, or `shared/**`:

- **`test`**: Python unit tests + bandit
- **`test-webui`**: ESLint + vitest (coverage-gated) + build, for `services/webui`
- **`build-platform`** → **`merge-manifests`**: multi-arch image builds, gated on `test`
- **`security-scan`**: Trivy on the merged images (skipped on PRs)
- **`integration-test`**: ephemeral docker-compose stack, health + auth checks (skipped
  on PRs)
- **`release`** / **`cleanup`**: on version tags / always, respectively

There is no separate nightly or performance-test CI job. See
[Workflows](WORKFLOWS.md) for the full per-job breakdown.

---

**Last Updated**: 2026-08-10
**Maintained by**: Penguin Tech Inc
