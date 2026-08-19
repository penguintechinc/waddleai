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
    # `((TESTS_PASSED++))` post-increment returns the *pre*-increment value as
    # the command's exit status -- 0 on the very first call, which `set -e`
    # treats as failure and kills the script after a single PASS. `: $(( ))`
    # never returns non-zero, avoiding the false trip.
    : $((TESTS_PASSED++))
}

fail() {
    echo -e "${RED}[FAIL]${NC} $1"
    : $((TESTS_FAILED++))
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

echo ""

# Test 2: Python syntax check
# Walks shared/ and services/management/app/ rather than a fixed file list so
# this stays correct as files are added/removed/renamed (a hardcoded list
# silently stopped covering renamed/deleted modules in the past).
echo "Test 2: Python syntax check..."

python_syntax_check() {
    local rel="${1#"$PROJECT_ROOT"/}"
    if python3 -m py_compile "$1" 2>/dev/null; then
        pass "Syntax OK: $rel"
    else
        fail "Syntax error: $rel"
    fi
}

while IFS= read -r -d '' pyfile; do
    python_syntax_check "$pyfile"
done < <(find "$PROJECT_ROOT/shared" "$PROJECT_ROOT/services/management/app" -name "*.py" -print0 | sort -z)

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

# Test 4: Requirements file check
# docker-compose is deprecated house-wide (K8s/Helm only) and no
# docker-compose.yml exists in this repo, so there is nothing left to
# syntax-check there -- that check has been dropped rather than kept
# failing against infrastructure that no longer exists. Package checks
# below reflect the actual Quart + penguin-libs stack (flask-security-too
# and bare pydal were removed from this service; auth is penguin-aaa/OIDC).
echo "Test 4: Requirements file validation..."

if [ -f "$PROJECT_ROOT/services/management/requirements.txt" ]; then
    # Check for essential packages (services/management/requirements.in)
    if grep -q "^quart==" "$PROJECT_ROOT/services/management/requirements.txt"; then
        pass "Quart package present"
    else
        fail "Quart package missing"
    fi

    if grep -q "^penguin-aaa==" "$PROJECT_ROOT/services/management/requirements.txt"; then
        pass "penguin-aaa package present"
    else
        fail "penguin-aaa package missing"
    fi

    if grep -q "^penguin-dal==" "$PROJECT_ROOT/services/management/requirements.txt"; then
        pass "penguin-dal package present"
    else
        fail "penguin-dal package missing"
    fi

    if grep -q "^grpcio==" "$PROJECT_ROOT/services/management/requirements.txt"; then
        pass "gRPC package present"
    else
        fail "gRPC package missing"
    fi
else
    fail "requirements.txt missing, cannot validate packages"
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
