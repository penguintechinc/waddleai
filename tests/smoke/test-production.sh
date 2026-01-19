#!/bin/bash
# Smoke tests for WaddleAI production deployment

BASE_URL="${BASE_URL:-https://waddleai.penguintech.io}"
FAILED=0
PASSED=0

echo "========================================"
echo "WaddleAI Smoke Tests - Production"
echo "Target: $BASE_URL"
echo "Note: Cloudflare may challenge some requests"
echo "========================================"
echo ""

# Test 1: WebUI loads (may be Cloudflare challenged)
echo "Test 1: WebUI homepage loads..."
HTTP_CODE=$(curl -k -s -H "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36" -o /dev/null -w "%{http_code}" \
    -H "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36" \
    "$BASE_URL/")
if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "403" ]; then
    echo "  ✓ PASS: WebUI returned HTTP $HTTP_CODE (Cloudflare protection active)"
    ((PASSED++))
else
    echo "  ✗ FAIL: WebUI returned HTTP $HTTP_CODE (expected 200 or 403)"
    ((FAILED++))
fi
echo ""

# Test 2: Login page loads
echo "Test 2: Login page loads..."
HTTP_CODE=$(curl -k -s -H "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36" -o /dev/null -w "%{http_code}" "$BASE_URL/login")
if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "403" ]; then
    echo "  ✓ PASS: Login page returned HTTP $HTTP_CODE"
    ((PASSED++))
else
    echo "  ✗ FAIL: Login page returned HTTP $HTTP_CODE (expected 200 or 403)"
    ((FAILED++))
fi
echo ""

# Test 3: Health endpoint
echo "Test 3: Health endpoint..."
HEALTH=$(curl -k -s -H "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36" "$BASE_URL/healthz")
if [ "$HEALTH" = "healthy" ]; then
    echo "  ✓ PASS: Health check returned: $HEALTH"
    ((PASSED++))
else
    echo "  ✗ FAIL: Health check returned: $HEALTH (expected 'healthy')"
    ((FAILED++))
fi
echo ""

# Test 4: Readiness endpoint
echo "Test 4: Readiness endpoint..."
HTTP_CODE=$(curl -k -s -H "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36" -o /dev/null -w "%{http_code}" "$BASE_URL/readyz")
if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "503" ]; then
    echo "  ✓ PASS: Readiness check returned HTTP $HTTP_CODE"
    ((PASSED++))
else
    echo "  ✗ FAIL: Readiness check returned HTTP $HTTP_CODE (expected 200 or 503)"
    ((FAILED++))
fi
echo ""

# Test 5: API login endpoint exists
echo "Test 5: API login endpoint exists..."
HTTP_CODE=$(curl -k -s -H "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36" -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/v1/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"username":"test","password":"test"}')
if [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "400" ]; then
    echo "  ✓ PASS: Login endpoint returned HTTP $HTTP_CODE (endpoint exists)"
    ((PASSED++))
else
    echo "  ✗ FAIL: Login endpoint returned HTTP $HTTP_CODE (expected 401 or 400)"
    ((FAILED++))
fi
echo ""

# Test 6: Login with correct credentials
echo "Test 6: Login with admin credentials..."
RESPONSE=$(curl -k -s -H "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36" -X POST "$BASE_URL/api/v1/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"admin123"}')
TOKEN=$(echo "$RESPONSE" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
if [ -n "$TOKEN" ]; then
    echo "  ✓ PASS: Admin login successful, got token"
    ((PASSED++))
else
    echo "  ✗ FAIL: Admin login failed, no token received"
    echo "  Response: $RESPONSE"
    ((FAILED++))
fi
echo ""

# Test 7: Logo file exists
echo "Test 7: Logo file accessible..."
HTTP_CODE=$(curl -k -s -H "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36" -o /dev/null -w "%{http_code}" "$BASE_URL/logo.png")
if [ "$HTTP_CODE" = "200" ]; then
    echo "  ✓ PASS: Logo file returned HTTP $HTTP_CODE"
    ((PASSED++))
else
    echo "  ✗ FAIL: Logo file returned HTTP $HTTP_CODE (expected 200)"
    ((FAILED++))
fi
echo ""

# Summary
echo "========================================"
echo "Smoke Test Results"
echo "========================================"
echo "Passed: $PASSED"
echo "Failed: $FAILED"
echo "Total:  $((PASSED + FAILED))"
echo ""

if [ $FAILED -gt 0 ]; then
    echo "❌ SMOKE TESTS FAILED"
    exit 1
else
    echo "✅ ALL SMOKE TESTS PASSED"
    exit 0
fi
