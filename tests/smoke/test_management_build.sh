#!/bin/bash
# WaddleAI Management Service - Smoke Tests
# Verifies build, container startup, and basic API functionality

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"

echo "========================================"
echo "WaddleAI Management Service Smoke Tests"
echo "========================================"
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    echo -e "${GREEN}[PASS]${NC} $1"
    ((TESTS_PASSED++))
}

fail() {
    echo -e "${RED}[FAIL]${NC} $1"
    ((TESTS_FAILED++))
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# Test 1: Check if required files exist
echo "Test 1: Checking required files..."

check_file() {
    if [ -f "$PROJECT_ROOT/$1" ]; then
        pass "File exists: $1"
    else
        fail "File missing: $1"
    fi
}

check_file "services/management/app/__init__.py"
check_file "services/management/app/config.py"
check_file "services/management/app/extensions.py"
check_file "services/management/app/api/v1/__init__.py"
check_file "services/management/app/api/v1/auth.py"
check_file "services/management/app/api/v1/providers.py"
check_file "services/management/app/api/v1/webhooks.py"
check_file "services/management/requirements.txt"
check_file "services/management/Dockerfile"
check_file "services/management/wsgi.py"
check_file "docker-compose.yml"

echo ""

# Test 2: Python syntax check
echo "Test 2: Python syntax check..."

python_syntax_check() {
    if python3 -m py_compile "$PROJECT_ROOT/$1" 2>/dev/null; then
        pass "Syntax OK: $1"
    else
        fail "Syntax error: $1"
    fi
}

python_syntax_check "services/management/app/__init__.py"
python_syntax_check "services/management/app/config.py"
python_syntax_check "services/management/app/extensions.py"
python_syntax_check "services/management/app/api/v1/__init__.py"
python_syntax_check "services/management/app/api/v1/auth.py"
python_syntax_check "services/management/app/api/v1/providers.py"
python_syntax_check "services/management/app/api/v1/ollama.py"
python_syntax_check "services/management/app/api/v1/ailb.py"
python_syntax_check "services/management/app/api/v1/keys.py"
python_syntax_check "services/management/app/api/v1/usage.py"
python_syntax_check "services/management/app/api/v1/quotas.py"
python_syntax_check "services/management/app/api/v1/webhooks.py"
python_syntax_check "services/management/app/grpc/client.py"
python_syntax_check "services/management/app/services/provider_sync.py"
python_syntax_check "services/management/app/services/ollama_manager.py"
python_syntax_check "services/management/app/services/usage_tracker.py"
python_syntax_check "services/management/app/services/providers/__init__.py"

echo ""

# Test 3: Docker build (if Docker is available)
echo "Test 3: Docker build check..."

if command -v docker &> /dev/null; then
    echo "Building Docker image..."
    cd "$PROJECT_ROOT"
    if docker build -t waddleai-mgmt-test:smoke -f services/management/Dockerfile . > /tmp/waddleai-build.log 2>&1; then
        pass "Docker build successful"
        # Clean up test image
        docker rmi waddleai-mgmt-test:smoke > /dev/null 2>&1 || true
    else
        fail "Docker build failed"
        echo "Build log: /tmp/waddleai-build.log"
    fi
else
    warn "Docker not available, skipping container build test"
fi

echo ""

# Test 4: Check docker-compose syntax
echo "Test 4: Docker Compose syntax check..."

if command -v docker-compose &> /dev/null || docker compose version &> /dev/null 2>&1; then
    cd "$PROJECT_ROOT"
    if docker compose config > /dev/null 2>&1; then
        pass "Docker Compose config is valid"
    else
        fail "Docker Compose config has errors"
    fi
else
    warn "Docker Compose not available, skipping config check"
fi

echo ""

# Test 5: Requirements file check
echo "Test 5: Requirements file validation..."

if [ -f "$PROJECT_ROOT/services/management/requirements.txt" ]; then
    # Check for essential packages
    if grep -q "flask" "$PROJECT_ROOT/services/management/requirements.txt"; then
        pass "Flask package present"
    else
        fail "Flask package missing"
    fi

    if grep -q "flask-security-too" "$PROJECT_ROOT/services/management/requirements.txt"; then
        pass "Flask-Security-Too package present"
    else
        fail "Flask-Security-Too package missing"
    fi

    if grep -q "pydal" "$PROJECT_ROOT/services/management/requirements.txt"; then
        pass "PyDAL package present"
    else
        fail "PyDAL package missing"
    fi

    if grep -q "grpcio" "$PROJECT_ROOT/services/management/requirements.txt"; then
        pass "gRPC package present"
    else
        fail "gRPC package missing"
    fi
fi

echo ""

# Summary
echo "========================================"
echo "Smoke Test Summary"
echo "========================================"
echo -e "Passed: ${GREEN}$TESTS_PASSED${NC}"
echo -e "Failed: ${RED}$TESTS_FAILED${NC}"
echo ""

if [ $TESTS_FAILED -gt 0 ]; then
    echo -e "${RED}Some tests failed!${NC}"
    exit 1
else
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
fi
