# Testing Guide

Comprehensive testing documentation for WaddleAI, including unit tests, integration tests, smoke tests, dual token validation, and multi-backend routing verification.

## Overview

Testing is organized into multiple levels to ensure comprehensive coverage, fast feedback, and production-ready code for the proxy and management services:

| Test Level | Purpose | Speed | Coverage |
|-----------|---------|-------|----------|
| **Smoke Tests** | Fast verification of basic functionality | <2 min | Proxy health, management health, dual token validation, backend routing |
| **Unit Tests** | Isolated function/method testing | <1 min | Proxy logic, token validation, routing algorithms |
| **Integration Tests** | Component interaction verification | 1-5 min | Proxy-DB interaction, multi-backend routing, token consumption tracking |
| **E2E Tests** | Critical workflows end-to-end | 5-10 min | API requests with dual token tracking, backend selection |
| **Performance Tests** | Scalability and throughput validation | 5-15 min | Token validation overhead, routing latency |

---

## Mock Data Scripts

### Purpose

Mock data scripts populate the development database with realistic test data, enabling:
- Rapid local development without manual data entry
- Consistent test data across the development team
- Documentation of expected data structure and relationships
- Quick feature iteration with pre-populated databases for dual token system and routing

### Location & Structure

```
scripts/mock-data/
├── seed-all.py             # Orchestrator: runs all seeders in order
├── seed-users.py           # 3-4 users with different roles/permissions
├── seed-backends.py        # 3-4 LLM backend connections (OpenAI, Anthropic, Ollama)
├── seed-tokens.py          # 3-4 API tokens with dual token quotas
├── seed-routes.py          # Routing rules and model mappings
├── seed-[feature].py       # Additional feature-specific seeders
└── README.md               # Instructions for running mock data
```

### Naming Convention

- **Python**: `seed-{feature-name}.py`
- **Shell**: `seed-{feature-name}.sh`
- **Organization**: One seeder per logical entity/feature

### Scope: 3-4 Items Per Feature

Each seeder should create **exactly 3-4 representative items** to test all feature variations without creating excessive test data:

**Example (API Tokens with Dual Token System)**:
```python
# seed-tokens.py
items = [
    {
        "name": "unlimited-token",
        "waddleai_quota": 1000000,
        "llm_quota": 500000,
        "status": "active"
    },
    {
        "name": "limited-token",
        "waddleai_quota": 100000,
        "llm_quota": 50000,
        "status": "active"
    },
    {
        "name": "demo-token",
        "waddleai_quota": 10000,
        "llm_quota": 5000,
        "status": "active"
    },
    {
        "name": "revoked-token",
        "waddleai_quota": 0,
        "llm_quota": 0,
        "status": "revoked"
    },
]
```

**Example (Multi-Backend Connections)**:
```python
# seed-backends.py
items = [
    {
        "name": "OpenAI GPT-4",
        "provider": "openai",
        "endpoint": "https://api.openai.com/v1",
        "enabled": True
    },
    {
        "name": "Anthropic Claude",
        "provider": "anthropic",
        "endpoint": "https://api.anthropic.com/v1",
        "enabled": True
    },
    {
        "name": "Ollama Local",
        "provider": "ollama",
        "endpoint": "http://localhost:11434/v1",
        "enabled": True
    },
    {
        "name": "Disabled Backend",
        "provider": "openai",
        "endpoint": "https://api.openai.com/v1",
        "enabled": False
    },
]
```

### Execution

**Seed all test data**:
```bash
make seed-mock-data          # Via Makefile
python scripts/mock-data/seed-all.py  # Direct execution
```

**Seed specific feature**:
```bash
python scripts/mock-data/seed-users.py
python scripts/mock-data/seed-tokens.py
python scripts/mock-data/seed-backends.py
```

### Implementation Pattern

**Python (PyDAL)**:
```python
#!/usr/bin/env python3
"""Seed mock data for API tokens with dual token system."""

from shared.db import get_db

def seed_tokens():
    db = get_db()

    tokens = [
        {
            "name": "unlimited-token",
            "waddleai_quota": 1000000,
            "llm_quota": 500000,
            "status": "active"
        },
        {
            "name": "limited-token",
            "waddleai_quota": 100000,
            "llm_quota": 50000,
            "status": "active"
        },
        {
            "name": "demo-token",
            "waddleai_quota": 10000,
            "llm_quota": 5000,
            "status": "active"
        },
        {
            "name": "revoked-token",
            "waddleai_quota": 0,
            "llm_quota": 0,
            "status": "revoked"
        },
    ]

    for token in tokens:
        db.api_tokens.insert(**token)

    print(f"✓ Seeded {len(tokens)} API tokens")

if __name__ == "__main__":
    seed_tokens()
```

**Shell (curl/API)**:
```bash
#!/bin/bash
# seed-backends.sh

API_URL="${API_URL:-http://localhost:8001}"
TOKEN="${AUTH_TOKEN}"

# Backend 1: OpenAI
curl -X POST "$API_URL/api/v1/backends" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "OpenAI GPT-4", "provider": "openai", "enabled": true}'

# Backend 2: Anthropic
curl -X POST "$API_URL/api/v1/backends" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"name": "Anthropic", "provider": "anthropic", "enabled": true}'

# Backend 3: Ollama
curl -X POST "$API_URL/api/v1/backends" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"name": "Ollama Local", "provider": "ollama", "enabled": true}'

# Backend 4: Disabled
curl -X POST "$API_URL/api/v1/backends" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"name": "Disabled Backend", "provider": "openai", "enabled": false}'

echo "✓ Seeded 4 backends"
```

### When to Create Mock Data Scripts

**Create a mock data script after each new feature/entity completion**:
- After implementing dual token tracking → create `seed-tokens.py`
- After implementing backend routing → create `seed-backends.py`
- After implementing user roles → create `seed-users.py`

This ensures developers can immediately test the feature without manual setup.

---

## Smoke Tests

### Purpose

Smoke tests provide fast verification that basic functionality works after code changes, preventing regressions in proxy/management services and dual token system.

### Requirements (Mandatory)

All projects **MUST** implement smoke tests before committing:

- ✅ **Proxy Health Check**: Proxy service responds with 200/healthy status
- ✅ **Management Health Check**: Management service responds with 200/healthy status
- ✅ **Dual Token Validation**: Token quota system works correctly
- ✅ **Backend Routing Check**: Multi-backend routing responds correctly
- ✅ **API Authentication**: API endpoints require valid tokens

### Location & Structure

```
tests/smoke/
├── health/              # Service health verification
│   ├── test-proxy-health.sh
│   ├── test-management-health.sh
│   └── README.md
├── tokens/              # Dual token system verification
│   ├── test-token-validation.sh
│   ├── test-quota-enforcement.sh
│   └── README.md
├── routing/             # Multi-backend routing verification
│   ├── test-backend-routing.sh
│   ├── test-model-mapping.sh
│   └── README.md
├── run-all.sh      # Execute all smoke tests
└── README.md       # Documentation
```

### Execution

**Run all smoke tests**:
```bash
make smoke-test              # Via Makefile
./tests/smoke/run-all.sh     # Direct execution
```

**Run specific test category**:
```bash
./tests/smoke/health/test-proxy-health.sh
./tests/smoke/tokens/test-token-validation.sh
./tests/smoke/routing/test-backend-routing.sh
```

### Speed Requirement

Complete smoke test suite **MUST run in under 2 minutes** to provide fast feedback during development.

### Implementation Examples

**Health Check Test (Shell)**:
```bash
#!/bin/bash
# tests/smoke/health/test-proxy-health.sh

set -e

echo "Testing Proxy service health..."
HEALTH_URL="http://localhost:8000/health"

RESPONSE=$(curl -s -w "\n%{http_code}" "$HEALTH_URL")
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [ "$HTTP_CODE" = "200" ]; then
    echo "✓ Proxy service is healthy (HTTP $HTTP_CODE)"
    echo "  Response: $BODY"
    exit 0
else
    echo "✗ Proxy service is unhealthy (HTTP $HTTP_CODE)"
    exit 1
fi
```

**Dual Token Validation Test**:
```bash
#!/bin/bash
# tests/smoke/tokens/test-token-validation.sh

set -e

echo "Validating dual token system..."
API_URL="http://localhost:8000"
TEST_TOKEN="wa-test-token"

# Test token authentication
RESPONSE=$(curl -s -X POST "$API_URL/v1/chat/completions" \
  -H "Authorization: Bearer $TEST_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "test"}]}' \
  -w "\n%{http_code}")

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "429" ]; then
    echo "✓ Token authentication working (HTTP $HTTP_CODE)"
    exit 0
else
    echo "✗ Token authentication failed (HTTP $HTTP_CODE)"
    exit 1
fi
```

**Backend Routing Test**:
```bash
#!/bin/bash
# tests/smoke/routing/test-backend-routing.sh

set -e

echo "Testing multi-backend routing..."
API_URL="http://localhost:8000"
TOKEN="wa-test-token"

# Test smart router
RESPONSE=$(curl -s -X POST "$API_URL/v1/chat/completions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "smart-router",
    "messages": [{"role": "user", "content": "test"}]
  }' \
  -w "\n%{http_code}")

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)

if [ "$HTTP_CODE" = "200" ]; then
    echo "✓ Backend routing working (HTTP $HTTP_CODE)"
    exit 0
else
    echo "✗ Backend routing failed (HTTP $HTTP_CODE)"
    exit 1
fi
```

### Pre-Commit Integration

Smoke tests run as part of the pre-commit checklist (step 5) and **must pass before proceeding** to full test suite:

```bash
./scripts/pre-commit/pre-commit.sh
# Step 1: Linters
# Step 2: Security scans
# Step 3: No secrets
# Step 4: Build & Run
# Step 5: Smoke tests ← Must pass
# Step 6: Full tests
```

---

## Unit Tests

### Purpose

Unit tests verify individual functions and methods in isolation with mocked dependencies, especially token validation and routing logic.

### Location

```
tests/unit/
├── proxy/
│   ├── test_auth.py
│   ├── test_token_validation.py
│   ├── test_routing.py
│   ├── test_dual_tokens.py
│   └── test_cache.py
├── management/
│   ├── test_user_management.py
│   ├── test_analytics.py
│   └── test_quota_management.py
└── shared/
    ├── test_db.py
    └── test_token_system.py
```

### Execution

```bash
make test-unit              # All unit tests
pytest tests/unit/          # Python
pytest tests/unit/proxy/test_token_validation.py  # Specific test
```

### Requirements

- All dependencies must be mocked
- Network calls must be stubbed
- Database access must be isolated
- Token validation must verify both WaddleAI and LLM tokens
- Routing logic must test all backend fallback scenarios

---

## Integration Tests

### Purpose

Integration tests verify that components work together correctly, including real database interactions, multi-backend routing, and dual token consumption tracking.

### Location

```
tests/integration/
├── proxy/
│   ├── test_token_consumption.py
│   ├── test_backend_routing.py
│   ├── test_quota_enforcement.py
│   └── test_api_contracts.py
├── management/
│   ├── test_user_creation.py
│   ├── test_analytics_pipeline.py
│   └── test_quota_management.py
├── services/
│   ├── test_proxy_management_integration.py
│   └── test_dual_token_tracking.py
└── database/
    ├── test_migrations.py
    └── test_queries.py
```

### Execution

```bash
make test-integration       # All integration tests
pytest tests/integration/   # Python
```

### Requirements

- Use real databases (test instances)
- Test complete workflows including token consumption
- Verify multi-backend routing decision logic
- Test error scenarios for token quota enforcement
- Test API contracts between proxy and management

---

## End-to-End Tests

### Purpose

E2E tests verify critical user workflows from start to finish, testing the entire application stack with dual token tracking.

### Location

```
tests/e2e/
├── token-consumption.spec.ts
├── backend-routing.spec.ts
├── user-workflow.spec.ts
├── quota-enforcement.spec.ts
└── analytics-tracking.spec.ts
```

### Execution

```bash
make test-e2e               # All E2E tests
npx playwright test tests/e2e/  # Playwright
```

---

## Performance Tests

### Purpose

Performance tests validate scalability, throughput, and resource usage under load, including dual token validation overhead.

### Location

```
tests/performance/
├── load-test.js
├── stress-test.js
├── token-validation-benchmark.js
├── routing-latency-test.js
└── profile-report.md
```

### Execution

```bash
make test-performance
npm run test:performance
```

---

## Cross-Architecture Testing

### Purpose

Cross-architecture testing ensures the application builds and runs correctly on both amd64 and arm64 architectures, preventing platform-specific bugs in token validation and routing.

### When to Test

**Before every final commit**, test on the alternate architecture:
- Developing on amd64 → Build and test arm64 with QEMU
- Developing on arm64 → Build and test amd64 with QEMU

### Setup (First Time)

Enable Docker buildx for multi-architecture builds:

```bash
docker buildx create --name multiarch --driver docker-container
docker buildx use multiarch
```

### Single Architecture Build

```bash
# Test current architecture (native, fast)
docker build -t waddleai-proxy:test proxy/

# Or explicitly specify architecture
docker build --platform linux/amd64 -t waddleai-proxy:test proxy/
```

### Cross-Architecture Build (QEMU)

```bash
# Test alternate architecture (uses QEMU emulation)
docker buildx build --platform linux/arm64 -t waddleai-proxy:test proxy/

# Or test both simultaneously
docker buildx build --platform linux/amd64,linux/arm64 -t waddleai-proxy:test proxy/
```

### Multi-Architecture Build Script

Create `scripts/build/test-multiarch.sh`:

```bash
#!/bin/bash
# Test both architectures before commit

set -e

SERVICES=("proxy" "management")
ARCHITECTURES=("linux/amd64" "linux/arm64")

for service in "${SERVICES[@]}"; do
    echo "Testing $service on multiple architectures..."

    for arch in "${ARCHITECTURES[@]}"; do
        echo "  → Building for $arch..."
        docker buildx build \
            --platform "$arch" \
            -t "waddleai-$service:multiarch-test" \
            "$service/" || {
            echo "✗ Build failed for $service on $arch"
            exit 1
        }
    done

    echo "✓ $service builds successfully on amd64 and arm64"
done

echo "✓ All services passed multi-architecture testing"
```

### Pre-Commit Integration

Add to pre-commit script (before final commit):

```bash
# Step 8: Cross-architecture testing
if [ "$ENABLE_QEMU_TEST" = "true" ]; then
    echo "Testing cross-architecture builds with QEMU..."
    make test-multiarch || exit 1
fi
```

### Troubleshooting

**QEMU not available**:
```bash
# Install QEMU support
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
```

**Slow builds with QEMU**:
- Expect 2-5x slower builds when using QEMU emulation
- Use for final validation, not every iteration
- Consider caching intermediate layers

**Architecture-specific issues**:
- File path separators (Windows vs Linux)
- Endianness in binary protocols
- Floating-point precision
- Package availability

---

## Test Execution Order (Pre-Commit)

Follow this order for efficient testing before commits:

1. **Linters** (fast, <1 min)
2. **Security scans** (fast, <1 min)
3. **Secrets check** (fast, <1 min)
4. **Build & Run** (5-10 min)
5. **Smoke tests** (fast, <2 min) ← Gates further testing
6. **Unit tests** (1-2 min)
7. **Integration tests** (2-5 min)
8. **E2E tests** (5-10 min)
9. **Cross-architecture build** (optional, slow)

## CI/CD Integration

All tests run automatically in GitHub Actions:

- **On PR**: Smoke + Unit + Integration tests
- **On main merge**: All tests + Performance tests
- **Nightly**: Performance + Cross-architecture tests
- **Release**: Full suite + Manual sign-off

See [Workflows](WORKFLOWS.md) for detailed CI/CD configuration.

---

**Last Updated**: 2026-01-06
**Maintained by**: Penguin Tech Inc
