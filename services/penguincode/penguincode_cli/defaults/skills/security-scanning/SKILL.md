---
name: security-scanning
description: "SAST, dependency audit, secret detection, and OWASP checks"
model: qwen2.5-coder:7b
---

# Security Scanning

## Overview
Run security scans to detect vulnerabilities, hardcoded secrets, and dependency issues before code reaches production.

## Scan Types
1. **Secret detection** — find hardcoded API keys, passwords, tokens
2. **SAST** — static application security testing
3. **Dependency audit** — check for vulnerable dependencies
4. **Container scanning** — image vulnerability analysis

## Secret Detection
```bash
# Using git-secrets
git secrets --scan

# Using trufflehog
trufflehog filesystem .

# Manual grep for common patterns
grep -rn "password\|secret\|api_key\|token" --include="*.py" --include="*.js" .
```

## Dependency Audit
```bash
# Python
pip-audit
safety check

# Node.js
npm audit

# Go
govulncheck ./...
```

## OWASP Top 10 Checks
- Injection (SQL, command, XSS)
- Broken authentication
- Sensitive data exposure
- Security misconfiguration
- Insecure deserialization

## Pre-Commit Integration
Run these scans before every commit (see waddlepowers:committing-changes):
1. Secret detection (blocks commit if secrets found)
2. Dependency audit (warns on known vulnerabilities)
3. Linting security rules (e.g., bandit for Python)

## CI Integration
- Run full SAST in CI pipeline
- Block merges on critical findings
- Generate security reports for review
