---
name: dependency-management
description: "Updating, auditing, and pinning dependencies across languages"
model: qwen2.5-coder:7b
---

# Dependency Management

## Overview
Manage project dependencies: update safely, audit for vulnerabilities, and pin versions.

## Python
```bash
# List outdated
pip list --outdated

# Update specific package
pip install --upgrade <package>

# Audit for vulnerabilities
pip-audit
safety check

# Pin versions
pip freeze > requirements.txt
```

## Node.js
```bash
# List outdated
npm outdated

# Update within semver range
npm update

# Audit
npm audit
npm audit fix

# Update to latest
npx npm-check-updates -u && npm install
```

## Go
```bash
# Update dependencies
go get -u ./...

# Tidy (remove unused)
go mod tidy

# Audit
govulncheck ./...
```

## Best Practices
- Pin exact versions in production dependencies
- Use lock files (package-lock.json, go.sum, etc.)
- Audit dependencies regularly (weekly in CI)
- Update one dependency at a time, test after each
- Review changelogs before major version upgrades
- Remove unused dependencies
